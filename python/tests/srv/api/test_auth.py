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
from python.srv.core.mongo_db_wrapper import SESSIONS, USERS, renew_session, session_key
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
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "120"}):
            token = self._login(client_type="web").json()["token"]
            absolute_expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
            created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
            delta = (absolute_expires_at - created_at).total_seconds()
            self.assertAlmostEqual(delta, 120, delta=5)

    def test_a_lifetime_of_zero_means_no_ceiling(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "0"}):
            response = self._login(client_type="web")
        token = response.json()["token"]
        self.assertIsNone(SESSIONS[session_key(token)]["absolute_expires_at"])
        self.assertIsNone(response.json()["absolute_expires_at"])


class PerLoginSessionDurationTests(AuthApiTestCase):
    def _idle_seconds(self, token: str) -> float:
        record = SESSIONS[session_key(token)]
        return (datetime.fromisoformat(record["expires_at"]) - datetime.fromisoformat(record["created_at"])).total_seconds()

    def test_chosen_duration_is_this_logins_inactivity_timeout(self) -> None:
        token = self._login(client_type="web", duration_seconds=3600).json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["idle_timeout_seconds"], 3600)
        self.assertAlmostEqual(self._idle_seconds(token), 3600, delta=5)

    def test_the_ceiling_comes_from_the_appliance_not_from_the_chosen_duration(self) -> None:
        token = self._login(client_type="web", duration_seconds=600).json()["token"]
        absolute_expires_at = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
        created_at = datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"])
        self.assertAlmostEqual(
            (absolute_expires_at - created_at).total_seconds(), auth_service.web_session_lifetime_seconds(), delta=5
        )

    def test_unspecified_duration_falls_back_to_the_appliance_default(self) -> None:
        token = self._login(client_type="web").json()["token"]
        self.assertEqual(SESSIONS[session_key(token)]["idle_timeout_seconds"], auth_service.web_session_ttl_seconds())
        self.assertAlmostEqual(self._idle_seconds(token), auth_service.web_session_ttl_seconds(), delta=5)

    def test_two_separate_logins_can_choose_two_different_durations(self) -> None:
        token_a = self._login(client_type="web", duration_seconds=2400).json()["token"]
        token_b = self._login(client_type="web", duration_seconds=600).json()["token"]

        self.assertAlmostEqual(self._idle_seconds(token_a), 2400, delta=5)
        self.assertAlmostEqual(self._idle_seconds(token_b), 600, delta=5)

    def test_choosing_a_duration_does_not_persist_to_a_later_unspecified_login(self) -> None:
        self._login(client_type="web", duration_seconds=3600)

        token = self._login(client_type="web").json()["token"]
        self.assertAlmostEqual(self._idle_seconds(token), auth_service.web_session_ttl_seconds(), delta=5)

    def test_an_inactivity_timeout_past_the_ceiling_ends_at_the_ceiling(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "900"}):
            token = self._login(client_type="web", duration_seconds=3600).json()["token"]
        record = SESSIONS[session_key(token)]
        self.assertEqual(record["expires_at"], record["absolute_expires_at"])

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
        self.assertNotIn("idle_timeout_seconds", SESSIONS[session_key(token)])

    def test_rejected_duration_leaves_no_session_behind(self) -> None:
        before = set(SESSIONS.keys())
        self._login(client_type="web", duration_seconds=1)
        self.assertEqual(set(SESSIONS.keys()), before)


class ReportedExpiryTests(AuthApiTestCase):
    def test_web_login_reports_when_an_idle_session_ends_and_the_ceiling(self) -> None:
        response = self._login(client_type="web", duration_seconds=3600)
        token = response.json()["token"]
        record = SESSIONS[session_key(token)]

        self.assertEqual(response.json()["expires_at"], record["expires_at"])
        self.assertEqual(response.json()["absolute_expires_at"], record["absolute_expires_at"])
        created_at = datetime.fromisoformat(record["created_at"])
        reported = datetime.fromisoformat(response.json()["expires_at"])
        self.assertAlmostEqual((reported - created_at).total_seconds(), 3600, delta=5)

    def test_cli_login_reports_the_sliding_window_unchanged(self) -> None:
        response = self._login(client_type="cli")
        token = response.json()["token"]

        reported = datetime.fromisoformat(response.json()["expires_at"])
        stored = datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])
        self.assertEqual(reported, stored)
        self.assertIsNone(response.json()["absolute_expires_at"])


