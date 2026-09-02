# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""GET /api/v1/sessions paging.

A thousand sessions in one response ran to ~1.8 MB, which the console re-polls
every few seconds. These cover the two things that shrank it: `limit`/`offset`
slicing with an honest `total`, and list rows no longer carrying their logs.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any
import unittest

os.environ.setdefault("ABR_ENABLE_MOCK_INVENTORY", "1")

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
from fastapi.testclient import TestClient

from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import TASKS
from python.srv.services import abr_v41

SESSION_COUNT = 25


def _task(index: int) -> dict[str, Any]:
    return {
        "id": f"sess-{index:03d}",
        "task_type": "backup",
        "status": "completed",
        # Descending ids and ascending timestamps, so a page that silently
        # ignored the sort would not accidentally come out in the right order.
        "created_at": f"2026-01-{index + 1:02d}T00:00:00Z",
        "resource_type": "virtual_machine",
        "resource_external_id": f"vm-{index}",
        "parameters": {"vm_name": f"vm-{index}"},
        "result": {"progress": 100, "finished_at": f"2026-01-{index + 1:02d}T01:00:00Z"},
        "logs": [{"timestamp": "2026-01-01T00:00:00Z", "level": "info", "component": "engine", "message": f"line {n}"} for n in range(20)],
    }


class SessionPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self._saved_tasks = dict(TASKS)
        TASKS.clear()
        for index in range(SESSION_COUNT):
            task = _task(index)
            TASKS[task["id"]] = task

    def tearDown(self) -> None:
        TASKS.clear()
        TASKS.update(self._saved_tasks)
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)

    def _list(self, **params: Any) -> dict[str, Any]:
        response = self.client.get("/api/v1/sessions", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_no_limit_returns_every_session(self) -> None:
        body = self._list()
        self.assertEqual(len(body["items"]), SESSION_COUNT)
        self.assertEqual(body["total"], SESSION_COUNT)
        self.assertIsNone(body["limit"])
        self.assertEqual(body["offset"], 0)

    def test_limit_slices_newest_first_and_reports_full_total(self) -> None:
        body = self._list(limit=10)
        self.assertEqual(len(body["items"]), 10)
        # `total` is the unpaged match count -- that is what tells a client how
        # far it still has to walk.
        self.assertEqual(body["total"], SESSION_COUNT)
        self.assertEqual(body["limit"], 10)
        self.assertEqual([item["id"] for item in body["items"]], [f"sess-{i:03d}" for i in range(24, 14, -1)])

    def test_offset_pages_through_without_gaps_or_repeats(self) -> None:
        seen: list[str] = []
        offset = 0
        while True:
            body = self._list(limit=10, offset=offset)
            if not body["items"]:
                break
            seen.extend(item["id"] for item in body["items"])
            offset += len(body["items"])
            if offset >= body["total"]:
                break
        self.assertEqual(len(seen), SESSION_COUNT)
        self.assertEqual(len(set(seen)), SESSION_COUNT)
        self.assertEqual(seen, [item["id"] for item in self._list()["items"]])

    def test_offset_past_the_end_is_an_empty_page_not_an_error(self) -> None:
        body = self._list(limit=10, offset=SESSION_COUNT + 5)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], SESSION_COUNT)

    def test_list_rows_carry_no_logs_but_the_session_itself_still_does(self) -> None:
        row = self._list(limit=1)["items"][0]
        self.assertNotIn("logs", row)
        # Everything the console's run feed reads is still on the row.
        for field in ("id", "state", "type", "vm_name", "started_at", "finished_at", "progress"):
            self.assertIn(field, row)

        detail = self.client.get(f"/api/v1/sessions/{row['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(len(detail.json()["logs"]), 20)

        log = self.client.get(f"/api/v1/sessions/{row['id']}/log")
        self.assertEqual(log.status_code, 200, log.text)
        self.assertEqual(len(log.json()["logs"]), 20)

    def test_limit_above_the_cap_is_rejected(self) -> None:
        response = self.client.get("/api/v1/sessions", params={"limit": abr_v41.SESSION_PAGE_LIMIT_MAX + 1})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/api/v1/sessions", params={"limit": 0}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/sessions", params={"offset": -1}).status_code, 422)

    def test_support_bundle_still_gets_logs_from_the_service_list(self) -> None:
        # The support bundle dumps sessions to JSON and wants their logs; only
        # the HTTP list drops them.
        sessions = abr_v41.list_sessions()
        self.assertEqual(len(sessions), SESSION_COUNT)
        self.assertEqual(len(sessions[0]["logs"]), 20)


if __name__ == "__main__":
    unittest.main()
