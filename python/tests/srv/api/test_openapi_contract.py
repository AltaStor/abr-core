# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.srv.api.schemas import TLS_WARNING_CODES
from python.srv.backup_server import SERVER_API_MAJOR, SERVER_API_MINOR, app
from python.srv.core.mongo_db_wrapper import SESSIONS, USERS
from python.srv.core.tls_bootstrap import ensure_self_signed_cert
from python.srv.services import auth_service, tls_certificate_service
from python.srv.services.abr_v41 import PRODUCT_NAME, product_version

from python.tests.srv.tls.test_certificate_service import BOOTSTRAP_ENV, _key_pem, _Pki

TLS_PATH = "/api/v1/system/tls-certificate"


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    raise AssertionError(f"{value!r} is not JSON")


def _admits(schema: dict, value: object) -> bool:
    if "anyOf" in schema:
        return any(_admits(option, value) for option in schema["anyOf"])
    declared = schema.get("type")
    if declared is None:
        return True
    actual = _json_type(value)
    if declared == "number" and actual == "integer":
        return True
    if declared != actual:
        return False
    if declared == "string" and "enum" in schema and value not in schema["enum"]:
        return False
    if declared == "array" and "items" in schema:
        return all(_admits(schema["items"], item) for item in value)
    return True


class OpenApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        USERS.clear()
        SESSIONS.clear()
        auth_service.reset_login_lockout_state_for_testing()
        env = patch.dict("os.environ", BOOTSTRAP_ENV)
        env.start()
        self.addCleanup(env.stop)
        client_cm = TestClient(app)
        self.client = client_cm.__enter__()
        self.addCleanup(client_cm.__exit__, None, None, None)
        login = self.client.post("/api/v1/auth/login", json={"name": "admin", "password": "Sup3rSecretPW"})
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cert_path, self.key_path = Path(tmp.name) / "server.crt", Path(tmp.name) / "server.key"
        ensure_self_signed_cert(self.cert_path, self.key_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        tls_certificate_service.register_live_tls(self.cert_path, self.key_path, context)
        self.addCleanup(tls_certificate_service.clear_live_tls)
        self.addCleanup(USERS.clear)
        self.addCleanup(SESSIONS.clear)

        self.spec = self.client.get("/openapi.json").json()

    def _schema_of(self, method: str) -> dict:
        content = self.spec["paths"][TLS_PATH][method]["responses"]["200"]["content"]
        ref = content["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
        return self.spec["components"]["schemas"][ref]

    def _assert_body_matches(self, schema: dict, body: dict) -> None:
        properties = schema["properties"]
        for field, value in body.items():
            self.assertIn(field, properties, f"{field} is returned but not documented")
            self.assertTrue(
                _admits(properties[field], value),
                f"{field}={value!r} is not admitted by {properties[field]}",
            )
        for field in schema.get("required", []):
            self.assertIn(field, body, f"{field} is required but not returned")

    def test_a_generated_certificate_matches_its_documented_schema(self) -> None:
        response = self.client.get(TLS_PATH, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "generated")
        self.assertIsNone(body["installed_at"])
        self.assertIsNone(body["installed_by"])
        self._assert_body_matches(self._schema_of("get"), body)

    def test_nullable_fields_say_so_and_explain_when_they_are_null(self) -> None:
        properties = self._schema_of("get")["properties"]
        for field in ("installed_at", "installed_by", "common_name", "issuer_common_name"):
            with self.subTest(field=field):
                self.assertEqual(
                    properties[field]["anyOf"], [{"type": "string"}, {"type": "null"}]
                )
                self.assertIn("null", properties[field]["description"].lower())
        self.assertIn("generated", properties["installed_at"]["description"])
        self.assertIn("generated", properties["installed_by"]["description"])

    def test_an_installed_certificate_matches_its_documented_schema(self) -> None:
        pki = _Pki()
        response = self.client.put(
            TLS_PATH,
            headers=self.headers,
            json={"certificate": pki.chain_pem, "private_key": _key_pem(pki.leaf_key)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["installed_by"], "admin")
        self._assert_body_matches(self._schema_of("put"), body)

        reverted = self.client.delete(TLS_PATH, headers=self.headers)
        self.assertEqual(reverted.status_code, 200, reverted.text)
        self._assert_body_matches(self._schema_of("delete"), reverted.json())

        again = self.client.delete(TLS_PATH, headers=self.headers)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertFalse(again.json()["reloaded"])
        self._assert_body_matches(self._schema_of("delete"), again.json())

    def test_reloaded_is_documented_only_where_it_is_returned(self) -> None:
        self.assertNotIn("reloaded", self._schema_of("get")["properties"])
        self.assertNotIn("reloaded", self.client.get(TLS_PATH, headers=self.headers).json())
        for method in ("put", "delete"):
            with self.subTest(method=method):
                self.assertIn("reloaded", self._schema_of(method)["properties"])

    def test_every_documented_field_is_required_and_returned(self) -> None:
        schema = self._schema_of("get")
        self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
        self.assertEqual(
            sorted(self.client.get(TLS_PATH, headers=self.headers).json()),
            sorted(schema["required"]),
        )

    def test_warnings_document_the_codes_the_server_can_return(self) -> None:
        items = self._schema_of("get")["properties"]["warnings"]["items"]
        self.assertEqual(items["properties"]["code"]["enum"], TLS_WARNING_CODES)
        self.assertEqual(items["required"], ["code", "message"])

        appliance = patch.object(
            tls_certificate_service, "_appliance_names", return_value=["appliance.example.com"]
        )
        threshold = patch.dict("os.environ", {"ABR_TLS_RENEWAL_THRESHOLD_DAYS": "30"})
        with appliance, threshold:
            produced = [
                *tls_certificate_service._warnings([], [], 5),
                *tls_certificate_service._warnings(["other.example.com"], [], -1),
                *tls_certificate_service._warnings(["other.example.com"], [], 5),
                *tls_certificate_service._chain_warnings([_Pki().leaf]),
            ]

        self.assertEqual({warning["code"] for warning in produced}, set(TLS_WARNING_CODES))
        for warning in produced:
            with self.subTest(code=warning["code"]):
                self.assertTrue(_admits(items, warning), f"{warning!r} is not admitted by {items}")
                for field in warning:
                    self.assertIn(field, items["properties"])

    def _resolve(self, node: dict) -> dict:
        while "$ref" in node:
            node = self.spec["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
        return node

    def _mismatches(self, schema: dict, body: object, trail: str) -> list[str]:
        schema = self._resolve(schema)
        if isinstance(body, list):
            if schema.get("type") != "array":
                return []
            item = schema.get("items", {})
            return [m for i, v in enumerate(body) for m in self._mismatches(item, v, f"{trail}[{i}]")]
        if not isinstance(body, dict):
            return []
        found = []
        for field, value in body.items():
            declared = schema.get("properties", {}).get(field)
            if declared is None:
                continue
            declared = self._resolve(declared)
            if isinstance(value, (dict, list)) and (declared.get("properties") or declared.get("items")):
                found += self._mismatches(declared, value, f"{trail}.{field}")
            elif not _admits(declared, value):
                found.append(f"{trail}.{field}={value!r} is not admitted by {declared}")
        return found

    def test_no_response_field_is_documented_narrower_than_it_is_returned(self) -> None:
        checked = []
        for path, operations in sorted(self.spec["paths"].items()):
            operation = operations.get("get")
            if not operation or "{" in path or "support-bundle" in path:
                continue
            schema = (
                operation["responses"].get("200", {}).get("content", {}).get("application/json", {})
            ).get("schema")
            if not schema:
                continue
            response = self.client.get(path, headers=self.headers)
            if response.status_code != 200:
                continue
            checked.append(path)
            with self.subTest(path=path):
                self.assertEqual(self._mismatches(schema, response.json(), path), [])
        self.assertIn("/api/v1/system/logs", checked)
        self.assertIn(TLS_PATH, checked)

    def test_info_names_the_product_and_the_build(self) -> None:
        info = self.spec["info"]
        self.assertEqual(info["version"], product_version())
        self.assertEqual(info["title"], PRODUCT_NAME)
        self.assertIn(PRODUCT_NAME, info["description"])
        self.assertIn(product_version(), info["description"])
        self.assertIn(f"/api/v{SERVER_API_MAJOR}", info["description"])
        self.assertIn(f"v{SERVER_API_MAJOR}.{SERVER_API_MINOR}", info["description"])
        self.assertNotIn("Mock", info["description"])


if __name__ == "__main__":
    unittest.main()
