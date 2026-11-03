# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyotp
from cryptography.fernet import Fernet

from python.srv.core import credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import (
    AUDIT_RECORDS,
    CUSTOM_ROLES,
    USERS,
    load_users,
    persist_custom_role,
    persist_user,
    restore_catalog,
    snapshot_catalog,
)
from python.srv.services import audit_service, auth_service, two_factor
from python.srv.services.api_errors import ApiError

from .test_auth import AuthApiTestCase

ADMIN_PASSWORD = "Sup3rSecretPW"
OPERATOR_PASSWORD = "OperatorPass1"


class _TwoFactorCase(AuthApiTestCase):
    def setUp(self) -> None:
        # A clock that moves only when a test says so, so that none straddles a
        # 30-second step by accident.
        self.moment = datetime.now(timezone.utc)
        clock = patch.object(two_factor, "_now", side_effect=lambda: self.moment)
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()
        self.token = self._login().json()["token"]

    def _advance(self, steps: int = 1) -> None:
        self.moment += timedelta(seconds=30 * steps)

    def _post(self, path: str, payload: dict, token: str | None = None):
        return self.client.post(
            f"/api/v1/auth/totp/{path}", json=payload, headers=self._auth_headers(token or self.token)
        )

    def _code(self, secret: str, steps: int = 0) -> str:
        return pyotp.TOTP(secret).at(self.moment + timedelta(seconds=30 * steps))

    def _start(self, **second_factor) -> dict:
        response = self._post("setup", {"password": ADMIN_PASSWORD, **second_factor})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _turn_on(self) -> tuple[str, list[str]]:
        """Leaves the current step used: the next code to sign in with is one step ahead."""
        secret = self._start()["secret"]
        response = self._post("enable", {"totp_code": self._code(secret)})
        self.assertEqual(response.status_code, 200, response.text)
        return secret, response.json()["recovery_codes"]

    def _sign_in(self, **second_factor):
        return self.client.post(
            "/api/v1/auth/login", json={"name": "admin", "password": ADMIN_PASSWORD, **second_factor}
        )

    def _error(self, response) -> tuple[int, str]:
        return response.status_code, response.json()["error"]["code"]

    def _admin(self) -> dict:
        return next(user for user in USERS.values() if user["name"] == "admin")


class SecretAndProvisioningTests(_TwoFactorCase):
    def test_setup_gives_provisioning_data_both_authenticator_apps_read(self) -> None:
        setup = self._start()
        parsed = pyotp.parse_uri(setup["otpauth_uri"])
        self.assertEqual(parsed.secret, setup["secret"])
        self.assertEqual((parsed.name, parsed.issuer), ("admin", "AltaStor Backup"))
        # SHA-1, 6 digits, 30 seconds: what Google and Microsoft Authenticator
        # both read reliably. The URI then names none of them.
        self.assertEqual((parsed.digits, parsed.interval, parsed.digest().name), (6, 30, "sha1"))
        for setting in ("algorithm=", "digits=", "period="):
            self.assertNotIn(setting, setup["otpauth_uri"])
        self.assertGreaterEqual(len(base64.b32decode(setup["secret"])) * 8, 160)
        prefix, encoded = setup["qr_code"].split(",", 1)
        self.assertEqual(prefix, "data:image/svg+xml;base64")
        self.assertIn(b"<svg", base64.b64decode(encoded))

    def test_every_secret_is_unique(self) -> None:
        self.assertEqual(len({two_factor.new_secret() for _ in range(200)}), 200)
        self.assertNotEqual(self._start()["secret"], self._start()["secret"])

    def test_a_colon_in_the_issuer_does_not_reach_the_label(self) -> None:
        with patch.dict("os.environ", {"ABR_TOTP_ISSUER": "backup:8443"}):
            parsed = pyotp.parse_uri(self._start()["otpauth_uri"])
        self.assertEqual((parsed.issuer, parsed.name), ("backup 8443", "admin"))


