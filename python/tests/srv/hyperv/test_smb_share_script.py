# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from python.srv.plugins.hyperv import abr_smb_share as script


def _catalog(*repositories: dict, jobs: tuple = (), tasks: tuple = (), status: dict | None = None) -> SimpleNamespace:
    reloads: dict[str, str] = {}
    return SimpleNamespace(
        REPOSITORIES={record["id"]: record for record in repositories},
        JOBS={job["id"]: job for job in jobs},
        TASKS={task["id"]: task for task in tasks},
        catalog_records=lambda store: list(store.values()),
        persist_repository=lambda record: None,
        server_status=lambda: status,
        reload_requests=reloads,
        request_repository_reload=lambda repository_id, request_id: reloads.__setitem__(repository_id, request_id),
        repository_reload_done=lambda repository_id: None,
    )


def _status(age_seconds: float = 5, **extra) -> dict:
    at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {"running": True, "repository_reload": True, "pid": 4242, "updated_at": at.isoformat(), **extra}


def _repository(repository_id: str, path: str, **extra) -> dict:
    return {"id": repository_id, "name": repository_id, "type": "local", "path": path, "metadata": {}, **extra}


def _shared(repository_id: str, path: str, share: str) -> dict:
    return _repository(
        repository_id,
        path,
        type="smb",
        metadata={"support_hyperv": True, "share": share, "username": "abr-smb-abcdefgh", "password": "gAAAA-encrypted", "server": "host-1"},
    )


class SambaConfigurationTests(unittest.TestCase):
    def test_sections_and_their_settings_are_read_without_comments(self) -> None:
        text = "[global]\n    workgroup = WG\n# a comment\n[abr-abcdefgh$]\n    path = /srv/repo\n    ; old = value\n    valid users = abr-smb-abcdefgh\n"

        sections = script.samba_sections(text)

        self.assertEqual(sections["abr-abcdefgh$"], {"path": "/srv/repo", "valid users": "abr-smb-abcdefgh"})
        self.assertEqual(sections["global"], {"workgroup": "WG"})

    def test_written_shares_read_back_the_same(self) -> None:
        sections = {"abr-aaaaaaaa$": {"path": "/a", "browseable": "no"}, "abr-bbbbbbbb$": {"path": "/b"}}

        self.assertEqual(script.samba_sections(script.shares_text(sections)), sections)


class RepositoryChoiceTests(unittest.TestCase):
    def test_local_and_script_shared_repositories_are_listed_but_not_s3_or_mounted_smb(self) -> None:
        catalog = _catalog(
            _repository("local", "/srv/b"),
            _shared("shared", "/srv/a", "abr-aaaaaaaa$"),
            _repository("s3", "", type="s3"),
            _repository("mounted", "/mnt/nas", type="smb", metadata={"share": "backups"}),
        )

        self.assertEqual([record["id"] for record in script.local_repositories(catalog)], ["shared", "local"])

    def test_show_prints_each_path_with_its_share_or_none(self) -> None:
        catalog = _catalog(_repository("local", "/srv/b"), _shared("shared", "/srv/a", "abr-aaaaaaaa$"))
        out = io.StringIO()

        with redirect_stdout(out):
            script.show(catalog)

        self.assertEqual(out.getvalue(), "/srv/a : \\\\host-1\\abr-aaaaaaaa$\n/srv/b : none\n")

    def test_a_path_is_matched_however_it_is_written_and_an_unknown_one_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "repo")
            folder.mkdir()
            catalog = _catalog(_repository("local", str(folder)))

            self.assertEqual(script.repository_at(catalog, f"{folder}/")["id"], "local")
            self.assertEqual(script.repository_at(catalog, f"{folder}/../repo")["id"], "local")
            with self.assertRaises(script.Refused) as caught:
                script.repository_at(catalog, root)
        self.assertEqual(str(caught.exception), f"{root} is not a local repository; see: sudo python3 abr_smb_share.py show")


