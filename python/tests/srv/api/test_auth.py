# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import hashlib
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import SESSIONS, USERS, session_key
from python.srv.services import auth_service

BOOTSTRAP_ENV = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW",
}


def _reset_auth_state() -> None:
    USERS.clear()
    SESSIONS.clear()
    auth_service.reset_login_lockout_state_for_testing()


class AuthApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_state()
        self._env_patcher = patch.dict("os.environ", BOOTSTRAP_ENV)
        self._env_patcher.start()
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        self._env_patcher.stop()
        _reset_auth_state()

    def _login(
        self,
        name: str = "admin",
        password: str = "Sup3rSecretPW",
        *,
        client_type: str | None = None,
        duration_seconds: int | None = None,
    ):
        headers = {"X-Client-Type": client_type} if client_type else {}
        payload = {"name": name, "password": password}
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        return self.client.post("/api/v1/auth/login", json=payload, headers=headers)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


class BootstrapAdminTests(AuthApiTestCase):
    def test_bootstrap_admin_is_created_on_startup(self) -> None:
        response = self._login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["name"], "admin")

    def test_bootstrap_is_skipped_when_users_already_exist(self) -> None:
        response = self._login()
        self.assertEqual(response.status_code, 200)
        auth_service.bootstrap_admin_if_needed()
        self.assertEqual(len(USERS), 1)

    def test_bootstrap_allows_a_short_password_and_flags_the_account(self) -> None:
        USERS.clear()
        with patch.dict("os.environ", {"ABR_BOOTSTRAP_ADMIN_PASSWORD": "admin", "ABR_BOOTSTRAP_ADMIN_USER": "admin"}):
            auth_service.bootstrap_admin_if_needed()
        response = self._login(name="admin", password="admin")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["user"]["passwordPolicyExempt"])

    def test_regular_bootstrap_password_is_not_flagged(self) -> None:
        response = self._login()
        self.assertFalse(response.json()["user"]["passwordPolicyExempt"])


