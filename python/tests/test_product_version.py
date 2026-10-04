# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from python.srv.services import abr_v41

DPKG_OUTPUT = (
    "altastor-backup-mongodb-config 8.0.26+4\n"
    "altastor-backup-server 26.11~pre+58.g56b5e7f8a40f\n"
    "altastor-backup-webui 1.0+24.g691e43457344\n"
)


class ProductVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        abr_v41.product_version.cache_clear()
        self.addCleanup(abr_v41.product_version.cache_clear)

    @staticmethod
    def _dpkg(output: str):
        return patch.object(abr_v41, "run_capped", return_value=SimpleNamespace(stdout=output, returncode=0))

    def test_the_installed_server_package_names_the_version(self) -> None:
        with self._dpkg(DPKG_OUTPUT):
            self.assertEqual(abr_v41.product_version(), "26.11~pre+58.g56b5e7f8a40f")

    def test_the_other_altastor_packages_are_read_too(self) -> None:
        with self._dpkg(DPKG_OUTPUT):
            self.assertEqual(
                abr_v41.installed_package_versions(),
                {
                    "altastor-backup-mongodb-config": "8.0.26+4",
                    "altastor-backup-server": "26.11~pre+58.g56b5e7f8a40f",
                    "altastor-backup-webui": "1.0+24.g691e43457344",
                },
            )

    def test_a_server_that_runs_from_no_package_falls_back_to_the_release_line(self) -> None:
        with self._dpkg(""):
            self.assertEqual(abr_v41.product_version(), abr_v41.PRODUCT_VERSION)

    def test_a_host_without_dpkg_falls_back_to_the_release_line(self) -> None:
        with patch.object(abr_v41, "run_capped", side_effect=FileNotFoundError("dpkg-query")):
            self.assertEqual(abr_v41.product_version(), abr_v41.PRODUCT_VERSION)

    def test_binary_output_is_decoded(self) -> None:
        with self._dpkg(DPKG_OUTPUT.encode("utf-8")):
            self.assertEqual(abr_v41.product_version(), "26.11~pre+58.g56b5e7f8a40f")

    def test_the_version_is_read_once(self) -> None:
        with self._dpkg(DPKG_OUTPUT) as dpkg:
            abr_v41.product_version()
            abr_v41.product_version()

        self.assertEqual(dpkg.call_count, 1)

    def test_the_cli_the_about_tab_and_the_catalog_snapshot_all_say_the_same(self) -> None:
        with self._dpkg(DPKG_OUTPUT):
            reported = abr_v41.system_version()["version"]
            about = abr_v41.system_info()["version"]

        self.assertEqual(reported, "26.11~pre+58.g56b5e7f8a40f")
        self.assertEqual(about, reported)

    def test_the_support_bundle_reports_the_same_version(self) -> None:
        from python.srv.services import support_bundle_service

        with self._dpkg(DPKG_OUTPUT):
            manifest_version = abr_v41.product_version()
            with patch.object(support_bundle_service, "_run_tool", return_value=DPKG_OUTPUT):
                bundle_packages = support_bundle_service._package_versions()

        self.assertEqual(manifest_version, bundle_packages["altastor-backup-server"])


if __name__ == "__main__":
    unittest.main()
