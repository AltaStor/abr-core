# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pymongo.errors import ServerSelectionTimeoutError

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core import mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import REPOSITORIES, SESSIONS, USERS, session_key
from python.srv.services import auth_service

BOOTSTRAP_ENV = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW",
}
CONFIGURED_URI = "mongodb://localhost:27017"


class _StoppedDatabase:
    def _refused(self, *_args, **_kwargs):
        raise ServerSelectionTimeoutError("localhost:27017: [Errno 111] Connection refused")

    upsert = find = get = delete = ping = _refused

    def close(self) -> None:
        pass


class _RunningDatabase:
    def ping(self) -> None:
        return None

    def upsert(self, *_args, **_kwargs) -> None:
        return None

    def find(self, *_args, **_kwargs) -> list:
        return []

    def get(self, *_args, **_kwargs):
        return None

    def delete(self, *_args, **_kwargs) -> bool:
        return True

    def close(self) -> None:
        pass


class _SlowDatabase:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.upsert_started = threading.Event()

    def ping(self) -> None:
        return None

    def upsert(self, *_args, **_kwargs) -> None:
        self.upsert_started.set()
        self.release.wait(timeout=5)
        return None

    def find(self, *_args, **_kwargs) -> list:
        return []

    def get(self, *_args, **_kwargs):
        return None

    def delete(self, *_args, **_kwargs) -> bool:
        return True

    def close(self) -> None:
        pass


def configured_database(handle):
    return patch.multiple(
        mongo_db_wrapper,
        mongodb_uri=lambda: CONFIGURED_URI,
        get_database=lambda: handle,
    )


class SystemHealthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        USERS.clear()
        SESSIONS.clear()
        REPOSITORIES.clear()
        auth_service.reset_login_lockout_state_for_testing()
        self._env_patcher = patch.dict("os.environ", BOOTSTRAP_ENV)
        self._env_patcher.start()
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        login = self.client.post(
            "/api/v1/auth/login", json={"name": "admin", "password": "Sup3rSecretPW"}
        )
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        self._env_patcher.stop()
        USERS.clear()
        SESSIONS.clear()
        REPOSITORIES.clear()

    def _health(self):
        return self.client.get("/api/v1/system/health", headers=self.headers)


class HealthyDatabaseTests(SystemHealthTestCase):
    def test_reachable_database_is_reported_ok(self) -> None:
        with configured_database(_RunningDatabase()):
            response = self._health()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["components"]["database"], "ok")

    def test_existing_components_and_counts_are_preserved(self) -> None:
        with configured_database(_RunningDatabase()):
            body = self._health().json()
        self.assertLessEqual(
            {"api", "repositories", "inventories", "scheduler", "task_pool"},
            body["components"].keys(),
        )
        self.assertIn("counts", body)

    def test_in_memory_mode_without_a_configured_uri_stays_ok(self) -> None:
        body = self._health().json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["components"]["database"], "not_configured")


class UnavailableDatabaseTests(SystemHealthTestCase):
    def test_health_reports_a_degraded_catalog_instead_of_failing(self) -> None:
        with configured_database(_StoppedDatabase()):
            response = self._health()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotEqual(body["status"], "ok")
        self.assertEqual(body["components"]["database"], "unavailable")

    def test_the_remaining_components_are_still_reported(self) -> None:
        with configured_database(_StoppedDatabase()):
            components = self._health().json()["components"]
        self.assertEqual(components["api"], "ok")
        self.assertEqual(components["scheduler"], "ok")

    def test_health_recovers_on_its_own_once_the_database_returns(self) -> None:
        with configured_database(_StoppedDatabase()):
            self.assertEqual(self._health().json()["status"], "degraded")
        with configured_database(_RunningDatabase()):
            body = self._health().json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["components"]["database"], "ok")

    def test_catalog_reads_still_answer_from_memory(self) -> None:
        with configured_database(_StoppedDatabase()):
            response = self.client.get("/api/v1/repositories", headers=self.headers)
        self.assertEqual(response.status_code, 200)


