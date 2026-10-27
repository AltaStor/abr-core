# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from python.srv.plugins.hyperv import hyperv, vhdx_tool
from python.srv.plugins.hyperv.hyperv import HyperVNode


class _Client:
    def __init__(self) -> None:
        self.copied: list[tuple[str, str]] = []

    def copy(self, src: str, dest: str) -> None:
        self.copied.append((src, dest))


class _Host:
    def __init__(self, folders: dict[str, dict[str, str]] | None = None) -> None:
        self.folders = folders or {}
        self.calls: list[tuple[str, dict, bool]] = []

    def run_script(self, name, args, node=None, *, depth=6, inline=False):
        self.calls.append((name, args, inline))
        if name == "bundle_check.ps1":
            present = self.folders.get(args["folder"], {})
            mismatched = [f["name"] for f in args["expected"] if present.get(f["name"]) != f["sha256"]]
            return [{"exists": args["folder"] in self.folders, "complete": not mismatched, "mismatched": mismatched}]
        if name == "stage_bundle.ps1":
            return [{"staging": args["staging"], "written": len(args["files"])}]
        if name == "finalize_bundle.ps1":
            self.folders[args["folder"]] = {f["name"]: f["sha256"] for f in args["expected"]}
            return [{"status": "deployed", "folder": args["folder"]}]
        if name == "tool_info.ps1":
            return [{"exists": True, "sha256": "x", "version": "1.0"}]
        return []

    def names(self) -> list[str]:
        return [name for name, _args, _inline in self.calls]


class HostBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = pathlib.Path(tempfile.mkdtemp(prefix="abr-bundle-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self._dir, ignore_errors=True))
        self.scripts = self._dir / "scripts"
        self.scripts.mkdir()
        for name in ("common.ps1", "run_tool.ps1", "tool_info.ps1"):
            (self.scripts / name).write_text(f"# {name}\n", encoding="utf-8")
        self.tool = self._dir / "abr-vhdx-tool.exe"
        self.tool.write_bytes(b"MZ tool build 1")
        for patcher in (
            patch.object(hyperv, "SCRIPT_DIR", self.scripts),
            patch.object(vhdx_tool, "local_tool_path", side_effect=lambda: self.tool if self.tool.exists() else None),
            patch.dict("os.environ", {"ALTASTOR_HYPERV_WORK_DIR": r"C:\work"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _node(self, host: _Host) -> tuple[HyperVNode, _Client]:
        node = HyperVNode("hv.local", "CORP\\svc", "secret")
        client = _Client()
        node._client = client
        for patcher in (
            patch.object(node, "_run_script", side_effect=host.run_script),
            patch.object(node, "_client_for", side_effect=lambda _node: node._client),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return node, client

    def test_the_bundle_is_named_by_the_sha256_of_its_files_and_changes_with_any_of_them(self) -> None:
        first = vhdx_tool.host_bundle(self.scripts)
        self.assertEqual(first, vhdx_tool.host_bundle(self.scripts))
        self.assertEqual(len(first.bundle_id), 64)
        self.assertEqual({name for name, _path, _digest in first.files}, {"abr-vhdx-tool.exe", "scripts\\common.ps1", "scripts\\run_tool.ps1", "scripts\\tool_info.ps1"})
        self.assertEqual(first.folder(vhdx_tool.remote_work_dir()), "C:\\work\\" + first.bundle_id)

        (self.scripts / "run_tool.ps1").write_text("# run_tool.ps1 v2\n", encoding="utf-8")
        second = vhdx_tool.host_bundle(self.scripts)
        self.assertNotEqual(second.bundle_id, first.bundle_id)

        self.tool.write_bytes(b"MZ tool build 2, a later one")
        self.assertNotEqual(vhdx_tool.host_bundle(self.scripts).bundle_id, second.bundle_id)

    def test_a_host_without_the_bundle_gets_it_uploaded_once(self) -> None:
        host = _Host()
        node, client = self._node(host)
        folder = vhdx_tool.host_bundle(self.scripts).folder(r"C:\work")

        self.assertEqual(node.ensure_host_bundle("hv01"), folder)
        self.assertEqual(node.ensure_host_bundle("hv01"), folder)

        self.assertEqual(host.names(), ["bundle_check.ps1", "stage_bundle.ps1", "finalize_bundle.ps1"])
        self.assertTrue(all(inline for _name, _args, inline in host.calls))
        staged = host.calls[1][1]
        self.assertEqual(sorted(f["name"] for f in staged["files"]), ["scripts\\common.ps1", "scripts\\run_tool.ps1", "scripts\\tool_info.ps1"])
        self.assertTrue(staged["staging"].startswith("C:\\work\\.staging-"))
        self.assertEqual(client.copied, [(str(self.tool.resolve()), staged["staging"] + "\\abr-vhdx-tool.exe")])
        self.assertEqual(host.calls[2][1]["folder"], folder)

    def test_a_host_that_already_holds_the_bundle_gets_nothing_uploaded(self) -> None:
        bundle = vhdx_tool.host_bundle(self.scripts)
        folder = bundle.folder(r"C:\work")
        host = _Host({folder: {name: digest for name, _path, digest in bundle.files}})
        node, client = self._node(host)

        self.assertEqual(node.ensure_host_bundle("hv01"), folder)

        self.assertEqual(host.names(), ["bundle_check.ps1"])
        self.assertEqual(client.copied, [])

    def test_a_bundle_with_a_changed_file_on_the_host_is_uploaded_again(self) -> None:
        bundle = vhdx_tool.host_bundle(self.scripts)
        folder = bundle.folder(r"C:\work")
        files = {name: digest for name, _path, digest in bundle.files}
        files["abr-vhdx-tool.exe"] = "0" * 64
        host = _Host({folder: files})
        node, client = self._node(host)

        node.ensure_host_bundle("hv01")

        self.assertEqual(host.names(), ["bundle_check.ps1", "stage_bundle.ps1", "finalize_bundle.ps1"])
        self.assertEqual(len(client.copied), 1)

    def test_another_version_goes_to_its_own_folder_and_leaves_the_first_alone(self) -> None:
        host = _Host()
        node, _client = self._node(host)
        first = node.ensure_host_bundle("hv01")
        (self.scripts / "common.ps1").write_text("# common.ps1 v2\n", encoding="utf-8")

        other, _ = self._node(host)
        second = other.ensure_host_bundle("hv01")

        self.assertNotEqual(first, second)
        self.assertIn(first, host.folders)
        self.assertIn(second, host.folders)

    def test_the_tool_runs_from_the_bundle_and_a_reconnect_checks_the_host_again(self) -> None:
        host = _Host()
        node, _client = self._node(host)
        folder = vhdx_tool.host_bundle(self.scripts).folder(r"C:\work")

        self.assertEqual(node.ensure_disk_tool("hv01"), folder + "\\abr-vhdx-tool.exe")
        self.assertEqual(host.calls[-1][0], "tool_info.ps1")
        self.assertEqual(host.calls[-1][1], {"path": folder + "\\abr-vhdx-tool.exe"})

        node.disconnect()
        node._client = _Client()
        node.ensure_disk_tool("hv01")
        self.assertEqual(host.names().count("bundle_check.ps1"), 2)

    def test_without_a_tool_on_the_appliance_the_hosts_own_install_is_used(self) -> None:
        self.tool.unlink()
        host = _Host()
        node, client = self._node(host)

        with patch.dict("os.environ", {"ALTASTOR_HYPERV_TOOL_PATH": r"C:\Program Files\AltaStor\abr-vhdx-tool.exe"}):
            path = node.ensure_disk_tool("hv01")

        self.assertEqual(path, r"C:\Program Files\AltaStor\abr-vhdx-tool.exe")
        self.assertEqual(client.copied, [])


class ScriptSourceTests(unittest.TestCase):
    def _sent(self, node: HyperVNode, **kwargs) -> str:
        with patch.object(node, "_run_json", return_value=[]) as run_json:
            node._run_script("tool_info.ps1", {"path": "x"}, "hv01", **kwargs)
        return run_json.call_args.args[0]

    def test_scripts_are_sent_whole_until_the_node_holds_the_bundle(self) -> None:
        node = HyperVNode("hv.local", "CORP\\svc", "secret")
        sent = self._sent(node)
        self.assertIn("Get-FileHash -LiteralPath $path -Algorithm SHA256", sent)
        self.assertNotIn("ReadAllText", sent)

    def test_once_the_node_holds_the_bundle_scripts_are_read_from_it(self) -> None:
        node = HyperVNode("hv.local", "CORP\\svc", "secret")
        node._bundles["hv01"] = r"C:\work\abc"
        sent = self._sent(node)
        self.assertIn("ReadAllText('C:\\work\\abc\\scripts\\common.ps1')", sent)
        self.assertIn("ReadAllText('C:\\work\\abc\\scripts\\tool_info.ps1')", sent)
        self.assertNotIn("Get-FileHash -LiteralPath $path -Algorithm SHA256", sent)
        self.assertIn("$AbrArgs = '{\"path\": \"x\"}' | ConvertFrom-Json", sent)

    def test_the_bundle_scripts_themselves_are_always_sent_whole(self) -> None:
        node = HyperVNode("hv.local", "CORP\\svc", "secret")
        node._bundles["hv01"] = r"C:\work\abc"
        self.assertNotIn("ReadAllText", self._sent(node, inline=True))


if __name__ == "__main__":
    unittest.main()


class LocalToolPathTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app_root = pathlib.Path(tmp.name) / "opt"
        self.repo_root = pathlib.Path(tmp.name) / "checkout"
        for patcher in (
            patch.object(vhdx_tool, "APP_ROOT", self.app_root),
            patch.object(vhdx_tool, "REPO_ROOT", self.repo_root),
            patch.dict("os.environ", {"ALTASTOR_VHDX_TOOL_PATH": ""}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _put(self, root: pathlib.Path, *folders: str) -> pathlib.Path:
        folder = root.joinpath("tools", "abr-vhdx-tool", *folders)
        folder.mkdir(parents=True, exist_ok=True)
        tool = folder / vhdx_tool.TOOL_NAME
        tool.write_bytes(b"MZ")
        return tool.resolve()

    def test_finds_the_tool_where_the_vhdx_tools_package_installs_it(self) -> None:
        installed = self._put(self.app_root, "x64")

        self.assertEqual(vhdx_tool.local_tool_path(), installed)

    def test_still_finds_a_copy_placed_without_the_architecture_folder(self) -> None:
        copied = self._put(self.app_root)

        self.assertEqual(vhdx_tool.local_tool_path(), copied)

    def test_the_package_copy_wins_over_one_placed_without_the_architecture_folder(self) -> None:
        installed = self._put(self.app_root, "x64")
        self._put(self.app_root)

        self.assertEqual(vhdx_tool.local_tool_path(), installed)

    def test_the_installed_copy_wins_over_the_checkout(self) -> None:
        installed = self._put(self.app_root, "x64")
        self._put(self.repo_root)

        self.assertEqual(vhdx_tool.local_tool_path(), installed)

    def test_a_checkout_finds_its_own_copy(self) -> None:
        for folders in ((), ("x64",)):
            with self.subTest(folders=folders):
                built = self._put(self.repo_root, *folders)
                self.assertEqual(vhdx_tool.local_tool_path(), built)
                built.unlink()

    def test_the_configured_path_wins_over_every_other_copy(self) -> None:
        self._put(self.app_root, "x64")
        configured = self._put(self.app_root.parent / "elsewhere")

        with patch.dict("os.environ", {"ALTASTOR_VHDX_TOOL_PATH": str(configured)}):
            self.assertEqual(vhdx_tool.local_tool_path(), configured)

    def test_no_copy_anywhere_means_the_hosts_must_have_it(self) -> None:
        self.assertIsNone(vhdx_tool.local_tool_path())
