# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import os
import pwd
import stat
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from python.srv.brokers import shares_broker
from python.srv.brokers.broker_core import FAILED, INVALID_REQUEST, REFUSED, BrokerError
from python.tests.srv.brokers.fake_samba import SMB_CONF_TEXT, current_user, make_system

SHARE = "abr-k3j9x2qa$"
OTHER_SHARE = "abr-z7w4c1vb$"
USER = "abr-smb-p4m8z1wt"
OTHER_USER = "abr-smb-q2n6t8ky"
PASSWORD = "A" * 20 + "b1" * 10


class _BrokerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.system, folder = make_system()
        self.addCleanup(folder.cleanup)
        self.samba = self.system.samba()
        self.folder = self.system.root / "repository"
        self.folder.mkdir()
        self.second = self.system.root / "second"
        self.second.mkdir()

    def add(self, share: str = SHARE, user: str = USER, folder: Path | None = None, **changes) -> dict:
        arguments = {"share": share, "user": user, "password": PASSWORD, "path": str(folder or self.folder), **changes}
        return shares_broker.add_share(self.samba, arguments)

    def remove(self, share: str = SHARE, user: str = USER) -> dict:
        return shares_broker.remove_share(self.samba, {"share": share, "user": user})

    def refused(self, call, code: str = REFUSED) -> BrokerError:
        with self.assertRaises(BrokerError) as caught:
            call()
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def ours(self) -> dict[str, dict[str, str]]:
        return shares_broker.parse_sections(self.system.shares_conf.read_text()) if self.system.shares_conf.is_file() else {}

    def tools_run(self) -> list[str]:
        return [argv[0] for argv, _stdin in self.system.calls]


class AddShareTests(_BrokerCase):
    def test_a_share_is_written_included_given_its_own_account_and_loaded(self) -> None:
        result = self.add()

        self.assertEqual(result, {"share": SHARE, "user": USER, "path": str(self.folder)})
        section = self.ours()[SHARE]
        self.assertEqual(section["path"], str(self.folder))
        self.assertEqual(section["valid users"], USER)
        self.assertEqual(section["force user"], current_user())
        self.assertEqual((section["browseable"], section["guest ok"], section["server smb encrypt"]), ("no", "no", "required"))
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT + f"include = {self.system.shares_conf}\n")
        self.assertEqual(self.system.accounts[USER], shares_broker.ACCOUNT_COMMENT)
        self.assertEqual((self.system.samba_accounts[USER], USER in self.system.enabled), (PASSWORD, True))
        self.assertEqual(self.tools_run()[-2:], ["smbcontrol", "systemctl"])

    def test_the_password_goes_to_smbpasswd_on_stdin_and_never_on_a_command_line(self) -> None:
        self.add()

        self.assertFalse(any(PASSWORD in " ".join(argv) for argv, _stdin in self.system.calls))
        self.assertIn((["smbpasswd", "-s", "-a", USER], f"{PASSWORD}\n{PASSWORD}\n"), self.system.calls)

    def test_the_shares_file_is_root_and_the_service_group_with_mode_0640(self) -> None:
        self.add()

        self.assertEqual(stat.S_IMODE(self.system.shares_conf.stat().st_mode), 0o640)
        service_gid = pwd.getpwnam(current_user()).pw_gid
        self.assertIn((0, service_gid), [(uid, gid) for path, uid, gid in self.system.owners if ".samba-shares.conf." in path])

    def test_a_missing_folder_for_the_shares_file_is_made_readable_for_the_server(self) -> None:
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)

        self.add()

        self.assertEqual(stat.S_IMODE(self.system.shares_conf.parent.stat().st_mode), 0o755)

    def test_smb_conf_keeps_its_owner_and_mode(self) -> None:
        os.chmod(self.system.smb_conf, 0o600)
        before = self.system.smb_conf.stat()

        self.add()

        self.assertEqual(stat.S_IMODE(self.system.smb_conf.stat().st_mode), 0o600)
        self.assertIn((before.st_uid, before.st_gid), [(uid, gid) for path, uid, gid in self.system.owners if ".smb.conf." in path])

    def test_a_second_share_goes_into_the_same_file_with_one_include_line(self) -> None:
        self.add()
        self.add(OTHER_SHARE, OTHER_USER, self.second)

        self.assertEqual(sorted(self.ours()), sorted([SHARE, OTHER_SHARE]))
        include = f"include = {self.system.shares_conf}"
        self.assertEqual(self.system.smb_conf.read_text().splitlines().count(include), 1)

    def test_an_account_already_made_by_altastor_backup_is_used_again(self) -> None:
        self.system.accounts[USER] = "AltaStor Backup Hyper-V share"

        self.add()

        self.assertNotIn("useradd", self.tools_run())
        self.assertEqual(self.system.samba_accounts[USER], PASSWORD)


