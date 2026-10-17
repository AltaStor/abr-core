# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import http.server
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from python.srv.core.tls_bootstrap import ensure_self_signed_cert


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class CertificateTlsHandshakeTests(unittest.TestCase):
    def _serve_and_validate(self, bind_ip: str, *, server_hostname: str | None = None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)

            server = http.server.HTTPServer((bind_ip, 0), _QuietHandler)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                client_ctx = ssl.create_default_context(cafile=str(cert_path))
                client_ctx.check_hostname = True
                with socket.create_connection((bind_ip, port), timeout=5) as sock:
                    with client_ctx.wrap_socket(
                        sock, server_hostname=server_hostname or bind_ip
                    ) as tls_sock:
                        self.assertIsNotNone(tls_sock.getpeercert())
                        self.assertEqual(tls_sock.version(), "TLSv1.3")
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_validates_against_the_loopback_san_entry(self) -> None:
        self._serve_and_validate("127.0.0.1")

    def test_validates_against_a_real_detected_host_interface_ip(self) -> None:
        from python.srv.core.tls_bootstrap import _detect_local_ips

        detected = _detect_local_ips()
        if not detected:
            self.skipTest("No non-loopback IP detected on this host to bind to.")
        self._serve_and_validate(detected[0])

    def test_client_rejects_an_ip_not_present_in_the_san(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)

            server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            try:
                client_ctx = ssl.create_default_context(cafile=str(cert_path))
                client_ctx.check_hostname = True
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        client_ctx.wrap_socket(sock, server_hostname="203.0.113.1")
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
