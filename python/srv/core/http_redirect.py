# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

logger = logging.getLogger(__name__)

REDIRECT_PORT_ENV_VAR = "ABR_HTTP_REDIRECT_PORT"
REDIRECT_STATUS = 308

_MAX_HOST_HEADER_LENGTH = 255
_HOSTNAME_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*\.?$")

SecurityHeaderProvider = Callable[[], dict[str, str]]


def _is_port(text: str) -> bool:
    return text.isdigit() and 0 < int(text) <= 65535


def normalise_host(raw: str | bytes) -> str | None:
    if isinstance(raw, bytes):
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    else:
        value = raw
    value = value.strip()
    if not value or len(value) > _MAX_HOST_HEADER_LENGTH:
        return None

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        literal, remainder = value[1:closing], value[closing + 1 :]
        if remainder and not (remainder.startswith(":") and _is_port(remainder[1:])):
            return None
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return None
        return f"[{literal}]"

    host, separator, port = value.partition(":")
    if separator and not _is_port(port):
        return None
    if not host:
        return None
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    if _HOSTNAME_RE.match(host):
        return host
    return None


def _host_from_scope(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == b"host":
            return normalise_host(value)
    return None


def _path_and_query(scope: dict[str, Any]) -> str:
    raw_path = scope.get("raw_path") or b""
    try:
        path = raw_path.decode("ascii")
    except UnicodeDecodeError:
        path = ""
    if not path:
        path = quote(scope.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path

    raw_query = scope.get("query_string") or b""
    try:
        query = raw_query.decode("ascii")
    except UnicodeDecodeError:
        query = quote(raw_query)
    return f"{path}?{query}" if query else path


def redirect_target(scope: dict[str, Any], https_port: int) -> str | None:
    host = _host_from_scope(scope)
    if host is None:
        return None
    netloc = host if https_port == 443 else f"{host}:{https_port}"
    return f"https://{netloc}{_path_and_query(scope)}"


def build_redirect_app(
    *,
    https_port: int,
    security_headers: SecurityHeaderProvider | None = None,
):
    async def _respond(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes = b"",
    ) -> None:
        headers = [*headers, (b"content-length", str(len(body)).encode("ascii"))]
        if security_headers is not None:
            for name, value in security_headers().items():
                headers.append((name.encode("latin-1"), value.encode("latin-1")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def redirect_app(scope, receive, send) -> None:
        if scope["type"] != "http":
            return

        target = redirect_target(scope, https_port)
        if target is None:
            await _respond(
                send,
                400,
                [(b"content-type", b"text/plain; charset=utf-8")],
                b"Missing or unusable Host header; this port only redirects to HTTPS.\n",
            )
            return

        await _respond(send, REDIRECT_STATUS, [(b"location", target.encode("ascii"))])

    return redirect_app


@dataclass
class RedirectListener:
    host: str
    port: int
    server: Any
    thread: threading.Thread

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=timeout)


def start_redirect_listener(
    *,
    host: str,
    https_port: int,
    security_headers: SecurityHeaderProvider | None = None,
) -> RedirectListener | None:
    raw_port = os.environ.get(REDIRECT_PORT_ENV_VAR, "").strip()
    if not raw_port:
        logger.info(
            "Plain-HTTP redirect listener is off (%s is empty).", REDIRECT_PORT_ENV_VAR
        )
        return None

    try:
        port = int(raw_port)
        if not 0 < port <= 65535:
            raise ValueError(f"{port} is not a usable port number")
    except ValueError as exc:
        logger.warning(
            "Invalid %s=%r (%s); the plain-HTTP redirect listener will not start.",
            REDIRECT_PORT_ENV_VAR, raw_port, exc,
        )
        return None

    if port == https_port:
        logger.warning(
            "%s=%s is the HTTPS port; a single port cannot serve both TLS and "
            "plaintext, so the redirect listener will not start.",
            REDIRECT_PORT_ENV_VAR, port,
        )
        return None

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    dualstack = family == socket.AF_INET6 and socket.has_dualstack_ipv6()
    try:
        sock = socket.create_server(
            (host, port), family=family, backlog=128, dualstack_ipv6=dualstack
        )
    except OSError as exc:
        logger.warning(
            "Could not bind the plain-HTTP redirect listener on %s:%s (%s). HTTPS "
            "is unaffected; browsers that reach the appliance over http:// simply "
            "will not be redirected. A port below 1024 needs "
            "AmbientCapabilities=CAP_NET_BIND_SERVICE in the systemd unit.",
            host, port, exc,
        )
        return None

    import uvicorn

    app = build_redirect_app(https_port=https_port, security_headers=security_headers)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="abr-http-redirect", daemon=True
    )
    thread.start()
    logger.info(
        "Plain-HTTP redirect listener on %s:%s -> https://<host>:%s", host, port, https_port
    )
    return RedirectListener(host=host, port=port, server=server, thread=thread)
