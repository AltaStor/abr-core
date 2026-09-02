# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.cli_context import CliContext
from python.cli.cli_output_renderer import CliOutputRenderer
from python.cli.commands.restore_commands import RestoreCommands
from python.cli.exit_codes import INVALID_STATE
from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.cli.services.restore_service import RestoreService


class _FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeApiClient:
    def get(self, path: str, params=None, timeout=None):  # noqa: ANN001
        return {"items": []}

    def post(self, path: str, json=None, params=None, timeout=None):  # noqa: ANN001
        raise AssertionError(f"Unexpected POST {path}")

    def patch(self, path: str, json=None, params=None, timeout=None):  # noqa: ANN001
        raise AssertionError(f"Unexpected PATCH {path}")


class RestoreLifecycleParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_restore_help_lists_subcommands(self) -> None:
        help_output = self._parse_help(["restore", "--help"])
        for command in ("list", "start", "point"):
            self.assertIn(command, help_output)

    def test_restore_subcommands_support_help(self) -> None:
        for argv in (
            ["restore", "--help"],
            ["restore", "list", "--help"],
            ["restore", "start", "--help"],
            ["restore", "point", "list", "--help"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self.parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 0)

    def test_restore_list_parser_accepts_optional_filters(self) -> None:
        args = self.parser.parse_args(
            ["restore", "list", "--vm", "Ubuntu-VM", "--backup", "backup-123", "--inventory", "blackdog"]
        )

        self.assertEqual(args.vm, "Ubuntu-VM")
        self.assertEqual(args.backup, "backup-123")
        self.assertEqual(args.inventory, "blackdog")

    def _parse_help(self, argv: list[str]) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(argv)
        return stream.getvalue()


class RestorePayloadBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        context = CliContext(base_url="http://example.test", output="json", api_client=_FakeApiClient(), output_renderer=CliOutputRenderer())
        self.service = RestoreService(context)

    def test_restore_start_requires_source_selector(self) -> None:
        args = SimpleNamespace(
            restore_point=None,
            point_id=None,
            backup=None,
            vm=None,
            new_name="vm-restored",
            name=None,
            target_inventory="lab",
            target_inventory_id=None,
            target_host="esx-01",
            datastore="datastore1",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        with self.assertRaisesRegex(ValueError, "--restore-point/--point-id or --backup"):
            self.service.build_start_payload(args)

    def test_restore_start_requires_target_inventory(self) -> None:
        args = SimpleNamespace(
            restore_point="rp-1",
            point_id=None,
            backup=None,
            vm=None,
            new_name="vm-restored",
            name=None,
            target_inventory=None,
            target_inventory_id=None,
            target_host="esx-01",
            datastore="datastore1",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        with self.assertRaisesRegex(ValueError, "--target-inventory"):
            self.service.build_start_payload(args)

    def test_restore_start_requires_target_host(self) -> None:
        args = SimpleNamespace(
            restore_point="rp-1",
            point_id=None,
            backup=None,
            vm=None,
            new_name="vm-restored",
            name=None,
            target_inventory="lab",
            target_inventory_id=None,
            target_host=None,
            datastore="datastore1",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        with self.assertRaisesRegex(ValueError, "--target-host"):
            self.service.build_start_payload(args)

    def test_restore_start_requires_datastore(self) -> None:
        args = SimpleNamespace(
            restore_point="rp-1",
            point_id=None,
            backup=None,
            vm=None,
            new_name="vm-restored",
            name=None,
            target_inventory="lab",
            target_inventory_id=None,
            target_host="esx-01",
            datastore=None,
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        with self.assertRaisesRegex(ValueError, "--datastore"):
            self.service.build_start_payload(args)

    def test_restore_start_requires_new_name_for_full_vm_restore(self) -> None:
        args = SimpleNamespace(
            restore_point="rp-1",
            point_id=None,
            backup=None,
            vm=None,
            new_name=None,
            name=None,
            target_inventory="lab",
            target_inventory_id=None,
            target_host="esx-01",
            datastore="datastore1",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        with self.assertRaisesRegex(ValueError, "--new-name"):
            self.service.build_start_payload(args)

    def test_restore_start_builds_payload_with_aliases(self) -> None:
        args = SimpleNamespace(
            restore_point=None,
            point_id="rp-123",
            backup=None,
            vm=None,
            new_name="Ubuntu-VM-restored",
            name=None,
            target_inventory="proxmox-main",
            target_inventory_id=None,
            target_host="pve-node-01",
            datastore=None,
            target_datastore="local-lvm",
            network_map=["VM Network=Recovery Network"],
            repo=None,
            repo_id=None,
            power_on=True,
            force=False,
        )

        payload = self.service.build_start_payload(args)

        self.assertEqual(
            payload,
            {
                "point_id": "rp-123",
                "name": "Ubuntu-VM-restored",
                "target_inventory": "proxmox-main",
                "target_host": "pve-node-01",
                "target_datastore": "local-lvm",
                "network_map": ["VM Network=Recovery Network"],
                "power_on": True,
                "force": False,
            },
        )


class RestoreStartInPlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        context = CliContext(base_url="http://example.test", output="json", api_client=_FakeApiClient(), output_renderer=CliOutputRenderer())
        self.service = RestoreService(context)

    @staticmethod
    def _args(**overrides) -> SimpleNamespace:
        base = dict(
            restore_point="rp-1",
            point_id=None,
            backup=None,
            vm=None,
            new_name=None,
            name=None,
            target_inventory=None,
            target_inventory_id=None,
            target_host=None,
            datastore=None,
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
            in_place=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_force_with_a_free_new_name_is_rejected_with_a_clear_message(self) -> None:
        args = self._args(
            target_inventory="lab", target_host="esx-01", datastore="ds1", new_name="brand-new-name", force=True
        )
        with self.assertRaisesRegex(ValueError, "--in-place restore"):
            self.service.build_start_payload(args)

    def test_force_with_a_taken_new_name_is_rejected_with_a_clear_message(self) -> None:
        args = self._args(
            target_inventory="lab", target_host="esx-01", datastore="ds1", new_name="existing-vm", force=True
        )
        with self.assertRaisesRegex(ValueError, "--in-place restore"):
            self.service.build_start_payload(args)

    def test_force_with_repo_and_no_target_is_rejected_with_a_clear_message(self) -> None:
        args = self._args(repo="copy-repo", force=True)
        with self.assertRaisesRegex(ValueError, "--in-place restore"):
            self.service.build_start_payload(args)

    def test_in_place_without_force_is_rejected(self) -> None:
        args = self._args(in_place=True, force=False)
        with self.assertRaisesRegex(ValueError, "--force"):
            self.service.build_start_payload(args)

    def test_in_place_cannot_be_combined_with_an_explicit_target(self) -> None:
        args = self._args(in_place=True, force=True, target_inventory="lab", target_host="esx-01", datastore="ds1", new_name="x")
        with self.assertRaisesRegex(ValueError, "--in-place restore cannot be combined"):
            self.service.build_start_payload(args)

    def test_in_place_builds_a_minimal_explicit_payload(self) -> None:
        args = self._args(in_place=True, force=True)
        payload = self.service.build_start_payload(args)
        self.assertEqual(payload, {"point_id": "rp-1", "restore_mode": "in_place", "power_on": False, "force": True})

    def test_in_place_works_alongside_repo(self) -> None:
        args = self._args(in_place=True, force=True, repo="copy-repo")
        payload = self.service.build_start_payload(args)
        self.assertEqual(payload, {"point_id": "rp-1", "restore_mode": "in_place", "power_on": False, "force": True, "repo": "copy-repo"})


class RestoreCommandTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_restore_list_text_output_contains_restore_point_fields(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "items": [
                    {
                        "id": "rp-1",
                        "backup_id": "backup-1",
                        "vm_id": "vm-1",
                        "vm_name": "Ubuntu-VM",
                        "inventory_id": "inv-1",
                        "inventory_name": "blackdog",
                        "repository_id": "repo-1",
                        "repository_name": "repo-local",
                        "created_at": "2026-05-26T20:30:00+00:00",
                        "point_type": "full",
                        "status": "available",
                    }
                ]
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", vm=None, backup=None, inventory=None)

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = RestoreCommands().list(args)

        self.assertEqual(exit_code, 0)
        output = stream.getvalue()
        self.assertIn("POINT ID", output)
        self.assertIn("rp-1", output)
        self.assertIn("Ubuntu-VM", output)
        self.assertIn("blackdog", output)
        self.assertIn("repo-local", output)
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/restore/points",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_restore_list_json_output_is_valid_json(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "items": [
                    {
                        "id": "rp-1",
                        "backup_id": "backup-1",
                        "vm_name": "Ubuntu-VM",
                        "inventory_name": "blackdog",
                        "repository_name": "repo-local",
                        "created_at": "2026-05-26T20:30:00+00:00",
                        "point_type": "full",
                        "status": "available",
                    }
                ]
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", vm="Ubuntu-VM", backup=None, inventory=None)

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = RestoreCommands().list(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["items"][0]["id"], "rp-1")
        self.assertEqual(payload["items"][0]["vm_name"], "Ubuntu-VM")
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/restore/points",
            json=None,
            params={"vm": "Ubuntu-VM"},
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_restore_start_posts_expected_request_and_prints_target_details(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "sess-1",
                "restore_session_id": "sess-1",
                "status": "started",
                "restore_point_id": "rp-123",
                "source_vm_name": "Ubuntu-VM",
                "target_inventory_name": "proxmox-main",
                "target_host_name": "pve-node-01",
                "datastore_name": "local-lvm",
                "new_name": "Ubuntu-VM-restored",
            }
        )
        args = SimpleNamespace(
            base_url="http://example.test",
            output="table",
            log_level="info",
            restore_point="rp-123",
            point_id=None,
            backup=None,
            vm=None,
            new_name="Ubuntu-VM-restored",
            name=None,
            target_inventory="proxmox-main",
            target_inventory_id=None,
            target_host="pve-node-01",
            datastore="local-lvm",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = RestoreCommands().start(args)

        self.assertEqual(exit_code, 0)
        output = stream.getvalue()
        self.assertIn("sess-1", output)
        self.assertIn("Ubuntu-VM-restored", output)
        self.assertIn("proxmox-main", output)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/restore/start",
            json={
                "point_id": "rp-123",
                "name": "Ubuntu-VM-restored",
                "target_inventory": "proxmox-main",
                "target_host": "pve-node-01",
                "target_datastore": "local-lvm",
                "power_on": False,
                "force": False,
            },
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_restore_start_json_output_matches_expected_schema(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "sess-1",
                "restore_session_id": "sess-1",
                "status": "started",
                "restore_point_id": "rp-123",
                "source_vm_id": "vm-1",
                "source_vm_name": "Ubuntu-VM",
                "target_inventory_id": "inv-1",
                "target_inventory_name": "proxmox-main",
                "target_host_id": "host-1",
                "target_host_name": "pve-node-01",
                "datastore_name": "local-lvm",
                "new_name": "Ubuntu-VM-restored",
            }
        )
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            log_level="info",
            restore_point="rp-123",
            point_id=None,
            backup=None,
            vm=None,
            new_name="Ubuntu-VM-restored",
            name=None,
            target_inventory="proxmox-main",
            target_inventory_id=None,
            target_host="pve-node-01",
            datastore="local-lvm",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = RestoreCommands().start(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(
            sorted(payload.keys()),
            sorted(
                [
                    "id",
                    "restore_session_id",
                    "status",
                    "restore_point_id",
                    "source_vm_id",
                    "source_vm_name",
                    "target_inventory_id",
                    "target_inventory_name",
                    "target_host_id",
                    "target_host_name",
                    "datastore_name",
                    "new_name",
                ]
            ),
        )


class RestoreStartWaitTests(unittest.TestCase):
    @staticmethod
    def _args(**overrides) -> SimpleNamespace:
        base = dict(
            base_url="http://example.test",
            output="json",
            log_level="info",
            restore_point="rp-123",
            point_id=None,
            backup=None,
            vm=None,
            new_name="Ubuntu-VM-restored",
            name=None,
            target_inventory="proxmox-main",
            target_inventory_id=None,
            target_host="pve-node-01",
            datastore="local-lvm",
            target_datastore=None,
            network_map=[],
            repo=None,
            repo_id=None,
            power_on=False,
            force=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    @patch("python.cli.api.client.requests.request")
    def test_start_without_wait_returns_the_start_response_unchanged(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"restore_session_id": "sess-1", "status": "started"})

        with patch("python.cli.commands.restore_commands.watch_session") as watch_mock:
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = RestoreCommands().start(self._args())

        self.assertEqual(exit_code, 0)
        watch_mock.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue())["status"], "started")

    @patch("python.cli.api.client.requests.request")
    def test_start_with_wait_follows_the_restore_session_id_to_completion(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"restore_session_id": "sess-1", "status": "started"})

        with patch("python.cli.commands.restore_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "completed", "progress": 100}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = RestoreCommands().start(self._args(wait=True))

        self.assertEqual(exit_code, 0)
        self.assertEqual(watch_mock.call_args.args[1], "sess-1")
        self.assertEqual(json.loads(stream.getvalue())["state"], "completed")

    @patch("python.cli.api.client.requests.request")
    def test_start_with_wait_returns_non_zero_when_the_restore_session_fails(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"restore_session_id": "sess-1", "status": "started"})

        with patch("python.cli.commands.restore_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "failed", "message": "disk write failed"}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = RestoreCommands().start(self._args(wait=True))

        self.assertEqual(json.loads(stream.getvalue())["state"], "failed")
        self.assertEqual(exit_code, INVALID_STATE)


if __name__ == "__main__":
    unittest.main()