class UnauthenticatedAccessTests(AuthApiTestCase):
    def test_protected_route_without_token_returns_401(self) -> None:
        response = self.client.get("/api/v1/repositories")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_protected_route_with_garbage_token_returns_401(self) -> None:
        response = self.client.get("/api/v1/repositories", headers=self._auth_headers("not-a-real-token"))
        self.assertEqual(response.status_code, 401)

    def test_protected_route_with_malformed_authorization_header_returns_401(self) -> None:
        response = self.client.get("/api/v1/repositories", headers={"Authorization": "Basic somevalue"})
        self.assertEqual(response.status_code, 401)

    def test_users_and_dashboard_routes_require_auth(self) -> None:
        for path in ("/api/v1/users", "/api/v1/dashboard/summary", "/api/v1/system/health"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_login_endpoint_itself_does_not_require_auth(self) -> None:
        response = self._login()
        self.assertEqual(response.status_code, 200)

    def test_health_check_outside_api_v1_stays_public(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)


class LoginLogoutTests(AuthApiTestCase):
    def test_login_success_returns_token_and_grants_access(self) -> None:
        login_response = self._login()
        token = login_response.json()["token"]

        listed = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(listed.status_code, 200)

    def test_login_with_wrong_password_is_rejected(self) -> None:
        response = self._login(password="wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

    def test_login_with_unknown_user_is_rejected_with_same_error(self) -> None:
        response = self._login(name="nobody", password="whatever123")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")
        unknown_user_message = response.json()["error"]["message"]
        wrong_password_message = self._login(password="wrong").json()["error"]["message"]
        self.assertEqual(unknown_user_message, wrong_password_message)

    def test_password_hash_is_never_returned_by_the_api(self) -> None:
        token = self._login().json()["token"]
        body = self.client.get("/api/v1/users", headers=self._auth_headers(token)).text
        self.assertNotIn("passwd", body)
        self.assertNotIn("Sup3rSecretPW", body)

    def test_logout_invalidates_the_token(self) -> None:
        token = self._login().json()["token"]
        logout_response = self.client.post("/api/v1/auth/logout", headers=self._auth_headers(token))
        self.assertEqual(logout_response.status_code, 200)

        after_logout = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(after_logout.status_code, 401)

    def test_logout_without_a_token_is_rejected(self) -> None:
        self.assertEqual(self.client.post("/api/v1/auth/logout").status_code, 401)


class SessionExpiryTests(AuthApiTestCase):
    def test_expired_session_is_rejected_and_purged(self) -> None:
        token = self._login().json()["token"]
        SESSIONS[session_key(token)]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

        response = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(session_key(token), SESSIONS)

    def test_activity_slides_the_expiration_forward(self) -> None:
        token = self._login().json()["token"]
        original_expiry = SESSIONS[session_key(token)]["expires_at"]

        SESSIONS[session_key(token)]["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        stale_expiry = SESSIONS[session_key(token)]["expires_at"]

        response = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(SESSIONS[session_key(token)]["expires_at"], stale_expiry)
        self.assertNotEqual(original_expiry, SESSIONS[session_key(token)]["expires_at"])

    def test_session_ttl_is_configurable_via_env_var(self) -> None:
        with patch.dict("os.environ", {"ABR_SESSION_TTL_SECONDS": "60"}):
            token = self._login().json()["token"]
            expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])
            created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
            delta = (expires_at - created_at).total_seconds()
            self.assertAlmostEqual(delta, 60, delta=5)


class WebSessionAbsoluteCeilingTests(AuthApiTestCase):
    def test_login_without_a_client_type_header_is_unaffected_by_the_ceiling(self) -> None:
        token = self._login().json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["client"], "cli")
        self.assertIsNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_explicit_web_client_type_gets_the_ceiling(self) -> None:
        token = self._login(client_type="web").json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["client"], "web")
        self.assertIsNotNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_cli_client_type_header_is_exempt_from_the_ceiling(self) -> None:
        token = self._login(client_type="cli").json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["client"], "cli")
        self.assertIsNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_unrecognized_client_type_fails_open_and_is_not_capped(self) -> None:
        token = self._login(client_type="some-future-mobile-app").json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["client"], "cli")
        self.assertIsNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_web_session_renewal_is_capped_at_the_absolute_ceiling(self) -> None:
        token = self._login(client_type="web").json()["token"]
        ceiling = datetime.now(timezone.utc) + timedelta(seconds=2)
        SESSIONS[session_key(token)]["absolute_expires_at"] = ceiling.isoformat()

        response = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        renewed_expiry = datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])
        self.assertLessEqual(renewed_expiry, ceiling)

    def test_web_session_dies_at_the_ceiling_even_with_continuous_activity(self) -> None:
        token = self._login(client_type="web").json()["token"]
        SESSIONS[session_key(token)]["absolute_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        SESSIONS[session_key(token)]["expires_at"] = SESSIONS[session_key(token)]["absolute_expires_at"]

        dead = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
        self.assertEqual(dead.status_code, 401)
        self.assertNotIn(session_key(token), SESSIONS)

    def test_cli_session_keeps_sliding_indefinitely_with_no_ceiling(self) -> None:
        token = self._login(client_type="cli").json()["token"]
        for _ in range(3):
            SESSIONS[session_key(token)]["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            response = self.client.get("/api/v1/repositories", headers=self._auth_headers(token))
            self.assertEqual(response.status_code, 200)
        self.assertIsNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_web_session_ceiling_is_configurable_via_env_var(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_TTL_SECONDS": "120"}):
            token = self._login(client_type="web").json()["token"]
            absolute_expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
            created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
            delta = (absolute_expires_at - created_at).total_seconds()
            self.assertAlmostEqual(delta, 120, delta=5)


class PerLoginSessionDurationTests(AuthApiTestCase):
    def test_chosen_duration_controls_this_logins_ceiling(self) -> None:
        token = self._login(client_type="web", duration_seconds=3600).json()["token"]
        absolute_expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
        created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        delta = (absolute_expires_at - created_at).total_seconds()
        self.assertAlmostEqual(delta, 3600, delta=5)

    def test_unspecified_duration_falls_back_to_the_appliance_default(self) -> None:
        token = self._login(client_type="web").json()["token"]
        absolute_expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
        created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        delta = (absolute_expires_at - created_at).total_seconds()
        self.assertAlmostEqual(delta, auth_service.web_session_ttl_seconds(), delta=5)

    def test_two_separate_logins_can_choose_two_different_durations(self) -> None:
        token_a = self._login(client_type="web", duration_seconds=2400).json()["token"]
        token_b = self._login(client_type="web", duration_seconds=600).json()["token"]

        delta_a = (
            datetime.fromisoformat(SESSIONS[session_key(token_a)]["absolute_expires_at"])
            - datetime.fromisoformat(SESSIONS[session_key(token_a)]["created_at"])
        ).total_seconds()
        delta_b = (
            datetime.fromisoformat(SESSIONS[session_key(token_b)]["absolute_expires_at"])
            - datetime.fromisoformat(SESSIONS[session_key(token_b)]["created_at"])
        ).total_seconds()

        self.assertAlmostEqual(delta_a, 2400, delta=5)
        self.assertAlmostEqual(delta_b, 600, delta=5)

    def test_choosing_a_duration_does_not_persist_to_a_later_unspecified_login(self) -> None:
        self._login(client_type="web", duration_seconds=3600)

        token = self._login(client_type="web").json()["token"]
        delta = (
            datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
            - datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        ).total_seconds()
        self.assertAlmostEqual(delta, auth_service.web_session_ttl_seconds(), delta=5)

    def test_out_of_range_duration_is_rejected_at_login(self) -> None:
        too_short = self._login(client_type="web", duration_seconds=10)
        self.assertEqual(too_short.status_code, 422)
        self.assertEqual(too_short.json()["error"]["code"], "invalid_session_duration")

        too_long = self._login(client_type="web", duration_seconds=10_000_000)
        self.assertEqual(too_long.status_code, 422)

    def test_bounds_are_configurable_via_env_vars(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_MIN_SECONDS": "600", "ABR_WEB_SESSION_MAX_SECONDS": "7200"}):
            self.assertEqual(self._login(client_type="web", duration_seconds=500).status_code, 422)
            self.assertEqual(self._login(client_type="web", duration_seconds=7300).status_code, 422)
            self.assertEqual(self._login(client_type="web", duration_seconds=3600).status_code, 200)

    def test_cli_login_ignores_duration_seconds_entirely(self) -> None:
        response = self._login(client_type="cli", duration_seconds=999_999_999)
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        self.assertIsNone(SESSIONS[session_key(token)].get("absolute_expires_at"))

    def test_rejected_duration_leaves_no_session_behind(self) -> None:
        before = set(SESSIONS.keys())
        self._login(client_type="web", duration_seconds=1)
        self.assertEqual(set(SESSIONS.keys()), before)


class ReportedExpiryTests(AuthApiTestCase):
    def test_web_login_reports_the_chosen_ceiling_not_the_sliding_window(self) -> None:
        response = self._login(client_type="web", duration_seconds=3600)
        token = response.json()["token"]

        reported = datetime.fromisoformat(response.json()["expires_at"])
        created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        self.assertAlmostEqual((reported - created_at).total_seconds(), 3600, delta=5)

        stored = datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])
        self.assertAlmostEqual((stored - created_at).total_seconds(), auth_service.session_ttl_seconds(), delta=5)
        self.assertLess(stored, reported)

    def test_web_login_without_explicit_duration_reports_the_default_ceiling(self) -> None:
        response = self._login(client_type="web")
        token = response.json()["token"]

        reported = datetime.fromisoformat(response.json()["expires_at"])
        created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        self.assertAlmostEqual(
            (reported - created_at).total_seconds(), auth_service.web_session_ttl_seconds(), delta=5
        )

    def test_cli_login_reports_the_sliding_window_unchanged(self) -> None:
        response = self._login(client_type="cli")
        token = response.json()["token"]

        reported = datetime.fromisoformat(response.json()["expires_at"])
        stored = datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])
        self.assertEqual(reported, stored)