class InactivityTimeoutTests(AuthApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now(timezone.utc)
        clock = patch.object(auth_service, "_now_dt", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        synchronous = patch.object(
            auth_service, "_renew_session_in_background", side_effect=lambda token, fields: renew_session(token, fields)
        )
        synchronous.start()
        self.addCleanup(synchronous.stop)

    def _get(self, token: str, *, refresh: bool = False):
        headers = self._auth_headers(token)
        if refresh:
            headers["X-ABR-Refresh"] = "1"
        return self.client.get("/api/v1/auth/me", headers=headers)

    def _expires_at(self, token: str) -> datetime:
        return datetime.fromisoformat(SESSIONS[session_key(token)]["expires_at"])

    def test_a_user_who_acts_every_four_minutes_stays_signed_in_past_twelve(self) -> None:
        token = self._login(client_type="web", duration_seconds=300).json()["token"]
        for _ in range(4):
            self.now += timedelta(minutes=4)
            self.assertEqual(self._get(token).status_code, 200)
        self.assertGreater(self.now - datetime.fromisoformat(SESSIONS[session_key(token)]["created_at"]), timedelta(minutes=12))
        self.assertEqual(self._expires_at(token), self.now + timedelta(seconds=300))

    def test_background_refreshes_alone_do_not_keep_a_web_session_alive(self) -> None:
        token = self._login(client_type="web", duration_seconds=300).json()["token"]
        ends = self._expires_at(token)
        for _ in range(33):
            self.now += timedelta(seconds=9)
            if self.now >= ends:
                break
            self.assertEqual(self._get(token, refresh=True).status_code, 200)
            self.assertEqual(self._expires_at(token), ends)
        self.now = ends + timedelta(seconds=1)
        self.assertEqual(self._get(token, refresh=True).status_code, 401)

    def test_a_cli_session_is_still_extended_by_its_progress_polling(self) -> None:
        token = self._login(client_type="cli").json()["token"]
        for _ in range(3):
            self.now += timedelta(seconds=240)
            self.assertEqual(self._get(token, refresh=True).status_code, 200)
        self.assertEqual(self._expires_at(token), self.now + timedelta(seconds=auth_service.session_ttl_seconds()))

    def test_a_web_session_from_before_the_inactivity_timeout_is_still_extended_by_polling(self) -> None:
        token = self._login(client_type="web").json()["token"]
        SESSIONS[session_key(token)].pop("idle_timeout_seconds")
        before = self._expires_at(token)
        self.now += timedelta(seconds=60)
        self.assertEqual(self._get(token, refresh=True).status_code, 200)
        self.assertGreater(self._expires_at(token), before)

    def test_activity_never_carries_a_web_session_past_its_ceiling(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "600"}):
            token = self._login(client_type="web", duration_seconds=300).json()["token"]
        ceiling = datetime.fromisoformat(SESSIONS[session_key(token)]["absolute_expires_at"])
        for _ in range(2):
            self.now += timedelta(seconds=240)
            self.assertEqual(self._get(token).status_code, 200)
            self.assertLessEqual(self._expires_at(token), ceiling)
        self.now = ceiling + timedelta(seconds=1)
        self.assertEqual(self._get(token).status_code, 401)


class SessionTouchTests(InactivityTimeoutTests):
    def _touch(self, token: str, *, refresh: bool = False):
        headers = self._auth_headers(token)
        if refresh:
            headers["X-ABR-Refresh"] = "1"
        return self.client.post("/api/v1/auth/session/touch", headers=headers)

    def test_touch_moves_the_end_to_now_plus_the_inactivity_timeout(self) -> None:
        token = self._login(client_type="web", duration_seconds=600).json()["token"]
        self.now += timedelta(seconds=200)

        response = self._touch(token)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(datetime.fromisoformat(response.json()["expires_at"]), self.now + timedelta(seconds=600))
        self.assertEqual(response.json()["absolute_expires_at"], SESSIONS[session_key(token)]["absolute_expires_at"])
        self.assertEqual(self._expires_at(token), self.now + timedelta(seconds=600))

    def test_touch_renews_even_when_flagged_as_a_background_refresh(self) -> None:
        token = self._login(client_type="web", duration_seconds=300).json()["token"]
        self.now += timedelta(seconds=200)
        self.assertEqual(self._touch(token, refresh=True).status_code, 200)
        self.assertEqual(self._expires_at(token), self.now + timedelta(seconds=300))

    def test_touch_does_not_pass_the_ceiling(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "400"}):
            token = self._login(client_type="web", duration_seconds=300).json()["token"]
        self.now += timedelta(seconds=200)
        response = self._touch(token)
        self.assertEqual(response.json()["expires_at"], response.json()["absolute_expires_at"])

    def test_touch_on_an_expired_session_answers_401(self) -> None:
        token = self._login(client_type="web", duration_seconds=300).json()["token"]
        self.now += timedelta(seconds=301)
        response = self._touch(token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")


class SessionDurationChangeTests(InactivityTimeoutTests):
    def _put(self, token: str, duration: object):
        return self.client.put(
            "/api/v1/auth/session/duration", json={"duration_seconds": duration}, headers=self._auth_headers(token)
        )

    def test_the_slider_changes_the_inactivity_timeout_and_leaves_the_ceiling(self) -> None:
        token = self._login(client_type="web", duration_seconds=1800).json()["token"]
        ceiling = SESSIONS[session_key(token)]["absolute_expires_at"]
        self.now += timedelta(seconds=120)

        response = self._put(token, 300)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(SESSIONS[session_key(token)]["idle_timeout_seconds"], 300)
        self.assertEqual(SESSIONS[session_key(token)]["absolute_expires_at"], ceiling)
        self.assertEqual(datetime.fromisoformat(response.json()["expires_at"]), self.now + timedelta(seconds=300))
        self.assertEqual(response.json()["absolute_expires_at"], ceiling)

    def test_after_the_slider_activity_keeps_extending_by_the_new_timeout(self) -> None:
        token = self._login(client_type="web").json()["token"]
        self._put(token, 300)
        for _ in range(4):
            self.now += timedelta(minutes=4)
            self.assertEqual(self._get(token).status_code, 200)
        self.assertEqual(self._expires_at(token), self.now + timedelta(seconds=300))

    def test_the_slider_cannot_carry_a_session_past_its_ceiling(self) -> None:
        with patch.dict("os.environ", {"ABR_WEB_SESSION_LIFETIME_SECONDS": "900"}):
            token = self._login(client_type="web", duration_seconds=300).json()["token"]
        response = self._put(token, 7200)
        self.assertEqual(response.json()["expires_at"], response.json()["absolute_expires_at"])

    def test_out_of_range_or_cli_is_refused_and_changes_nothing(self) -> None:
        token = self._login(client_type="web", duration_seconds=600).json()["token"]
        before = dict(SESSIONS[session_key(token)])
        for value in (10, 10_000_000):
            response = self._put(token, value)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "invalid_session_duration"))
        self.assertEqual(SESSIONS[session_key(token)], before)

        cli = self._login(client_type="cli").json()["token"]
        response = self._put(cli, 600)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "session_not_extendable"))


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
            json={"name": "alice", "password": "AlicePassw0rd", "fullName": "Alice Doe", "role": "backup_operator"},
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
        self.client.post("/api/v1/users", json={"name": "bob", "password": "BobPassw0rd", "role": "auditor"}, headers=headers)

        duplicate = self.client.post(
            "/api/v1/users", json={"name": "bob", "password": "AnotherPassw0rd", "role": "auditor"}, headers=headers
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
        self.assertEqual(response.json()["error"]["code"], "last_administrator")

    def test_deleting_a_user_invalidates_their_sessions(self) -> None:
        admin_headers = self._auth_headers(self._admin_token())
        self.client.post("/api/v1/users", json={"name": "dana", "password": "DanaPassw0rd", "role": "backup_operator"}, headers=admin_headers)
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
            json={"name": "oleh", "password": "пароль-Юнікод-8", "fullName": "Олег Шевченко", "role": "auditor"},
            headers=admin_headers,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["fullName"], "Олег Шевченко")

        login_response = self._login(name="oleh", password="пароль-Юнікод-8")
        self.assertEqual(login_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
