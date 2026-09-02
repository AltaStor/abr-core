# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.srv import backup_server
from python.srv.backup_server import TlsRuntime, app
from python.srv.core.tls_bootstrap import ensure_self_signed_cert


class CertRenewalLifespanWiringTests(unittest.TestCase):
    """Integration tests for the wiring between backup_server.lifespan() and
    tls_renewal's background task -- not just the task's own logic (covered
    in test_tls_renewal.py), but that the running FastAPI app actually starts
    and stops it, and that a real renewal happens while the app is serving."""

    def tearDown(self) -> None:
        backup_server.TLS_RUNTIME = None
        backup_server.CERT_RENEWAL_TASK = None

    def test_no_renewal_task_when_tls_runtime_unset(self) -> None:
        # Matches every other test in this suite (and the bare `uvicorn` CLI
        # dev path): TLS_RUNTIME stays None, so no renewal task is scheduled.
        backup_server.TLS_RUNTIME = None
        with TestClient(app):
            self.assertIsNone(backup_server.CERT_RENEWAL_TASK)

    def test_renewal_task_starts_and_stops_with_the_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))

            backup_server.TLS_RUNTIME = TlsRuntime(
                cert_path=cert_path, key_path=key_path, ssl_context=ssl_context
            )

            with TestClient(app):
                self.assertIsNotNone(backup_server.CERT_RENEWAL_TASK)
                self.assertFalse(backup_server.CERT_RENEWAL_TASK.done())

            self.assertIsNone(backup_server.CERT_RENEWAL_TASK)

    def test_a_real_renewal_cycle_hot_swaps_the_live_context_while_the_app_runs(
        self,
    ) -> None:
        # The scenario asked about directly: a certificate renewed while the
        # process is up and serving requests, picked up without a restart.
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))
            original_cert_bytes = cert_path.read_bytes()

            backup_server.TLS_RUNTIME = TlsRuntime(
                cert_path=cert_path, key_path=key_path, ssl_context=ssl_context
            )

            with (
                patch(
                    "python.srv.core.tls_renewal.renewal_check_interval_seconds",
                    return_value=0,
                ),
                patch("python.srv.core.tls_bootstrap.env_int", return_value=100_000),
            ):
                with TestClient(app) as client:
                    response = client.get("/health")
                    self.assertEqual(response.status_code, 200)

                    deadline = time.monotonic() + 5
                    while cert_path.read_bytes() == original_cert_bytes:
                        if time.monotonic() > deadline:
                            self.fail(
                                "Certificate was not renewed by the background "
                                "task while the app was running."
                            )
                        time.sleep(0.05)

                    # The app keeps serving requests across the hot-swap.
                    response = client.get("/health")
                    self.assertEqual(response.status_code, 200)

            self.assertNotEqual(cert_path.read_bytes(), original_cert_bytes)


if __name__ == "__main__":
    unittest.main()
