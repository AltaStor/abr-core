# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv import backup_server
from python.srv.backup_server import app, security_headers_for

CONSOLE_AND_API_PATHS = ("/", "/health", "/api/v1/sessions", "/api/v1/no-such-route")
ALWAYS_SENT = (
    "X-Frame-Options",
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
)


class SecurityHeaderResolutionTests(unittest.TestCase):
    def test_https_request_gets_the_whole_set(self) -> None:
        headers = security_headers_for("https")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("max-age=", headers["Strict-Transport-Security"])

    def test_hsts_is_withheld_from_plain_http(self) -> None:
        """RFC 6797 makes a browser ignore HSTS on a non-secure response, so
        sending it there would only tell a scanner something untrue."""
        headers = security_headers_for("http")
        self.assertNotIn("Strict-Transport-Security", headers)
        for header in ALWAYS_SENT:
            self.assertIn(header, headers)

    def test_an_empty_value_drops_only_that_header(self) -> None:
        with patch.dict(os.environ, {"ABR_SECURITY_HEADER_FRAME_OPTIONS": ""}):
            headers = security_headers_for("https")
        self.assertNotIn("X-Frame-Options", headers)
        self.assertIn("Content-Security-Policy", headers)

    def test_values_are_read_per_request_not_frozen_at_import(self) -> None:
        with patch.dict(os.environ, {"ABR_SECURITY_HEADER_REFERRER_POLICY": "same-origin"}):
            self.assertEqual(security_headers_for("https")["Referrer-Policy"], "same-origin")
        self.assertEqual(security_headers_for("https")["Referrer-Policy"], "no-referrer")

    def test_the_frame_rules_are_stated_in_both_forms(self) -> None:
        headers = security_headers_for("https")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_the_default_policy_does_not_restrict_resource_loading(self) -> None:
        policy = security_headers_for("https")["Content-Security-Policy"]
        for directive in ("default-src", "script-src", "style-src", "font-src", "connect-src", "img-src"):
            self.assertNotIn(directive, policy)


class SecurityHeadersOnResponsesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.https = TestClient(app, base_url="https://testserver")

    def test_every_console_and_api_surface_carries_the_headers(self) -> None:
        for path in CONSOLE_AND_API_PATHS:
            with self.subTest(path=path):
                response = self.https.get(path)
                for header in (*ALWAYS_SENT, "Strict-Transport-Security"):
                    self.assertIn(header, response.headers)

    def test_static_assets_are_covered_too(self) -> None:
        web_root = backup_server.resolve_web_root()
        if web_root is None:
            self.skipTest("web UI is not built in this checkout")
        assets = sorted((web_root / "assets").glob("*.js"))
        if not assets:
            self.skipTest("web UI build has no JS assets")
        response = self.https.get(f"/assets/{assets[0].name}")
        self.assertEqual(response.status_code, 200)
        for header in ALWAYS_SENT:
            self.assertIn(header, response.headers)

    def test_a_browser_navigation_to_a_client_route_is_covered(self) -> None:
        response = self.https.get("/jobs/some-id", headers={"Accept": "text/html"})
        self.assertIn("X-Frame-Options", response.headers)

    def test_existing_headers_are_not_clobbered(self) -> None:
        response = self.https.get("/health")
        self.assertEqual(response.headers.get("x-abr-api-version"), "1.0")

    def test_plain_http_responses_omit_only_hsts(self) -> None:
        response = TestClient(app, base_url="http://testserver").get("/health")
        self.assertNotIn("Strict-Transport-Security", response.headers)
        for header in ALWAYS_SENT:
            self.assertIn(header, response.headers)


if __name__ == "__main__":
    unittest.main()
