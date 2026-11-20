# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import hashlib
import socket
import stat
import tempfile
import unittest
from pathlib import Path

import paramiko

from python.srv.plugins.pve.ssh import (
    NodeShell,
    SshAuthError,
    SshCommandError,
    SshHostKeyError,
    SshUnreachableError,
    generate_key_pair,
    key_fingerprint,
    public_key_line,
)
from python.tests.srv.pve.fake_ssh import FakeSshNode, ed25519_host_key, key_line


class KeyTests(unittest.TestCase):
    def test_a_new_key_pair_is_ed25519_with_its_comment(self):
        private, public = generate_key_pair("AltaStor pve-cluster")
        self.assertTrue(private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertTrue(public.startswith("ssh-ed25519 "))
        self.assertTrue(public.endswith(" AltaStor pve-cluster"))
        self.assertEqual(public_key_line(private, "AltaStor pve-cluster"), public)

    def test_a_fingerprint_reads_as_ssh_keygen_shows_it(self):
        _private, public = generate_key_pair("")
        blob = base64.b64decode(public.split()[1])
        expected = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual(key_fingerprint(public), f"SHA256:{expected}")


class NodeShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa = paramiko.RSAKey.generate(2048)
        cls.ed25519 = ed25519_host_key()

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.node = FakeSshNode(self.root, host_keys=[self.ed25519, self.rsa])
        self.addCleanup(self.node.close)

    def shell(self, **options) -> NodeShell:
        options.setdefault("password", "root-password")
        shell = NodeShell("127.0.0.1", port=self.node.port, **options)
        self.addCleanup(shell.close)
        return shell

    # ---- host keys and login ----------------------------------------------------------------------------------------

    def test_the_first_connection_takes_the_key_the_node_presents(self):
        shell = self.shell()
        shell.connect()
        self.assertEqual(shell.host_key, key_line(self.ed25519))
        self.assertEqual(self.node.logins, ["password"])

    def test_a_pinned_key_is_the_one_the_node_presents(self):
        shell = self.shell(host_keys=[key_line(self.rsa) + " node2"])
        shell.connect()
        self.assertEqual(shell.host_key, key_line(self.rsa))

    def test_a_node_without_the_pinned_key_is_refused_before_login(self):
        other = paramiko.RSAKey.generate(2048)
        with self.assertRaisesRegex(
            SshHostKeyError, r"The SSH host key of 127.0.0.1 \(SHA256:.*\) is not the one AltaStor pinned"
        ):
            self.shell(host_keys=[key_line(other)]).connect()
        self.assertEqual(self.node.logins, [])

    def test_the_inventory_key_logs_in_once_it_is_authorized(self):
        private, public = generate_key_pair("AltaStor")
        with self.assertRaisesRegex(SshAuthError, "refused AltaStor's SSH key"):
            self.shell(password=None, private_key=private).connect()
        self.node.authorized_keys.append(" ".join(public.split()[:2]))
        self.shell(password=None, private_key=private).connect()
        self.assertEqual(self.node.logins, ["key"])

    def test_a_wrong_password_is_refused(self):
        with self.assertRaisesRegex(SshAuthError, "refused this password"):
            self.shell(password="wrong").connect()

    def test_a_closed_port_is_unreachable(self):
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]
        with self.assertRaisesRegex(SshUnreachableError, f"SSH to 127.0.0.1:{port} cannot be reached"):
            NodeShell("127.0.0.1", port=port, password="x").connect()

    # ---- commands ---------------------------------------------------------------------------------------------------

    def test_a_command_gets_its_input_and_returns_output_and_status(self):
        shell = self.shell()
        self.assertEqual(shell.run("wc -c", input=b"x" * 300000).text.strip(), "300000")
        result = shell.run("echo out; echo err >&2; exit 3", check=False)
        self.assertEqual((result.status, result.text, result.error_text), (3, "out\n", "err\n"))

    def test_a_list_of_arguments_is_quoted_for_the_shell(self):
        shell = self.shell()
        self.assertEqual(shell.run(["printf", "%s|", "a b", "$HOME", "it's"]).text, "a b|$HOME|it's|")

    def test_a_failed_command_says_its_last_error_line(self):
        with self.assertRaises(SshCommandError) as raised:
            self.shell().run("echo first >&2; echo 'no such storage' >&2; exit 2")
        self.assertEqual(raised.exception.status, 2)
        self.assertRegex(str(raised.exception), r"failed with exit status 2: no such storage$")

    def test_a_shell_connects_again_when_the_node_dropped_its_connection(self):
        shell = self.shell()
        (self.root / "result.json").write_text("{}")
        shell.run("true")
        self.assertEqual(shell.read_file("/result.json"), b"{}")
        for transport in self.node._transports:
            transport.close()
        self.assertEqual(shell.run("echo again").text, "again\n")
        for transport in self.node._transports:
            transport.close()
        self.assertEqual(shell.read_file("/result.json"), b"{}")
        self.assertEqual(self.node.logins, ["password"] * 3)

    # ---- files ------------------------------------------------------------------------------------------------------

    def test_a_file_is_written_whole_with_its_mode(self):
        shell = self.shell()
        (self.root / "run").mkdir()
        shell.make_directory("/run/abr", mode=0o700)
        shell.write_file("/run/abr/backup-9002.json", '{"version": 1}', mode=0o600)
        shell.write_file("/run/abr/backup-9002.json", '{"version": 2}', mode=0o600)
        written = self.root / "run" / "abr" / "backup-9002.json"
        self.assertEqual(written.read_text(), '{"version": 2}')
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "run" / "abr").stat().st_mode), 0o700)
        self.assertEqual(shell.list_directory("/run/abr"), ["backup-9002.json"])

    def test_reading_and_removing_a_missing_file_is_not_an_error(self):
        shell = self.shell()
        (self.root / "result.json").write_text("{}")
        self.assertEqual(shell.read_file("/result.json"), b"{}")
        self.assertTrue(shell.remove_file("/result.json"))
        self.assertIsNone(shell.read_file("/result.json"))
        self.assertFalse(shell.remove_file("/result.json"))
        self.assertEqual(shell.list_directory("/run/abr"), [])

    def test_an_existing_folder_gets_the_mode(self):
        (self.root / "run" / "abr").mkdir(parents=True, mode=0o755)
        self.shell().make_directory("/run/abr", mode=0o700)
        self.assertEqual(stat.S_IMODE((self.root / "run" / "abr").stat().st_mode), 0o700)

    def test_a_failed_write_leaves_no_temporary_file(self):
        shell = self.shell()
        (self.root / "run").mkdir()
        with self.assertRaises(OSError):
            shell.write_file("/run/missing/backup-9002.json", "{}")
        (self.root / "run" / "abr").mkdir()
        (self.root / "run" / "abr" / "taken").mkdir()
        with self.assertRaises(OSError):
            shell.write_file("/run/abr/taken", "{}")
        self.assertEqual(sorted(path.name for path in (self.root / "run" / "abr").iterdir()), ["taken"])


if __name__ == "__main__":
    unittest.main()