class EnrollmentTests(_TwoFactorCase):
    def test_setup_needs_the_password_and_a_wrong_one_keeps_the_session(self) -> None:
        self.assertEqual(self._error(self._post("setup", {"password": "wrong"})), (422, "invalid_password"))
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(self.token)).status_code, 200)

    def test_sign_in_does_not_change_until_a_code_confirms_the_setup(self) -> None:
        self._start()
        self.assertEqual(self._sign_in().status_code, 200)
        self.assertFalse(self._admin().get("totp_enabled"))

    def test_a_wrong_first_code_leaves_it_off(self) -> None:
        self._start()
        self.assertEqual(self._error(self._post("enable", {"totp_code": "000000"})), (422, "invalid_totp_code"))
        self.assertFalse(self._admin().get("totp_enabled"))

    def test_enabling_needs_a_setup_first(self) -> None:
        self.assertEqual(self._error(self._post("enable", {"totp_code": "123456"})), (409, "totp_setup_required"))

    def test_enabling_keeps_this_session_and_ends_the_others(self) -> None:
        other = self._login().json()["token"]
        self._turn_on()
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(self.token)).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(other)).status_code, 401)

    def test_status_is_reported_and_the_secret_never_again(self) -> None:
        secret, _ = self._turn_on()
        me = self.client.get("/api/v1/auth/me", headers=self._auth_headers(self.token))
        self.assertTrue(me.json()["user"]["totpEnabled"])
        self.assertEqual(me.json()["recovery_codes_left"], 10)
        users = self.client.get("/api/v1/users", headers=self._auth_headers(self.token))
        self.assertTrue(users.json()["items"][0]["totpEnabled"])
        for text in (me.text, users.text, self._sign_in(totp_code=self._code(secret, 1)).text):
            self.assertNotIn(secret, text)
            self.assertNotIn('"totp_recovery"', text)
            self.assertNotIn('"totp_secret"', text)


class ReEnrollmentTests(_TwoFactorCase):
    def test_it_needs_the_password_and_a_code(self) -> None:
        secret, _ = self._turn_on()
        self.assertEqual(self._error(self._post("setup", {"password": ADMIN_PASSWORD})), (422, "totp_required"))
        wrong_password = self._post("setup", {"password": "wrong", "totp_code": self._code(secret, 1)})
        self.assertEqual(self._error(wrong_password), (422, "invalid_password"))
        wrong_code = self._post("setup", {"password": ADMIN_PASSWORD, "totp_code": "000000"})
        self.assertEqual(self._error(wrong_code), (422, "invalid_totp_code"))

    def test_the_old_secret_works_until_the_new_one_is_confirmed_and_never_after(self) -> None:
        old, old_codes = self._turn_on()
        self._advance()
        new = self._start(totp_code=self._code(old))["secret"]
        self.assertNotEqual(new, old)

        self._advance()
        self.assertEqual(self._sign_in(totp_code=self._code(old)).status_code, 200)

        self._advance()
        confirmed = self._post("enable", {"totp_code": self._code(new)})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self._advance()
        self.assertEqual(self._sign_in(totp_code=self._code(old)).json()["error"]["code"], "invalid_totp_code")
        self.assertEqual(self._sign_in(totp_code=self._code(new)).status_code, 200)

        self.assertEqual(len(confirmed.json()["recovery_codes"]), 10)
        self.assertEqual(self._sign_in(recovery_code=old_codes[0]).json()["error"]["code"], "invalid_recovery_code")
        self.assertEqual(self._sign_in(recovery_code=confirmed.json()["recovery_codes"][0]).status_code, 200)

    def test_someone_who_lost_the_phone_does_it_with_a_recovery_code(self) -> None:
        _, codes = self._turn_on()
        new = self._start(recovery_code=codes[0])["secret"]
        self.assertEqual(self._post("enable", {"totp_code": self._code(new)}).status_code, 200)


