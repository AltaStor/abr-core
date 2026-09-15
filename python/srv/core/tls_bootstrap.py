# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import array
import fcntl
import ipaddress
import json
import logging
import os
import socket
import ssl
import struct
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .server_defaults import env_int, shipped_default

logger = logging.getLogger(__name__)


TLS_CIPHERS_ENV_VAR = "ABR_TLS_CIPHERS"

# Held around every rewrite of the certificate/key pair on disk and the reload
# into the live TLS context that follows it, so the periodic renewal check and
# an administrator installing their own certificate cannot interleave.
TLS_FILES_LOCK = threading.RLock()

_GENERATED_SUBJECT = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])


def _cipher_string_error(value: str | None) -> str | None:
    if not value or not value.strip():
        return "value is empty"
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).set_ciphers(value.strip())
    except ssl.SSLError as exc:
        return str(exc)
    return None


def resolve_tls_ciphers() -> str:
    override = os.environ.get(TLS_CIPHERS_ENV_VAR)
    if override is not None:
        error = _cipher_string_error(override)
        if error is None:
            return override.strip()
        logger.warning(
            "Invalid %s=%r (%s); falling back to the shipped cipher list",
            TLS_CIPHERS_ENV_VAR, override, error,
        )

    fallback = shipped_default(TLS_CIPHERS_ENV_VAR)
    fallback_error = _cipher_string_error(fallback)
    if fallback_error is not None:
        raise RuntimeError(
            f"No usable {TLS_CIPHERS_ENV_VAR} in defaults.env: {fallback!r} ({fallback_error})"
        )
    return str(fallback).strip()


def _dev_tls_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tls"


def _dir_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe_path = tempfile.mkstemp(prefix=".write-test-", dir=path)
        os.close(fd)
        os.remove(probe_path)
        return True
    except OSError:
        return False


def default_cert_paths() -> tuple[Path, Path]:
    cert = os.environ.get("ABR_TLS_CERT_FILE", "").strip()
    key = os.environ.get("ABR_TLS_KEY_FILE", "").strip()
    if bool(cert) != bool(key):
        raise RuntimeError(
            "ABR_TLS_CERT_FILE and ABR_TLS_KEY_FILE must both be set, or both left unset"
        )

    dev_dir = _dev_tls_dir()
    if not cert:
        return dev_dir / "server.crt", dev_dir / "server.key"

    cert_path, key_path = Path(cert).expanduser(), Path(key).expanduser()
    if _dir_is_writable(cert_path.parent):
        return cert_path, key_path

    logger.warning(
        "TLS directory %s is not usable (not writable); using %s instead. "
        "Expected in a dev checkout; a real install should have created it "
        "with the right ownership.",
        cert_path.parent,
        dev_dir,
    )
    return dev_dir / "server.crt", dev_dir / "server.key"


def administrator_marker_path(cert_path: Path) -> Path:
    return cert_path.with_name(cert_path.name + ".administrator.json")


