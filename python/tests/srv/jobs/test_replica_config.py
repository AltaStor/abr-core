# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, TASKS
from python.srv.services.internal.replication import replication_settings
from python.srv.services.internal.replication.replica_config import (
    adapter_key,
    hardware_changes,
    plan_adapters,
)
from python.srv.services.internal.replication.replica_update import run_replication
from python.tests.srv.jobs.test_replica_update import (
    JOB_ID,
    SESSION_ID,
    _layer,
    _UpdateCase,
)

CONFIG = "python.srv.services.internal.replication.replica_config"


def _source(label: str, network: str, *, connected: bool = True, **extra: Any) -> dict[str, Any]:
    return {"label": label, "network": network, "connected": connected, **extra}


def _replica(adapter_id: str, label: str, network: str, *, connected: bool = True, vlan_id: int = 0) -> dict[str, Any]:
    return {"id": adapter_id, "label": label, "network": network, "connected": connected, "vlan_id": vlan_id}


def _pairs(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"source": source, "replica": replica} for source, replica in items]


class AdapterPlanTests(unittest.TestCase):
    def _plan(self, wanted, current, pairs, *, targets=None, available=("dr-lan",)):
        return plan_adapters(wanted, current, pairs, dict(targets or {"lan": "dr-lan"}), set(available), "esx-dr-01")

    def test_an_unchanged_adapter_needs_nothing(self) -> None:
        plan = self._plan([_source("Network adapter 1", "lan")], [_replica("4000", "Network adapter 1", "dr-lan")], _pairs(("label:network adapter 1", "4000")))

        self.assertEqual((plan["add"], plan["change"], plan["remove"], plan["notes"]), ([], [], [], []))
        self.assertEqual(plan["kept"], _pairs(("label:network adapter 1", "4000")))

    def test_a_new_adapter_on_a_mapped_network_is_added_connected(self) -> None:
        plan = self._plan(
            [_source("Network adapter 1", "lan"), _source("Network adapter 2", "lan")],
            [_replica("4000", "Network adapter 1", "dr-lan")],
            _pairs(("label:network adapter 1", "4000")),
        )

        self.assertEqual(len(plan["add"]), 1)
        self.assertEqual(
            {key: plan["add"][0][key] for key in ("source", "label", "network", "connected")},
            {"source": "label:network adapter 2", "label": "Network adapter 2", "network": "dr-lan", "connected": True},
        )
        self.assertEqual(plan["notes"], [])

    def test_a_new_adapter_on_a_network_without_a_mapping_is_added_disconnected_with_a_note(self) -> None:
        plan = self._plan([_source("Network adapter 1", "backup-net")], [], [])

        self.assertEqual(
            {key: plan["add"][0][key] for key in ("network", "source_network", "connected")},
            {"network": "", "source_network": "backup-net", "connected": False},
        )
        self.assertEqual(len(plan["notes"]), 1)
        self.assertIn("backup-net, which this job maps to no network and which is not on host esx-dr-01", plan["notes"][0])
        self.assertIn("left disconnected", plan["notes"][0])

    def test_a_network_of_the_same_name_on_the_target_needs_no_mapping(self) -> None:
        plan = self._plan([_source("Network adapter 1", "VM Network")], [], [], available=("dr-lan", "VM Network"))

        self.assertEqual((plan["add"][0]["network"], plan["add"][0]["connected"], plan["notes"]), ("VM Network", True, []))

    def test_a_mapping_to_a_network_the_host_lacks_leaves_the_adapter_disconnected(self) -> None:
        plan = self._plan(
            [_source("Network adapter 1", "lan")], [_replica("4000", "Network adapter 1", "dr-lan")], _pairs(("label:network adapter 1", "4000")), available=()
        )

        self.assertEqual([(item["id"], item["connected"]) for item in plan["change"]], [("4000", False)])
        self.assertIn("maps to dr-lan, but dr-lan is not on host esx-dr-01", plan["notes"][0])

    def test_an_adapter_the_source_removed_is_removed(self) -> None:
        plan = self._plan(
            [_source("Network adapter 2", "lan")],
            [_replica("4000", "Network adapter 1", "dr-lan"), _replica("4001", "Network adapter 2", "dr-lan")],
            _pairs(("label:network adapter 1", "4000"), ("label:network adapter 2", "4001")),
        )

        self.assertEqual(plan["remove"], ["4000"])
        self.assertEqual(plan["kept"], _pairs(("label:network adapter 2", "4001")))
        self.assertEqual(plan["change"], [])
        self.assertIn("Network adapter 1 on dr-lan removed: the source VM no longer has it", plan["messages"])

    def test_an_adapter_moved_to_another_network_follows_it(self) -> None:
        plan = self._plan(
            [_source("Network adapter 1", "dmz")],
            [_replica("4000", "Network adapter 1", "dr-lan")],
            _pairs(("label:network adapter 1", "4000")),
            targets={"lan": "dr-lan", "dmz": "dr-dmz"},
            available=("dr-lan", "dr-dmz"),
        )

        self.assertEqual([(item["id"], item["network"], item["connected"]) for item in plan["change"]], [("4000", "dr-dmz", True)])

    def test_an_adapter_the_source_disconnected_is_disconnected_without_a_note(self) -> None:
        plan = self._plan(
            [_source("Network adapter 1", "lan", connected=False)], [_replica("4000", "Network adapter 1", "dr-lan")], _pairs(("label:network adapter 1", "4000"))
        )

        self.assertEqual([(item["id"], item["network"], item["connected"]) for item in plan["change"]], [("4000", "dr-lan", False)])
        self.assertEqual(plan["notes"], [])

    def test_a_hyperv_adapter_is_paired_by_its_id_and_follows_its_vlan(self) -> None:
        source = {"label": "Network Adapter", "network": "lan", "adapter_id": "aaaa-1", "vlan_mode": "Access", "vlan_id": 20}
        plan = self._plan(
            [source, {"label": "Network Adapter", "network": "lan", "adapter_id": "bbbb-2", "vlan_mode": "Untagged", "vlan_id": 0}],
            [_replica("R-2", "Network Adapter", "dr-lan"), _replica("R-1", "Network Adapter", "dr-lan")],
            _pairs(("id:AAAA-1", "R-1"), ("id:BBBB-2", "R-2")),
        )

        self.assertEqual([(item["id"], item["vlan_id"]) for item in plan["change"]], [("R-1", 20)])
        self.assertEqual((plan["add"], plan["remove"]), ([], []))

    def test_a_recorded_adapter_the_replica_no_longer_has_is_added_again(self) -> None:
        plan = self._plan([_source("Network adapter 1", "lan")], [], _pairs(("label:network adapter 1", "4000")))

        self.assertEqual((len(plan["add"]), plan["kept"]), (1, []))

    def test_adapters_of_the_same_name_are_told_apart_by_their_number(self) -> None:
        plan = self._plan(
            [{"label": "Network Adapter", "network": "lan", "adapter_id": "a"}, {"label": "Network Adapter", "network": "gone", "adapter_id": "b"}],
            [_replica("R-1", "Network Adapter", "dr-lan"), _replica("R-9", "Network Adapter", "dr-lan")],
            _pairs(("id:A", "R-1")),
        )

        self.assertTrue(plan["notes"][0].startswith("Network Adapter 2 of the source VM is on gone"))
        self.assertIn("Network Adapter 2 added disconnected, as on the source VM", plan["messages"])
        self.assertIn("Network Adapter 2 on dr-lan removed: the source VM no longer has it", plan["messages"])
        self.assertEqual(plan["add"][0]["label"], "Network Adapter")

    def test_adapter_keys(self) -> None:
        self.assertEqual(adapter_key({"adapter_id": "ab-1", "label": "x"}, 0), "id:AB-1")
        self.assertEqual(adapter_key({"label": "Network adapter 2"}, 1), "label:network adapter 2")
        self.assertEqual(adapter_key({}, 3), "index:3")