class SignInTests(_TwoFactorCase):
    def test_the_password_alone_is_not_enough(self) -> None:
        self._turn_on()
        self.assertEqual(self._error(self._sign_in()), (401, "totp_required"))

    def test_a_wrong_password_never_gets_as_far_as_the_code(self) -> None:
        secret, _ = self._turn_on()
        response = self.client.post(
            "/api/v1/auth/login",
            json={"name": "admin", "password": "wrong-password", "totp_code": self._code(secret, 1)},
        )
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

    def test_a_valid_code_signs_in(self) -> None:
        secret, _ = self._turn_on()
        response = self._sign_in(totp_code=self._code(secret, 1))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("recovery_codes_left", response.json())

    def test_an_invalid_code_is_rejected(self) -> None:
        secret, _ = self._turn_on()
        valid = self._code(secret, 1)
        wrong = f"{(int(valid) + 1) % 1_000_000:06d}"
        self.assertEqual(self._error(self._sign_in(totp_code=wrong)), (401, "invalid_totp_code"))

    def test_a_code_works_once(self) -> None:
        secret, _ = self._turn_on()
        code = self._code(secret, 1)
        self.assertEqual(self._sign_in(totp_code=code).status_code, 200)
        self.assertEqual(self._error(self._sign_in(totp_code=code)), (401, "totp_code_used"))

    def test_the_code_that_confirmed_the_setup_does_not_sign_in(self) -> None:
        secret = self._start()["secret"]
        code = self._code(secret)
        self._post("enable", {"totp_code": code})
        self.assertEqual(self._error(self._sign_in(totp_code=code)), (401, "totp_code_used"))

    def test_an_older_code_is_refused_after_a_newer_one(self) -> None:
        secret, _ = self._turn_on()
        self.assertEqual(self._sign_in(totp_code=self._code(secret, 1)).status_code, 200)
        self.assertEqual(self._error(self._sign_in(totp_code=self._code(secret, 0))), (401, "totp_code_used"))

    def test_an_expired_code_says_so(self) -> None:
        secret, _ = self._turn_on()
        self.assertEqual(self._error(self._sign_in(totp_code=self._code(secret, -3))), (401, "totp_code_expired"))

    def test_malformed_input_is_refused_without_an_error(self) -> None:
        self._turn_on()
        for code in ("abc", "12345", "1234567", "١٢٣٤٥٦", " ", "-"):
            with self.subTest(code=code):
                auth_service.reset_login_lockout_state_for_testing()
                self.assertEqual(self._error(self._sign_in(totp_code=code)), (401, "invalid_totp_code"))

    def test_a_recovery_code_given_as_a_totp_code_is_refused(self) -> None:
        # What the CLI sends: it has no way to give a recovery code.
        _, codes = self._turn_on()
        self.assertEqual(self._error(self._sign_in(totp_code=codes[0])), (401, "invalid_totp_code"))
        self.assertEqual(self._sign_in(recovery_code=codes[0]).status_code, 200)

    def test_both_at_once_is_refused(self) -> None:
        secret, codes = self._turn_on()
        response = self._sign_in(totp_code=self._code(secret, 1), recovery_code=codes[0])
        self.assertEqual(self._error(response), (422, "invalid_argument"))

    def test_the_same_code_sent_at_once_opens_one_session(self) -> None:
        secret, _ = self._turn_on()
        code = self._code(secret, 1)

        def attempt(_: int) -> bool:
            try:
                auth_service.login("admin", ADMIN_PASSWORD, totp_code=code)
                return True
            except ApiError:
                return False

        def slow_write(record: dict) -> None:
            # Holds the door open: without the lock, the others read the user
            # before this write and take the same code.
            time.sleep(0.05)
            persist_user(record)

        with (
            patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "100"}),
            patch.object(two_factor, "persist_user", side_effect=slow_write),
            ThreadPoolExecutor(8) as pool,
        ):
            self.assertEqual(sum(pool.map(attempt, range(8))), 1)

    def test_a_refused_duration_does_not_waste_the_code(self) -> None:
        secret, _ = self._turn_on()
        code = self._code(secret, 1)
        refused = self.client.post(
            "/api/v1/auth/login",
            json={"name": "admin", "password": ADMIN_PASSWORD, "totp_code": code, "duration_seconds": 1},
            headers={"X-Client-Type": "web"},
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self._sign_in(totp_code=code).status_code, 200)

    def test_a_disabled_user_is_told_so_rather_than_asked_for_a_code(self) -> None:
        self._turn_on()
        persist_user({**self._admin(), "enabled": False})
        self.assertEqual(self._sign_in().json()["error"]["code"], "account_disabled")

    def test_without_two_factor_a_stray_recovery_code_changes_nothing(self) -> None:
        response = self._sign_in(recovery_code="AAAA-BBBB-CCCC-DDDD")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("recovery_codes_left", response.json())


