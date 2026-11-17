# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import socket
from typing import Any

MAX_REPLY_BYTES = 64 * 1024


class BrokerUnavailable(Exception):
    pass


class BrokerRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def call(socket_path: str, operation: str, arguments: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    request = json.dumps({"operation": operation, "arguments": arguments}, separators=(",", ":")).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(socket_path)
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_REPLY_BYTES + 1)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise BrokerUnavailable(f"nothing listens on {socket_path} ({exc.strerror or exc})") from exc
    except PermissionError as exc:
        raise BrokerUnavailable(f"this server may not connect to {socket_path} ({exc.strerror or exc})") from exc
    except TimeoutError as exc:
        raise BrokerUnavailable(f"{socket_path} did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise BrokerUnavailable(f"{socket_path} could not be used ({exc.strerror or exc})") from exc

    if len(line) > MAX_REPLY_BYTES or not line.endswith(b"\n"):
        raise BrokerUnavailable(f"{socket_path} closed the connection without a complete answer")
    try:
        reply = json.loads(line)
    except ValueError as exc:
        raise BrokerUnavailable(f"{socket_path} answered something other than JSON") from exc

    if isinstance(reply, dict) and reply.get("ok") is True and isinstance(reply.get("result"), dict):
        return reply["result"]
    if isinstance(reply, dict) and reply.get("ok") is False:
        raise BrokerRefused(str(reply.get("code") or "failed"), str(reply.get("message") or "no reason given"))
    raise BrokerUnavailable(f"{socket_path} answered in a form this server does not know")