class AuthenticationDuringOutageTests(SystemHealthTestCase):
    def test_a_valid_session_keeps_working(self) -> None:
        with configured_database(_StoppedDatabase()):
            self.assertEqual(self._health().status_code, 200)

    def test_a_missing_token_is_still_401(self) -> None:
        with configured_database(_StoppedDatabase()):
            self.assertEqual(self.client.get("/api/v1/system/health").status_code, 401)

    def test_an_expired_session_is_still_401_and_evicted(self) -> None:
        token = "expired-token"
        SESSIONS[session_key(token)] = {
            "token": session_key(token),
            "user_id": next(iter(USERS)),
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        with configured_database(_StoppedDatabase()):
            response = self.client.get(
                "/api/v1/system/health", headers={"Authorization": f"Bearer {token}"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(session_key(token), SESSIONS)

    def test_a_session_whose_user_is_gone_is_still_401(self) -> None:
        token = "orphan-token"
        SESSIONS[session_key(token)] = {
            "token": session_key(token),
            "user_id": "no-such-user",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with configured_database(_StoppedDatabase()):
            response = self.client.get(
                "/api/v1/system/health", headers={"Authorization": f"Bearer {token}"}
            )
        self.assertEqual(response.status_code, 401)

    def test_the_session_survives_the_outage(self) -> None:
        with configured_database(_StoppedDatabase()):
            self._health()
        with configured_database(_RunningDatabase()):
            self.assertEqual(self._health().status_code, 200)

    def test_a_session_renewal_does_not_block_the_request(self) -> None:
        slow_db = _SlowDatabase()
        with configured_database(slow_db):
            started = time.monotonic()
            response = self._health()
            elapsed = time.monotonic() - started
            self.assertEqual(response.status_code, 200)
            self.assertLess(
                elapsed, 1.0, "the request waited for the session-renewal DB write"
            )
            slow_db.release.set()

    def test_a_logout_racing_a_slow_renewal_is_not_undone(self) -> None:
        token = self.headers["Authorization"].split(" ")[1]
        slow_db = _SlowDatabase()
        with configured_database(slow_db):
            self._health()
            self.assertTrue(
                slow_db.upsert_started.wait(timeout=2),
                "the background renewal never reached the database write",
            )
            auth_service.logout(token)
            self.assertNotIn(session_key(token), SESSIONS)

            slow_db.release.set()
            deadline = time.monotonic() + 2
            while session_key(token) in SESSIONS and time.monotonic() < deadline:
                time.sleep(0.02)

            self.assertNotIn(
                session_key(token), SESSIONS, "a delayed renewal resurrected a logged-out session"
            )


class HealthDocumentationTests(SystemHealthTestCase):
    def _operation(self) -> dict:
        return self.client.get("/openapi.json").json()["paths"]["/api/v1/system/health"]["get"]

    def test_openapi_documents_a_200_and_no_other_status(self) -> None:
        self.assertEqual(sorted(self._operation()["responses"]), ["200"])

    def test_openapi_publishes_the_degraded_contract(self) -> None:
        self.assertIn("degraded", self._operation().get("description", ""))


class DatabaseStatusProbeTests(unittest.TestCase):
    def test_status_is_not_configured_without_a_uri(self) -> None:
        with patch.object(mongo_db_wrapper, "mongodb_uri", lambda: None):
            self.assertEqual(mongo_db_wrapper.database_status(), "not_configured")

    def test_status_is_unavailable_when_no_handle_can_be_opened(self) -> None:
        with configured_database(None):
            self.assertEqual(mongo_db_wrapper.database_status(), "unavailable")

    def test_status_is_unavailable_when_the_probe_raises(self) -> None:
        with configured_database(_StoppedDatabase()):
            self.assertEqual(mongo_db_wrapper.database_status(), "unavailable")

    def test_the_probe_never_raises_at_the_caller(self) -> None:
        class Exploding:
            def ping(self):
                raise RuntimeError("something entirely unexpected")

        with configured_database(Exploding()):
            self.assertEqual(mongo_db_wrapper.database_status(), "unavailable")


if __name__ == "__main__":
    unittest.main()
