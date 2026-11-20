# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""SSH to the nodes of a Proxmox VE cluster, as root.

abr-core uses SSH only for what the REST API cannot do: writing the run files the provider reads and reading its result
files, installing the package, and the one-time setup of the API user, its token and the storage. Everything else goes
through the API.

Each inventory has its own key pair. The admin's root password is used once, to install the public key; PVE shares
root's authorized_keys across the cluster through /etc/pve, so the key opens every node.

Host keys are pinned. The first connection to the address the admin gave takes the key the node presents; every later
connection, and the first one to another node, must present a pinned key, which abr-core reads from the cluster over a
pinned connection. Only the host key algorithms of the pinned keys are offered, so a node with several host keys
presents the pinned one.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import posixpath
import secrets
import shlex
import socket
import stat
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

logger = logging.getLogger(__name__)

DEFAULT_PORT = 22
CONNECT_TIMEOUT_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 120.0
KEEPALIVE_SECONDS = 30
POLL_SECONDS = 0.02

# The host key algorithms each type of pinned key can be presented with.
HOST_KEY_ALGORITHMS = {
    "ssh-ed25519": ("ssh-ed25519",),
    "ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"),
    "ecdsa-sha2-nistp256": ("ecdsa-sha2-nistp256",),
    "ecdsa-sha2-nistp384": ("ecdsa-sha2-nistp384",),
    "ecdsa-sha2-nistp521": ("ecdsa-sha2-nistp521",),
}


class SshError(RuntimeError):
    pass


class SshUnreachableError(SshError):
    pass


class SshAuthError(SshError):
    pass


class SshHostKeyError(SshError):
    pass


class SshCommandError(SshError):
    def __init__(self, command: str, status: int, stderr: str) -> None:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no error output"
        super().__init__(f"{command} failed with exit status {status}: {detail}")
        self.command = command
        self.status = status
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    status: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def error_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def generate_key_pair(comment: str) -> tuple[str, str]:
    """A new Ed25519 key pair: the private key in OpenSSH format and the public key as an authorized_keys line."""
    private = ed25519.Ed25519PrivateKey.generate()
    private_text = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    public_text = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return private_text, f"{public_text.decode()} {comment}".strip()


def _load_private_key(text: str) -> paramiko.PKey:
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_class.from_private_key(io.StringIO(text))
        except (paramiko.SSHException, ValueError):
            continue
    raise SshError("The SSH private key AltaStor keeps for this inventory cannot be read")


def public_key_line(private_key: str, comment: str = "") -> str:
    key = _load_private_key(private_key)
    return f"{key.get_name()} {key.get_base64()} {comment}".strip()


def host_key_line(key: paramiko.PKey) -> str:
    """A host key as "type base64", the form pinned keys are kept in."""
    return f"{key.get_name()} {key.get_base64()}"


def key_fingerprint(line: str) -> str:
    """The SHA256 fingerprint of a key line, as ssh-keygen -l shows it."""
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _normalized_key(line: str) -> str:
    parts = str(line or "").split()
    if len(parts) < 2:
        raise ValueError(f"not an SSH public key: {line!r}")
    return f"{parts[0]} {parts[1]}"


def quote_command(command: str | Sequence[str]) -> str:
    return command if isinstance(command, str) else " ".join(shlex.quote(str(part)) for part in command)


