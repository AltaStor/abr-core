# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import os
import tempfile
import unittest

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES
from python.srv.services import abr_v41


class JobRepositoryNameTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, REPOSITORIES):
            saved = dict(store)
            store.clear()
            self.addCleanup(lambda store=store, saved=saved: (store.clear(), store.update(saved)))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        os.chmod(folder.name, 0o700)
        self.root = folder.name
        self.source = self._repository("src")
        self.destination = self._repository("dst")
        JOBS["job-1"] = abr_v41.normalize_job_record(
            {"id": "job-1", "name": "J", "job_type": "backup", "repo_id": self.source, "repo_name": "src", "retention_days": 7}
        )
        JOBS["copy-1"] = abr_v41.normalize_job_record(
            {
                "id": "copy-1",
                "name": "C",
                "job_type": "backup-copy",
                "repo_id": self.destination,
                "repo_name": "dst",
                "source_repository_ids": [self.source],
                "source_repository_names": ["src"],
                "retention_days": 7,
            }
        )

    def _repository(self, name: str) -> str:
        path = os.path.join(self.root, name)
        os.mkdir(path, 0o700)
        REPOSITORIES[name] = {"id": name, "name": name, "type": "local", "path": path}
        return name

    def _rename(self, record_id: str, name: str) -> None:
        abr_v41.edit_repository_record(record_id, {"name": name})

    def test_a_job_names_its_repository_by_the_current_name_after_a_rename(self) -> None:
        self._rename(self.source, "src-renamed")
        self._rename(self.destination, "dst-renamed")

        self.assertEqual(abr_v41.job_to_dto(JOBS["job-1"])["repository"], {"id": "src", "name": "src-renamed"})
        copy = abr_v41.job_to_dto(JOBS["copy-1"])
        self.assertEqual(copy["repository"], {"id": "dst", "name": "dst-renamed"})
        self.assertEqual([item["name"] for item in copy["backup_copy"]["source_repositories"]], ["src-renamed"])

    def test_new_sessions_and_other_readers_get_the_current_name(self) -> None:
        self._rename(self.source, "src-renamed")

        self.assertEqual(abr_v41.normalize_job_record(JOBS["job-1"])["repo_name"], "src-renamed")

    def test_a_job_whose_repository_is_not_in_the_catalog_keeps_the_name_it_recorded(self) -> None:
        REPOSITORIES.pop(self.source)

        self.assertEqual(abr_v41.job_to_dto(JOBS["job-1"])["repository"], {"id": "src", "name": "src"})


class RenamedNamesOnJobsAndChainsTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, REPOSITORIES, HYPERVISORS, BACKUPS):
            saved = dict(store)
            store.clear()
            self.addCleanup(lambda store=store, saved=saved: (store.clear(), store.update(saved)))
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "repo-new", "type": "local", "path": "/srv/abr"}
        HYPERVISORS["inv-1"] = {"id": "inv-1", "name": "vc-new", "type": "vsphere"}
        JOBS["job-1"] = abr_v41.normalize_job_record(
            {"id": "job-1", "name": "nightly-new", "job_type": "backup", "repo_id": "repo-1", "repo_name": "repo-old",
             "inventory_id": "inv-1", "inventory_name": "vc-old", "retention_days": 7}
        )
        BACKUPS["p-1"] = self._point("p-1", "job-1")

    @staticmethod
    def _point(point_id: str, job_id: str) -> dict:
        return {
            "id": point_id,
            "job_id": job_id,
            "job_name": "nightly-old",
            "vm_id": "vm-1",
            "vm_name": "app",
            "inventory_id": "inv-1",
            "inventory_name": "vc-old",
            "repository_id": "repo-1",
            "repository_name": "repo-old",
            "placements": [{"repository_id": "repo-1", "repository_name": "repo-old", "repository_type": "local"}],
            "execution_type": "ActiveFull",
            "created_at": "2026-11-01T00:00:00+00:00",
        }

    def test_a_job_names_its_inventory_by_the_current_name(self) -> None:
        self.assertEqual(abr_v41.job_to_dto(JOBS["job-1"])["inventory"], {"id": "inv-1", "name": "vc-new"})

    def test_a_chain_and_its_points_name_the_job_inventory_and_repository_by_their_current_names(self) -> None:
        [chain] = abr_v41.list_backup_chains()

        self.assertEqual(chain["name"], "nightly-new / app / repo-new")
        self.assertEqual((chain["job"]["name"], chain["inventory"]["name"], chain["repository"]["name"]), ("nightly-new", "vc-new", "repo-new"))
        point = abr_v41.normalize_backup_point(BACKUPS["p-1"])
        self.assertEqual((point["repository_name"], point["placements"][0]["repository_name"]), ("repo-new", "repo-new"))

    def test_chains_are_found_by_the_repositorys_current_name_only(self) -> None:
        self.assertEqual(len(abr_v41.list_backup_chains(repo_name="repo-new")), 1)
        self.assertEqual(abr_v41.list_backup_chains(repo_name="repo-old"), [])

    def test_names_whose_object_is_gone_stay_as_recorded(self) -> None:
        BACKUPS["p-2"] = {**self._point("p-2", "job-gone"), "vm_id": "vm-2"}
        HYPERVISORS.clear()
        REPOSITORIES.clear()

        point = abr_v41.normalize_backup_point(BACKUPS["p-2"])

        self.assertEqual((point["job_name"], point["inventory_name"], point["repository_name"]), ("nightly-old", "vc-old", "repo-old"))
        self.assertEqual(point["placements"][0]["repository_name"], "repo-old")


if __name__ == "__main__":
    unittest.main()
