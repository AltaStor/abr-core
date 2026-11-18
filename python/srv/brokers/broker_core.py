# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import json
import pwd
import socket
import struct
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SERVICE_USER = "altastor-backup"
MAX_REQUEST_BYTES = 64 * 1024
READ_TIMEOUT_SECONDS = 10
PEERCRED_FORMAT = "3i"

INVALID_REQUEST = "invalid_request"
NOT_PERMITTED = "not_permitted"
REFUSED = "refused"
FAILED = "failed"
UNEXPECTED_ERRORS = (OSError, ValueError, LookupError, RuntimeError, TypeError)


class BrokerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Peer:
    pid: int
    uid: int
    gid: int


Operation = Callable[[dict[str, Any]], dict[str, Any]]


def peer_of(connection: socket.socket) -> Peer:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(PEERCRED_FORMAT))
    pid, uid, gid = struct.unpack(PEERCRED_FORMAT, raw)
    return Peer(pid, uid, gid)


def allowed_uids(service_user: str) -> frozenset[int]:
    try:
        return frozenset({0, pwd.getpwnam(service_user).pw_uid})
    except KeyError:
        return frozenset({0})


def read_request(connection: socket.socket) -> tuple[str, dict[str, Any]]:
    connection.settimeout(READ_TIMEOUT_SECONDS)
    with connection.makefile("rb") as stream:
        try:
            line = stream.readline(MAX_REQUEST_BYTES + 1)
        except TimeoutError as exc:
            raise BrokerError(INVALID_REQUEST, f"no request arrived within {READ_TIMEOUT_SECONDS} s") from exc

    if len(line) > MAX_REQUEST_BYTES:
        raise BrokerError(INVALID_REQUEST, f"the request is longer than {MAX_REQUEST_BYTES} bytes")
    if not line.endswith(b"\n"):
        raise BrokerError(INVALID_REQUEST, "the request must be one JSON object on one line")

    try:
        request = json.loads(line)
    except ValueError as exc:
        raise BrokerError(INVALID_REQUEST, f"the request is not JSON: {exc}") from exc

    if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
        raise BrokerError(INVALID_REQUEST, "the request must hold exactly an operation and its arguments")
    operation, arguments = request["operation"], request["arguments"]
    if not isinstance(operation, str) or not isinstance(arguments, dict):
        raise BrokerError(INVALID_REQUEST, "the operation must be a name and the arguments an object")
    return operation, arguments


def send_reply(connection: socket.socket, reply: dict[str, Any]) -> None:
    connection.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\n")


def handle(
    connection: socket.socket, operations: Mapping[str, Operation], *, service_user: str
) -> tuple[str, dict[str, Any]]:
    peer = peer_of(connection)
    operation = "-"
    try:
        if peer.uid not in allowed_uids(service_user):
            raise BrokerError(NOT_PERMITTED, f"uid {peer.uid} may not use this broker; only root and {service_user} may")
        operation, arguments = read_request(connection)
        if operation not in operations:
            raise BrokerError(INVALID_REQUEST, f"unknown operation {operation!r}; this broker runs {', '.join(sorted(operations))}")
        reply = {"ok": True, "result": operations[operation](arguments)}
    except BrokerError as exc:
        reply = {"ok": False, "code": exc.code, "message": exc.message}
    except UNEXPECTED_ERRORS as exc:
        reply = {"ok": False, "code": FAILED, "message": f"{type(exc).__name__}: {exc}"}

    outcome = "ok" if reply["ok"] else f"{reply['code']}: {reply['message']}"
    print(f"{operation} for uid {peer.uid} (pid {peer.pid}): {outcome}", file=sys.stderr, flush=True)
    return operation, reply


def serve(
    operations: Mapping[str, Operation], *, service_user: str, connection: socket.socket | None = None
) -> int:
    connection = connection or socket.socket(fileno=sys.stdin.fileno())
    _operation, reply = handle(connection, operations, service_user=service_user)
    try:
        send_reply(connection, reply)
    except OSError as exc:
        print(f"the reply could not be sent: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0 if reply["ok"] else 1


def argument_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--service-user",
        default=SERVICE_USER,
        help=f"the account the server runs as, which may connect besides root (default {SERVICE_USER})",
    )
    return parser


def parse_arguments(argv: Sequence[str], *, description: str) -> argparse.Namespace:
    return argument_parser(description).parse_args(list(argv))
