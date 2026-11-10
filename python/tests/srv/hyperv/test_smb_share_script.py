# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from python.srv.plugins.hyperv import abr_smb_share as script


def _catalog(*repositories: dict, jobs: tuple = (), tasks: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(
        REPOSITORIES={record["id"]: record for record in repositories},
        JOBS={job["id"]: job for job in jobs},
        TASKS={task["id"]: task for task in tasks},
        catalog_records=lambda store: list(store.values()),
        persist_repository=lambda record: None,
    )


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

        self.assertEqual(out.getvalue(), "/srv/a : abr-aaaaaaaa$\n/srv/b : none\n")

    def test_a_path_is_matched_however_it_is_written_and_an_unknown_one_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "repo")
            folder.mkdir()
            catalog = _catalog(_repository("local", str(folder)))

            self.assertEqual(script.repository_at(catalog, f"{folder}/")["id"], "local")
            self.assertEqual(script.repository_at(catalog, f"{folder}/../repo")["id"], "local")
            with self.assertRaises(script.Refused) as caught:
                script.repository_at(catalog, root)
        self.assertIn("is not a local repository of AltaStor Backup", str(caught.exception))


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
        self.assertIn("already shared as abr-aaaaaaaa$; nothing to do", out.getvalue())

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