class LoginLockoutTests(AuthApiTestCase):
    def test_repeated_failed_logins_are_locked_out(self) -> None:
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "2", "ABR_LOGIN_LOCKOUT_SECONDS": "300"}):
            self.assertEqual(self._login(password="wrong-1").status_code, 401)
            self.assertEqual(self._login(password="wrong-2").status_code, 401)

            locked_out = self._login(password="wrong-3")
            self.assertEqual(locked_out.status_code, 429)
            self.assertEqual(locked_out.json()["error"]["code"], "too_many_attempts")

            still_locked = self._login(password="Sup3rSecretPW")
            self.assertEqual(still_locked.status_code, 429)

    def test_successful_login_clears_the_failure_counter(self) -> None:
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "2", "ABR_LOGIN_LOCKOUT_SECONDS": "300"}):
            self.assertEqual(self._login(password="wrong-1").status_code, 401)
            self.assertEqual(self._login().status_code, 200)
            self.assertEqual(self._login(password="wrong-again").status_code, 401)


class UserManagementTests(AuthApiTestCase):
    def _admin_token(self) -> str:
        return self._login().json()["token"]

    def test_add_list_and_delete_user(self) -> None:
        token = self._admin_token()
        headers = self._auth_headers(token)

        created = self.client.post(
            "/api/v1/users",
            json={"name": "alice", "password": "AlicePassw0rd", "fullName": "Alice Doe"},
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)
        alice_id = created.json()["id"]
        self.assertEqual(created.json()["fullName"], "Alice Doe")
        self.assertNotIn("passwd", created.json())

        listed = self.client.get("/api/v1/users", headers=headers)
        names = {item["name"] for item in listed.json()["items"]}
        self.assertEqual(names, {"admin", "alice"})

        deleted = self.client.delete(f"/api/v1/users/{alice_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)

        listed_after = self.client.get("/api/v1/users", headers=headers)
        names_after = {item["name"] for item in listed_after.json()["items"]}
        self.assertEqual(names_after, {"admin"})

    def test_duplicate_user_name_is_rejected(self) -> None:
        headers = self._auth_headers(self._admin_token())
        self.client.post("/api/v1/users", json={"name": "bob", "password": "BobPassw0rd"}, headers=headers)

        duplicate = self.client.post(
            "/api/v1/users", json={"name": "bob", "password": "AnotherPassw0rd"}, headers=headers
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "user_exists")

    def test_short_password_is_rejected(self) -> None:
        headers = self._auth_headers(self._admin_token())
        response = self.client.post("/api/v1/users", json={"name": "carl", "password": "short"}, headers=headers)
        self.assertEqual(response.status_code, 422)

    def test_cannot_delete_the_last_remaining_user(self) -> None:
        headers = self._auth_headers(self._admin_token())
        admin_id = next(uid for uid, user in USERS.items() if user["name"] == "admin")

        response = self.client.delete(f"/api/v1/users/{admin_id}", headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "last_user")

    def test_deleting_a_user_invalidates_their_sessions(self) -> None:
        admin_headers = self._auth_headers(self._admin_token())
        self.client.post("/api/v1/users", json={"name": "dana", "password": "DanaPassw0rd"}, headers=admin_headers)
        dana_login = self._login(name="dana", password="DanaPassw0rd")
        dana_token = dana_login.json()["token"]
        dana_id = dana_login.json()["user"]["id"]

        self.client.delete(f"/api/v1/users/{dana_id}", headers=admin_headers)

        still_valid = self.client.get("/api/v1/repositories", headers=self._auth_headers(dana_token))
        self.assertEqual(still_valid.status_code, 401)

    def test_user_add_requires_auth(self) -> None:
        response = self.client.post("/api/v1/users", json={"name": "eve", "password": "EvePassword1"})
        self.assertEqual(response.status_code, 401)


class ChangePasswordTests(AuthApiTestCase):
    def _change_password(self, token: str, current: str, new: str):
        return self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": current, "new_password": new},
            headers=self._auth_headers(token),
        )

    def test_requires_authentication(self) -> None:
        response = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "Sup3rSecretPW", "new_password": "NewPassw0rd1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_successful_change_updates_the_password(self) -> None:
        token = self._login().json()["token"]
        response = self._change_password(token, "Sup3rSecretPW", "NewPassw0rd1")
        self.assertEqual(response.status_code, 200)

        # Old password no longer works, new one does.
        self.assertEqual(self._login(password="Sup3rSecretPW").status_code, 401)
        self.assertEqual(self._login(password="NewPassw0rd1").status_code, 200)

    def test_wrong_current_password_is_rejected_and_leaves_password_unchanged(self) -> None:
        token = self._login().json()["token"]
        response = self._change_password(token, "totally-wrong", "NewPassw0rd1")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

        # Original password still works; the "new" one was never applied.
        self.assertEqual(self._login(password="Sup3rSecretPW").status_code, 200)
        self.assertEqual(self._login(password="NewPassw0rd1").status_code, 401)

    def test_new_password_too_short_is_rejected(self) -> None:
        token = self._login().json()["token"]
        response = self._change_password(token, "Sup3rSecretPW", "short")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self._login(password="Sup3rSecretPW").status_code, 200)

    def test_repeated_wrong_current_password_is_locked_out(self) -> None:
        token = self._login().json()["token"]
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "2", "ABR_LOGIN_LOCKOUT_SECONDS": "300"}):
            self.assertEqual(self._change_password(token, "wrong-1", "NewPassw0rd1").status_code, 401)
            self.assertEqual(self._change_password(token, "wrong-2", "NewPassw0rd1").status_code, 401)

            locked_out = self._change_password(token, "Sup3rSecretPW", "NewPassw0rd1")
            self.assertEqual(locked_out.status_code, 429)

            # The lockout counter is shared with login for this username.
            self.assertEqual(self._login(password="Sup3rSecretPW").status_code, 429)

    def test_change_invalidates_other_sessions_but_keeps_the_current_one(self) -> None:
        token_a = self._login().json()["token"]
        token_b = self._login().json()["token"]
        self.assertNotEqual(token_a, token_b)

        response = self._change_password(token_a, "Sup3rSecretPW", "NewPassw0rd1")
        self.assertEqual(response.status_code, 200)

        # token_a made the change -> still valid.
        self.assertEqual(self.client.get("/api/v1/repositories", headers=self._auth_headers(token_a)).status_code, 200)
        # token_b was a different, older session for the same user -> logged out.
        self.assertEqual(self.client.get("/api/v1/repositories", headers=self._auth_headers(token_b)).status_code, 401)

    def test_change_clears_the_password_policy_exempt_flag(self) -> None:
        USERS.clear()
        with patch.dict("os.environ", {"ABR_BOOTSTRAP_ADMIN_PASSWORD": "admin", "ABR_BOOTSTRAP_ADMIN_USER": "admin"}):
            auth_service.bootstrap_admin_if_needed()
        login_response = self._login(name="admin", password="admin")
        self.assertTrue(login_response.json()["user"]["passwordPolicyExempt"])
        token = login_response.json()["token"]

        self._change_password(token, "admin", "BrandNewPassw0rd")

        relogin = self._login(name="admin", password="BrandNewPassw0rd")
        self.assertEqual(relogin.status_code, 200)
        self.assertFalse(relogin.json()["user"]["passwordPolicyExempt"])

    def test_extra_payload_fields_cannot_target_another_user(self) -> None:
        # There is no user_id in the request body/path at all -- the account
        # being changed always comes from the session, so there's no field to
        # smuggle another user's id through in the first place.
        token = self._login().json()["token"]
        response = self.client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "Sup3rSecretPW", "new_password": "NewPassw0rd1", "user_id": "someone-else"},
            headers=self._auth_headers(token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._login(password="NewPassw0rd1").status_code, 200)


class PasswordHashingUnitTests(unittest.TestCase):
    def test_hash_matches_the_spec_formula(self) -> None:
        expected = hashlib.sha256("alice,secret".encode("utf-8")).hexdigest()
        self.assertEqual(auth_service.hash_password("alice", "secret"), expected)

    def test_hash_is_utf8_stable_for_non_latin_input(self) -> None:
        expected = hashlib.sha256("Глеб,парольок".encode("utf-8")).hexdigest()
        self.assertEqual(auth_service.hash_password("Глеб", "парольок"), expected)

    def test_different_names_with_same_password_hash_differently(self) -> None:
        self.assertNotEqual(auth_service.hash_password("alice", "secret"), auth_service.hash_password("bob", "secret"))


class UnicodeLoginTests(AuthApiTestCase):
    def test_login_works_with_non_latin_full_name_and_password(self) -> None:
        admin_headers = self._auth_headers(self._login().json()["token"])
        created = self.client.post(
            "/api/v1/users",
            json={"name": "oleh", "password": "пароль-Юнікод-8", "fullName": "Олег Шевченко"},
            headers=admin_headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["fullName"], "Олег Шевченко")

        login_response = self._login(name="oleh", password="пароль-Юнікод-8")
        self.assertEqual(login_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