class ClockDriftPolicyTests(_TwoFactorCase):
    def _accepted(self, secret: str, steps: int) -> bool:
        auth_service.reset_login_lockout_state_for_testing()
        persist_user({**self._admin(), "totp_last_step": None})
        return self._sign_in(totp_code=self._code(secret, steps)).status_code == 200

    def test_by_default_one_step_either_way_is_accepted(self) -> None:
        secret, _ = self._turn_on()
        self.assertEqual([self._accepted(secret, steps) for steps in (-2, -1, 0, 1, 2)], [False, True, True, True, False])

    def test_the_window_is_configurable(self) -> None:
        secret, _ = self._turn_on()
        with patch.dict("os.environ", {"ABR_TOTP_VALID_WINDOW": "0"}):
            self.assertEqual([self._accepted(secret, steps) for steps in (-1, 0, 1)], [False, True, False])
        with patch.dict("os.environ", {"ABR_TOTP_VALID_WINDOW": "2"}):
            self.assertEqual([self._accepted(secret, steps) for steps in (-3, -2, 2, 3)], [False, True, True, False])

    def test_a_code_from_a_phone_running_ahead_is_invalid_and_one_behind_expired(self) -> None:
        secret, _ = self._turn_on()
        self.assertEqual(self._error(self._sign_in(totp_code=self._code(secret, 3))), (401, "invalid_totp_code"))
        self.assertEqual(self._error(self._sign_in(totp_code=self._code(secret, -3))), (401, "totp_code_expired"))


class LockoutTests(_TwoFactorCase):
    def test_a_correct_password_does_not_reset_the_count_of_wrong_codes(self) -> None:
        self._turn_on()
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "3"}):
            for _ in range(3):
                self.assertEqual(self._sign_in(totp_code="000000").json()["error"]["code"], "invalid_totp_code")
            self.assertEqual(self._error(self._sign_in(totp_code="000000")), (429, "too_many_attempts"))

    def test_wrong_recovery_codes_count_too(self) -> None:
        self._turn_on()
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "2"}):
            self._sign_in(recovery_code="AAAA-BBBB-CCCC-DDDD")
            self._sign_in(recovery_code="AAAA-BBBB-CCCC-DDDD")
            self.assertEqual(self._error(self._sign_in(recovery_code="AAAA-BBBB-CCCC-DDDD")), (429, "too_many_attempts"))

    def test_asking_for_the_code_is_not_a_failure(self) -> None:
        self._turn_on()
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "3"}):
            for _ in range(5):
                self.assertEqual(self._sign_in().json()["error"]["code"], "totp_required")

    def test_signing_in_clears_the_count(self) -> None:
        secret, _ = self._turn_on()
        with patch.dict("os.environ", {"ABR_LOGIN_MAX_ATTEMPTS": "3"}):
            self._sign_in(totp_code="000000")
            self._sign_in(totp_code="000000")
            self.assertEqual(self._sign_in(totp_code=self._code(secret, 1)).status_code, 200)
            self._sign_in(totp_code="000000")
            self._sign_in(totp_code="000000")
            self.assertEqual(self._sign_in(totp_code="000000").json()["error"]["code"], "invalid_totp_code")


