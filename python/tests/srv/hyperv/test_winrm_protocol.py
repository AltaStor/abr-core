# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import unittest
from typing import Self
from unittest.mock import MagicMock, patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV
from python.srv.plugins import build_hypervisor_plugin
from python.srv.plugins.hyperv import hyperv, hyperv_backup
from python.srv.plugins.hyperv.hyperv import (
    HyperVCertificateError,
    HyperVNode,
    HyperVUnreachableError,
    node_options,
    record_detected_protocol,
    with_protocol,
)
from python.srv.services import abr_v41, inventory_management
from python.srv.services.api_errors import ApiError
from python.srv.services.inventory_management import apply_inventory_updates

HOST = "192.0.2.90"
SELF_SIGNED = "HTTPSConnectionPool: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
PLAIN_HTTP = "HTTPSConnectionPool: [SSL: WRONG_VERSION_NUMBER] wrong version number"


class _Client:
    def __init__(self, error: str = "") -> None:
        self.error = error

    def execute_ps(self, script: str):
        if self.error:
            raise RuntimeError(self.error)
        return "HV", None, False


class _Host:
    def __init__(self, open_ports: set[int], errors: dict[int, str] | None = None) -> None:
        self.open_ports = open_ports
        self.errors = errors or {}
        self.built: list[tuple[str, int, str]] = []

    def __enter__(self) -> Self:
        self._patches = [
            patch.object(hyperv, "_port_is_open", side_effect=lambda host, port, timeout=None: port in self.open_ports),
            patch.object(HyperVNode, "_build_client", side_effect=self._build),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc) -> None:
        for item in reversed(self._patches):
            item.stop()

    def _build(self, host: str, port: int, protocol: str) -> _Client:
        self.built.append((host, port, protocol))
        return _Client(self.errors.get(port, ""))


def _node(**kwargs) -> HyperVNode:
    return HyperVNode(HOST, "CORP\\backup", "secret", **kwargs)


class ConfiguredProtocolIsStrictTests(unittest.TestCase):
    def test_https_tries_only_its_port_and_never_falls_back_to_http(self) -> None:
        with (
            _Host({hyperv.HTTP_PORT}) as host,
            self.assertLogs(hyperv.logger, level="WARNING"),
            self.assertRaises(HyperVUnreachableError) as caught,
        ):
            _node(protocol="https").connect()

        self.assertEqual(host.built, [])
        self.assertIn("over HTTPS on port 5986", str(caught.exception))
        self.assertIn("HTTPS listener", str(caught.exception))

    def test_https_on_a_custom_port_stays_https(self) -> None:
        with _Host({8443}) as host:
            _node(protocol="https", port=8443).connect()

        self.assertEqual(host.built, [(HOST, 8443, "https")])

    def test_http_tries_only_its_port(self) -> None:
        with _Host({hyperv.HTTP_PORT, hyperv.HTTPS_PORT}) as host:
            _node(protocol="http").connect()

        self.assertEqual(host.built, [(HOST, hyperv.HTTP_PORT, "http")])

    def test_an_untrusted_certificate_is_named_and_http_is_not_tried(self) -> None:
        with (
            _Host({hyperv.HTTP_PORT, hyperv.HTTPS_PORT}, {hyperv.HTTPS_PORT: SELF_SIGNED}) as host,
            self.assertLogs(hyperv.logger, level="WARNING"),
            self.assertRaises(HyperVCertificateError) as caught,
        ):
            _node(protocol="https", verify_ssl=True).connect()

        self.assertEqual(host.built, [(HOST, hyperv.HTTPS_PORT, "https")])
        self.assertIn("was not trusted (self-signed certificate).", str(caught.exception))
        self.assertNotIn("HTTPSConnectionPool", str(caught.exception))
        self.assertIn("--insecure", str(caught.exception))

    def test_https_against_an_http_listener_says_so(self) -> None:
        with (
            _Host({hyperv.HTTP_PORT}, {hyperv.HTTP_PORT: PLAIN_HTTP}) as host,
            self.assertLogs(hyperv.logger, level="WARNING"),
            self.assertRaises(HyperVUnreachableError) as caught,
        ):
            _node(protocol="https", port=hyperv.HTTP_PORT).connect()

        self.assertEqual(host.built, [(HOST, hyperv.HTTP_PORT, "https")])
        self.assertIn("answers without TLS", str(caught.exception))

    def test_a_configured_protocol_is_never_reported_as_detected(self) -> None:
        detected = MagicMock()
        with _Host({hyperv.HTTPS_PORT}):
            _node(protocol="https", on_protocol_detected=detected).connect()

        detected.assert_not_called()

    def test_an_unknown_protocol_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "https, http"):
            _node(protocol="ftp")