class RefusalTests(unittest.TestCase):
    def test_running_sessions_stop_add_and_del(self) -> None:
        catalog = _catalog(tasks=({"id": "t", "status": "running"},))

        with self.assertRaises(script.Refused):
            script.refuse_while_sessions_run(catalog)
        script.refuse_while_sessions_run(_catalog(tasks=({"id": "t", "status": "completed"},)))

    def test_an_already_shared_repository_is_left_as_it_is(self) -> None:
        catalog = _catalog(_shared("shared", "/srv/a", "abr-aaaaaaaa$"))
        out = io.StringIO()

        with patch.object(script, "run") as run, patch.object(script.os.path, "realpath", side_effect=lambda value: value), redirect_stdout(out):
            script.add(catalog, None, "/srv/a")

        run.assert_not_called()
        self.assertEqual(out.getvalue(), "/srv/a is already shared as \\\\host-1\\abr-aaaaaaaa$\n")

    def test_del_leaves_a_share_made_by_the_default_repo_switch(self) -> None:
        catalog = _catalog(_shared("default", "/srv/a", "abr-aaaaaaaa$"))

        with patch.object(script, "our_shares", return_value={}), patch.object(script.os.path, "realpath", side_effect=lambda value: value), self.assertRaises(script.Refused) as caught:
            script.delete(catalog, SimpleNamespace(_inventory_is_hyperv=lambda _id: False), "/srv/a")

        self.assertIn("abr repo default --no-support-hyperv", str(caught.exception))

    def test_del_waits_for_the_hyperv_jobs_that_use_the_repository(self) -> None:
        catalog = _catalog(_shared("repo", "/srv/a", "abr-aaaaaaaa$"), jobs=({"id": "j", "name": "hv-nightly", "repo_id": "repo", "inventory_id": "hv"},))

        with patch.object(script, "our_shares", return_value={"abr-aaaaaaaa$": {}}), patch.object(script.os.path, "realpath", side_effect=lambda value: value), self.assertRaises(script.Refused) as caught:
            script.delete(catalog, SimpleNamespace(_inventory_is_hyperv=lambda inventory: inventory == "hv"), "/srv/a")

        self.assertIn("hv-nightly", str(caught.exception))


class OutputTests(unittest.TestCase):
    def test_add_and_del_name_the_full_network_path_and_say_nothing_of_hyper_v(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            catalog = _catalog(_repository("repo", root))
            crypto = SimpleNamespace(encrypt_secret=lambda value: "enc", decrypt_secret=lambda value: "plain")
            added, removed = io.StringIO(), io.StringIO()
            patches = (
                patch.object(script, "preflight"),
                patch.object(script, "our_shares", return_value={}),
                patch.object(script, "all_shares", return_value={}),
                patch.object(script, "new_names", return_value=("abr-aaaaaaaa$", "abr-smb-abcdefgh", "secret")),
                patch.object(script, "open_share"),
                patch.object(script, "close_share"),
                patch.object(script, "restart_server", return_value="No altastor-backup-server service here; restart the AltaStor Backup server to apply the change."),
                patch.object(script.socket, "getfqdn", return_value="host-1"),
            )
            for item in patches:
                item.start()
                self.addCleanup(item.stop)
            with redirect_stdout(added):
                script.add(catalog, crypto, root)
            stored = dict(catalog.REPOSITORIES["repo"], type="smb", metadata={"support_hyperv": True, "share": "abr-aaaaaaaa$", "username": "abr-smb-abcdefgh", "server": "host-1"})
            catalog.REPOSITORIES["repo"] = stored
            with patch.object(script, "our_shares", return_value={"abr-aaaaaaaa$": {}}), redirect_stdout(removed):
                script.delete(catalog, SimpleNamespace(_inventory_is_hyperv=lambda _id: False), root)

        self.assertTrue(added.getvalue().startswith(f"Shared {root} as \\\\host-1\\abr-aaaaaaaa$, account abr-smb-abcdefgh\n"), added.getvalue())
        self.assertTrue(removed.getvalue().startswith(f"Unshared {root}: removed \\\\host-1\\abr-aaaaaaaa$ and account abr-smb-abcdefgh\n"), removed.getvalue())
        for text in (added.getvalue(), removed.getvalue(), script.USAGE):
            self.assertNotIn("Hyper-V", text)

    def test_accounts_made_before_and_after_the_rename_are_both_removed(self) -> None:
        for comment in ("AltaStor Backup SMB share", "AltaStor Backup Hyper-V share"):
            with self.subTest(comment=comment):
                calls: list[list[str]] = []

                def run(command, check=True, calls=calls, comment=comment):
                    calls.append(command)
                    stdout = f"abr-smb-x:x:999:65534:{comment}:/nonexistent:/usr/sbin/nologin\n" if command[0] == "getent" else ""
                    return SimpleNamespace(returncode=0, stdout=stdout)

                with patch.object(script, "run", side_effect=run):
                    script.remove_account("abr-smb-x")

                self.assertIn(["userdel", "abr-smb-x"], calls)

    def test_an_account_the_script_did_not_make_is_left(self) -> None:
        calls: list[list[str]] = []

        def run(command, check=True):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="abr-smb-x:x:999:65534:Someone else:/home/x:/bin/bash\n" if command[0] == "getent" else "")

        with patch.object(script, "run", side_effect=run):
            script.remove_account("abr-smb-x")

        self.assertNotIn(["userdel", "abr-smb-x"], calls)


