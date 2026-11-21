# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""abr-node-helper, the only command AltaStor's SSH key runs on a Proxmox VE node.

The setup writes the helper to /etc/pve/priv, PVE's cluster file system, so that every node has it at once, also nodes
without the abr-pve-backup package and nodes that join the cluster later, and puts the key into root's shared
authorized_keys with the helper as its forced command. Everything abr-core does on a node after the setup goes
through it: run and result files, a run's secrets, the package, the cluster's members and certificates, and the
removal of an inventory's token and key. The helper comes inside the abr-pve-backup package abr-core installs, and
abr-core replaces the copy in /etc/pve when the package brings a newer one.

A command reaches the helper the same way with the key and with the root password during the setup: as the command
line `/usr/bin/perl -T /etc/pve/priv/abr-node-helper <command> <arguments>`. Under the key's forced command sshd runs
the helper whatever the line says, and the helper reads its request from the line.
"""

from __future__ import annotations

import io
import json
import lzma
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import paramiko

from .ssh import CommandResult, NodeShell, SshCommandError, SshError, SshUnreachableError, quote_command

HELPER_PATH = "/etc/pve/priv/abr-node-helper"
HELPER_COMMAND = ["/usr/bin/perl", "-T", HELPER_PATH]
FORCED_COMMAND = " ".join(HELPER_COMMAND)

# Where the package carries the helper.
PACKAGE_MEMBER = "usr/lib/abr/abr-node-helper"

INSTALL_TIMEOUT_SECONDS = 900
READY_TIMEOUT_SECONDS = 30
POLL_SECONDS = 0.05

_VERSION = re.compile(rb"^our \$VERSION = '([0-9][A-Za-z0-9.+~-]*)';$", re.MULTILINE)
_DEB_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+~-]*$")


class NodeHelperError(SshError):
    pass


class NodeHelperMissing(NodeHelperError):
    """The node has no helper in /etc/pve/priv: the cluster was set up before abr-core used it."""


def helper_from_package(package: Path) -> bytes:
    """The helper inside an abr-pve-backup package: a Debian package is an ar archive whose data.tar.xz holds it."""
    data = package.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise NodeHelperError(f"{package} is not a Debian package")
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip() or "0")
        member = data[offset + 60 : offset + 60 + size]
        offset += 60 + size + (size % 2)
        if name != "data.tar.xz":
            continue
        with tarfile.open(fileobj=io.BytesIO(lzma.decompress(member)), mode="r:") as archive:
            for entry in archive.getmembers():
                if entry.isfile() and entry.name.lstrip("./") == PACKAGE_MEMBER:
                    extracted = archive.extractfile(entry)
                    if extracted is not None:
                        return extracted.read()
        break
    raise NodeHelperError(f"{package} does not carry {PACKAGE_MEMBER}")


def helper_version(text: bytes) -> str:
    match = _VERSION.search(text)
    if not match:
        raise NodeHelperError("The node helper names no version")
    return match.group(1).decode()


def version_key(version: str) -> tuple:
    """A version as its numbers and words, for comparing the helper's versions, which are plain dotted numbers."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"[.+~-]", version))


