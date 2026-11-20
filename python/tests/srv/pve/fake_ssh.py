# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""A fake PVE node over SSH, for tests: paramiko's server side on a local port.

It runs commands with sh in a scratch folder, or through a handler a test gives, and serves that folder over SFTP as
its root, so /run/abr on the fake node is <folder>/run/abr. It records every command it runs.
"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Callable

import paramiko

from python.srv.plugins.pve.ssh import generate_key_pair

Handler = Callable[[str, bytes], tuple[int, bytes, bytes] | None]


class _Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root: Path, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root

    def _path(self, path: str) -> str:
        return str(self.root) + self.canonicalize(path)

    def _call(self, action, *args):
        try:
            action(*args)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def list_folder(self, path):
        try:
            folder = self._path(path)
            return [
                paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(folder, name)), name) for name in os.listdir(folder)
            ]
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._path(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        real = self._path(path)
        try:
            descriptor = os.open(real, flags, 0o666)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        handle = paramiko.SFTPHandle(flags)
        handle.filename = real
        handle.readfile = handle.writefile = os.fdopen(descriptor, mode)
        return handle

    def remove(self, path):
        return self._call(os.remove, self._path(path))

    def rename(self, oldpath, newpath):
        return self._call(os.rename, self._path(oldpath), self._path(newpath))

    posix_rename = rename

    def mkdir(self, path, attr):
        return self._call(os.mkdir, self._path(path), 0o777)

    def rmdir(self, path):
        return self._call(os.rmdir, self._path(path))

    def chattr(self, path, attr):
        return self._call(paramiko.SFTPServer.set_file_attr, self._path(path), attr)


class FakeSshNode(paramiko.ServerInterface):
    def __init__(
        self,
        root: Path,
        *,
        password: str = "root-password",
        host_keys: list[paramiko.PKey] | None = None,
        handler: Handler | None = None,
    ) -> None:
        self.root = root
        self.password = password
        self.host_keys = host_keys or [ed25519_host_key()]
        self.authorized_keys: list[str] = []
        self.handler = handler
        self.commands: list[str] = []
        self.logins: list[str] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._transports: list[paramiko.Transport] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._listener.close()
        for transport in self._transports:
            transport.close()

    def _accept(self) -> None:
        while True:
            try:
                connection, _address = self._listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(connection)
            for key in self.host_keys:
                transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _Files, root=self.root)
            self._transports.append(transport)
            try:
                transport.start_server(server=self)
            except (paramiko.SSHException, EOFError):
                continue

    # ---- authentication ---------------------------------------------------------------------------------------------

    def get_allowed_auths(self, username):
        return "publickey,password"

    def check_auth_password(self, username, password):
        if username == "root" and password == self.password:
            self.logins.append("password")
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if username == "root" and f"{key.get_name()} {key.get_base64()}" in self.authorized_keys:
            self.logins.append("key")
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    # ---- sessions ---------------------------------------------------------------------------------------------------

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self._execute, args=(channel, command.decode()), daemon=True).start()
        return True

    def _execute(self, channel: paramiko.Channel, command: str) -> None:
        self.commands.append(command)
        stdin = bytearray()
        while True:
            chunk = channel.recv(65536)
            if not chunk:
                break
            stdin += chunk
        answer = self.handler(command, bytes(stdin)) if self.handler else None
        if answer is None:
            process = subprocess.run(["sh", "-c", command], input=bytes(stdin), capture_output=True, cwd=self.root)
            answer = (process.returncode, process.stdout, process.stderr)
        status, stdout, stderr = answer
        channel.sendall(stdout)
        channel.sendall_stderr(stderr)
        channel.send_exit_status(status)
        channel.close()


def ed25519_host_key() -> paramiko.PKey:
    return paramiko.Ed25519Key.from_private_key(io.StringIO(generate_key_pair("host")[0]))


def key_line(key: paramiko.PKey) -> str:
    return f"{key.get_name()} {key.get_base64()}"
