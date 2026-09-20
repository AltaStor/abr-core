# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import types
import unittest

try:
    from python.srv.plugins.vcenter.vcenter import VCenterNode
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for site_packages in sorted((repo_root / ".venv" / "lib").glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.plugins.vcenter.vcenter import VCenterNode

from pyVmomi import vim, vmodl


class _Container(vim.view.ContainerView):
    def Destroy(self) -> None:
        return None


class _Collector:
    def __init__(self, rows: list[tuple], pages: int = 1) -> None:
        self._rows = rows
        self._pages = pages
        self.calls = 0

    def _entry(self, member, name):
        if name is None:
            return types.SimpleNamespace(obj=member, propSet=[])
        return types.SimpleNamespace(
            obj=member, propSet=[types.SimpleNamespace(name="name", val=name)]
        )

    def RetrievePropertiesEx(self, filter_specs, options):
        self.calls += 1
        if self._pages > 1:
            head, self._rows = self._rows[:1], self._rows[1:]
            return types.SimpleNamespace(
                objects=[self._entry(m, n) for m, n in head], token="more"
            )
        return types.SimpleNamespace(
            objects=[self._entry(m, n) for m, n in self._rows], token=None
        )

    def ContinueRetrievePropertiesEx(self, token):
        self.calls += 1
        if not self._rows:
            return None
        head, self._rows = self._rows[:1], self._rows[1:]
        return types.SimpleNamespace(
            objects=[self._entry(m, n) for m, n in head],
            token="more" if self._rows else None,
        )


class _ViewManager:
    def CreateContainerView(self, root, types_, recursive):
        return _Container("session[fake]view-1")


class _Content:
    def __init__(self, collector: _Collector) -> None:
        self.rootFolder = object()
        self.viewManager = _ViewManager()
        self.propertyCollector = collector


class _ServiceInstance:
    def __init__(self, collector: _Collector) -> None:
        self._collector = collector

    def RetrieveContent(self) -> _Content:
        return _Content(self._collector)


class _Unreadable:
    """A managed object whose name raises, the way a destroyed one does."""

    def __init__(self, moid: str) -> None:
        self._moId = moid

    @property
    def name(self):
        raise vim.fault.NoPermission(privilegeId="System.View")


def _node(collector: _Collector) -> VCenterNode:
    node = VCenterNode(host="vc.local", user="u", password="p", verify_ssl=False)
    node.si = _ServiceInstance(collector)
    return node


class NameLookupTests(unittest.TestCase):
    def test_one_call_serves_the_whole_inventory(self) -> None:
        rows = [(object(), f"vm-{index}") for index in range(44)]
        collector = _Collector(rows)

        found = _node(collector)._get_vm("vm-43")

        self.assertIs(found, rows[43][0])
        self.assertEqual(collector.calls, 1)

    def test_a_missing_name_is_returned_as_none(self) -> None:
        collector = _Collector([(object(), "other")])

        self.assertIsNone(_node(collector)._get_vm("absent"))

    def test_an_unrelated_object_that_cannot_be_read_does_not_fail_the_lookup(self) -> None:
        wanted = object()
        collector = _Collector([(_Unreadable("vm-23367"), None), (wanted, "mine")])

        found = _node(collector)._get_vm("mine")

        self.assertIs(found, wanted)

    def test_the_lookup_never_touches_a_managed_object_property(self) -> None:
        collector = _Collector([(_Unreadable("vm-3685"), None), (object(), "mine")])

        node = _node(collector)

        self.assertIsNone(node._get_vm("not-here"))
        self.assertIs(node._get_vm("mine"), collector._rows[1][0])

    def test_every_page_is_followed(self) -> None:
        rows = [(object(), "first"), (object(), "second"), (object(), "third")]
        collector = _Collector(list(rows), pages=3)

        found = _node(collector)._get_vm("third")

        self.assertIs(found, rows[2][0])

    def test_listing_skips_the_cluster_service_vms(self) -> None:
        keep, drop = object(), object()
        collector = _Collector([(drop, "vCLS-1234"), (keep, "payload")])

        self.assertEqual(_node(collector)._list_vms(), [keep])

    def test_listing_tolerates_an_object_it_cannot_read(self) -> None:
        keep = object()
        collector = _Collector([(_Unreadable("vm-35375"), None), (keep, "payload")])

        self.assertEqual(_node(collector)._list_vms(), [keep])

    def test_a_host_is_resolved_the_same_way(self) -> None:
        host = object()
        collector = _Collector([(_Unreadable("host-1"), None), (host, "esx-1.local")])

        self.assertIs(_node(collector)._get_host("esx-1.local"), host)

    def test_a_datastore_is_resolved_the_same_way(self) -> None:
        store = object()
        collector = _Collector([(_Unreadable("ds-1"), None), (store, "qa-52-r1-4T")])

        self.assertIs(_node(collector)._get_datastore("qa-52-r1-4T"), store)


if __name__ == "__main__":
    unittest.main()
