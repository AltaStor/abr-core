# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import unittest

from python.srv.plugins.vcenter.vcenter_restore import _build_abr_restore_command


class BuildAbrRestoreCommandForceFlagTests(unittest.TestCase):
    def _build(self, *, force: bool) -> list[str]:
        return _build_abr_restore_command(
            tool_path=pathlib.Path("/opt/abr-vmdk-tool"),
            repository={"type": "local", "id": ""},
            abr_path="/backups/demo-vm/task-1",
            backup_name="01-demo-vm.vmdk",
            remote_datastore_path="[datastore1] demo-vm-restored/01-demo-vm.vmdk",
            host="vc.local",
            port=443,
            username="administrator",
            password="secret",
            thumbprint="AA:BB",
            vm_moref="vm-101",
            transport_mode="nbdssl",
            force=force,
        )

    def test_incremental_layer_includes_force_flag(self) -> None:
        command = self._build(force=True)
        self.assertIn("--force", command)

    def test_full_layer_omits_force_flag(self) -> None:
        command = self._build(force=False)
        self.assertNotIn("--force", command)