class LegacyInventoryDetectsOnceTests(unittest.TestCase):
    def test_without_a_port_https_is_tried_first_then_http_and_the_result_is_reported(self) -> None:
        detected = MagicMock()
        with _Host({hyperv.HTTP_PORT, hyperv.HTTPS_PORT}, {hyperv.HTTPS_PORT: SELF_SIGNED}) as host:
            node = _node(verify_ssl=True, on_protocol_detected=detected)
            with self.assertLogs(hyperv.logger, level="WARNING"):
                node.connect()
            node._open("node-b.example.test")

        detected.assert_called_once_with("http")
        self.assertEqual(node.protocol, "http")
        self.assertEqual(
            host.built,
            [
                (HOST, hyperv.HTTPS_PORT, "https"),
                (HOST, hyperv.HTTP_PORT, "http"),
                ("node-b.example.test", hyperv.HTTP_PORT, "http"),
            ],
        )

    def test_a_host_that_answers_https_is_reported_as_https(self) -> None:
        detected = MagicMock()
        with _Host({hyperv.HTTPS_PORT}):
            _node(on_protocol_detected=detected).connect()

        detected.assert_called_once_with("https")

    def test_an_explicit_port_keeps_the_meaning_it_had(self) -> None:
        detected = MagicMock()
        with _Host({hyperv.HTTP_PORT, hyperv.HTTPS_PORT}) as host:
            _node(port=hyperv.HTTP_PORT, on_protocol_detected=detected).connect()

        self.assertEqual(host.built, [(HOST, hyperv.HTTP_PORT, "http")])
        detected.assert_called_once_with("http")


def _hyperv_record(**metadata) -> dict:
    return {
        "id": "inv-hv",
        "name": "HyperV-test",
        "type": "hyper-v",
        "kind": "hyper-v",
        "address": HOST,
        "endpoint": HOST,
        "username": "CORP\\backup",
        "status": "available",
        "metadata": {
            "host": HOST,
            "port": hyperv.HTTPS_PORT,
            "port_explicit": False,
            "verify_ssl": False,
            "credentials": {"username": "CORP\\backup", "password": "secret"},
            **metadata,
        },
    }


class _CatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        self.addCleanup(HYPERVISORS.clear)
        self.addCleanup(RESOURCES_BY_HV.clear)
        discovery = patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False)
        discovery.start()
        self.addCleanup(discovery.stop)

    def _store(self, record: dict) -> dict:
        HYPERVISORS[record["id"]] = copy.deepcopy(record)
        return HYPERVISORS[record["id"]]


class StoredProtocolTests(_CatalogTestCase):
    def test_a_legacy_record_without_a_port_leaves_the_choice_to_the_node(self) -> None:
        self.assertEqual(node_options(_hyperv_record()["metadata"])["port"], None)
        self.assertEqual(node_options(_hyperv_record()["metadata"])["protocol"], None)

    def test_a_record_with_a_protocol_always_uses_its_port(self) -> None:
        options = node_options(_hyperv_record(protocol="http", port=hyperv.HTTP_PORT)["metadata"])

        self.assertEqual((options["port"], options["protocol"]), (hyperv.HTTP_PORT, "http"))

    def test_a_standard_port_follows_the_protocol_and_a_custom_port_stays(self) -> None:
        self.assertEqual(with_protocol({"port": 5986, "port_explicit": True}, "http")["port"], 5985)
        self.assertEqual(with_protocol({"port": 8443, "port_explicit": True}, "http")["port"], 8443)
        self.assertEqual(with_protocol({"port": 5986, "port_explicit": False}, "http")["port"], 5985)

    def test_a_detected_protocol_is_saved_once_with_a_warning_for_http(self) -> None:
        self._store(_hyperv_record())

        with self.assertLogs(hyperv.logger, level="WARNING") as logs:
            record_detected_protocol("inv-hv", "http")
        record_detected_protocol("inv-hv", "https")

        metadata = HYPERVISORS["inv-hv"]["metadata"]
        self.assertEqual((metadata["protocol"], metadata["port"]), ("http", hyperv.HTTP_PORT))
        self.assertIn("only over HTTP", "\n".join(logs.output))

    def test_every_way_of_opening_a_legacy_inventory_saves_what_it_detects(self) -> None:
        self._store(_hyperv_record())
        nodes = {
            "plugin": build_hypervisor_plugin(HYPERVISORS["inv-hv"]),
            "backup": hyperv_backup.open_node(hyperv_backup.connection_settings(HYPERVISORS["inv-hv"])),
        }

        for name, node in nodes.items():
            with self.subTest(name):
                self.assertIsNone(node.protocol)
                self.assertIsNotNone(node._on_protocol_detected)
        nodes["backup"]._on_protocol_detected("https")

        self.assertEqual(HYPERVISORS["inv-hv"]["metadata"]["protocol"], "https")

    def test_backup_and_restore_open_a_configured_inventory_strictly(self) -> None:
        node = hyperv_backup.open_node(
            hyperv_backup.connection_settings(_hyperv_record(protocol="http", port=8080, port_explicit=True))
        )

        self.assertEqual((node.protocol, node.port), ("http", 8080))


HYPERV_PAYLOAD = {
    "name": "hv-new",
    "type": "hyper-v",
    "address": HOST,
    "username": "CORP\\backup",
    "password": "secret",
    "insecure": True,
}