class HardwareTests(unittest.TestCase):
    KEYS = ("num_cpu", "memory_mb", "dynamic_memory", "memory_minimum_mb", "memory_maximum_mb", "memory_buffer")

    def test_cpu_and_memory_changes_are_found(self) -> None:
        changes = hardware_changes(self.KEYS, {"num_cpu": 2, "memory_mb": 4096}, {"num_cpu": 4, "memory_mb": 4096})

        self.assertEqual(changes, {"num_cpu": (2, 4)})

    def test_dynamic_memory_limits_count_only_when_the_source_uses_dynamic_memory(self) -> None:
        current = {"num_cpu": 2, "memory_mb": 2048, "dynamic_memory": False, "memory_minimum_mb": 2048, "memory_maximum_mb": 2048}

        self.assertEqual(hardware_changes(self.KEYS, current, {**current, "memory_minimum_mb": 512}), {})
        self.assertEqual(
            hardware_changes(self.KEYS, current, {**current, "dynamic_memory": True, "memory_minimum_mb": 512}),
            {"dynamic_memory": (False, True), "memory_minimum_mb": (2048, 512)},
        )

    def test_a_setting_the_restore_point_does_not_record_is_left_alone(self) -> None:
        self.assertEqual(hardware_changes(self.KEYS, {"num_cpu": 2}, {"memory_mb": None}), {})