class LiveServerTests(unittest.TestCase):
    def test_a_fresh_status_of_a_server_that_reloads_means_no_restart(self) -> None:
        self.assertEqual(script.server_mode(_catalog(status=_status())), script.LIVE)
        for status in (None, _status(age_seconds=300), _status(running=False), _status(repository_reload=False), {"running": True, "updated_at": "garbage", "repository_reload": True}):
            with self.subTest(status=status):
                self.assertEqual(script.server_mode(_catalog(status=status)), "restart")

    def test_the_time_the_catalog_stores_as_a_naive_utc_datetime_is_understood(self) -> None:
        stored = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)

        self.assertEqual(script.server_mode(_catalog(status=_status(updated_at=stored))), script.LIVE)
        self.assertEqual(script.server_mode(_catalog(status=_status(updated_at=stored - timedelta(minutes=10)))), "restart")

    def test_only_sessions_that_use_the_repository_count(self) -> None:
        jobs = (
            {"id": "backup", "repo_id": "repo"},
            {"id": "other", "repo_id": "elsewhere"},
            {"id": "copy", "repo_id": "dst", "source_job_ids": ["backup"]},
            {"id": "copy-repo", "repo_id": "dst", "source_repository_ids": ["repo"]},
            {"id": "replica", "replication": {"source_job_id": "backup"}},
        )
        tasks = (
            {"id": "t-backup", "status": "running", "job_id": "backup"},
            {"id": "t-other", "status": "running", "job_id": "other"},
            {"id": "t-copy", "status": "queued", "job_id": "copy"},
            {"id": "t-copy-repo", "status": "running", "job_id": "copy-repo"},
            {"id": "t-replica", "status": "running", "job_id": "replica"},
            {"id": "t-restore", "status": "running", "parameters": {"repository_id": "repo"}},
            {"id": "t-restore-elsewhere", "status": "running", "parameters": {"repository_id": "elsewhere"}},
            {"id": "t-done", "status": "completed", "job_id": "backup"},
        )
        catalog = _catalog(_repository("repo", "/srv/a"), jobs=jobs, tasks=tasks)

        busy = {task["id"] for task in script.sessions_using(catalog, catalog.REPOSITORIES["repo"])}

        self.assertEqual(busy, {"t-backup", "t-copy", "t-copy-repo", "t-replica", "t-restore"})

    def test_a_live_server_refuses_only_for_this_repository_and_an_older_one_for_any_session(self) -> None:
        tasks = ({"id": "t", "status": "running", "job_id": "other"},)
        jobs = ({"id": "other", "repo_id": "elsewhere"},)
        live = _catalog(_repository("repo", "/srv/a"), jobs=jobs, tasks=tasks, status=_status())

        script.refuse_while_in_use(live, live.REPOSITORIES["repo"], script.LIVE)
        with self.assertRaises(script.Refused) as caught:
            script.refuse_while_in_use(live, live.REPOSITORIES["repo"], "restart")
        self.assertIn("the server restarts afterwards", str(caught.exception))
        busy = _catalog(_repository("repo", "/srv/a"), tasks=({"id": "r", "status": "running", "parameters": {"repository_id": "repo"}},))
        with self.assertRaises(script.Refused) as caught:
            script.refuse_while_in_use(busy, busy.REPOSITORIES["repo"], script.LIVE)
        self.assertEqual(str(caught.exception), "1 session(s) use this repository; run this again when they finish")

    def test_a_live_server_is_asked_to_re_read_the_repository_and_its_answer_is_awaited(self) -> None:
        catalog = _catalog(_repository("repo", "/srv/a"), status=_status())
        catalog.repository_reload_done = lambda repository_id: catalog.reload_requests.get(repository_id)

        with patch.object(script, "restart_server") as restart:
            message = script.apply_change(catalog, catalog.REPOSITORIES["repo"], script.LIVE)

        restart.assert_not_called()
        self.assertEqual(message, "The server picked up the change.")
        self.assertIn("repo", catalog.reload_requests)

    def test_a_server_that_does_not_answer_is_named_and_an_older_one_is_restarted_as_before(self) -> None:
        catalog = _catalog(_repository("repo", "/srv/a"), status=_status())
        with patch.object(script, "CONFIRM_SECONDS", 0.2), patch.object(script.time, "sleep"):
            self.assertEqual(
                script.apply_change(catalog, catalog.REPOSITORIES["repo"], script.LIVE),
                "The server did not confirm within 0.2 s; restart it to apply the change.",
            )
        with patch.object(script, "restart_server", return_value="Restarted altastor-backup-server.") as restart:
            self.assertEqual(script.apply_change(catalog, catalog.REPOSITORIES["repo"], "restart"), "Restarted altastor-backup-server.")
        restart.assert_called_once()