def read_administrator_marker(cert_path: Path) -> dict | None:
    try:
        payload = json.loads(administrator_marker_path(cert_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Present but unreadable still means somebody put their own certificate
        # here on purpose; that intent is what matters, not the metadata.
        return {}
    return payload if isinstance(payload, dict) else {}


def write_administrator_marker(cert_path: Path, metadata: dict) -> None:
    marker = administrator_marker_path(cert_path)
    marker.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    marker.chmod(0o644)


def is_appliance_generated(cert: x509.Certificate) -> bool:
    return cert.subject == _GENERATED_SUBJECT and cert.issuer == _GENERATED_SUBJECT


def _is_managed_by_appliance(cert_path: Path, cert: x509.Certificate) -> bool:
    # Only a certificate this module minted is ours to renew. Anything else --
    # installed through the API (marker present) or dropped in over the files
    # by hand -- names hosts and a chain the appliance cannot reproduce, and
    # regenerating it would silently put the self-signed one back.
    return read_administrator_marker(cert_path) is None and is_appliance_generated(cert)


def _warn_if_administrator_cert_expiring(cert_path: Path, cert: x509.Certificate) -> None:
    threshold_days = env_int("ABR_TLS_RENEWAL_THRESHOLD_DAYS")
    remaining = cert.not_valid_after_utc - datetime.now(timezone.utc)
    if remaining > timedelta(days=threshold_days):
        return
    if remaining <= timedelta(0):
        logger.error(
            "The administrator-provided TLS certificate %s expired on %s. It is not "
            "renewed automatically; install a new one from Settings > Security or "
            "revert to the appliance's self-signed certificate.",
            cert_path,
            cert.not_valid_after_utc.isoformat(),
        )
        return
    logger.warning(
        "The administrator-provided TLS certificate %s expires on %s (%d days left). "
        "It is not renewed automatically; install a replacement before then.",
        cert_path,
        cert.not_valid_after_utc.isoformat(),
        remaining.days,
    )


def _detect_ips_via_hostname_and_probe() -> set[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if not ip.startswith("127.") and "%" not in ip:
                ips.add(ip)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            ips.add(probe.getsockname()[0])
    except OSError:
        pass

    return ips


def _is_usable_san_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (ip_obj.is_loopback or ip_obj.is_link_local)


_SIOCGIFCONF = 0x8912
_IFREQ_SIZE = 40 if sys.maxsize > 2**32 else 32


def _fetch_siocgifconf_buffer() -> tuple[bytes, int]:
    max_interfaces = 128
    buffer_size = max_interfaces * _IFREQ_SIZE
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        names = array.array("B", b"\0" * buffer_size)
        buf_addr, _ = names.buffer_info()
        outbytes = struct.unpack(
            "iL",
            fcntl.ioctl(
                probe.fileno(), _SIOCGIFCONF, struct.pack("iL", buffer_size, buf_addr)
            ),
        )[0]
        return names.tobytes(), outbytes


def _parse_siocgifconf_buffer(raw: bytes, outbytes: int) -> set[str]:
    ips: set[str] = set()
    for offset in range(0, outbytes, _IFREQ_SIZE):
        entry = raw[offset : offset + _IFREQ_SIZE]
        if len(entry) < 24:
            continue
        try:
            name = entry[:16].split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            continue
        if not name or name == "lo":
            continue
        ip_str = socket.inet_ntoa(entry[20:24])
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_usable_san_ip(ip_obj):
            ips.add(str(ip_obj))
    return ips


def _detect_ipv4_via_ioctl() -> set[str]:
    try:
        raw, outbytes = _fetch_siocgifconf_buffer()
    except OSError:
        return set()
    return _parse_siocgifconf_buffer(raw, outbytes)


def _detect_ipv6_via_procfs() -> set[str]:
    ips: set[str] = set()
    try:
        with open("/proc/net/if_inet6", encoding="ascii") as handle:
            lines = handle.readlines()
    except OSError:
        return ips

    for line in lines:
        parts = line.split()
        if len(parts) != 6:
            continue
        addr_hex, _dev_no, _prefixlen, _scope, _flags, ifname = parts
        if ifname == "lo" or len(addr_hex) != 32:
            continue
        try:
            ip_obj = ipaddress.IPv6Address(int(addr_hex, 16))
        except ValueError:
            continue
        if _is_usable_san_ip(ip_obj):
            ips.add(str(ip_obj))

    return ips


def _detect_ips_from_interfaces() -> set[str]:
    return _detect_ipv4_via_ioctl() | _detect_ipv6_via_procfs()


def _detect_local_ips() -> list[str]:
    ips = _detect_ips_via_hostname_and_probe() | _detect_ips_from_interfaces()
    return sorted(ips)


def _desired_san_entries() -> list[x509.GeneralName]:
    entries: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in _detect_local_ips():
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    return entries


def _existing_san_ips(cert: x509.Certificate) -> set[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}


def _load_or_generate_key(key_path: Path) -> rsa.RSAPrivateKey:
    if key_path.exists():
        return serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return private_key


def _needs_renewal(cert_path: Path, *, threshold_days: int | None = None) -> bool:
    if threshold_days is None:
        threshold_days = env_int("ABR_TLS_RENEWAL_THRESHOLD_DAYS")
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return True
    return datetime.now(timezone.utc) >= cert.not_valid_after_utc - timedelta(days=threshold_days)


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> bool:
    san_entries = _desired_san_entries()

    if cert_path.exists() and key_path.exists():
        desired_ips = {str(entry.value) for entry in san_entries if isinstance(entry, x509.IPAddress)}
        stale_san = False
        try:
            existing_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except (OSError, ValueError):
            existing_cert = None
        if existing_cert is not None:
            if not _is_managed_by_appliance(cert_path, existing_cert):
                _warn_if_administrator_cert_expiring(cert_path, existing_cert)
                return False
            stale_san = _existing_san_ips(existing_cert) != desired_ips

        if not _needs_renewal(cert_path) and not stale_san:
            return False

        if stale_san:
            logger.warning(
                "Machine's network addresses changed; renewing certificate %s to match (same key).",
                cert_path,
            )
        else:
            logger.warning(
                "Existing certificate %s is expired or expiring within %d days; renewing (same key).",
                cert_path,
                env_int("ABR_TLS_RENEWAL_THRESHOLD_DAYS"),
            )
        cert_path.unlink()

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    # Whatever stood here before is gone or unreadable, so a marker left behind
    # would claim the certificate about to be generated as an administrator's
    # and exempt it from renewal for good.
    administrator_marker_path(cert_path).unlink(missing_ok=True)

    private_key = _load_or_generate_key(key_path)
    subject = issuer = _GENERATED_SUBJECT
    now = datetime.now(timezone.utc)
    validity_days = env_int("ABR_TLS_CERT_VALIDITY_DAYS")

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(private_key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)

    logger.warning(
        "Generated a self-signed TLS certificate at %s (valid %d days). "
        "Clients will see an untrusted-certificate warning until this is replaced "
        "with a certificate from a trusted CA.",
        cert_path,
        validity_days,
    )
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    cert_path, key_path = default_cert_paths()

    if "--renew" in sys.argv[1:]:
        cert_path.unlink(missing_ok=True)

    generated = ensure_self_signed_cert(cert_path, key_path)
    action = "Generated" if generated else "Already present"
    logger.info("%s: %s", action, cert_path)
    print(cert_path)
    print(key_path)


if __name__ == "__main__":
    main()
