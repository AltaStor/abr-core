# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.plugins.pve import setup
from python.srv.plugins.pve.setup import (
    PveSetupError,
    authorized_key_line,
    ensure_package,
    known_host_keys,
    package_version,
    pve_major_version,
    restrict_key,
    set_up_cluster,
    tear_down,
    token_name,
    with_authorized_key,
    without_authorized_key,
)
from python.srv.plugins.pve.node_helper import FORCED_COMMAND, HELPER_COMMAND, helper_from_package, helper_version, version_key
from python.srv.plugins.pve.ssh import NodeShell, generate_key_pair
from python.tests.srv.pve.fake_helper import helper_text, make_package
from python.tests.srv.pve.fake_ssh import FakeSshNode, key_line

ROOT_KEYS = "ssh-rsa AAAAnode1 root@node1\nssh-rsa AAAAnode2 root@node2\n"


class FakeCluster:
    """The PVE commands the setup runs, answered from a small state: two nodes, both served by one fake SSH node."""

    def __init__(self, node: FakeSshNode) -> None:
        self.node = node
        self.version = "pve-manager/9.2.26/41a0e2e09b9e6f9d (running kernel: 7.0.14-28-pve)"
        self.roles: dict[str, str] = {"PVEAdmin": "..."}
        self.users = {"root@pam"}
        self.acl: list[tuple[str, str, str]] = []
        self.tokens: dict[str, str] = {}
        self.storages: dict[str, dict] = {}
        self.installed: str | None = None
        self.apt_fails = False
        self.created_tokens = 0
        self.helper_calls: list[list[str]] = []
        root = node.root
        for folder in ("etc/pve/priv", "etc/pve/nodes/node1", "etc/pve/nodes/node2", "root/.ssh", "var/tmp"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / "etc/pve/priv/authorized_keys").write_text(ROOT_KEYS)
        (root / "etc/pve/pve-root-ca.pem").write_text("-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n")
        host_key = key_line(node.host_keys[0])
        for name in ("node1", "node2"):
            (root / f"etc/pve/nodes/{name}/ssh_known_hosts").write_text(f"{name} {host_key}")
        node.handler = self.answer

    def answer(self, command: str, stdin: bytes):
        words = shlex.split(command)
        text = None
        if words[:3] == HELPER_COMMAND:
            return self.helper(words[3:], stdin)
        if words == ["pveversion"]:
            text = self.version
        elif words[:3] == ["pvesh", "get", "/cluster/status"]:
            text = json.dumps(
                [
                    {"type": "cluster", "name": "cluster1"},
                    {"type": "node", "name": "node1", "ip": "127.0.0.1", "online": 1, "local": 1},
                    {"type": "node", "name": "node2", "ip": "127.0.0.1", "online": 1, "local": 0},
                ]
            )
        elif words[:2] == ["printf", "%s"]:
            text = "127.0.0.1 51192 22"
        elif words[:2] == ["readlink", "-f"]:
            text = "/etc/pve/priv/authorized_keys"
        elif command.startswith("cat /etc/ssh/ssh_host_"):
            text = key_line(self.node.host_keys[0]) + " root@node1\n"
        elif words[:3] == ["pveum", "role", "list"]:
            text = json.dumps([{"roleid": role, "privs": privs} for role, privs in self.roles.items()])
        elif words[:3] in (["pveum", "role", "add"], ["pveum", "role", "modify"]):
            self.roles[words[3]] = words[5]
            text = ""
        elif words[:3] == ["pveum", "user", "list"]:
            text = json.dumps([{"userid": user} for user in sorted(self.users)])
        elif words[:3] == ["pveum", "user", "add"]:
            self.users.add(words[3])
            text = ""
        elif words[:3] == ["pveum", "acl", "modify"]:
            self.acl.append((words[3], words[5], words[7]))
            text = ""
        elif words[:4] == ["pveum", "user", "token", "list"]:
            if words[4] not in self.users:
                return 2, b"", b"no such user ('abr@pve')\n"
            text = json.dumps([{"tokenid": name} for name in self.tokens])
        elif words[:4] == ["pveum", "user", "token", "remove"]:
            self.tokens.pop(words[5])
            text = ""
        elif words[:4] == ["pveum", "user", "token", "add"]:
            self.created_tokens += 1
            self.tokens[words[5]] = f"secret-{self.created_tokens}"
            text = json.dumps({"full-tokenid": f"{words[4]}!{words[5]}", "value": self.tokens[words[5]]})
        elif words[:1] == ["dpkg-query"]:
            if self.installed is None:
                return 1, b"", b"dpkg-query: no packages found matching abr-pve-backup\n"
            text = f"install ok installed {self.installed}"
        elif command.startswith("DEBIAN_FRONTEND=noninteractive apt-get install"):
            if self.apt_fails:
                return 100, b"", b"W: some warning\nE: Unable to locate package nbdkit\n"
            self.installed = package_version(Path(words[-1]))
            text = ""
        elif words[:3] == ["pvesh", "get", "/storage"]:
            text = json.dumps([{"storage": name, **options} for name, options in self.storages.items()])
        elif words[:2] == ["pvesm", "add"]:
            self.storages[words[3]] = {"type": words[2], "content": words[5], "prune-backups": words[7]}
            text = ""
        elif words[:3] == ["pvesm", "set", "abr"]:
            options = self.storages["abr"]
            for flag, value in zip(words[3::2], words[4::2]):
                if flag == "--content":
                    options["content"] = value
                elif flag == "--delete":
                    for option in value.split(","):
                        options.pop(option, None)
            text = ""
        if text is None:
            return None
        return 0, (text + ("\n" if text and not text.endswith("\n") else "")).encode(), b""

    def authorized_keys(self) -> str:
        return (self.node.root / "etc/pve/priv/authorized_keys").read_text()

    def helper(self, words: list[str], stdin: bytes):
        """abr-node-helper as the fake node runs it, over the fake cluster's state."""
        path = self.node.root / "etc/pve/priv/abr-node-helper"
        if not path.exists():
            return 2, b"", b'Can\'t open perl script "/etc/pve/priv/abr-node-helper": No such file or directory\n'
        self.helper_calls.append(words)
        command, arguments = words[0], words[1:]

        def read(name: str) -> str | None:
            file = self.node.root / name
            return file.read_text() if file.exists() else None

        if command == "version":
            answer = {
                "helper": helper_version(path.read_bytes()),
                "package": self.installed,
                "restricted": 'command="' in self.authorized_keys(),
            }
        elif command == "update-helper":
            path.write_bytes(stdin)
            answer = {"helper": helper_version(stdin)}
        elif command == "package-status":
            current = self.installed is not None and version_key(self.installed) >= version_key(arguments[0])
            answer = {"installed": self.installed, "current": current}
        elif command == "install-package":
            if self.apt_fails:
                return 1, b"", b"W: some warning\nE: Unable to locate package nbdkit\n"
            package = self.node.root / "var/tmp/received.deb"
            package.write_bytes(stdin)
            self.installed = helper_version(helper_from_package(package))
            package.unlink()
            answer = {"installed": self.installed}
        elif command == "cluster-info":
            answer = {
                "members": read("etc/pve/.members")
                or json.dumps({"nodelist": {"node1": {"ip": "127.0.0.1"}, "node2": {"ip": "127.0.0.1"}}}),
                "root_ca": read("etc/pve/pve-root-ca.pem"),
                "nodes": {
                    name: {
                        "ssh_known_hosts": read(f"etc/pve/nodes/{name}/ssh_known_hosts"),
                        "pveproxy_ssl_pem": read(f"etc/pve/nodes/{name}/pveproxy-ssl.pem"),
                    }
                    for name in ("node1", "node2")
                },
                "host_keys": key_line(self.node.host_keys[0]) + " root@node1\n",
            }
        elif command == "remove-token":
            name = token_name(arguments[0])
            answer = {"removed": self.tokens.pop(name, None) is not None}
        elif command == "remove-key":
            current = self.authorized_keys()
            updated = without_authorized_key(current, arguments[0])
            (self.node.root / "etc/pve/priv/authorized_keys").write_text(updated)
            answer = {"removed": updated != current}
        else:
            return 2, b"", f"abr-node-helper: unknown command {command}\n".encode()
        return 0, (json.dumps(answer) + "\n").encode(), b""