class AddShareRefusalTests(_BrokerCase):
    def assert_nothing_changed(self) -> None:
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual((self.system.accounts, self.system.samba_accounts), ({}, {}))
        self.assertNotIn("smbcontrol", self.tools_run())

    def test_a_stopped_smbd_is_refused_and_never_started(self) -> None:
        self.system.running = False

        error = self.refused(self.add)

        self.assertIn("smbd is not running", error.message)
        self.assertFalse(any(argv[:2] in (["systemctl", "start"], ["systemctl", "restart"], ["systemctl", "enable"]) for argv, _ in self.system.calls))
        self.assert_nothing_changed()

    def test_samba_programs_that_are_missing_are_named(self) -> None:
        self.system.missing = {"pdbedit", "smbcontrol"}

        self.assertIn("missing: pdbedit smbcontrol", self.refused(self.add).message)
        self.assert_nothing_changed()

    def test_a_missing_smb_conf_means_samba_is_not_installed(self) -> None:
        self.system.smb_conf.unlink()

        self.assertIn("Samba is not installed", self.refused(self.add).message)

    def test_an_invalid_samba_configuration_is_left_alone(self) -> None:
        self.system.valid = False

        self.assertIn("does not validate", self.refused(self.add).message)
        self.assert_nothing_changed()

    def test_an_active_directory_domain_controller_is_left_alone(self) -> None:
        self.system.parameters["server role"] = "active directory domain controller"

        self.assertIn("Active Directory domain controller", self.refused(self.add).message)
        self.assert_nothing_changed()

    def test_a_registry_configuration_is_left_alone(self) -> None:
        self.system.parameters["config backend"] = "registry"

        self.assertIn("registry", self.refused(self.add).message)
        self.assert_nothing_changed()

    def test_accounts_kept_outside_tdbsam_are_left_alone(self) -> None:
        self.system.parameters["passdb backend"] = "ldapsam:ldap://ldap.example"

        self.assertIn("not tdbsam", self.refused(self.add).message)
        self.assert_nothing_changed()

    def test_a_folder_of_another_user_is_refused(self) -> None:
        samba = self.system.samba(service_user="root")

        error = self.refused(lambda: shares_broker.add_share(samba, {"share": SHARE, "user": USER, "password": PASSWORD, "path": str(self.folder)}))

        self.assertIn(f"belongs to {current_user()}, not to root", error.message)
        self.assert_nothing_changed()

    def test_a_service_user_that_does_not_exist_is_refused(self) -> None:
        samba = self.system.samba(service_user="abr-nobody-here")

        self.assertIn("no account abr-nobody-here", self.refused(
            lambda: shares_broker.add_share(samba, {"share": SHARE, "user": USER, "password": PASSWORD, "path": str(self.folder)})
        ).message)

    def test_a_folder_given_through_a_symbolic_link_or_relative_parts_is_refused(self) -> None:
        link = self.system.root / "link"
        link.symlink_to(self.folder)
        for path in (str(link), f"{self.folder}/../repository"):
            with self.subTest(path=path):
                self.assertIn("without symbolic links", self.refused(lambda path=path: self.add(path=path)).message)
        self.assert_nothing_changed()

    def test_a_relative_path_a_file_or_a_missing_folder_is_refused(self) -> None:
        (self.system.root / "file").write_text("x")
        self.refused(lambda: self.add(path="repository"), INVALID_REQUEST)
        self.assertIn("is not a folder", self.refused(lambda: self.add(path=str(self.system.root / "file"))).message)
        self.assertIn("is not a folder", self.refused(lambda: self.add(path=str(self.system.root / "gone"))).message)

    def test_names_and_passwords_that_do_not_match_the_patterns_are_refused(self) -> None:
        cases = {
            "share": ("abr-UPPER123$", "abr-k3j9x2qa", "print$", "abr-k3j9x2qa$\n[x]"),
            "user": ("root", "abr-smb-1", "abr-smb-p4m8z1wt;id"),
            "password": ("short1", "with space and more letters to be long enough", "x" * 129),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(field=name, value=value):
                    self.refused(lambda name=name, value=value: self.add(**{name: value}), INVALID_REQUEST)
        self.assert_nothing_changed()

    def test_missing_extra_or_non_text_arguments_are_refused(self) -> None:
        for arguments in (
            {"share": SHARE, "user": USER, "password": PASSWORD},
            {"share": SHARE, "user": USER, "password": PASSWORD, "path": str(self.folder), "force user": "root"},
            {"share": SHARE, "user": USER, "password": PASSWORD, "path": ["/"]},
        ):
            with self.subTest(arguments=sorted(arguments)):
                self.refused(lambda arguments=arguments: shares_broker.add_share(self.samba, arguments), INVALID_REQUEST)

    def test_a_share_name_taken_anywhere_in_samba_is_refused(self) -> None:
        self.system.smb_conf.write_text(SMB_CONF_TEXT + f"\n[{SHARE.upper()}]\n   path = /srv\n")
        before = self.system.smb_conf.read_text()

        self.assertIn("already exists in the Samba configuration", self.refused(self.add).message)
        self.assertEqual(self.system.smb_conf.read_text(), before)

    def test_a_folder_that_is_already_shared_is_refused(self) -> None:
        self.add()

        self.assertIn("is already shared as", self.refused(lambda: self.add(OTHER_SHARE, OTHER_USER)).message)
        self.assertEqual(sorted(self.ours()), [SHARE])

    def test_a_share_or_account_of_ours_already_in_use_is_refused(self) -> None:
        self.add()

        self.assertIn("already one of the shares", self.refused(lambda: self.add(folder=self.second, user=OTHER_USER)).message)
        self.assertIn("already belongs to the share", self.refused(lambda: self.add(OTHER_SHARE, USER, self.second)).message)

    def test_a_system_account_of_that_name_that_is_not_ours_is_refused_and_rolled_back(self) -> None:
        self.system.accounts[USER] = "Someone else"

        error = self.refused(self.add)

        self.assertIn("is not one AltaStor Backup made", error.message)
        self.assertIn("the previous Samba configuration is restored", error.message)
        self.assertEqual(self.system.accounts, {USER: "Someone else"})
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertFalse(self.system.shares_conf.exists())


class AddShareRollbackTests(_BrokerCase):
    def assert_rolled_back(self, error: BrokerError, *, existing: str | None = None) -> None:
        self.assertIn("the previous Samba configuration is restored", error.message)
        self.assertEqual(self.system.smb_conf.read_text(), existing or SMB_CONF_TEXT)
        self.assertNotIn(USER, self.system.accounts)
        self.assertNotIn(USER, self.system.samba_accounts)

    def test_a_configuration_samba_rejects_is_put_back(self) -> None:
        self.system.invalid_after_write = True

        error = self.refused(self.add, FAILED)

        self.assertIn("does not validate with the share added", error.message)
        self.assert_rolled_back(error)
        self.assertFalse(self.system.shares_conf.exists())

    def test_a_failing_smbpasswd_removes_the_account_it_made(self) -> None:
        self.system.failing = {"smbpasswd"}

        error = self.refused(self.add, FAILED)

        self.assertIn("smbpasswd -s failed", error.message)
        self.assert_rolled_back(error)

    def test_a_failing_reload_puts_everything_back(self) -> None:
        self.system.failing = {"smbcontrol"}

        self.assert_rolled_back(self.refused(self.add, FAILED))
        self.assertFalse(self.system.shares_conf.exists())

    def test_smbd_stopping_after_the_reload_puts_everything_back(self) -> None:
        self.system.stop_on_reload = True

        error = self.refused(self.add, FAILED)

        self.assertIn("smbd stopped after reloading", error.message)
        self.assert_rolled_back(error)

    def test_a_failure_with_shares_already_there_keeps_them(self) -> None:
        self.add(OTHER_SHARE, OTHER_USER, self.second)
        existing_smb, existing_shares = self.system.smb_conf.read_text(), self.system.shares_conf.read_text()
        self.system.failing = {"smbcontrol"}

        self.assert_rolled_back(self.refused(self.add, FAILED), existing=existing_smb)

        self.assertEqual(self.system.shares_conf.read_text(), existing_shares)
        self.assertIn(OTHER_USER, self.system.accounts)

    def test_an_unexpected_error_still_rolls_back_and_is_raised(self) -> None:
        def broken(*_args, **_kwargs):
            raise KeyboardInterrupt

        original = self.system._smbpasswd
        self.system._smbpasswd = broken
        self.addCleanup(setattr, self.system, "_smbpasswd", original)

        with self.assertRaises(KeyboardInterrupt):
            self.add()

        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertNotIn(USER, self.system.accounts)


class RemoveShareTests(_BrokerCase):
    def test_removing_the_last_share_leaves_smb_conf_byte_for_byte_as_it_was(self) -> None:
        self.add()

        result = self.remove()

        self.assertEqual(result, {"share": SHARE, "user": USER, "removed_share": True, "removed_account": True, "reloaded": True})
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual((self.system.accounts, self.system.samba_accounts), ({}, {}))

    def test_removing_one_of_two_shares_keeps_the_other_and_the_include(self) -> None:
        self.add()
        self.add(OTHER_SHARE, OTHER_USER, self.second)

        self.remove()

        self.assertEqual(sorted(self.ours()), [OTHER_SHARE])
        self.assertIn(f"include = {self.system.shares_conf}", self.system.smb_conf.read_text())
        self.assertEqual(sorted(self.system.accounts), [OTHER_USER])

    def test_a_stopped_smbd_does_not_stop_the_removal_and_is_not_started(self) -> None:
        self.add()
        self.system.running = False

        result = self.remove()

        self.assertEqual((result["removed_share"], result["reloaded"]), (True, False))
        self.assertFalse(self.system.shares_conf.exists())
        self.assertNotIn(["smbcontrol", "smbd", "reload-config"], [argv for argv, _ in self.system.calls[-6:]])

    def test_a_share_that_is_not_configured_still_removes_our_account(self) -> None:
        self.system.accounts[USER] = shares_broker.ACCOUNT_COMMENT
        self.system.samba_accounts[USER] = PASSWORD

        result = self.remove()

        self.assertEqual((result["removed_share"], result["removed_account"]), (False, True))
        self.assertEqual((self.system.accounts, self.system.samba_accounts), ({}, {}))
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)

    def test_nothing_to_remove_answers_so(self) -> None:
        self.assertEqual(self.remove(), {"share": SHARE, "user": USER, "removed_share": False, "removed_account": False, "reloaded": False})

    def test_an_account_altastor_backup_did_not_make_is_left_alone(self) -> None:
        self.add()
        self.system.accounts[USER] = "Someone else"

        self.assertIn("is not one AltaStor Backup made", self.refused(self.remove).message)
        self.assertEqual(sorted(self.ours()), [SHARE])

    def test_a_share_of_another_account_is_left_alone(self) -> None:
        self.add()

        self.assertIn(f"belongs to the account {USER}", self.refused(lambda: self.remove(SHARE, OTHER_USER)).message)
        self.assertIn(f"also belongs to the share {SHARE}", self.refused(lambda: self.remove(OTHER_SHARE, USER)).message)
        self.assertEqual(sorted(self.ours()), [SHARE])

    def test_a_failing_reload_keeps_the_share(self) -> None:
        self.add()
        before_smb, before_shares = self.system.smb_conf.read_text(), self.system.shares_conf.read_text()
        self.system.failing = {"smbcontrol"}

        self.assertIn("the share is kept", self.refused(self.remove, FAILED).message)

        self.assertEqual((self.system.smb_conf.read_text(), self.system.shares_conf.read_text()), (before_smb, before_shares))
        self.assertIn(USER, self.system.accounts)

    def test_invalid_names_or_arguments_are_refused(self) -> None:
        for arguments in ({"share": "print$", "user": USER}, {"share": SHARE, "user": "root"}, {"share": SHARE}, {"share": SHARE, "user": USER, "path": "/"}):
            with self.subTest(arguments=arguments):
                self.refused(lambda arguments=arguments: shares_broker.remove_share(self.samba, arguments), INVALID_REQUEST)

    def test_a_samba_account_without_its_system_account_is_removed_with_pdbedit(self) -> None:
        self.system.samba_accounts[USER] = PASSWORD

        self.assertTrue(self.remove()["removed_account"])
        self.assertIn(["pdbedit", "-x", "-u", USER], [argv for argv, _ in self.system.calls])
        self.assertEqual(self.system.samba_accounts, {})

    def test_without_samba_installed_our_system_account_is_still_removed(self) -> None:
        self.system.missing = set(shares_broker.SAMBA_TOOLS)
        self.system.accounts[USER] = shares_broker.ACCOUNT_COMMENT

        self.assertTrue(self.remove()["removed_account"])
        self.assertNotIn("pdbedit", self.tools_run())
        self.assertEqual(self.system.accounts, {})


