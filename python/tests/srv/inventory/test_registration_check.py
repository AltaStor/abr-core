# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV
from python.srv.services import abr_v41, inventory_management
from python.srv.services.api_errors import ApiError
from python.srv.services.inventory_management import (
    EsxHostInClusterError,
    InventoryUnreachableError,
    assert_inventory_registrable,
)

PAYLOAD = {
    "name": "probe-fake",
    "type": "vsphere",
    "address": "203.0.113.77",
    "username": "nobody@vsphere.local",
    "password": "definitely-not-real",
    "insecure": True,
}


def _record(host: str = "203.0.113.77") -> dict:
    return {
        "id": "inv-1",
        "name": "probe",
        "type": "vsphere",
        "kind": "vcenter",
        "metadata": {
            "host": host,
            "port": 443,
            "verify_ssl": False,
            "credentials": {"username": "u", "password": "p"},
        },
    }


class RegistrationRefusesAnEndpointItCannotUseTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

    def test_an_unreachable_address_is_refused_and_nothing_is_registered(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=False):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "inventory_unreachable")
        self.assertIn("not reachable", caught.exception.message)
        self.assertEqual(caught.exception.details.get("reason"), "unreachable")
        self.assertEqual(list(HYPERVISORS), [])

    def test_rejected_credentials_are_named_as_such(self) -> None:
        failure = RuntimeError("Unable to connect")
        failure.__cause__ = Exception("vim.fault.InvalidLogin: Cannot complete login")

        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider, "test_connection", side_effect=failure
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.details.get("reason"), "authentication")
        self.assertIn("rejected these credentials", caught.exception.message)
        self.assertEqual(list(HYPERVISORS), [])

    def test_a_refused_certificate_points_at_the_flag_when_verification_was_on(self) -> None:
        failure = RuntimeError("Unable to connect")
        failure.__cause__ = Exception("certificate verify failed: self signed certificate")
        payload = dict(PAYLOAD)
        payload.pop("insecure")

        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider, "test_connection", side_effect=failure
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(payload)

        self.assertEqual(caught.exception.details.get("reason"), "certificate")
        self.assertIn("--insecure", caught.exception.message)

    def test_a_tls_failure_with_insecure_already_set_does_not_suggest_it_again(self) -> None:
        failure = RuntimeError("Unable to connect")
        failure.__cause__ = Exception("ssl: wrong version number")

        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider, "test_connection", side_effect=failure
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.details.get("reason"), "certificate")
        self.assertNotIn("--insecure", caught.exception.message)
        self.assertIn("Check the address and port", caught.exception.message)

    def test_any_other_connection_failure_still_refuses_with_its_detail(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider,
            "test_connection",
            side_effect=RuntimeError("the server closed the connection"),
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.details.get("reason"), "connection")
        self.assertIn("closed the connection", caught.exception.message)
        self.assertEqual(list(HYPERVISORS), [])

    def test_a_reachable_endpoint_is_registered_as_before(self) -> None:
        with patch.object(abr_v41, "assert_inventory_registrable", return_value=None):
            created = abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(created["name"], "probe-fake")
        self.assertIn(created["id"], HYPERVISORS)

    def test_a_clustered_esx_host_is_still_refused_with_its_own_code(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider,
            "test_connection",
            side_effect=EsxHostInClusterError("cluster-1"),
        ):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.code, "esx_host_in_cluster")
        self.assertEqual(list(HYPERVISORS), [])


class _Endpoint:
    def __init__(self, service_type: str, cluster: str | None = None) -> None:
        self.service_type = service_type
        self.cluster = cluster

    def node(self, *args, **kwargs):
        endpoint = self

        class _Node:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_service_type(self):
                return endpoint.service_type

            def clustered_host_name(self):
                return endpoint.cluster

        return _Node()


class DirectEsxiRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        self.addCleanup(HYPERVISORS.clear)
        self.addCleanup(RESOURCES_BY_HV.clear)
        for patcher in (
            patch.object(inventory_management, "_endpoint_reachable", return_value=True),
            patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _connect_to(self, endpoint: _Endpoint):
        module = types.SimpleNamespace(VCenterNode=endpoint.node)
        return patch.dict(sys.modules, {"python.srv.plugins.vcenter.vcenter": module})

    def test_a_standalone_esxi_host_is_registered(self) -> None:
        with self._connect_to(_Endpoint("HostAgent")):
            created = abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertIn(created["id"], HYPERVISORS)

    def test_an_esxi_host_in_a_cluster_keeps_its_own_code(self) -> None:
        with self._connect_to(_Endpoint("HostAgent", cluster="cluster-1")):
            with self.assertRaises(ApiError) as caught:
                abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertEqual(caught.exception.code, "esx_host_in_cluster")
        self.assertEqual(list(HYPERVISORS), [])

    def test_a_vcenter_is_registered_as_before(self) -> None:
        with self._connect_to(_Endpoint("VirtualCenter")):
            created = abr_v41.create_inventory_record(dict(PAYLOAD))

        self.assertIn(created["id"], HYPERVISORS)

    def test_editing_an_esxi_inventory_registered_earlier_keeps_it_available(self) -> None:
        HYPERVISORS["inv-1"] = _record()
        with self._connect_to(_Endpoint("HostAgent")), self.assertNoLogs(abr_v41.LOGGER, level="WARNING"):
            edited = abr_v41.edit_inventory_record("inv-1", {"password": "new-secret"})

        self.assertEqual(edited["status"], "available")
        self.assertIsNone(edited["status_error"])

    def test_an_esxi_inventory_tests_as_connected(self) -> None:
        HYPERVISORS["inv-1"] = _record()
        with self._connect_to(_Endpoint("HostAgent")):
            result = abr_v41.test_inventory("inv-1")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["message"], "Connection successful")


class RegistrationCheckShapeTests(unittest.TestCase):
    def test_a_non_vsphere_inventory_is_not_probed(self) -> None:
        record = _record()
        record["type"] = "mock"
        record["kind"] = "mock"

        with patch.object(inventory_management, "_endpoint_reachable", return_value=True) as probe:
            assert_inventory_registrable(record)

        probe.assert_not_called()

    def test_the_probe_is_bounded_by_the_configured_timeout(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=False) as probe:
            with self.assertRaises(InventoryUnreachableError):
                assert_inventory_registrable(_record())

        self.assertEqual(
            probe.call_args.kwargs["timeout"],
            float(inventory_management.inventory_connect_timeout()),
        )

    def test_the_check_authenticates_rather_than_walking_the_inventory(self) -> None:
        with patch.object(inventory_management, "_endpoint_reachable", return_value=True), patch.object(
            inventory_management.VsphereInventoryProvider, "test_connection", return_value=None
        ) as connect, patch.object(
            inventory_management.VsphereInventoryProvider, "rescan"
        ) as rescan:
            assert_inventory_registrable(_record())

        connect.assert_called_once()
        rescan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
