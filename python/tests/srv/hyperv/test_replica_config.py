# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from typing import Any

from python.srv.plugins.hyperv import hyperv_replica

STATE = {"vm_id": "vm-r", "name": "replica", "host": "HV-01"}


class ScriptNode:
    def __init__(self, answers: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.answers = answers or {}
        self.runs: list[tuple[str, dict[str, Any], str]] = []

    def _run_script(self, name: str, args: dict[str, Any], host: str) -> list[dict[str, Any]]:
        self.runs.append((name, dict(args), host))
        return self.answers.get(name, [])

    def resolve_node(self, host: str) -> str:
        return host.upper()

    def host_resources(self, node: str) -> dict[str, Any]:
        self.runs.append(("host_resources.ps1", {}, node))
        return {"switches": [{"name": "DR-LAN"}, {"name": "DR-DMZ"}]}


class HyperVConfigTests(unittest.TestCase):
    def test_the_configuration_is_read_with_adapter_ids_and_vlans(self) -> None:
        node = ScriptNode(
            {
                "vm_config.ps1": [
                    {
                        "num_cpu": 2,
                        "memory_mb": 2048,
                        "network_adapters": [
                            {"label": "Network Adapter", "network": "DR-LAN", "adapter_id": "ab-1", "vlan_mode": "Access", "vlan_id": 20},
                            {"label": "Network Adapter", "network": "", "adapter_id": "cd-2", "vlan_mode": "Untagged", "vlan_id": 0},
                        ],
                    }
                ]
            }
        )

        config = hyperv_replica.read_config(node, STATE)

        self.assertEqual(node.runs[0][:2], ("vm_config.ps1", {"vm_id": "vm-r"}))
        self.assertEqual(
            [(item["id"], item["network"], item["connected"], item["vlan_id"]) for item in config["network_adapters"]],
            [("AB-1", "DR-LAN", True, 20), ("CD-2", "", False, 0)],
        )

    def test_hardware_and_adapters_go_to_their_own_scripts(self) -> None:
        node = ScriptNode({"replica_config.ps1": [{"vm_id": "vm-r", "added": "ef-3"}]})

        added = hyperv_replica.apply_config(
            node,
            STATE,
            {"num_cpu": 4, "memory_mb": 4096, "dynamic_memory": False},
            remove=["CD-2"],
            change=[{"id": "AB-1", "network": "DR-DMZ", "connected": True, "vlan_id": 0}],
            add=[{"label": "Network Adapter", "network": "DR-LAN", "connected": True, "vlan_id": 30, "legacy": False}],
        )

        self.assertEqual(added, ["EF-3"])
        self.assertEqual([run[0] for run in node.runs], ["set_hardware.ps1", "replica_config.ps1"])
        self.assertEqual(node.runs[0][1], {"vm_id": "vm-r", "num_cpu": 4, "memory_mb": 4096, "dynamic_memory": False})
        self.assertNotIn("notes", node.runs[0][1])
        self.assertEqual(node.runs[1][1]["remove"], ["CD-2"])
        self.assertEqual(node.runs[1][1]["add"][0]["vlan_id"], 30)

    def test_nothing_to_change_runs_no_script(self) -> None:
        node = ScriptNode()

        self.assertEqual(hyperv_replica.apply_config(node, STATE, {}, remove=[], change=[], add=[]), [])
        self.assertEqual(node.runs, [])

    def test_several_added_adapters_come_back_in_order(self) -> None:
        node = ScriptNode({"replica_config.ps1": [{"added": ["a-1", "b-2"]}]})

        added = hyperv_replica.apply_config(node, STATE, {}, remove=[], change=[], add=[{}, {}])

        self.assertEqual(added, ["A-1", "B-2"])

    def test_a_rename_uses_the_replica_script(self) -> None:
        node = ScriptNode()

        hyperv_replica.rename(node, STATE, "replica-dr")

        self.assertEqual(node.runs, [("replica_config.ps1", {"vm_id": "vm-r", "rename": "replica-dr"}, "HV-01")])

    def test_only_the_switches_of_the_replica_host_are_offered(self) -> None:
        node = ScriptNode()

        self.assertEqual(hyperv_replica.networks(node, STATE, ["DR-LAN", "LAN"]), {"DR-LAN"})
        self.assertEqual(node.runs, [("host_resources.ps1", {}, "HV-01")])

    def test_settings_replication_does_not_change_are_named_for_the_replica(self) -> None:
        current = {"secure_boot_enabled": False, "tpm_enabled": False, "dvd_drives": [{"slot": "SCSI0:1", "path": ""}], "version": "12.0"}
        wanted = {"secure_boot_enabled": True, "tpm_enabled": True, "dvd_drives": [], "version": "9.0", "hard_drives": [{"slot": "SCSI0:0"}]}

        notes = hyperv_replica.unsynced(current, wanted)

        self.assertIn("Secure Boot is off (the source VM: on)", notes)
        self.assertIn("the virtual TPM is off (the source VM: on)", notes)
        self.assertIn("the DVD drive SCSI0:1 is on the replica but not on the source VM", notes)
        self.assertIn("the configuration version is 12.0 (the source VM: 9.0)", notes)
        self.assertFalse(any("hard disk" in note or "the backup" in note for note in notes))


class RebuildLocationTests(unittest.TestCase):
    def test_the_rebuild_goes_to_the_node_the_replica_is_on_with_the_job_datastore(self) -> None:
        settings = {"target_host": "HV-01", "target_datastore": "D:\\Replicas"}

        self.assertEqual(hyperv_replica.rebuild_location({"host": "HV-02"}, settings), ("HV-02", "D:\\Replicas"))
        self.assertEqual(hyperv_replica.rebuild_location({"host": ""}, settings), ("HV-01", "D:\\Replicas"))


if __name__ == "__main__":
    unittest.main()
