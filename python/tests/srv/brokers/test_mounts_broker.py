# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import io
import os
import pwd
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from python.srv.brokers import mounts_broker
from python.srv.brokers.broker_core import FAILED, INVALID_REQUEST, REFUSED, BrokerError

REPOSITORY = "89316b5e-be92-4d98-9dd2-2bfe47ee1672"
OTHER = "0d5a3c6e-1111-4222-8333-944455556666"
PASSWORD = "p,a=s s;'\"%ü\\x"


def current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


class FakeMounts:
    def __init__(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.root = base / "mounts"
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.mountinfo = base / "mountinfo"
        self.mounts: list[tuple[str, str, str]] = [("/", "ext4", "/dev/sda1"), ("/run", "tmpfs", "tmpfs")]
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.passwords: list[bytes] = []
        self.mount_result: tuple[int, str] = (0, "")
        self.mount_lands = True
        self.busy: set[str] = set()
        self.umount_fails: set[str] = set()
        self.write()

    def write(self) -> None:
        lines = [
            f"{index} 1 0:{index} / {point.replace(' ', chr(92) + '040')} rw,nosuid shared:1 - {fstype} {source} rw"
            for index, (point, fstype, source) in enumerate(self.mounts, start=20)
        ]
        self.mountinfo.write_text("\n".join(lines) + "\n")

    def system(self, **changes) -> mounts_broker.System:
        values = {
            "service_user": current_user(),
            "root": self.root,
            "mountinfo": self.mountinfo,
            "run": self.run,
            "geteuid": lambda: 0,
            "root_uid": os.getuid(),
        }
        values.update(changes)
        return mounts_broker.System(**values)

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.environments.append(dict(kwargs.get("env") or {}))
        if argv[0] == "mount.cifs":
            fd = int(kwargs["env"]["PASSWD_FD"])
            self.passwords.append(os.read(fd, 4096))
            code, stderr = self.mount_result
            if code == 0 and self.mount_lands:
                self.mounts.append((argv[2], "cifs", argv[1]))
                self.write()
            return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)
        if argv[0] == "umount":
            point = argv[-1]
            if point in self.umount_fails:
                return subprocess.CompletedProcess(argv, 32, stdout="", stderr=f"umount: {point}: permission denied.\n")
            if point in self.busy and "-l" not in argv:
                return subprocess.CompletedProcess(argv, 32, stdout="", stderr=f"umount: {point}: target is busy.\n")
            self.mounts = [mount for mount in self.mounts if mount[0] != point]
            self.write()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {argv}")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMounts()
        self.addCleanup(self.fake.folder.cleanup)
        self.system = self.fake.system()

    def mount(self, **changes) -> dict:
        arguments = {"repository_id": REPOSITORY, "server": "nas.example", "share": "backups", "username": "backup", "password": PASSWORD}
        arguments.update(changes)
        return mounts_broker.mount_share(self.system, arguments)

    def refused(self, call, code: str = INVALID_REQUEST) -> BrokerError:
        with self.assertRaises(BrokerError) as caught:
            call()
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def point(self, trial: bool = False) -> Path:
        return self.fake.root / f"{REPOSITORY}{'.trial' if trial else ''}"


