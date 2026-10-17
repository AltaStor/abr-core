# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from cryptography import x509

from python.srv.core import tls_bootstrap
from python.srv.core.tls_bootstrap import (
    _detect_ips_from_interfaces,
    _detect_local_ips,
    _desired_san_entries,
    ensure_self_signed_cert,
)


def _make_ifreq_entry(name: bytes, ip: str) -> bytes:
    entry = bytearray(tls_bootstrap._IFREQ_SIZE)
    entry[0 : len(name)] = name
    entry[16:18] = struct.pack("H", socket.AF_INET)
    entry[20:24] = socket.inet_aton(ip)
    return bytes(entry)


def _make_siocgifconf_buffer(entries: list[tuple[bytes, str]]) -> tuple[bytes, int]:
    raw = b"".join(_make_ifreq_entry(name, ip) for name, ip in entries)
    return raw, len(raw)


class DetectIpv4ViaIoctlTests(unittest.TestCase):
    def test_parses_multiple_interfaces_including_a_secondary_ip(self) -> None:
        raw, outbytes = _make_siocgifconf_buffer(
            [
                (b"lo", "127.0.0.1"),
                (b"ens18", "192.0.2.90"),
                (b"veth0", "10.250.88.1"),
                (b"veth0", "10.250.88.5"),
            ]
        )
        with patch.object(
            tls_bootstrap, "_fetch_siocgifconf_buffer", return_value=(raw, outbytes)
        ):
            ips = tls_bootstrap._detect_ipv4_via_ioctl()

        self.assertEqual(ips, {"192.0.2.90", "10.250.88.1", "10.250.88.5"})

    def test_excludes_loopback_interface_and_link_local_address(self) -> None:
        raw, outbytes = _make_siocgifconf_buffer(
            [
                (b"lo", "127.0.0.1"),
                (b"ens18", "169.254.1.5"),
            ]
        )
        with patch.object(
            tls_bootstrap, "_fetch_siocgifconf_buffer", return_value=(raw, outbytes)
        ):
            ips = tls_bootstrap._detect_ipv4_via_ioctl()

        self.assertEqual(ips, set())

    def test_returns_empty_set_when_the_ioctl_call_fails(self) -> None:
        with patch.object(
            tls_bootstrap, "_fetch_siocgifconf_buffer", side_effect=OSError("EPERM")
        ):
            self.assertEqual(tls_bootstrap._detect_ipv4_via_ioctl(), set())

    def test_ignores_a_truncated_trailing_entry(self) -> None:
        raw, outbytes = _make_siocgifconf_buffer([(b"ens18", "10.0.0.5")])
        with patch.object(
            tls_bootstrap,
            "_fetch_siocgifconf_buffer",
            return_value=(raw, outbytes + tls_bootstrap._IFREQ_SIZE),
        ):
            ips = tls_bootstrap._detect_ipv4_via_ioctl()

        self.assertEqual(ips, {"10.0.0.5"})