class RecoveryCodeTests(_TwoFactorCase):
    def test_ten_unique_codes_are_issued_and_only_digests_are_kept(self) -> None:
        _, codes = self._turn_on()
        self.assertEqual(len(set(codes)), 10)
        stored = self._admin()["totp_recovery"]
        self.assertEqual(len(stored), 10)
        for code in codes:
            self.assertNotIn(code, repr(self._admin()))
            self.assertNotIn(code.replace("-", ""), repr(self._admin()))

    def test_a_recovery_code_signs_in_once_and_the_count_says_so(self) -> None:
        _, codes = self._turn_on()
        response = self._sign_in(recovery_code=codes[0])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["recovery_codes_left"], 9)
        self.assertEqual(self._error(self._sign_in(recovery_code=codes[0])), (401, "invalid_recovery_code"))
        self.assertEqual(self._sign_in(recovery_code=codes[1]).json()["recovery_codes_left"], 8)

    def test_spaces_case_and_missing_hyphens_do_not_matter(self) -> None:
        _, codes = self._turn_on()
        self.assertEqual(self._sign_in(recovery_code=f"  {codes[0].lower().replace('-', ' ')} ").status_code, 200)
        self.assertEqual(self._sign_in(recovery_code=codes[1].replace("-", "")).status_code, 200)

    def test_regenerating_replaces_every_earlier_code(self) -> None:
        secret, codes = self._turn_on()
        response = self._post("recovery-codes", {"password": ADMIN_PASSWORD, "totp_code": self._code(secret, 1)})
        self.assertEqual(response.status_code, 200, response.text)
        fresh = response.json()["recovery_codes"]
        self.assertEqual(len(set(fresh)), 10)
        self.assertFalse(set(fresh) & set(codes))
        for old in codes[:3]:
            self.assertEqual(self._error(self._sign_in(recovery_code=old)), (401, "invalid_recovery_code"))
        auth_service.reset_login_lockout_state_for_testing()
        self.assertEqual(self._sign_in(recovery_code=fresh[0]).status_code, 200)

    def test_regenerating_needs_the_password_and_a_code(self) -> None:
        self._turn_on()
        self.assertEqual(self._error(self._post("recovery-codes", {"password": ADMIN_PASSWORD})), (422, "totp_required"))
        response = self._post("recovery-codes", {"password": "wrong", "totp_code": "000000"})
        self.assertEqual(self._error(response), (422, "invalid_password"))

    def test_regenerating_is_refused_while_it_is_off(self) -> None:
        response = self._post("recovery-codes", {"password": ADMIN_PASSWORD})
        self.assertEqual(self._error(response), (409, "totp_not_enabled"))

    def test_it_works_when_the_secret_cannot_be_read(self) -> None:
        secret, codes = self._turn_on()
        persist_user({**self._admin(), "totp_secret": "abr-enc:v1:from-another-server"})
        refused = self._sign_in(totp_code=self._code(secret, 1))
        self.assertEqual(self._error(refused), (503, "totp_secret_unreadable"))
        self.assertEqual(self._sign_in(recovery_code=codes[0]).status_code, 200)