class NodeShell:
    """One SSH connection to one node, for commands and files.

    Give host_keys to require one of them; without them the connection takes the key the node presents, which
    host_key then holds for pinning.
    """

    def __init__(
        self,
        address: str,
        *,
        port: int = DEFAULT_PORT,
        user: str = "root",
        private_key: str | None = None,
        password: str | None = None,
        host_keys: Sequence[str] | None = None,
        timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if not private_key and not password:
            raise ValueError("SSH to a PVE node needs a private key or a password")
        self.address = address
        self.port = int(port)
        self.user = user
        self._private_key = private_key
        self._password = password
        self._pinned = [_normalized_key(line) for line in host_keys or []]
        self._timeout = timeout
        self._transport: paramiko.Transport | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self.host_key = ""

    def __enter__(self) -> "NodeShell":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def target(self) -> str:
        return f"{self.user}@{self.address}:{self.port}"

    def _disabled_host_key_algorithms(self) -> list[str]:
        if not self._pinned:
            return []
        offered = {algorithm for line in self._pinned for algorithm in HOST_KEY_ALGORITHMS.get(line.split()[0], ())}
        return [algorithm for algorithm in paramiko.Transport._preferred_keys if algorithm not in offered]

    def connect(self) -> None:
        if self._transport is not None and self._transport.is_active():
            return
        try:
            sock = socket.create_connection((self.address, self.port), timeout=self._timeout)
        except OSError as exc:
            raise SshUnreachableError(f"SSH to {self.address}:{self.port} cannot be reached: {exc}") from exc
        transport = paramiko.Transport(sock, disabled_algorithms={"keys": self._disabled_host_key_algorithms()})
        try:
            try:
                transport.start_client(timeout=self._timeout)
            except (paramiko.SSHException, OSError, EOFError) as exc:
                raise SshUnreachableError(f"SSH to {self.address}:{self.port} failed: {exc}") from exc
            presented = host_key_line(transport.get_remote_server_key())
            if self._pinned and presented not in self._pinned:
                raise SshHostKeyError(
                    f"The SSH host key of {self.address} ({key_fingerprint(presented)}) is not the one AltaStor "
                    "pinned for this node. If the node was reinstalled, register the inventory again; otherwise "
                    "something answers in its place"
                )
            self.host_key = presented
            try:
                if self._private_key:
                    transport.auth_publickey(self.user, _load_private_key(self._private_key))
                else:
                    transport.auth_password(self.user, self._password or "")
            except paramiko.AuthenticationException as exc:
                how = "AltaStor's SSH key" if self._private_key else "this password"
                raise SshAuthError(f"{self.target} refused {how}: {exc}") from exc
            except (paramiko.SSHException, OSError, EOFError) as exc:
                raise SshUnreachableError(f"SSH to {self.address}:{self.port} failed while logging in: {exc}") from exc
            transport.set_keepalive(KEEPALIVE_SECONDS)
        except BaseException:
            transport.close()
            raise
        self._transport = transport
        logger.debug("SSH connected to %s, host key %s", self.target, key_fingerprint(presented))

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _active_transport(self) -> paramiko.Transport:
        if self._transport is None or not self._transport.is_active():
            self._sftp = None
            self._transport = None
            self.connect()
        assert self._transport is not None
        return self._transport

    # ---- commands ---------------------------------------------------------------------------------------------------

    def run(
        self,
        command: str | Sequence[str],
        *,
        input: bytes | str | None = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        check: bool = True,
    ) -> CommandResult:
        """Runs a command, a list of arguments quoted for the shell or a shell line, with input on its stdin."""
        text = quote_command(command)
        channel = self._open(lambda transport: transport.open_session(timeout=self._timeout), "a session")
        stdout = bytearray()
        stderr = bytearray()
        try:
            channel.exec_command(text)
            if input is not None:
                channel.sendall(input.encode() if isinstance(input, str) else input)
            channel.shutdown_write()
            deadline = time.monotonic() + timeout
            while True:
                if channel.recv_ready():
                    stdout += channel.recv(65536)
                elif channel.recv_stderr_ready():
                    stderr += channel.recv_stderr(65536)
                elif channel.exit_status_ready():
                    break
                elif not self._transport_active():
                    raise SshUnreachableError(f"The SSH connection to {self.address} broke while running {text}")
                elif time.monotonic() > deadline:
                    raise SshError(f"{text} on {self.address} did not finish in {int(timeout)}s")
                else:
                    time.sleep(POLL_SECONDS)
            # Output can still be on its way when the exit status arrives; the end of the output follows it.
            while True:
                if channel.recv_ready():
                    stdout += channel.recv(65536)
                elif channel.recv_stderr_ready():
                    stderr += channel.recv_stderr(65536)
                elif channel.eof_received or channel.closed or not self._transport_active():
                    break
                elif time.monotonic() > deadline:
                    raise SshError(f"{text} on {self.address} did not finish in {int(timeout)}s")
                else:
                    time.sleep(POLL_SECONDS)
            status = channel.recv_exit_status()
        except (paramiko.SSHException, OSError, EOFError) as exc:
            raise SshUnreachableError(f"The SSH connection to {self.address} broke while running {text}: {exc}") from exc
        finally:
            channel.close()
        result = CommandResult(status, bytes(stdout), bytes(stderr))
        if check and status != 0:
            raise SshCommandError(text, status, result.error_text)
        return result

    def _transport_active(self) -> bool:
        return self._transport is not None and self._transport.is_active()

    def _open(self, opener: Callable[[paramiko.Transport], Any], what: str) -> Any:
        """Opens a channel. A connection the node dropped can look alive until then; nothing has run yet, so the
        connection is made again once."""
        for attempt in (1, 2):
            try:
                opened = opener(self._active_transport())
            except (paramiko.SSHException, OSError, EOFError) as exc:
                if attempt == 2:
                    raise SshUnreachableError(f"SSH to {self.address} cannot open {what}: {exc}") from exc
                logger.debug("SSH to %s cannot open %s, connecting again: %s", self.target, what, exc)
                self.close()
                continue
            if opened is None:
                raise SshUnreachableError(f"SSH to {self.address} cannot open {what}")
            return opened
        raise AssertionError("unreachable")

    # ---- files ------------------------------------------------------------------------------------------------------

    def _files(self) -> paramiko.SFTPClient:
        if self._sftp is None or not self._transport_active():
            self._sftp = self._open(paramiko.SFTPClient.from_transport, "SFTP")
        return self._sftp

    def read_file(self, path: str) -> bytes | None:
        """The file's content, or None when there is no such file."""
        try:
            with self._files().open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except (paramiko.SSHException, EOFError) as exc:
            raise SshUnreachableError(f"Reading {path} on {self.address} failed: {exc}") from exc

    def write_file(self, path: str, data: bytes | str, *, mode: int = 0o600) -> None:
        """Writes a file whole: readers see the old content or the new one, never part of it.

        The content goes to a temporary file in the same folder, which gets its mode before any data, and replaces the
        file by a rename.
        """
        sftp = self._files()
        folder, name = posixpath.split(path)
        temporary = posixpath.join(folder, f".{name}.{secrets.token_hex(6)}.tmp")
        payload = data.encode() if isinstance(data, str) else data
        try:
            with sftp.open(temporary, "wbx") as handle:
                sftp.chmod(temporary, mode)
                handle.write(payload)
            sftp.posix_rename(temporary, path)
        except BaseException:
            try:
                sftp.remove(temporary)
            except (OSError, paramiko.SSHException, EOFError):
                pass
            raise

    def upload_file(self, local_path: str, path: str, *, mode: int = 0o644) -> None:
        with open(local_path, "rb") as handle:
            self.write_file(path, handle.read(), mode=mode)

    def remove_file(self, path: str) -> bool:
        """Removes a file; False when there was none."""
        try:
            self._files().remove(path)
            return True
        except FileNotFoundError:
            return False

    def make_directory(self, path: str, *, mode: int = 0o700) -> None:
        """Creates a folder with this mode, or gives an existing one the mode."""
        sftp = self._files()
        try:
            attributes = sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path, mode)
            sftp.chmod(path, mode)
            return
        if not stat.S_ISDIR(attributes.st_mode or 0):
            raise SshError(f"{path} on {self.address} is not a folder")
        if stat.S_IMODE(attributes.st_mode or 0) != mode:
            sftp.chmod(path, mode)

    def list_directory(self, path: str) -> list[str]:
        try:
            return sorted(self._files().listdir(path))
        except FileNotFoundError:
            return []
