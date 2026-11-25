# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pyVmomi import vim, vmodl

from python.srv.plugins.vcenter import vcenter_replica

MARKER = "AltaStor replica of job job-1"


class _Vm:
    def __init__(self, moref: str, annotation: str = "", fault: Exception | None = None) -> None:
        self._moId = moref
        self._annotation = annotation
        self._fault = fault

    @property
    def config(self):
        if self._fault is not None:
            raise self._fault
        return SimpleNamespace(annotation=self._annotation)


class _Node:
    host = "vc01.example.test"

    def __init__(self, vms: list[_Vm]) -> None:
        self.vms = vms
        self.reauthenticated = 0

    def _list_vms(self) -> list[_Vm]:
        return list(self.vms)

    def _get_vm_by_moref(self, moref: str):
        return next((vm for vm in self.vms if vm._moId == moref), None)

    def reauthenticate(self) -> None:
        self.reauthenticated += 1


def _gone(moref: str) -> vmodl.fault.ManagedObjectNotFound:
    return vmodl.fault.ManagedObjectNotFound(
        msg=f"The object 'vim.VirtualMachine:{moref}' has already been deleted or has not been completely created"
    )


def _state(vm: _Vm) -> dict:
    return {"vm_id": vm._moId, "name": f"name-{vm._moId}"}


class ReplicaWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        state = patch.object(vcenter_replica, "_state", side_effect=_state)
        state.start()
        self.addCleanup(state.stop)

    def test_a_vm_deleted_while_the_walk_reads_it_is_skipped_and_the_walk_goes_on(self) -> None:
        node = _Node([_Vm("vm-1"), _Vm("vm-15000", fault=_gone("vm-15000")), _Vm("vm-7", MARKER)])

        with self.assertLogs(vcenter_replica.logger, "INFO") as logs:
            found = vcenter_replica.replicas_with_marker(node, MARKER)

        self.assertEqual([state["vm_id"] for state in found], ["vm-7"])
        self.assertIn("Skipping VM vm-15000: it was deleted from vCenter vc01.example.test", "\n".join(logs.output))

    def test_a_marked_vm_deleted_while_its_state_is_read_is_skipped_too(self) -> None:
        node = _Node([_Vm("vm-7", MARKER), _Vm("vm-8", MARKER)])

        def state(vm):
            if vm._moId == "vm-7":
                raise _gone("vm-7")
            return _state(vm)

        with patch.object(vcenter_replica, "_state", side_effect=state), self.assertLogs(vcenter_replica.logger, "INFO"):
            found = vcenter_replica.replicas_with_marker(node, MARKER)

        self.assertEqual([state["vm_id"] for state in found], ["vm-8"])

    def test_another_fault_says_what_was_being_done_instead_of_the_raw_object(self) -> None:
        node = _Node([_Vm("vm-1", fault=vim.fault.NoPermission(msg="Permission to perform this operation was denied."))])

        with self.assertRaises(vcenter_replica.VCenterReplicaError) as caught:
            vcenter_replica.replicas_with_marker(node, MARKER)

        message = str(caught.exception)
        self.assertEqual(message, "Looking up the replicas of the job on vc01.example.test failed: Permission to perform this operation was denied.")
        self.assertNotIn("dynamicType", message)

    def test_an_expired_session_still_signs_in_again_and_retries(self) -> None:
        node = _Node([_Vm("vm-7", MARKER)])
        calls = []
        original = node._list_vms

        def listing():
            calls.append(1)
            if len(calls) == 1:
                raise vim.fault.NotAuthenticated()
            return original()

        node._list_vms = listing

        with self.assertLogs(vcenter_replica.logger, "WARNING"):
            found = vcenter_replica.replicas_with_marker(node, MARKER)

        self.assertEqual((node.reauthenticated, [state["vm_id"] for state in found]), (1, ["vm-7"]))


class FindReplicaTests(unittest.TestCase):
    def setUp(self) -> None:
        state = patch.object(vcenter_replica, "_state", side_effect=_state)
        state.start()
        self.addCleanup(state.stop)

    def test_the_replica_is_found_as_before(self) -> None:
        self.assertEqual(vcenter_replica.find_replica(_Node([_Vm("vm-7", MARKER)]), "vm-7")["vm_id"], "vm-7")

    def test_a_replica_deleted_between_listing_and_reading_is_not_found(self) -> None:
        self.assertIsNone(vcenter_replica.find_replica(_Node([_Vm("vm-7", fault=_gone("vm-7"))]), "vm-7"))

    def test_another_fault_names_the_replica_lookup(self) -> None:
        node = _Node([_Vm("vm-7", fault=vim.fault.NoPermission(msg="denied"))])

        with self.assertRaises(vcenter_replica.VCenterReplicaError) as caught:
            vcenter_replica.find_replica(node, "vm-7")

        self.assertEqual(str(caught.exception), "Looking up the replica on vc01.example.test failed: denied")


if __name__ == "__main__":
    unittest.main()
