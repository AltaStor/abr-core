# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from python.srv.brokers import shares_broker

SMB_CONF_TEXT = "[global]\n   workgroup = WORKGROUP\n   server role = standalone server\n\n[printers]\n   comment = All Printers\n"
PARAMETERS = {"server role": "ROLE_STANDALONE", "config backend": "file", "passdb backend": "tdbsam"}


@dataclass
class FakeSystem:
    root: Path
    running: bool = True
    valid: bool = True
    parameters: dict[str, str] = field(default_factory=lambda: dict(PARAMETERS))
    accounts: dict[str, str] = field(default_factory=dict)
    samba_accounts: dict[str, str] = field(default_factory=dict)
    enabled: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    failing: set[str] = field(default_factory=set)
    invalid_after_write: bool = False
    stop_on_reload: bool = False
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)
    owners: list[tuple[str, int, int]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def smb_conf(self) -> Path:
        return self.root / "etc" / "samba" / "smb.conf"

    @property
    def shares_conf(self) -> Path:
        return self.root / "etc" / "altastor-backup" / "samba-shares.conf"

    def samba(self, service_user: str | None = None) -> shares_broker.Samba:
        return shares_broker.Samba(
            service_user=service_user or current_user(),
            smb_conf=self.smb_conf,
            shares_conf=self.shares_conf,
            lock_file=self.root / "run" / "lock" / "shares.lock",
            run=self.run,
            which=lambda tool: None if tool in self.missing else f"/usr/bin/{tool}",
            chown=lambda path, uid, gid: self.owners.append((path, uid, gid)),
        )

    def configuration(self) -> str:
        text = self.smb_conf.read_text() if self.smb_conf.is_file() else ""
        include = f"include = {self.shares_conf}"
        if include in text.splitlines() and self.shares_conf.is_file():
            text += "\n" + self.shares_conf.read_text()
        return text

    def answer(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def run(self, argv, *, input=None, **_kwargs):
        with self.lock:
            self.calls.append((list(argv), input))
            tool = argv[0]
            if tool in self.failing:
                return self.answer(1, stderr=f"{tool}: simulated failure\n")
            return self._run(tool, list(argv[1:]), input)

    def _run(self, tool: str, arguments: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if tool == "systemctl":
            return self.answer(0 if self.running else 3)
        if tool == "testparm":
            return self._testparm(arguments)
        if tool == "getent":
            user = arguments[-1]
            if user not in self.accounts:
                return self.answer(2)
            return self.answer(0, f"{user}:x:999:65534:{self.accounts[user]}:/nonexistent:/usr/sbin/nologin\n")
        if tool == "useradd":
            self.accounts[arguments[-1]] = arguments[arguments.index("--comment") + 1]
            return self.answer()
        if tool == "userdel":
            self.accounts.pop(arguments[-1], None)
            return self.answer()
        if tool == "smbpasswd":
            return self._smbpasswd(arguments, stdin)
        if tool == "pdbedit":
            if "-x" in arguments:
                self.samba_accounts.pop(arguments[-1], None)
                self.enabled.discard(arguments[-1])
            return self.answer(0 if arguments[-1] in self.samba_accounts or "-x" in arguments else 255)
        if tool == "smbcontrol":
            if self.stop_on_reload:
                self.running = False
            return self.answer()
        return self.answer(127, stderr=f"{tool}: not simulated\n")

    def _testparm(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        written = self.shares_conf.is_file() and f"include = {self.shares_conf}" in self.smb_conf.read_text()
        if not self.valid or (self.invalid_after_write and written):
            return self.answer(1, stderr="Error loading services.\n")
        for argument in arguments:
            if argument.startswith("--parameter-name="):
                return self.answer(0, self.parameters.get(argument.partition("=")[2], "") + "\n")
        return self.answer(0, self.configuration())

    def _smbpasswd(self, arguments: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        user = arguments[-1]
        if "-a" in arguments:
            first, _second = (stdin or "\n").splitlines()[:2]
            self.samba_accounts[user] = first
        elif "-e" in arguments:
            self.enabled.add(user)
        elif "-x" in arguments:
            self.samba_accounts.pop(user, None)
            self.enabled.discard(user)
        return self.answer()


def current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def make_system() -> tuple[FakeSystem, tempfile.TemporaryDirectory]:
    folder = tempfile.TemporaryDirectory(prefix="abr-shares-broker-")
    system = FakeSystem(Path(folder.name))
    system.smb_conf.parent.mkdir(parents=True)
    system.smb_conf.write_text(SMB_CONF_TEXT)
    os.chmod(system.smb_conf, 0o644)
    return system, folder