class MountTests(_Case):
    def test_a_share_is_mounted_at_the_repositorys_point_with_the_fixed_options(self) -> None:
        result = self.mount(subfolder="vm/prod", domain="CORP")

        self.assertEqual(result, {"mount_point": str(self.point()), "source": "//nas.example/backups/vm/prod", "already_mounted": False})
        [argv] = self.fake.calls
        self.assertEqual(argv[:4], ["mount.cifs", "//nas.example/backups/vm/prod", str(self.point()), "-o"])
        account = pwd.getpwnam(current_user())
        self.assertEqual(
            argv[4].split(","),
            [
                "vers=3.1.1", "seal", f"uid={account.pw_uid}", f"gid={account.pw_gid}", "file_mode=0640", "dir_mode=0750",
                "iocharset=utf8", "nosuid", "nodev", "noexec", "soft", "username=backup", "domain=CORP",
            ],
        )
        self.assertEqual(stat.S_IMODE(self.point().stat().st_mode), 0o555)

    def test_the_password_goes_through_a_descriptor_as_it_is_without_a_line_end(self) -> None:
        self.mount()

        self.assertEqual(self.fake.passwords, [PASSWORD.encode()])
        self.assertTrue(all(PASSWORD not in part for part in self.fake.calls[0]))
        self.assertEqual(set(self.fake.environments[0]), {"PATH", "LC_ALL", "PASSWD_FD"})

    def test_a_trial_mount_has_its_own_point(self) -> None:
        result = self.mount(trial=True)

        self.assertEqual(result["mount_point"], str(self.point(trial=True)))
        self.assertTrue(self.point(trial=True).is_dir())

    def test_a_share_already_mounted_there_is_reported_not_mounted_again(self) -> None:
        self.mount()
        again = self.mount(server="NAS.example", share="Backups")

        self.assertTrue(again["already_mounted"])
        self.assertEqual(len(self.fake.calls), 1)

    def test_another_share_at_the_point_is_refused(self) -> None:
        self.mount()

        error = self.refused(lambda: self.mount(share="other"), REFUSED)
        self.assertIn("//nas.example/backups is already mounted", error.message)

    def test_a_failed_mount_leaves_no_point_and_names_the_reason(self) -> None:
        for code, stderr, words in (
            (32, "mount error(13): Permission denied\nRefer to the mount.cifs(8) manual page\n", "the user name or password is wrong"),
            (32, "mount error(2): No such file or directory\nRefer to the mount.cifs(8) manual page\n", "does not exist"),
            (32, "mount error(11): Resource temporarily unavailable\nRefer to the mount.cifs(8) manual page\n", "guest session cannot be encrypted"),
            (32, "mount error(5): Input/output error\nRefer to the mount.cifs(8) manual page\n", "mount error 5 (Input/output error)"),
            (
                32,
                "mount error(115): could not connect to 192.0.2.10Unable to find suitable address.\n",
                "mount error 115 (could not connect to 192.0.2.10: no address answered): the server did not answer",
            ),
            (32, "mount error(22): could not resolve address for nas.localWrong address given\n", "nas.local; Wrong address given"),
            (1, "something else went wrong\n", "something else went wrong"),
        ):
            with self.subTest(stderr=stderr):
                self.fake.mount_result = (code, stderr)
                error = self.refused(self.mount, REFUSED)
                self.assertIn(words, error.message)
                self.assertNotIn("Refer to the mount.cifs", error.message)
                self.assertFalse(self.point().exists())

    def test_a_mount_that_reports_success_but_mounts_nothing_fails(self) -> None:
        self.fake.mount_lands = False

        self.refused(self.mount, FAILED)

    def test_the_point_must_be_an_empty_folder_of_root(self) -> None:
        self.point().mkdir()
        (self.point() / "leftover").write_text("x")
        self.assertIn("is not empty", self.refused(self.mount, REFUSED).message)

        (self.point() / "leftover").unlink()
        self.point().rmdir()
        self.point().symlink_to(self.fake.root.parent)
        self.assertIn("is not a folder of root", self.refused(self.mount, REFUSED).message)

    def test_the_mounts_folder_must_exist_and_be_writable_by_root_only(self) -> None:
        self.fake.root.chmod(0o777)
        self.assertIn("only root can write to", self.refused(self.mount, REFUSED).message)

        self.system = self.fake.system(root=self.fake.root / "missing")
        self.assertIn("the package creates it", self.refused(self.mount, REFUSED).message)

    def test_a_missing_service_account_is_refused(self) -> None:
        self.system = self.fake.system(service_user="no-such-account-x")

        self.assertIn("there is no account", self.refused(self.mount, REFUSED).message)


class ValidationTests(_Case):
    def test_each_field_refuses_what_could_become_an_option(self) -> None:
        for field_name, value in (
            ("server", "nas,port=1"),
            ("server", "nas=x"),
            ("share", "backups,ro"),
            ("share", "a=b"),
            ("subfolder", "vm,ro"),
            ("subfolder", "vm=x"),
            ("username", "backup,password=x"),
            ("username", "backup=x"),
            ("domain", "corp,uid=0"),
            ("domain", "corp=x"),
        ):
            with self.subTest(field=field_name, value=value):
                self.refused(lambda field_name=field_name, value=value: self.mount(**{field_name: value}))
        self.assertEqual(self.fake.calls, [])

    def test_names_paths_and_texts_are_checked(self) -> None:
        for changes, words in (
            ({"repository_id": "../../etc"}, "repository's UUID"),
            ({"repository_id": REPOSITORY.upper()}, "repository's UUID"),
            ({"server": "nas example"}, "host name or an IP address"),
            ({"server": ""}, "must be given"),
            ({"server": "-nas"}, "host name or an IP address"),
            ({"share": "back/ups"}, "must not hold /"),
            ({"share": ""}, "must be given"),
            ({"share": " backups"}, "blanks"),
            ({"subfolder": "vm/../etc"}, "without . or .."),
            ({"subfolder": "vm\x07"}, "control characters"),
            ({"username": ""}, "must be given"),
            ({"username": "a/b"}, "must not hold /"),
            ({"domain": "-corp"}, "domain or workgroup name"),
            ({"domain": "корп"}, "domain or workgroup name"),
            ({"password": ""}, "password must be given"),
            ({"password": "a\x00b"}, "without NUL"),
            ({"password": "x" * 257}, "at most 256"),
            ({"trial": "yes"}, "true or false"),
            ({"server": 5}, "must be a text"),
            ({"options": "uid=0"}, "unknown: options"),
        ):
            with self.subTest(changes=changes):
                self.assertIn(words, self.refused(lambda changes=changes: self.mount(**changes)).message)

    def test_missing_arguments_are_named(self) -> None:
        error = self.refused(lambda: mounts_broker.mount_share(self.system, {"repository_id": REPOSITORY}))
        self.assertIn("missing: password, server, share, username", error.message)

    def test_addresses_and_backslashes_are_taken(self) -> None:
        for server in ("192.168.15.9", "[fe80::1]", "fe80::1", "nas-01.corp.local"):
            with self.subTest(server=server):
                self.mount(server=server)
                mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY})
        self.mount(subfolder="vm\\prod\\")
        self.assertEqual(self.fake.calls[-1][1], "//nas.example/backups/vm/prod")