class TurningOffTests(_TwoFactorCase):
    def test_it_needs_the_password_and_a_code(self) -> None:
        secret, _ = self._turn_on()
        code = self._code(secret, 1)
        self.assertEqual(self._error(self._post("disable", {"password": "wrong", "totp_code": code})), (422, "invalid_password"))
        self.assertEqual(self._error(self._post("disable", {"password": ADMIN_PASSWORD})), (422, "totp_required"))
        wrong = self._post("disable", {"password": ADMIN_PASSWORD, "totp_code": "000000"})
        self.assertEqual(self._error(wrong), (422, "invalid_totp_code"))
        self.assertTrue(self._admin()["totp_enabled"])
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(self.token)).status_code, 200)

    def test_afterwards_the_password_is_enough_and_nothing_is_left(self) -> None:
        secret, _ = self._turn_on()
        self._advance()
        self._start(totp_code=self._code(secret))
        self._advance()
        response = self._post("disable", {"password": ADMIN_PASSWORD, "totp_code": self._code(secret)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["totpEnabled"])
        self.assertEqual(self._sign_in().status_code, 200)
        admin = self._admin()
        self.assertEqual(
            (admin["totp_secret"], admin["totp_pending_secret"], admin["totp_recovery"]), (None, None, [])
        )

    def test_a_recovery_code_does_it_too(self) -> None:
        _, codes = self._turn_on()
        self.assertEqual(self._post("disable", {"password": ADMIN_PASSWORD, "recovery_code": codes[0]}).status_code, 200)

    def test_it_is_refused_when_it_is_not_on(self) -> None:
        response = self._post("disable", {"password": ADMIN_PASSWORD, "totp_code": "123456"})
        self.assertEqual(self._error(response), (409, "totp_not_enabled"))


class AdministratorResetTests(_TwoFactorCase):
    def setUp(self) -> None:
        super().setUp()
        self.operator = auth_service.create_user(
            {"name": "operator", "password": OPERATOR_PASSWORD, "role": "backup_operator"}
        )

    def _operator_session(self) -> str:
        return self._login("operator", OPERATOR_PASSWORD).json()["token"]

    def _reset(self, user_id: str, value=True, token: str | None = None):
        return self.client.patch(
            f"/api/v1/users/{user_id}", json={"totp_reset": value}, headers=self._auth_headers(token or self.token)
        )

    def test_it_turns_the_user_off_and_signs_them_out(self) -> None:
        token = self._operator_session()
        secret = self._post("setup", {"password": OPERATOR_PASSWORD}, token).json()["secret"]
        self.assertEqual(self._post("enable", {"totp_code": self._code(secret)}, token).status_code, 200)
        response = self._reset(self.operator["id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["totpEnabled"])
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(token)).status_code, 401)
        self.assertEqual(self._login("operator", OPERATOR_PASSWORD).status_code, 200)

    def test_nobody_resets_their_own(self) -> None:
        self._turn_on()
        self.assertEqual(self._error(self._reset(self._admin()["id"])), (409, "own_totp"))
        self.assertTrue(self._admin()["totp_enabled"])

    def test_a_user_without_it_is_left_alone_and_keeps_their_sessions(self) -> None:
        token = self._operator_session()
        self.assertEqual(self._reset(self.operator["id"]).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self._auth_headers(token)).status_code, 200)

    def test_the_value_must_be_a_boolean(self) -> None:
        self.assertEqual(self._reset(self.operator["id"], "yes").status_code, 422)

    def test_a_role_without_user_edit_cannot(self) -> None:
        self.assertEqual(self._reset(self._admin()["id"], token=self._operator_session()).status_code, 403)

    def test_a_custom_role_stored_before_this_feature_can_set_it_up(self) -> None:
        created = self.client.post(
            "/api/v1/roles",
            json={"name": "Older role", "allowed_functions": ["dashboard.view"]},
            headers=self._auth_headers(self.token),
        )
        self.assertLess(created.status_code, 300, created.text)
        role_id = created.json()["id"]
        stored = dict(CUSTOM_ROLES[role_id])
        stored["allowed_functions"] = [
            function for function in stored["allowed_functions"] if not function.startswith("auth.totp_")
        ]
        persist_custom_role(stored)
        auth_service.create_user({"name": "older", "password": OPERATOR_PASSWORD, "role": role_id})
        token = self._login("older", OPERATOR_PASSWORD).json()["token"]
        self.assertEqual(self._post("setup", {"password": OPERATOR_PASSWORD}, token).status_code, 200)