class InventoryRegistrationTests(_CatalogTestCase):
    def setUp(self) -> None:
        super().setUp()
        identity = {"computer": "HV-NEW", "cluster": "", "version": "", "protocol": "https"}
        probe = patch.object(inventory_management.HyperVInventoryProvider, "test_connection", return_value=identity)
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def _create(self, **payload) -> dict:
        return abr_v41.create_inventory_record({**HYPERV_PAYLOAD, **payload})

    def test_a_new_hyperv_inventory_is_https_on_5986_by_default(self) -> None:
        created = self._create()

        metadata = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual((metadata["protocol"], metadata["port"]), ("https", hyperv.HTTPS_PORT))
        self.assertEqual(created["connection"]["protocol"], "https")

    def test_http_without_a_port_uses_5985(self) -> None:
        created = self._create(protocol="http")

        metadata = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual((metadata["protocol"], metadata["port"]), ("http", hyperv.HTTP_PORT))

    def test_https_with_a_custom_port_keeps_both(self) -> None:
        created = self._create(protocol="https", port=8443)

        metadata = HYPERVISORS[created["id"]]["metadata"]
        self.assertEqual((metadata["protocol"], metadata["port"]), ("https", 8443))

    def test_a_protocol_on_a_vsphere_inventory_or_an_unknown_one_is_refused(self) -> None:
        for payload in ({"type": "vsphere", "protocol": "http"}, {"protocol": "ftp"}):
            with self.subTest(payload), self.assertRaises(ApiError) as caught:
                self._create(**payload)
            self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "invalid_inventory"))
            self.probe.assert_not_called()
        self.assertEqual(list(HYPERVISORS), [])

    def test_an_untrusted_certificate_refuses_the_registration_with_its_reason(self) -> None:
        self.probe.side_effect = HyperVCertificateError(HOST, hyperv.HTTPS_PORT, "self-signed certificate")
        with self.assertRaises(ApiError) as caught:
            self._create(insecure=False)

        self.assertEqual(caught.exception.details.get("reason"), "certificate")
        self.assertIn("--insecure", caught.exception.message)
        self.assertEqual(list(HYPERVISORS), [])


class InventoryEditTests(_CatalogTestCase):
    def test_switching_the_protocol_moves_a_standard_port_and_keeps_a_custom_one(self) -> None:
        standard = apply_inventory_updates(_hyperv_record(protocol="https"), {"protocol": "http"})
        custom = apply_inventory_updates(
            _hyperv_record(protocol="https", port=8443, port_explicit=True), {"protocol": "http"}
        )

        self.assertEqual((standard["metadata"]["protocol"], standard["metadata"]["port"]), ("http", hyperv.HTTP_PORT))
        self.assertEqual((custom["metadata"]["protocol"], custom["metadata"]["port"]), ("http", 8443))

    def test_a_new_address_keeps_the_protocol_port(self) -> None:
        edited = apply_inventory_updates(_hyperv_record(protocol="http", port=hyperv.HTTP_PORT), {"address": "192.0.2.91"})

        self.assertEqual(edited["metadata"]["port"], hyperv.HTTP_PORT)

    def test_a_vsphere_inventory_has_no_protocol_to_edit(self) -> None:
        record = {**_hyperv_record(), "type": "vsphere", "kind": "vcenter"}

        with self.assertRaisesRegex(ValueError, "Only a Hyper-V inventory"):
            apply_inventory_updates(record, {"protocol": "http"})

    def test_an_edit_of_a_legacy_inventory_keeps_the_protocol_its_check_detected(self) -> None:
        self._store(_hyperv_record())
        identity = {"computer": "HV", "cluster": "", "version": "", "protocol": "http"}

        with (
            patch.object(inventory_management.HyperVInventoryProvider, "test_connection", return_value=identity),
            self.assertLogs(hyperv.logger, level="WARNING"),
        ):
            abr_v41.edit_inventory_record("inv-hv", {"username": "CORP\\other"})

        metadata = HYPERVISORS["inv-hv"]["metadata"]
        self.assertEqual((metadata["protocol"], metadata["port"]), ("http", hyperv.HTTP_PORT))

    def test_the_connection_test_of_a_legacy_inventory_saves_the_protocol(self) -> None:
        self._store(_hyperv_record())
        identity = {"computer": "HV", "cluster": "", "version": "", "protocol": "https"}

        with patch.object(inventory_management.HyperVInventoryProvider, "test_connection", return_value=identity):
            abr_v41.test_inventory("inv-hv")

        self.assertEqual(HYPERVISORS["inv-hv"]["metadata"]["protocol"], "https")

    def test_a_check_never_overrides_a_protocol_that_is_already_set(self) -> None:
        self._store(_hyperv_record(protocol="https"))
        identity = {"computer": "HV", "cluster": "", "version": "", "protocol": "http"}

        with patch.object(inventory_management.HyperVInventoryProvider, "test_connection", return_value=identity):
            abr_v41.test_inventory("inv-hv")

        self.assertEqual(HYPERVISORS["inv-hv"]["metadata"]["protocol"], "https")


if __name__ == "__main__":
    unittest.main()
