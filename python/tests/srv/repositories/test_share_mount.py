# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.services import share_mount
from python.srv.services.share_mount import read_mounts, share_mount_check


def _line(point: str, fstype: str, source: str) -> str:
    return f"36 30 0:44 / {point} rw,relatime shared:1 - {fstype} {source} rw\n"


ROOT = _line("/", "ext4", "/dev/mapper/vg-root")


class ShareMountCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.mountinfo = Path(self._tmp.name) / "mountinfo"
        addresses = {"nas.example.test": {"192.0.2.10"}, "192.0.2.10": {"192.0.2.10"}, "other.example.test": {"192.0.2.20"}}
        self._addresses = patch.object(share_mount, "_addresses", side_effect=lambda host: addresses.get(host, set()))
        self._addresses.start()
        self._appliance = patch.object(share_mount, "is_this_appliance", return_value=False)
        self.appliance = self._appliance.start()

    def tearDown(self) -> None:
        self._appliance.stop()
        self._addresses.stop()
        self._tmp.cleanup()

    def _check(self, path: str, server: str, share: str, *lines: str, **metadata: object) -> dict:
        self.mountinfo.write_text(ROOT + "".join(lines), encoding="utf-8")
        repository = {"type": "smb", "path": path, "metadata": {"server": server, "share": share, **metadata}}
        with patch.object(Path, "resolve", lambda self, strict=False: self):
            return share_mount_check(repository, self.mountinfo)

    def test_a_mount_of_the_share_matches(self) -> None:
        check = self._check("/mnt/hv", "nas.example.test", "backups", _line("/mnt/hv", "cifs", "//nas.example.test/backups"))

        self.assertEqual(check["status"], "ok")
        self.assertIn("is \\\\nas.example.test\\backups, mounted at /mnt/hv", check["message"])

    def test_the_server_may_be_named_by_its_address_and_the_share_in_another_case(self) -> None:
        check = self._check("/mnt/hv", "nas.example.test", "Backups", _line("/mnt/hv", "smb3", "//192.0.2.10/backups"))

        self.assertEqual(check["status"], "ok")

    def test_a_folder_of_the_share_matches_the_share_and_folder(self) -> None:
        mount = _line("/mnt/hv", "cifs", "//nas.example.test/backups")

        self.assertEqual(self._check("/mnt/hv/hyperv", "nas.example.test", "backups\\hyperv", mount)["status"], "ok")
        self.assertEqual(self._check("/mnt/hv/hyperv", "nas.example.test", "backups", mount)["status"], "failed")

    def test_a_mount_of_another_share_or_server_is_refused_naming_it(self) -> None:
        other_share = self._check("/mnt/hv", "nas.example.test", "backups", _line("/mnt/hv", "cifs", "//nas.example.test/media"))
        other_server = self._check("/mnt/hv", "nas.example.test", "backups", _line("/mnt/hv", "cifs", "//other.example.test/backups"))

        self.assertEqual(other_share["status"], "failed")
        self.assertIn("it is \\\\nas.example.test\\media", other_share["message"])
        self.assertEqual(other_server["status"], "failed")
        self.assertIn("it is \\\\other.example.test\\backups", other_server["message"])

    def test_a_folder_on_the_local_disk_is_refused_with_how_to_mount_the_share(self) -> None:
        check = self._check("/aqa-data/repos/smb-fake", "192.0.2.99", "nosuchshare")

        self.assertEqual(check["status"], "failed")
        self.assertIn("is not the share \\\\192.0.2.99\\nosuchshare", check["message"])
        self.assertIn("own ext4 filesystem at /", check["message"])
        self.assertIn("mount -t cifs //192.0.2.99/nosuchshare /aqa-data/repos/smb-fake", check["message"])

    def test_an_nfs_export_of_the_same_server_is_a_warning_of_another_server_a_refusal(self) -> None:
        same = self._check("/mnt/hv", "nas.example.test", "backups", _line("/mnt/hv", "nfs4", "192.0.2.10:/volume1/backups"))
        other = self._check("/mnt/hv", "nas.example.test", "backups", _line("/mnt/hv", "nfs", "other.example.test:/backups"))

        self.assertEqual(same["status"], "warning")
        self.assertIn("cannot be checked", same["message"])
        self.assertEqual(other["status"], "failed")

    def test_a_share_of_this_appliance_must_share_this_folder(self) -> None:
        self.appliance.return_value = True
        with patch.object(share_mount, "samba_share_path", return_value="/srv/hv"):
            ok = self._check("/srv/hv/vms", "appliance", "abr-share\\vms")
            elsewhere = self._check("/srv/other", "appliance", "abr-share")
        with patch.object(share_mount, "samba_share_path", return_value=None):
            missing = self._check("/srv/hv", "appliance", "nosuch")
        with patch.object(share_mount, "samba_share_path", side_effect=FileNotFoundError("testparm is not installed")):
            unknown = self._check("/srv/hv", "appliance", "abr-share")

        self.assertEqual(ok["status"], "ok")
        self.assertEqual(elsewhere["status"], "failed")
        self.assertIn("this appliance shares /srv/hv under that name", elsewhere["message"])
        self.assertEqual(missing["status"], "failed")
        self.assertIn("has no Samba share named nosuch", missing["message"])
        self.assertEqual(unknown["status"], "warning")

    def test_the_default_repository_share_is_its_own(self) -> None:
        self.assertEqual(self._check("/var/lib/altastor-backup/repository", "appliance", "abr-x$", support_hyperv=True)["status"], "ok")

    def test_other_repository_types_are_not_checked(self) -> None:
        self.assertIsNone(share_mount_check({"type": "local", "path": "/srv/repo"}, self.mountinfo))

    def test_an_unreadable_mount_table_is_a_warning(self) -> None:
        check = share_mount_check(
            {"type": "smb", "path": "/mnt/hv", "metadata": {"server": "nas.example.test", "share": "backups"}},
            Path(self._tmp.name) / "missing",
        )

        self.assertEqual(check["status"], "warning")


class MountTableTests(unittest.TestCase):
    def test_escaped_spaces_in_mount_points_are_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mountinfo = Path(tmp) / "mountinfo"
            mountinfo.write_text(ROOT + _line("/mnt/hyper\\040v", "cifs", "//nas/back\\040ups"), encoding="utf-8")

            mounts = read_mounts(mountinfo)

        self.assertEqual(mounts[-1].point, Path("/mnt/hyper v"))
        self.assertEqual(mounts[-1].source, "//nas/back ups")

    def test_the_deepest_mount_holds_the_path(self) -> None:
        mounts = [
            share_mount.Mount(Path("/"), "ext4", "/dev/sda1"),
            share_mount.Mount(Path("/mnt"), "ext4", "/dev/sdb1"),
            share_mount.Mount(Path("/mnt/hv"), "cifs", "//nas/backups"),
        ]

        self.assertEqual(share_mount.mount_holding(Path("/mnt/hv/vms"), mounts).fstype, "cifs")
        self.assertEqual(share_mount.mount_holding(Path("/mnt/hvx"), mounts).source, "/dev/sdb1")


if __name__ == "__main__":
    unittest.main()