class SecretsOffer:
    """A run's secrets, which the helper keeps in memory on the node until the run's task takes them. The SSH channel
    stays open for as long: closing it, or losing the connection, withdraws the offer."""

    def __init__(self, channel: paramiko.Channel, address: str, base: str) -> None:
        self._channel = channel
        self._address = address
        self._base = base
        self._buffer = b""
        self.state = "ready"

    def _line(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            if self._channel.recv_ready():
                chunk = self._channel.recv(4096)
                if not chunk:
                    return None
                self._buffer += chunk
            elif self._channel.exit_status_ready() or self._channel.closed:
                if not self._channel.recv_ready():
                    return None
            elif time.monotonic() > deadline:
                return None
            else:
                time.sleep(POLL_SECONDS)
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode(errors="replace")

    def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        line = self._line(timeout)
        state = _state(line)
        if state != "ready":
            error = b""
            while self._channel.recv_stderr_ready():
                error += self._channel.recv_stderr(65536)
            self.close()
            reason = error.decode(errors="replace").strip() or (f"it answered {line!r}" if line else "no answer")
            raise NodeHelperError(f"The node helper on {self._address} could not keep the secrets of {self._base}: "
                                  f"{reason}")

    def poll(self) -> str:
        """The offer's state: ready while the task has not taken the secrets, then handed, gone or expired."""
        if self.state == "ready":
            line = self._line(0)
            if line is not None:
                self.state = _state(line) or "unknown"
            elif self._channel.closed or self._channel.exit_status_ready():
                self.state = "gone"
        return self.state

    def close(self) -> None:
        self._channel.close()


def _state(line: str | None) -> str | None:
    try:
        answer = json.loads(line or "")
    except ValueError:
        return None
    return str(answer.get("state")) if isinstance(answer, dict) and answer.get("state") else None


class NodeHelper:
    """The helper on one node, reached through an SSH connection to it."""

    def __init__(self, shell: NodeShell) -> None:
        self.shell = shell

    @property
    def address(self) -> str:
        return self.shell.address

    def call(
        self, command: str, *arguments: str, input: bytes | str | None = None, timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        options: dict[str, Any] = {"input": input, "check": False}
        if timeout is not None:
            options["timeout"] = timeout
        result = self.shell.run([*HELPER_COMMAND, command, *arguments], **options)
        if result.status == 2 and "Can't open perl script" in result.error_text:
            raise NodeHelperMissing(
                f"{self.address} has no AltaStor node helper in {HELPER_PATH}: the cluster was set up before abr-core "
                "used one. Run inventory rescan, or set the cluster up again with inventory edit and the root password"
            )
        if check and result.status != 0:
            raise SshCommandError(f"abr-node-helper {command}", result.status, result.error_text)
        return result

    def answer(self, command: str, *arguments: str, **options: Any) -> dict[str, Any]:
        result = self.call(command, *arguments, **options)
        try:
            answer = json.loads(result.text or "null")
        except ValueError as exc:
            raise NodeHelperError(f"The node helper on {self.address} answered {command} with {result.text[:200]!r}") from exc
        if not isinstance(answer, dict):
            raise NodeHelperError(f"The node helper on {self.address} answered {command} with {result.text[:200]!r}")
        return answer

    # ---- the helper and the package ---------------------------------------------------------------------------------

    def version(self) -> dict[str, Any]:
        """The helper's version, the installed package's, and whether it runs as the key's forced command: a key
        from before the helper runs it as an ordinary command."""
        return self.answer("version")

    def update(self, text: bytes) -> str:
        """Replaces the helper in /etc/pve with text, which must run before it takes the old one's place."""
        return str(self.answer("update-helper", input=text).get("helper") or "")

    def package_status(self, version: str) -> tuple[str | None, bool]:
        """The installed package's version, and whether it is version or newer."""
        if not _DEB_VERSION.match(version):
            raise NodeHelperError(f"{version!r} is not a package version")
        answer = self.answer("package-status", version)
        return (str(answer["installed"]) if answer.get("installed") else None), bool(answer.get("current"))

    def install_package(self, package: Path) -> str:
        """Installs the package with apt on the node, which takes its dependencies from the node's repositories."""
        result = self.call("install-package", input=package.read_bytes(), timeout=INSTALL_TIMEOUT_SECONDS, check=False)
        if result.status != 0:
            lines = [line for line in result.error_text.splitlines() if line.strip() and not line.startswith("W:")]
            raise NodeHelperError(lines[-1] if lines else f"the node helper ended with exit code {result.status}")
        try:
            return str(json.loads(result.text).get("installed") or "")
        except (ValueError, AttributeError) as exc:
            raise NodeHelperError(f"The node helper on {self.address} answered {result.text[:200]!r}") from exc

    # ---- runs -------------------------------------------------------------------------------------------------------

    def put_run(self, name: str, text: str) -> None:
        self.answer("put-run", name, input=text)

    def get_result(self, name: str) -> bytes | None:
        """A result file, or None when the node has none."""
        result = self.call("get-result", name, check=False)
        if result.status == 3:
            return None
        if result.status != 0:
            raise SshCommandError("abr-node-helper get-result", result.status, result.error_text)
        return result.stdout

    def remove_run(self, base: str) -> list[str]:
        return [str(name) for name in self.answer("remove-run", base).get("removed") or []]

    def offer_secrets(self, base: str, secrets: dict[str, str]) -> SecretsOffer:
        """Has the helper keep a run's secrets for its task; returns once they are on offer."""
        channel = self.shell.start(quote_command([*HELPER_COMMAND, "hand-secrets", base]))
        try:
            channel.sendall((json.dumps(secrets) + "\n").encode())
        except (paramiko.SSHException, OSError, EOFError) as exc:
            channel.close()
            raise SshUnreachableError(f"The SSH connection to {self.address} broke while offering secrets: {exc}") from exc
        offer = SecretsOffer(channel, self.address, base)
        offer.wait_ready()
        return offer

    # ---- the node and the cluster -----------------------------------------------------------------------------------

    def nbd_devices(self) -> tuple[int, int]:
        answer = self.answer("nbd-devices")
        return int(answer.get("devices") or 0), int(answer.get("in_use") or 0)

    def cluster_info(self) -> dict[str, Any]:
        return self.answer("cluster-info")

    def remove_token(self, inventory_id: str) -> bool:
        return bool(self.answer("remove-token", inventory_id).get("removed"))

    def remove_key(self, inventory_id: str) -> bool:
        return bool(self.answer("remove-key", inventory_id).get("removed"))