class EntryPointTests(unittest.TestCase):
    def run_main(self, argv: list[str], *, euid: int = 0) -> tuple[int, str]:
        err = io.StringIO()
        with patch.object(script.os, "geteuid", return_value=euid), redirect_stderr(err):
            code = script.main(argv)
        return code, err.getvalue()

    def test_a_bad_command_line_shows_the_usage(self) -> None:
        for argv in ([], ["add"], ["show", "/x"], ["remove", "/x"], ["add", "/x", "/y"]):
            with self.subTest(argv=argv):
                code, err = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("usage: sudo python3 abr_smb_share.py show | add <path>", err)

    def test_it_asks_for_root(self) -> None:
        code, err = self.run_main(["show"], euid=1000)

        self.assertEqual(code, 1)
        self.assertIn("Run it as root: sudo python3 abr_smb_share.py show", err)

    def test_a_copy_outside_an_install_looks_in_opt_whatever_its_folder(self) -> None:
        for location in ("/abr_smb_share.py", "/home/qa/abr_smb_share.py", "/home/qa/Downloads/scripts/abr/x/abr_smb_share.py"):
            with self.subTest(location=location), patch.object(script, "__file__", location), patch.dict(script.os.environ, {}, clear=False) as env:
                env.pop("ABR_APP_ROOT", None)
                self.assertEqual(script.app_root(), "/opt/altastor-backup")

    def test_in_a_checkout_it_uses_its_own_tree(self) -> None:
        with patch.dict(script.os.environ, {}, clear=False) as env:
            env.pop("ABR_APP_ROOT", None)
            self.assertEqual(Path(script.app_root()), Path(script.__file__).resolve().parents[4])


if __name__ == "__main__":
    unittest.main()
