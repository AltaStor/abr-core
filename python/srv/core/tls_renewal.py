# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

from .server_defaults import env_int
from .tls_bootstrap import TLS_FILES_LOCK, ensure_self_signed_cert

logger = logging.getLogger(__name__)


def renewal_check_interval_seconds() -> int:
    return env_int("ABR_TLS_RENEWAL_CHECK_INTERVAL_SECONDS")


def load_into_live_context(ssl_context: ssl.SSLContext, cert_path: Path, key_path: Path) -> None:
    # load_cert_chain() on a context already serving a certificate only replaces
    # the one of the same key type: load an EC certificate into an RSA context
    # and it holds both, and clients that prefer RSA keep getting the old one
    # (and the other way round). So the pair is loaded into a context of its own
    # and every handshake is moved onto it from the ClientHello callback, which
    # OpenSSL calls with or without SNI. Protocol, cipher and session settings
    # stay those of the live context. Loading the pair into the live context as
    # well only keeps what it holds current; the callback decides what is served.
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(str(cert_path), str(key_path))
    ssl_context.load_cert_chain(str(cert_path), str(key_path))

    def use_current_certificate(ssl_object, _server_name, _context) -> None:
        ssl_object.context = serving

    ssl_context.sni_callback = use_current_certificate


def check_and_renew(
    cert_path: Path, key_path: Path, ssl_context: ssl.SSLContext | None
) -> bool:
    with TLS_FILES_LOCK:
        return _check_and_renew_locked(cert_path, key_path, ssl_context)


def _check_and_renew_locked(
    cert_path: Path, key_path: Path, ssl_context: ssl.SSLContext | None
) -> bool:
    logger.info("Checking TLS certificate %s for renewal.", cert_path)
    try:
        renewed = ensure_self_signed_cert(cert_path, key_path)
    except Exception:
        logger.exception(
            "TLS certificate renewal check failed for %s; keeping the current "
            "certificate in use.",
            cert_path,
        )
        return False

    if not renewed:
        logger.info("TLS certificate %s does not need renewal yet.", cert_path)
        return False

    if ssl_context is None:
        logger.warning(
            "TLS certificate %s was renewed on disk, but no live TLS server context "
            "is registered to reload it into; the running process will keep serving "
            "the previous certificate until restarted.",
            cert_path,
        )
        return True

    try:
        load_into_live_context(ssl_context, cert_path, key_path)
    except Exception:
        logger.exception(
            "Renewed TLS certificate %s on disk but failed to load it into the "
            "running server; the process will keep serving the previous certificate "
            "until restarted.",
            cert_path,
        )
        return False

    logger.warning(
        "TLS certificate %s renewed and reloaded into the running server without a restart.",
        cert_path,
    )
    return True


async def periodic_renewal_loop(
    cert_path: Path,
    key_path: Path,
    ssl_context: ssl.SSLContext | None,
    *,
    interval_seconds: int | None = None,
) -> None:
    interval = (
        interval_seconds if interval_seconds is not None else renewal_check_interval_seconds()
    )
    logger.info(
        "Starting periodic TLS certificate renewal check (every %d seconds).", interval
    )
    while True:
        await asyncio.sleep(interval)
        check_and_renew(cert_path, key_path, ssl_context)
