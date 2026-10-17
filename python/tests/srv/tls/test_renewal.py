# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography import x509

from python.srv.core import tls_renewal
from python.srv.core.tls_bootstrap import ensure_self_signed_cert


class CheckAndRenewTests(unittest.TestCase):
    def test_returns_false_when_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert", return_value=False
            ) as mock_ensure:
                ssl_context = MagicMock()
                result = tls_renewal.check_and_renew(cert_path, key_path, ssl_context)

            self.assertFalse(result)
            mock_ensure.assert_called_once_with(cert_path, key_path)
            ssl_context.load_cert_chain.assert_not_called()

    def test_reloads_live_ssl_context_when_renewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)
            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert", return_value=True
            ):
                ssl_context = MagicMock()
                result = tls_renewal.check_and_renew(cert_path, key_path, ssl_context)

            self.assertTrue(result)
            ssl_context.load_cert_chain.assert_called_once_with(str(cert_path), str(key_path))

    def test_renewed_but_no_live_context_logs_and_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert", return_value=True
            ):
                result = tls_renewal.check_and_renew(cert_path, key_path, None)

            self.assertTrue(result)

    def test_renewal_failure_is_caught_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert",
                side_effect=RuntimeError("disk full"),
            ):
                ssl_context = MagicMock()
                result = tls_renewal.check_and_renew(cert_path, key_path, ssl_context)

            self.assertFalse(result)
            ssl_context.load_cert_chain.assert_not_called()

    def test_ssl_reload_failure_is_caught_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert", return_value=True
            ):
                ssl_context = MagicMock()
                ssl_context.load_cert_chain.side_effect = OSError("bad permissions")
                result = tls_renewal.check_and_renew(cert_path, key_path, ssl_context)

            self.assertFalse(result)

    def test_real_expiry_driven_renewal_reloads_a_real_ssl_context(self) -> None:
        import ssl

        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))

            original_cert_bytes = cert_path.read_bytes()

            with patch(
                "python.srv.core.tls_bootstrap.env_int", return_value=100_000
            ):
                result = tls_renewal.check_and_renew(cert_path, key_path, ssl_context)

            self.assertTrue(result)
            self.assertNotEqual(cert_path.read_bytes(), original_cert_bytes)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.assertIsNotNone(cert)


class PeriodicRenewalLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_repeatedly_on_the_configured_interval(self) -> None:
        calls: list[tuple[Path, Path, object]] = []

        def fake_check(cert_path, key_path, ssl_context):
            calls.append((cert_path, key_path, ssl_context))

        with patch("python.srv.core.tls_renewal.check_and_renew", side_effect=fake_check):
            cert_path, key_path = Path("/tmp/server.crt"), Path("/tmp/server.key")
            task = asyncio.create_task(
                tls_renewal.periodic_renewal_loop(
                    cert_path, key_path, None, interval_seconds=0
                )
            )
            for _ in range(200):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(c == (cert_path, key_path, None) for c in calls))

    async def test_cancellation_stops_the_loop_cleanly(self) -> None:
        with patch("python.srv.core.tls_renewal.check_and_renew") as mock_check:
            task = asyncio.create_task(
                tls_renewal.periodic_renewal_loop(
                    Path("/tmp/server.crt"), Path("/tmp/server.key"), None, interval_seconds=60
                )
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        mock_check.assert_not_called()

    async def test_uses_configured_interval_by_default(self) -> None:
        observed_intervals: list[float] = []

        async def capture_sleep(seconds):
            observed_intervals.append(seconds)
            raise asyncio.CancelledError()

        with (
            patch(
                "python.srv.core.tls_renewal.renewal_check_interval_seconds", return_value=42
            ),
            patch("asyncio.sleep", new=capture_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await tls_renewal.periodic_renewal_loop(
                    Path("/tmp/server.crt"), Path("/tmp/server.key"), None
                )

        self.assertEqual(observed_intervals, [42])


if __name__ == "__main__":
    unittest.main()