class CloseSharesTests(_BrokerCase):
    def close(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.object(shares_broker.os, "geteuid", return_value=0), redirect_stdout(out), redirect_stderr(err):
            code = shares_broker.close_from_command_line(self.samba)
        return code, out.getvalue(), err.getvalue()

    def test_closing_removes_the_shares_file_and_the_include_and_keeps_the_accounts(self) -> None:
        self.add()
        self.add(OTHER_SHARE, OTHER_USER, self.second)
        reloads = self.tools_run().count("smbcontrol")

        code, out, _err = self.close()

        self.assertEqual(code, shares_broker.CLOSED)
        self.assertIn(", ".join(sorted([SHARE, OTHER_SHARE])), out)
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual(sorted(self.system.accounts), sorted([USER, OTHER_USER]))
        self.assertEqual(sorted(self.system.samba_accounts), sorted([USER, OTHER_USER]))
        self.assertEqual(self.tools_run().count("smbcontrol"), reloads + 1)

    def test_nothing_to_close_succeeds_and_changes_nothing(self) -> None:
        code, out, _err = self.close()

        self.assertEqual((code, out.strip()), (shares_broker.CLOSED, "No AltaStor Backup shares to close."))
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertNotIn("smbcontrol", self.tools_run())

    def test_an_include_left_without_its_file_or_a_file_without_its_include_is_closed_too(self) -> None:
        self.system.smb_conf.write_text(SMB_CONF_TEXT + f"include = {self.system.shares_conf}\n")
        self.assertEqual(self.close()[0], shares_broker.CLOSED)
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)

        self.system.shares_conf.parent.mkdir(parents=True, exist_ok=True)
        self.system.shares_conf.write_text(f"[{SHARE}]\n    path = /srv\n")
        self.assertEqual(self.close()[0], shares_broker.CLOSED)
        self.assertFalse(self.system.shares_conf.exists())

    def test_a_stopped_smbd_is_not_started_and_the_close_still_succeeds(self) -> None:
        self.add()
        self.system.running = False
        before = len(self.system.calls)

        code, _out, _err = self.close()

        closing = [argv for argv, _ in self.system.calls[before:]]
        self.assertEqual(code, shares_broker.CLOSED)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual([argv[0] for argv in closing], ["systemctl"])
        self.assertEqual(closing[0][1:3], ["is-active", "--quiet"])
        self.assertFalse(self.system.running)

    def test_a_failing_reload_still_closes_and_says_so_with_its_own_exit_code(self) -> None:
        self.add()
        self.system.failing = {"smbcontrol"}

        code, _out, err = self.close()

        self.assertEqual(code, shares_broker.CLOSED_WITHOUT_RELOAD)
        self.assertIn("restart smbd to stop serving them", err)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)

    def test_samba_removed_already_leaves_only_our_file_to_delete(self) -> None:
        self.add()
        self.system.smb_conf.unlink()
        self.system.missing = set(shares_broker.SAMBA_TOOLS)

        self.assertEqual(self.close()[0], shares_broker.CLOSED)
        self.assertFalse(self.system.shares_conf.exists())

    def test_not_root_is_refused_at_once(self) -> None:
        self.add()
        err = io.StringIO()
        with patch.object(shares_broker.os, "geteuid", return_value=1000), redirect_stderr(err):
            code = shares_broker.close_from_command_line(self.samba)

        self.assertEqual(code, shares_broker.CLOSE_FAILED)
        self.assertIn("must run as root", err.getvalue())
        self.assertTrue(self.system.shares_conf.exists())

    def test_a_file_that_cannot_be_changed_fails_with_the_reason(self) -> None:
        self.add()
        with patch.object(shares_broker, "_write", side_effect=PermissionError(13, "Permission denied")):
            code, _out, err = self.close()

        self.assertEqual(code, shares_broker.CLOSE_FAILED)
        self.assertIn("could not be closed: [Errno 13] Permission denied", err)

    def test_the_command_line_option_runs_the_close_and_not_the_socket(self) -> None:
        with patch.object(shares_broker, "close_from_command_line", return_value=7) as close, patch.object(shares_broker.broker_core, "serve") as serve, patch.object(
            shares_broker.os, "umask"
        ):
            self.assertEqual(shares_broker.main(["--close-shares"]), 7)

        close.assert_called_once()
        serve.assert_not_called()


