# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""A fake abr-node-helper for the session tests, over the fake shell's own state: run files go to its written, result
files come from its files, removals go to its removed, and offered secrets to its offered, with each offer in offers.
"""

from __future__ import annotations

import json


class FakeOffer:
    def __init__(self) -> None:
        self.closed = False
        self.state = "ready"

    def poll(self) -> str:
        return self.state

    def close(self) -> None:
        self.closed = True


class FakeHelper:
    def __init__(self, shell) -> None:
        self.shell = shell

    def put_run(self, name: str, text: str) -> None:
        self.shell.written[f"/run/abr/{name}"] = json.loads(text)

    def get_result(self, name: str) -> bytes | None:
        return self.shell.files.get(f"/run/abr/{name}")

    def remove_run(self, base: str) -> list[str]:
        removed = []
        for suffix in ("json", "result.json", "log"):
            path = f"/run/abr/{base}.{suffix}"
            self.shell.removed.append(path)
            if self.shell.files.pop(path, None) is not None:
                removed.append(path)
        return removed

    def offer_secrets(self, base: str, secrets: dict[str, str]) -> FakeOffer:
        offer = FakeOffer()
        self.shell.offered[base] = dict(secrets)
        self.shell.offers.append(offer)
        return offer

    def nbd_devices(self) -> tuple[int, int]:
        numbers = [int(word) for word in str(getattr(self.shell, "nbd", "16 0")).split() if word.isdigit()]
        return (numbers[0], numbers[1]) if len(numbers) == 2 else (0, 0)


def helper_text(version: str) -> bytes:
    """A stand-in for abr-node-helper, as the package carries it: what abr-core reads of it is its version."""
    return (
        "#!/usr/bin/perl -T\n# abr-node-helper: a stand-in for tests\nuse strict;\n"
        f"our $VERSION = '{version}';\n"
    ).encode()


def make_package(path, version: str, helper_version: str | None = None) -> None:
    """Writes a Debian package as abr-pve-backup is built: an ar archive with debian-binary, control.tar.xz and a
    data.tar.xz that holds usr/lib/abr/abr-node-helper."""
    import io
    import tarfile

    def tar_xz(files: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def member(name: str, data: bytes) -> bytes:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        return header + data + (b"\n" if len(data) % 2 else b"")

    control = f"Package: abr-pve-backup\nVersion: {version}\nArchitecture: amd64\n".encode()
    data = tar_xz({"./usr/lib/abr/abr-node-helper": helper_text(helper_version or version)})
    path.write_bytes(
        b"!<arch>\n"
        + member("debian-binary", b"2.0\n")
        + member("control.tar.xz", tar_xz({"./control": control}))
        + member("data.tar.xz", data)
    )