class NothingSecretInLogsTests(_TwoFactorCase):
    def setUp(self) -> None:
        super().setUp()
        self.logged: list[str] = []
        self.logger_names: set[str] = set()
        test = self

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                test.logger_names.add(record.name)
                test.logged.append(f"{record.name} {record.getMessage()} {record.args!r} {record.exc_text or ''}")

        # Every logger at DEBUG, whatever the server is configured to write,
        # and the handler also on each that does not propagate to the root
        # (python.srv among them), so that no record escapes it.
        handler = Capture(level=logging.DEBUG)
        loggers = [logging.getLogger()] + [
            item for item in logging.root.manager.loggerDict.values() if isinstance(item, logging.Logger)
        ]
        for logger in loggers:
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.DEBUG)
            if logger is logging.getLogger() or not logger.propagate:
                logger.addHandler(handler)
                self.addCleanup(logger.removeHandler, handler)

    def test_a_whole_life_cycle_leaves_no_secret_or_code_in_any_log_or_audit_record(self) -> None:
        setup = self._start()
        first = self._code(setup["secret"])
        codes = self._post("enable", {"totp_code": first}).json()["recovery_codes"]
        wrong = "000000" if first != "000000" else "111111"
        self._sign_in(totp_code=wrong)
        self._advance()
        second = self._code(setup["secret"])
        self.assertEqual(self._sign_in(totp_code=second).status_code, 200)
        self._sign_in(recovery_code=codes[0])
        self._advance()
        third = self._code(setup["secret"])
        fresh = self._post("recovery-codes", {"password": ADMIN_PASSWORD, "totp_code": third}).json()["recovery_codes"]
        renewed = self._start(recovery_code=fresh[0])
        self.assertEqual(self._post("enable", {"totp_code": self._code(renewed["secret"])}).status_code, 200)

        self.assertIn("python.srv.services.audit_service", self.logger_names, "the server's own logs were not captured")
        written = "\n".join(self.logged) + repr(list(AUDIT_RECORDS))
        for secret in (setup["secret"], renewed["secret"], first, second, third, wrong, *codes, *fresh, ADMIN_PASSWORD):
            self.assertNotIn(secret, written)
            self.assertNotIn(secret.replace("-", ""), written)


class AuditTests(_TwoFactorCase):
    def setUp(self) -> None:
        super().setUp()
        self.records: list[dict] = []
        audit = patch.object(audit_service, "record", side_effect=lambda **fields: self.records.append(fields))
        audit.start()
        self.addCleanup(audit.stop)

    def _logins(self) -> list[dict]:
        return [record for record in self.records if record["function_id"] == "auth.login"]

    def test_being_asked_for_the_code_is_not_a_failed_sign_in(self) -> None:
        self._turn_on()
        self.records.clear()
        self._sign_in()
        self.assertEqual(self._logins(), [])

    def test_a_wrong_code_is_a_failed_sign_in_and_a_right_one_a_success(self) -> None:
        secret, _ = self._turn_on()
        self.records.clear()
        self._sign_in(totp_code="000000")
        self._sign_in(totp_code=self._code(secret, 1))
        self.assertEqual([record["result"] for record in self._logins()], ["failed", "allowed"])

    def test_a_recovery_code_sign_in_is_visible(self) -> None:
        _, codes = self._turn_on()
        self.records.clear()
        self._sign_in(recovery_code=codes[0])
        self.assertEqual(self._logins()[0]["details"], {"recovery_codes_left": 9})


class _RecordingDatabase:
    def __init__(self) -> None:
        self.users: dict[str, dict] = {}

    def find(self, collection: str, query: dict) -> list[dict]:
        return [dict(row) for row in self.users.values()] if collection == "users" else []

    def upsert(self, collection: str, id_field: str, document: dict) -> None:
        # As the real one: sets what the document holds and removes nothing.
        if collection == "users":
            self.users.setdefault(document[id_field], {}).update(document)