class ConcurrencyTests(_BrokerCase):
    def test_requests_at_the_same_time_wait_for_each_other_and_keep_both_shares(self) -> None:
        folders = [self.system.root / f"folder-{index}" for index in range(6)]
        for folder in folders:
            folder.mkdir()

        with ThreadPoolExecutor(max_workers=len(folders)) as pool:
            results = list(pool.map(lambda index: self.add(f"abr-concur{index:02d}$", f"abr-smb-concur{index:02d}", folders[index]), range(len(folders))))

        self.assertEqual(len(results), len(folders))
        self.assertEqual(len(self.ours()), len(folders))
        self.assertEqual(self.system.smb_conf.read_text().count("include ="), 1)


class SectionTextTests(unittest.TestCase):
    def test_sections_read_back_as_they_were_written(self) -> None:
        sections = {SHARE: {"path": "/srv/a", "valid users": USER}, OTHER_SHARE: {"path": "/srv/b", "valid users": OTHER_USER}}

        self.assertEqual(shares_broker.parse_sections(shares_broker.render_sections(sections)), sections)

    def test_comments_and_lines_before_a_section_are_ignored(self) -> None:
        text = "loose = value\n# comment = no\n[share]\n; also = no\n   Path = /srv\n"

        self.assertEqual(shares_broker.parse_sections(text), {"share": {"path": "/srv"}})


class EntryPointTests(unittest.TestCase):
    def test_the_operations_are_exactly_add_and_remove(self) -> None:
        self.assertEqual(sorted(shares_broker.operations(shares_broker.Samba("altastor-backup"))), ["add-share", "remove-share"])

    def test_the_service_user_defaults_to_altastor_backup(self) -> None:
        captured = {}

        def serve(operations, *, service_user):
            captured["service_user"] = service_user
            captured["operations"] = sorted(operations)
            return 0

        original = shares_broker.broker_core.serve
        shares_broker.broker_core.serve = serve
        self.addCleanup(setattr, shares_broker.broker_core, "serve", original)

        with patch.object(shares_broker.os, "umask") as umask:
            self.assertEqual(shares_broker.main([]), 0)
            self.assertEqual(captured, {"service_user": "altastor-backup", "operations": ["add-share", "remove-share"]})
            shares_broker.main(["--service-user", "as"])

        self.assertEqual(captured["service_user"], "as")
        umask.assert_called_with(0o077)


if __name__ == "__main__":
    unittest.main()