class UnmountTests(_Case):
    def test_a_mounted_share_is_unmounted_and_its_point_removed(self) -> None:
        self.mount()

        result = mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY})

        self.assertEqual(result, {"mount_point": str(self.point()), "source": "//nas.example/backups", "unmounted": True})
        self.assertFalse(self.point().exists())

    def test_nothing_mounted_is_not_an_error_and_a_stale_point_goes(self) -> None:
        self.point().mkdir()

        result = mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY})

        self.assertFalse(result["unmounted"])
        self.assertFalse(self.point().exists())

    def test_a_share_in_use_is_refused_and_stays(self) -> None:
        self.mount()
        self.fake.busy.add(str(self.point()))

        error = self.refused(lambda: mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY}), mounts_broker.BUSY)

        self.assertIn("is in use", error.message)
        self.assertIsNotNone(mounts_broker.mount_at(self.system, self.point()))
        self.assertTrue(all("-l" not in call for call in self.fake.calls))

    def test_another_failure_is_reported(self) -> None:
        self.mount()
        self.fake.umount_fails.add(str(self.point()))

        self.assertIn("permission denied", self.refused(lambda: mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY}), FAILED).message)

    def test_the_trial_point_is_unmounted_on_its_own(self) -> None:
        self.mount()
        self.mount(trial=True)

        mounts_broker.unmount_share(self.system, {"repository_id": REPOSITORY, "trial": True})

        self.assertFalse(self.point(trial=True).exists())
        self.assertIsNotNone(mounts_broker.mount_at(self.system, self.point()))


class UnmountAllTests(_Case):
    def run_command_line(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mounts_broker.unmount_from_command_line(self.system)
        return code, out.getvalue(), err.getvalue()

    def test_every_share_of_ours_goes_and_the_rest_stays(self) -> None:
        self.mount()
        self.mount(repository_id=OTHER)
        self.mount(trial=True)
        self.fake.mounts.append((str(self.fake.root / "not-a-repository"), "cifs", "//other/share"))
        self.fake.mounts.append(("/mnt/hv", "cifs", "//nas/hv"))
        self.fake.write()
        self.fake.busy.add(str(self.fake.root / OTHER))

        code, out, err = self.run_command_line()

        self.assertEqual((code, err), (0, ""))
        left = [point for point, _fstype, _source in self.fake.mounts]
        self.assertIn(str(self.fake.root / "not-a-repository"), left)
        self.assertIn("/mnt/hv", left)
        self.assertNotIn(str(self.point()), left)
        self.assertNotIn(str(self.fake.root / OTHER), left)
        self.assertIn("Detached", out)
        self.assertEqual(sorted(path.name for path in self.fake.root.iterdir()), [])

    def test_nothing_to_unmount_succeeds(self) -> None:
        code, out, _err = self.run_command_line()

        self.assertEqual((code, out.strip()), (0, "No AltaStor Backup shares to unmount."))

    def test_only_root_may_run_it(self) -> None:
        self.system = self.fake.system(geteuid=lambda: 1000)

        code, _out, err = self.run_command_line()

        self.assertEqual(code, 1)
        self.assertIn("must run as root", err)

    def test_a_share_that_cannot_even_be_detached_fails_the_command(self) -> None:
        self.mount()
        self.fake.umount_fails.add(str(self.point()))

        code, _out, err = self.run_command_line()

        self.assertEqual(code, 1)
        self.assertIn("could not unmount", err)

    def test_the_command_line_option_runs_it(self) -> None:
        with (
            patch.object(mounts_broker, "unmount_from_command_line", return_value=0) as run,
            patch.object(mounts_broker.broker_core, "serve") as serve,
            patch.object(mounts_broker.os, "umask") as umask,
        ):
            self.assertEqual(mounts_broker.main(["--unmount-all", "--service-user", current_user()]), 0)

        run.assert_called_once()
        serve.assert_not_called()
        umask.assert_called_once_with(0o077)


class OperationTests(_Case):
    def test_the_broker_runs_mount_and_unmount_only(self) -> None:
        self.assertEqual(sorted(mounts_broker.operations(self.system)), ["mount", "unmount"])


if __name__ == "__main__":
    unittest.main()