class StorageTests(_TwoFactorCase):
    def setUp(self) -> None:
        super().setUp()
        self.database = _RecordingDatabase()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.key_file = Path(directory.name) / "credentials.key"
        self.key_file.write_bytes(Fernet.generate_key())
        for patcher in (
            patch.object(mongo_db_wrapper, "get_database", return_value=self.database),
            patch.dict(os.environ, {credential_encryption.KEY_FILE_ENV: str(self.key_file)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)

    def _restart(self) -> None:
        USERS.clear()
        USERS.update(load_users())

    def _stored(self) -> dict:
        return self.database.users[self._admin()["id"]]

    def test_secrets_are_encrypted_on_disk_and_in_clear_in_memory(self) -> None:
        secret, _ = self._turn_on()
        pending = self._start(totp_code=self._code(secret, 1))["secret"]
        self.assertNotIn(secret, repr(self.database.users))
        self.assertNotIn(pending, repr(self.database.users))
        for field in ("totp_secret", "totp_pending_secret"):
            self.assertTrue(credential_encryption.is_encrypted(self._stored()[field]), field)
        self.assertEqual((self._admin()["totp_secret"], self._admin()["totp_pending_secret"]), (secret, pending))

    def test_it_still_works_after_a_restart(self) -> None:
        secret, _ = self._turn_on()
        self._restart()
        self.assertEqual(self._sign_in(totp_code=self._code(secret, 1)).status_code, 200)

    def test_a_re_enrollment_survives_a_restart_on_both_sides(self) -> None:
        old, _ = self._turn_on()
        self._advance()
        new = self._start(totp_code=self._code(old))["secret"]
        self._restart()
        self.assertEqual(self._post("enable", {"totp_code": self._code(new)}).status_code, 200)
        self._restart()
        self._advance()
        self.assertEqual(self._sign_in(totp_code=self._code(old)).json()["error"]["code"], "invalid_totp_code")
        self.assertEqual(self._sign_in(totp_code=self._code(new)).status_code, 200)

    def test_turning_it_off_survives_a_restart(self) -> None:
        secret, _ = self._turn_on()
        self._post("disable", {"password": ADMIN_PASSWORD, "totp_code": self._code(secret, 1)})
        self._restart()
        self.assertFalse(self._admin()["totp_enabled"])
        self.assertIsNone(self._stored()["totp_secret"])
        self.assertEqual(self._sign_in().status_code, 200)

    def test_used_codes_stay_used_after_a_restart(self) -> None:
        secret, codes = self._turn_on()
        code = self._code(secret, 1)
        self.assertEqual(self._sign_in(totp_code=code).status_code, 200)
        self.assertEqual(self._sign_in(recovery_code=codes[0]).status_code, 200)
        self._restart()
        self.assertEqual(self._sign_in(totp_code=code).json()["error"]["code"], "totp_code_used")
        self.assertEqual(self._sign_in(recovery_code=codes[0]).json()["error"]["code"], "invalid_recovery_code")

    def test_a_catalog_export_moves_to_a_server_with_another_key(self) -> None:
        secret, codes = self._turn_on()
        snapshot = snapshot_catalog()

        self.key_file.write_bytes(Fernet.generate_key())
        credential_encryption.reset_cipher()
        restore_catalog(snapshot)
        self._restart()

        self.assertEqual(self._sign_in(totp_code=self._code(secret, 1)).status_code, 200)
        self.assertEqual(self._sign_in(recovery_code=codes[0]).status_code, 200)

    def test_raw_database_files_without_the_key_still_take_a_recovery_code(self) -> None:
        secret, codes = self._turn_on()
        self.key_file.write_bytes(Fernet.generate_key())
        credential_encryption.reset_cipher()
        self._restart()
        self.assertEqual(self._sign_in(totp_code=self._code(secret, 1)).json()["error"]["code"], "totp_secret_unreadable")
        self.assertEqual(self._sign_in(recovery_code=codes[0]).status_code, 200)

    def test_a_server_that_cannot_use_its_key_stores_no_secret(self) -> None:
        self.key_file.write_bytes(b"")
        credential_encryption.reset_cipher()
        response = self._post("setup", {"password": ADMIN_PASSWORD})
        self.assertEqual(self._error(response), (503, "credential_key_unavailable"))
        self.assertFalse(self._admin().get("totp_pending_secret"))
        self.assertEqual(self.database.users, {})


if __name__ == "__main__":
    unittest.main()