class DetectIpv6ViaProcfsTests(unittest.TestCase):
    def test_parses_global_addresses_excluding_loopback_and_link_local(self) -> None:
        fake_content = (
            "fe800000000000000f978cb351ca55fa 02 40 20 80 ens18\n"
            "00000000000000000000000000000001 01 80 10 80       lo\n"
            "20010db8000000000000000000000042 03 40 00 80 eth2\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_content)):
            ips = tls_bootstrap._detect_ipv6_via_procfs()

        self.assertEqual(ips, {"2001:db8::42"})

    def test_returns_empty_set_when_procfs_is_unavailable(self) -> None:
        with patch("builtins.open", side_effect=FileNotFoundError()):
            self.assertEqual(tls_bootstrap._detect_ipv6_via_procfs(), set())

    def test_ignores_malformed_lines(self) -> None:
        with patch("builtins.open", mock_open(read_data="not enough columns\n")):
            self.assertEqual(tls_bootstrap._detect_ipv6_via_procfs(), set())


class DetectIpsFromInterfacesTests(unittest.TestCase):
    def test_combines_ipv4_and_ipv6_results(self) -> None:
        with (
            patch.object(
                tls_bootstrap, "_detect_ipv4_via_ioctl", return_value={"10.0.0.5"}
            ),
            patch.object(
                tls_bootstrap, "_detect_ipv6_via_procfs", return_value={"2001:db8::42"}
            ),
        ):
            ips = _detect_ips_from_interfaces()

        self.assertEqual(ips, {"10.0.0.5", "2001:db8::42"})

    def test_works_end_to_end_against_the_real_host(self) -> None:
        ips = _detect_ips_from_interfaces()
        for ip in ips:
            self.assertFalse(ip.startswith("127."))
            self.assertNotEqual(ip, "::1")


class DetectLocalIpsTests(unittest.TestCase):
    def test_merges_hostname_probe_and_interface_detection(self) -> None:
        with (
            patch.object(
                tls_bootstrap,
                "_detect_ips_via_hostname_and_probe",
                return_value={"10.0.0.5"},
            ),
            patch.object(
                tls_bootstrap,
                "_detect_ips_from_interfaces",
                return_value={"10.0.0.5", "172.16.0.10"},
            ),
        ):
            self.assertEqual(_detect_local_ips(), ["10.0.0.5", "172.16.0.10"])

    def test_falls_back_to_hostname_probe_when_interface_detection_empty(self) -> None:
        with (
            patch.object(
                tls_bootstrap,
                "_detect_ips_via_hostname_and_probe",
                return_value={"192.168.1.5"},
            ),
            patch.object(tls_bootstrap, "_detect_ips_from_interfaces", return_value=set()),
        ):
            self.assertEqual(_detect_local_ips(), ["192.168.1.5"])


class DesiredSanEntriesTests(unittest.TestCase):
    def test_includes_localhost_loopback_and_all_detected_ips(self) -> None:
        with patch.object(
            tls_bootstrap, "_detect_local_ips", return_value=["10.0.0.5", "172.16.0.10"]
        ):
            entries = _desired_san_entries()

        dns_names = [e.value for e in entries if isinstance(e, x509.DNSName)]
        ip_values = {str(e.value) for e in entries if isinstance(e, x509.IPAddress)}
        self.assertEqual(dns_names, ["localhost"])
        self.assertEqual(ip_values, {"127.0.0.1", "10.0.0.5", "172.16.0.10"})


class EnsureSelfSignedCertTests(unittest.TestCase):
    def _san_ips(self, cert_path: Path) -> set[str]:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}

    def test_initial_generation_includes_all_detected_ips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch.object(
                tls_bootstrap,
                "_detect_local_ips",
                return_value=["10.0.0.5", "172.16.0.10"],
            ):
                self.assertTrue(ensure_self_signed_cert(cert_path, key_path))

            self.assertEqual(self._san_ips(cert_path), {"127.0.0.1", "10.0.0.5", "172.16.0.10"})

    def test_unchanged_ips_do_not_trigger_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch.object(tls_bootstrap, "_detect_local_ips", return_value=["10.0.0.5"]):
                self.assertTrue(ensure_self_signed_cert(cert_path, key_path))
                self.assertFalse(ensure_self_signed_cert(cert_path, key_path))

    def test_new_ip_triggers_regeneration_and_reuses_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch.object(tls_bootstrap, "_detect_local_ips", return_value=["10.0.0.5"]):
                ensure_self_signed_cert(cert_path, key_path)
            key_before = key_path.read_bytes()

            with patch.object(
                tls_bootstrap,
                "_detect_local_ips",
                return_value=["10.0.0.5", "192.168.99.5"],
            ):
                self.assertTrue(ensure_self_signed_cert(cert_path, key_path))

            self.assertEqual(
                self._san_ips(cert_path), {"127.0.0.1", "10.0.0.5", "192.168.99.5"}
            )
            self.assertEqual(key_path.read_bytes(), key_before)

    def test_removed_ip_is_dropped_from_san_on_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            with patch.object(
                tls_bootstrap,
                "_detect_local_ips",
                return_value=["10.0.0.5", "172.16.0.10"],
            ):
                ensure_self_signed_cert(cert_path, key_path)

            with patch.object(tls_bootstrap, "_detect_local_ips", return_value=["10.0.0.5"]):
                self.assertTrue(ensure_self_signed_cert(cert_path, key_path))

            self.assertEqual(self._san_ips(cert_path), {"127.0.0.1", "10.0.0.5"})


if __name__ == "__main__":
    unittest.main()