class FakeApi:
    calls: list[tuple] = []

    def __init__(self, addresses, **options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def get(self, path):
        FakeApi.calls.append((path, self.options["token_id"], self.options["token_secret"]))
        return {"active": 1}


class SetupTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = folder / "node"
        self.root.mkdir()
        self.node = FakeSshNode(self.root)
        self.addCleanup(self.node.close)
        self.cluster = FakeCluster(self.node)
        self.package = folder / "abr-pve-backup_0.2.0_amd64.deb"
        make_package(self.package, "0.2.0")
        FakeApi.calls = []
        self.enterContext(patch.object(setup, "PveApi", FakeApi))

    def set_up(self, previous=None, **options):
        messages: list[str] = []
        result = set_up_cluster(
            inventory_id="inv-1234-5678",
            inventory_name="PVE-Cluster",
            address="127.0.0.1",
            root_password="root-password",
            ssh_port=self.node.port,
            previous=previous,
            package=options.pop("package", self.package),
            progress=messages.append,
            **options,
        )
        return result, messages

    def test_a_first_setup_leaves_a_key_a_token_the_package_and_the_storage(self):
        result, messages = self.set_up()
        pve = result["pve"]
        self.assertEqual(
            (result["cluster_name"], result["nodes"], result["warnings"]),
            ("cluster1", ["node1", "node2"], []),
        )
        self.assertEqual((pve["token_id"], result["token"]), ("abr@pve!altastor-inv12345678", "secret-1"))
        lines = self.cluster.authorized_keys().splitlines()
        self.assertEqual(lines[:2], ROOT_KEYS.splitlines())
        self.assertEqual(
            lines[2].split(" ssh-ed25519 ")[0], f'from="127.0.0.1",restrict,command="{FORCED_COMMAND}"'
        )
        self.assertTrue(lines[2].endswith(" altastor-inventory:inv-1234-5678"))
        self.assertEqual((self.root / "etc/pve/priv/abr-node-helper").read_bytes(), helper_text("0.2.0"))
        self.assertIn("Wrote AltaStor's node helper 0.2.0 to /etc/pve/priv/abr-node-helper", messages)
        self.assertEqual(self.cluster.roles["AltaStorBackup"], ",".join(setup.PRIVILEGES))
        self.assertEqual(self.cluster.acl, [("/", "abr@pve", "AltaStorBackup")])
        self.assertEqual(self.cluster.installed, "0.2.0")
        self.assertEqual(
            self.cluster.storages["abr"], {"type": "abr", "content": "backup", "prune-backups": "keep-all=1"}
        )
        self.assertEqual(pve["package_versions"], {"node1": "0.2.0", "node2": "0.2.0"})
        self.assertEqual([node["host_keys"] for node in pve["nodes"]], [[key_line(self.node.host_keys[0])]] * 2)
        self.assertEqual(len(pve["certificates"]), 1)
        self.assertEqual(
            FakeApi.calls, [("/nodes/node1/storage/abr/status", "abr@pve!altastor-inv12345678", "secret-1")]
        )
        self.assertEqual(list((self.root / "var/tmp").iterdir()), [])
        self.assertIn("Installing abr-pve-backup 0.2.0 on node node1", messages)
        # The key from the setup opens the node, which must present the pinned host key, and reaches the helper.
        with NodeShell(
            "127.0.0.1", port=self.node.port, private_key=pve["ssh_key"], host_keys=pve["nodes"][0]["host_keys"]
        ) as shell:
            self.assertEqual(shell.helper.version()["helper"], "0.2.0")

    def test_setting_up_again_keeps_the_key_and_pins_and_replaces_the_token(self):
        first, _ = self.set_up()
        self.cluster.storages["abr"]["nodes"] = "node1"
        again, messages = self.set_up(previous=first["pve"])
        self.assertEqual(again["pve"]["ssh_key"], first["pve"]["ssh_key"])
        self.assertEqual(again["token"], "secret-2")
        self.assertEqual(list(self.cluster.tokens), ["altastor-inv12345678"])
        self.assertEqual(self.cluster.authorized_keys().count("altastor-inventory:inv-1234-5678"), 1)
        self.assertEqual(self.cluster.acl, [("/", "abr@pve", "AltaStorBackup")] * 2)
        self.assertNotIn("nodes", self.cluster.storages["abr"])
        self.assertIn("Changed: the abr storage is available on every node", messages)
        self.assertFalse([message for message in messages if message.startswith("Installing")])

    def test_an_older_pve_is_refused_before_anything_changes(self):
        self.cluster.version = "pve-manager/8.4.1/2a5fa54a8503f96d (running kernel: 6.8.12-9-pve)"
        with self.assertRaisesRegex(PveSetupError, "runs Proxmox VE 8; AltaStor supports Proxmox VE 9"):
            self.set_up()
        self.assertEqual(self.cluster.authorized_keys(), ROOT_KEYS)
        self.assertFalse([command for command in self.node.commands if command.startswith("pveum")])

    def test_a_cluster_registered_already_is_refused_before_anything_changes(self):
        seen = []

        def refuse(cluster, nodes):
            seen.append((cluster, nodes))
            raise RuntimeError("registered already")

        with self.assertRaisesRegex(RuntimeError, "registered already"):
            self.set_up(check_new=refuse)
        self.assertEqual(seen, [("cluster1", ["node1", "node2"])])
        self.assertEqual(self.cluster.authorized_keys(), ROOT_KEYS)
        self.assertEqual(self.cluster.tokens, {})

    def test_a_failed_first_setup_takes_back_its_key_and_token(self):
        self.cluster.apt_fails = True
        with self.assertRaisesRegex(
            PveSetupError,
            "apt could not install abr-pve-backup 0.2.0 on node node1: E: Unable to locate package nbdkit",
        ):
            self.set_up()
        self.assertEqual(self.cluster.authorized_keys(), ROOT_KEYS)
        self.assertEqual(self.cluster.tokens, {})
        self.assertNotIn("abr", self.cluster.storages)

    def test_a_failed_setup_again_keeps_what_worked_before(self):
        first, _ = self.set_up()
        self.cluster.installed = "0.1.0"
        self.cluster.apt_fails = True
        with self.assertRaises(PveSetupError):
            self.set_up(previous=first["pve"])
        self.assertIn("altastor-inventory:inv-1234-5678", self.cluster.authorized_keys())

    def test_a_storage_of_another_type_named_abr_is_refused(self):
        self.cluster.storages["abr"] = {"type": "dir", "content": "backup"}
        with self.assertRaisesRegex(PveSetupError, "has a storage named abr of type dir"):
            self.set_up()

    def test_a_node_with_the_package_and_no_package_here_is_enough(self):
        self.cluster.installed = "0.3.0"
        (self.root / "usr/lib/abr").mkdir(parents=True)
        (self.root / "usr/lib/abr/abr-node-helper").write_bytes(helper_text("0.3.0"))
        result, messages = self.set_up(package=None)
        self.assertEqual(result["pve"]["package_versions"], {"node1": "0.3.0", "node2": "0.3.0"})
        self.assertIn("Wrote AltaStor's node helper 0.3.0 to /etc/pve/priv/abr-node-helper", messages)

    def test_a_node_without_the_package_and_no_package_here_fails(self):
        with self.assertRaisesRegex(
            PveSetupError, "127.0.0.1 has no abr-pve-backup package, and this appliance has none either"
        ):
            self.set_up(package=None)
        self.assertEqual(self.cluster.authorized_keys(), ROOT_KEYS)

    def test_tearing_down_removes_the_inventorys_token_and_key_only(self):
        result, _ = self.set_up()
        self.cluster.tokens["altastor-other"] = "x"
        with NodeShell(
            "127.0.0.1",
            port=self.node.port,
            private_key=result["pve"]["ssh_key"],
            host_keys=result["pve"]["nodes"][0]["host_keys"],
        ) as shell:
            removed = tear_down(shell, "inv-1234-5678")
        self.assertEqual(
            removed, ["API token abr@pve!altastor-inv12345678", "SSH key in /etc/pve/priv/authorized_keys"]
        )
        self.assertEqual(list(self.cluster.tokens), ["altastor-other"])
        self.assertEqual(self.cluster.authorized_keys(), ROOT_KEYS)

    def test_a_newer_package_on_a_node_stays(self):
        self.cluster.installed = "0.3.0"
        (self.root / "etc/pve/priv/abr-node-helper").write_bytes(helper_text("0.3.0"))
        with NodeShell("127.0.0.1", port=self.node.port, password="root-password") as shell:
            self.assertEqual(ensure_package(shell, "node1", self.package), "0.3.0")
        self.assertFalse([words for words in self.cluster.helper_calls if words[0] in ("install-package", "update-helper")])
        self.assertEqual((self.root / "etc/pve/priv/abr-node-helper").read_bytes(), helper_text("0.3.0"))

    def test_a_newer_package_brings_its_helper_to_the_cluster(self):
        self.cluster.installed = "0.1.0"
        (self.root / "etc/pve/priv/abr-node-helper").write_bytes(helper_text("0.1.0"))
        with NodeShell("127.0.0.1", port=self.node.port, password="root-password") as shell:
            self.assertEqual(ensure_package(shell, "node1", self.package), "0.2.0")
        self.assertEqual([words[0] for words in self.cluster.helper_calls if words[0] != "version"],
                         ["update-helper", "package-status", "install-package"])
        self.assertEqual((self.root / "etc/pve/priv/abr-node-helper").read_bytes(), helper_text("0.2.0"))

    def test_a_key_from_before_the_helper_is_moved_onto_it(self):
        private, public = generate_key_pair("")
        old = f'from="127.0.0.1",restrict {public} altastor-inventory:inv-1234-5678\n'
        (self.root / "etc/pve/priv/authorized_keys").write_text(ROOT_KEYS + old)
        with NodeShell("127.0.0.1", port=self.node.port, private_key=private) as shell:
            moved = restrict_key(
                shell, ssh_key=private, source="127.0.0.1", inventory_id="inv-1234-5678", package=self.package
            )
            again = restrict_key(
                shell, ssh_key=private, source="127.0.0.1", inventory_id="inv-1234-5678", package=self.package
            )
        self.assertEqual((moved, again), (True, False))
        lines = self.cluster.authorized_keys().splitlines()
        self.assertEqual(lines[:2], ROOT_KEYS.splitlines())
        self.assertEqual(lines[2], authorized_key_line(private, "127.0.0.1", "inv-1234-5678"))
        self.assertEqual((self.root / "etc/pve/priv/abr-node-helper").read_bytes(), helper_text("0.2.0"))


class LineTests(unittest.TestCase):
    def test_the_key_line_limits_the_key_to_abr_cores_address(self):
        private, public = generate_key_pair("")
        line = authorized_key_line(private, "backup.example.com", "inv-1")
        command = 'command="/usr/bin/perl -T /etc/pve/priv/abr-node-helper"'
        self.assertEqual(line, f'from="backup.example.com",restrict,{command} {public} altastor-inventory:inv-1')
        self.assertTrue(authorized_key_line(private, "", "inv-1").startswith(f"restrict,{command} ssh-ed25519 "))

    def test_an_inventorys_line_replaces_its_earlier_one_and_leaves_the_rest(self):
        text = (
            ROOT_KEYS
            + "restrict ssh-ed25519 OLD altastor-inventory:inv-1\nrestrict ssh-ed25519 KEEP altastor-inventory:inv-12\n"
        )
        updated = with_authorized_key(text, "restrict ssh-ed25519 NEW altastor-inventory:inv-1", "inv-1")
        self.assertEqual(
            updated.splitlines(),
            ROOT_KEYS.splitlines()
            + [
                "restrict ssh-ed25519 KEEP altastor-inventory:inv-12",
                "restrict ssh-ed25519 NEW altastor-inventory:inv-1",
            ],
        )
        self.assertEqual(
            without_authorized_key(updated, "inv-1").splitlines()[-1],
            "restrict ssh-ed25519 KEEP altastor-inventory:inv-12",
        )
        self.assertEqual(
            with_authorized_key("ssh-rsa AAAA root", "L altastor-inventory:i", "i"),
            "ssh-rsa AAAA root\nL altastor-inventory:i\n",
        )

    def test_host_keys_come_from_known_hosts_and_pub_files(self):
        text = (
            "node2 ssh-rsa AAAAB3 \n"
            "|1|hash= ecdsa-sha2-nistp256 AAAAE2\n"
            "ssh-ed25519 AAAAC3 root@node2\n"
            "\n"
            "not a key\n"
        )
        self.assertEqual(known_host_keys(text), ["ssh-rsa AAAAB3", "ecdsa-sha2-nistp256 AAAAE2", "ssh-ed25519 AAAAC3"])

    def test_names_and_versions(self):
        self.assertEqual(pve_major_version("pve-manager/9.2.26/41a0e2e09b9e6f9d (running kernel: 7.0.14-28-pve)"), 9)
        self.assertIsNone(pve_major_version("bash: pveversion: command not found"))
        self.assertEqual(token_name("abb9c27b-7655-47bf-9869-e38d2734a9f0"), "altastor-abb9c27b7655")
        self.assertEqual(package_version(Path("abr-pve-backup_0.1.0+g1a2b_amd64.deb")), "0.1.0+g1a2b")
        with self.assertRaisesRegex(PveSetupError, "is not named as the abr-pve-backup package is"):
            package_version(Path("abr-pve-backup.deb"))


if __name__ == "__main__":
    unittest.main()