class SettleTests(_UpdateCase):
    def setUp(self) -> None:
        super().setUp()
        self.platform.replica_config = {"num_cpu": 2, "memory_mb": 4096, "network_adapters": [_replica("4000", "Network adapter 1", "dr-lan")]}
        self.platform.available_networks = frozenset({"dr-lan", "dr-dmz"})
        self.point_config = {"num_cpu": 2, "memory_mb": 4096, "network_adapters": [_source("Network adapter 1", "lan")]}
        JOBS[JOB_ID]["replication"]["network_map"] = ["lan=dr-lan", "dmz=dr-dmz"]
        JOBS[JOB_ID]["replica"]["adapters"] = _pairs(("label:network adapter 1", "4000"))

    def _warnings(self) -> list[str]:
        return [str(update["message"]) for update in self.updates if update.get("log_level") == "warn"]

    def _infos(self) -> list[str]:
        return [str(update.get("message")) for update in self.updates if update.get("stage") == "configure" and update.get("log_level") != "warn"]

    def _again(self, session: str = "session-2") -> dict[str, Any]:
        TASKS[SESSION_ID]["status"] = "completed"
        self.updates.clear()
        TASKS[session] = {"id": session, "job_id": JOB_ID, "task_type": "replication", "status": "running", "created_at": "2026-11-10T00:00:00"}
        return run_replication({"id": session, "job_id": JOB_ID}, progress_callback=self.updates.append)

    def test_cpu_and_memory_follow_the_new_point_and_the_next_run_trusts_the_replica(self) -> None:
        self.point_config.update(num_cpu=4, memory_mb=8192)

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(self.platform.config_calls()[0][:2], ("apply", {"num_cpu": 4, "memory_mb": 8192}))
        self.assertIn("4 vCPU (was 2), 8192 MB memory (was 4096)", " ".join(self._infos()))
        self.assertEqual(self._replica()["fingerprint"], self.platform.state["fingerprint"])
        self.assertEqual(self._warnings(), [])

        again = self._again()

        self.assertEqual(again["mode"], "up_to_date")
        self.assertEqual(len(self.platform.config_calls()), 1)

    def test_an_up_to_date_replica_with_nothing_to_change_is_not_touched(self) -> None:
        JOBS[JOB_ID]["replica"]["point_id"] = "rp-3"

        result = self._run()

        self.assertEqual(result["mode"], "up_to_date")
        self.assertFalse(result["replica_touched"])
        self.assertEqual(self.platform.config_calls(), [])
        self.assertEqual(self._replica()["fingerprint"], "cv-1")

    def test_a_change_on_an_up_to_date_replica_marks_the_session_touched(self) -> None:
        JOBS[JOB_ID]["replica"]["point_id"] = "rp-3"
        self.point_config["num_cpu"] = 8

        result = self._run()

        self.assertEqual(result["mode"], "up_to_date")
        self.assertTrue(result["replica_touched"])
        self.assertIn({"replica_touched": True, "log_entry": False}, self.updates)

    def test_an_added_adapter_is_recorded_so_the_next_run_leaves_it_alone(self) -> None:
        self.point_config["network_adapters"].append(_source("Network adapter 2", "dmz"))

        self._run()

        added = self.platform.config_calls()[0][4]
        self.assertEqual([(item["label"], item["network"], item["connected"]) for item in added], [("Network adapter 2", "dr-dmz", True)])
        self.assertEqual(
            self._replica()["adapters"], _pairs(("label:network adapter 1", "4000"), ("label:network adapter 2", "nic-1-1"))
        )

        self._again()

        self.assertEqual(len(self.platform.config_calls()), 1)

    def test_an_unmapped_adapter_is_warned_about_once_and_removed_when_the_source_drops_it(self) -> None:
        self.point_config["network_adapters"].append(_source("Network adapter 2", "backup-net"))

        self._run()

        self.assertEqual(len(self._warnings()), 1)
        self.assertIn("backup-net, which this job maps to no network", self._warnings()[0])
        self.assertEqual(self.platform.config_calls()[0][4][0]["connected"], False)

        self._again()

        self.assertEqual(self._warnings(), [])

        self.point_config["network_adapters"].pop()
        self._again("session-3")

        self.assertEqual(self.platform.config_calls()[-1][2], ["nic-1-1"])
        self.assertEqual(self._replica()["config_warnings"], [])

    def test_settings_replication_does_not_change_are_warned_about_once(self) -> None:
        self.platform.unsynced_items = ("Secure Boot is off (the source VM: on)",)

        self._run()
        first = self._warnings()
        self._again()

        self.assertEqual(len(first), 1)
        self.assertIn("Replication does not change this on sql-01-replica", first[0])
        self.assertIn("Secure Boot is off (the source VM: on)", first[0])
        self.assertEqual(self._warnings(), [])

    def test_a_setting_that_stays_different_is_not_warned_about_again_at_the_next_restore_point(self) -> None:
        self.platform.unsynced_items = ("it boots first from Drive SCSI0:0 (the source VM: Network Network Adapter)",)
        self._run()
        first = self._warnings()
        BACKUPS["rp-4"] = dict(BACKUPS["rp-3"], id="rp-4", created_at="2026-11-04T01:00:00+00:00")
        for layers in self.layers.values():
            layers.append(_layer("rp-4"))

        again = self._again()

        self.assertEqual(again["restore_point_id"], "rp-4")
        self.assertEqual(len(first), 1)
        self.assertEqual(self._warnings(), [])
        self.assertEqual(self._replica()["config_warnings"], list(self.platform.unsynced_items))

    def test_a_configuration_that_cannot_be_applied_is_a_warning_and_is_tried_again(self) -> None:
        self.point_config["num_cpu"] = 64
        self.platform.apply_error = RuntimeError("The host does not have 64 CPUs")

        result = self._run()

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(self._replica()["point_id"], "rp-3")
        self.assertTrue(any("could not be brought in line with it (The host does not have 64 CPUs)" in item for item in self._warnings()))

        self.platform.apply_error = None
        self._again()

        self.assertEqual(self.platform.replica_config["num_cpu"], 64)

    def test_a_pending_name_renames_the_replica_on_the_next_run(self) -> None:
        JOBS[JOB_ID]["replication"]["replica_name"] = "sql-01-dr"
        JOBS[JOB_ID]["replica"]["pending_name"] = "sql-01-dr"

        result = self._run()

        self.assertIn(("rename", "sql-01-replica", "sql-01-dr"), self.platform.config_calls())
        self.assertEqual(result["replica_vm_name"], "sql-01-dr")
        self.assertEqual(self._replica()["vm_name"], "sql-01-dr")
        self.assertNotIn("pending_name", self._replica())
        self.assertIn("Renaming replica sql-01-replica to sql-01-dr", self._infos())

    def test_a_rename_that_fails_keeps_the_pending_name_for_the_next_run(self) -> None:
        JOBS[JOB_ID]["replica"]["pending_name"] = "sql-01-dr"
        self.platform.rename_error = RuntimeError("DuplicateName")

        self._run()

        self.assertEqual(self._replica()["pending_name"], "sql-01-dr")
        self.assertEqual(self._replica()["vm_name"], "sql-01-replica")
        self.assertTrue(any("could not be renamed to sql-01-dr (DuplicateName); the next run tries again" in item for item in self._warnings()))

    def test_a_name_another_vm_took_meanwhile_is_not_used(self) -> None:
        JOBS[JOB_ID]["replica"]["pending_name"] = "sql-01-dr"

        with patch("python.srv.services.abr_v41._inventory_vm_name_exists", lambda _inventory, name: name == "sql-01-dr"):
            self._run()

        self.assertFalse(any(call[0] == "rename" for call in self.platform.config_calls()))
        self.assertEqual(self._replica()["pending_name"], "sql-01-dr")
        self.assertTrue(any("keeps its name: a VM named sql-01-dr is already on vc-dr" in item for item in self._warnings()))

    def test_a_rename_asked_for_while_the_disks_are_written_is_done_by_the_same_run(self) -> None:
        original = self.platform.write_increments

        def write(*args, **kwargs):
            JOBS[JOB_ID] = dict(JOBS[JOB_ID], replica=dict(JOBS[JOB_ID]["replica"], pending_name="sql-01-dr"))
            return original(*args, **kwargs)

        self.platform.write_increments = write

        self._run()

        self.assertIn(("rename", "sql-01-replica", "sql-01-dr"), self.platform.config_calls())
        self.assertNotIn("pending_name", self._replica())
        self.assertEqual(self._replica()["point_id"], "rp-3")

    def test_a_rename_asked_for_after_the_run_read_the_job_is_kept_for_the_next_run(self) -> None:
        self.point_config["num_cpu"] = 4

        def asked(_state):
            JOBS[JOB_ID] = dict(JOBS[JOB_ID], replica=dict(JOBS[JOB_ID]["replica"], pending_name="sql-01-dr"))

        self.platform._config_changed = asked

        self._run()

        self.assertFalse(any(call[0] == "rename" for call in self.platform.config_calls()))
        self.assertEqual(self._replica()["pending_name"], "sql-01-dr")
        self.assertEqual(self._replica()["point_id"], "rp-3")

    def test_a_pending_name_that_is_already_the_replica_name_is_cleared_without_a_rename(self) -> None:
        JOBS[JOB_ID]["replica"]["pending_name"] = "sql-01-replica"

        self._run()

        self.assertFalse(any(call[0] == "rename" for call in self.platform.config_calls()))
        self.assertNotIn("pending_name", self._replica())

    def test_a_replica_recorded_without_adapters_is_paired_by_position(self) -> None:
        del JOBS[JOB_ID]["replica"]["adapters"]

        self._run()

        self.assertEqual(self.platform.config_calls(), [])
        self.assertEqual(self._replica()["adapters"], _pairs(("label:network adapter 1", "4000")))

    def test_a_dropped_connection_while_reading_the_configuration_is_retried(self) -> None:
        reads = {"n": 0}
        original = self.platform.read_config

        def read(node, state):
            reads["n"] += 1
            if reads["n"] == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            return original(node, state)

        self.platform.read_config = read
        self.point_config["num_cpu"] = 4

        with patch("python.srv.services.internal.replication.replica_update.retry_settings", lambda: (1, 0)), patch(
            "python.srv.services.internal.replication.replica_update._wait_before_retry", lambda *_args: None
        ):
            result = self._run()

        self.assertEqual((result["attempts"], result["mode"]), (2, "up_to_date"))
        self.assertEqual(self.platform.replica_config["num_cpu"], 4)
        self.assertEqual(self._replica()["point_id"], "rp-3")


class KeepRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        self.addCleanup(JOBS.clear)

    def test_an_edit_keeps_what_a_run_saved_meanwhile_and_adds_its_rename(self) -> None:
        JOBS["j"] = {"id": "j", "replica": {"vm_id": "vm-r", "point_id": "rp-9", "disks": [{"path": "new"}]}, "run_pending": True, "pending_trigger": {"trigger": "after_source"}}
        edited = {"id": "j", "replica": {"vm_id": "vm-r", "point_id": "rp-1", "disks": [{"path": "old"}], "pending_name": "dr"}, "run_pending": False}

        replication_settings.keep_run_state(edited)

        self.assertEqual(edited["replica"], {"vm_id": "vm-r", "point_id": "rp-9", "disks": [{"path": "new"}], "pending_name": "dr"})
        self.assertEqual((edited["run_pending"], edited["pending_trigger"]), (True, {"trigger": "after_source"}))

    def test_an_edit_without_a_rename_keeps_a_pending_name_already_stored(self) -> None:
        JOBS["j"] = {"id": "j", "replica": {"vm_id": "vm-r", "pending_name": "dr"}}
        edited = copy.deepcopy(JOBS["j"])
        edited["replica"].pop("pending_name")

        replication_settings.keep_run_state(edited)

        self.assertEqual(edited["replica"]["pending_name"], "dr")


if __name__ == "__main__":
    unittest.main()
