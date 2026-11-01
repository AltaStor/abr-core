# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The notification script. These run real scripts as real processes: what is
under test is what a script can see and what stops it."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import AUDIT_RECORDS, SYSTEM_STATE
from python.srv.services import audit_service, notification_service, rbac, script_channel
from python.srv.services.api_errors import ApiError
from python.srv.services.notification_events import DeliveryError, Notification
from python.tests.srv.rbac.rbac_case import RbacApiCase

NOTIFICATION = Notification(
    "job.failed", 'Backup job "nightly" failed', "The body\n", "error", {"job_name": "nightly", "failed": 1}
)


def run(notification: Notification = NOTIFICATION) -> dict:
    entry: dict = {}
    entry.update(script_channel.send(notification, entry))
    return entry


def forget_history() -> None:
    for channel in notification_service.CHANNELS:
        channel.history._items = None


def in_memory(case: unittest.TestCase) -> None:
    """Keep the settings in memory. Importing the server applies defaults.env,
    so without this they would go to whatever catalog answers on its URI."""
    database = patch("python.srv.core.mongo_db_wrapper.get_database", return_value=None)
    database.start()
    case.addCleanup(database.stop)
    SYSTEM_STATE.clear()
    forget_history()
    case.addCleanup(SYSTEM_STATE.clear)
    case.addCleanup(forget_history)


class _ScriptCase(unittest.TestCase):
    def setUp(self) -> None:
        in_memory(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def upload(self, content: str, name: str = "notify.sh") -> dict:
        return script_channel.upload({"name": name, "script_content": content}, updated_by="admin")

    def refused(self, call, *args, **kwargs) -> ApiError:
        with self.assertRaises(ApiError) as raised:
            call(*args, **kwargs)
        return raised.exception


class UploadTests(_ScriptCase):
    def test_a_script_is_stored_encrypted_with_its_hash(self) -> None:
        file = self.upload("#!/bin/sh\necho hello\n")["file"]
        self.assertEqual((file["name"], file["size"], file["uploaded_by"]), ("notify.sh", 21, "admin"))
        self.assertEqual(len(file["sha256"]), 64)
        self.assertNotIn("content", file)
        self.assertNotIn("echo hello", SYSTEM_STATE[script_channel.STATE_ID]["file"]["content"])
        self.assertEqual(script_channel.content()["script_content"], "#!/bin/sh\necho hello\n")

    def test_what_cannot_run_is_refused(self) -> None:
        for content, expected in (
            ("", "Choose a script"),
            ("echo no interpreter\n", "#! line"),
            ("#!\necho\n", "names no interpreter"),
            ("#!sh\necho\n", "full path"),
            ("#!/no/such/shell\necho\n", "does not exist"),
            ("#!/usr/bin/env no-such-language\necho\n", "does not exist"),
            ("#!/bin/sh\n\x00binary", "must be text"),
            ("#!/bin/sh\n" + "#" * script_channel.MAX_SCRIPT_BYTES, "larger than"),
        ):
            with self.subTest(content=content[:30]):
                error = self.refused(self.upload, content)
                self.assertEqual((error.status_code, error.code), (422, "invalid_notification_script"))
                self.assertIn(expected, error.details["fields"]["script_content"])
        self.assertIsNone(script_channel.load_settings()["file"])

    def test_windows_line_ends_and_a_byte_order_mark_do_not_stop_it_running(self) -> None:
        self.upload("\ufeff#!/bin/sh\r\necho ran\r\n")
        self.assertEqual(run()["output"], "ran")

    def test_the_file_name_loses_any_path(self) -> None:
        self.assertEqual(self.upload("#!/bin/sh\n", name="C:\\scripts\\..\\notify.sh")["file"]["name"], "notify.sh")
        self.assertEqual(self.upload("#!/bin/sh\n", name="../../etc/notify.sh")["file"]["name"], "notify.sh")
        self.assertIn("name", self.refused(self.upload, "#!/bin/sh\n", name="../..").details["fields"])

    def test_it_cannot_be_enabled_before_a_script_is_there(self) -> None:
        error = self.refused(script_channel.update_settings, {"enabled": True}, updated_by="admin")
        self.assertEqual(error.details["fields"]["enabled"], "Upload a script first")

    def test_settings_are_checked_as_a_whole(self) -> None:
        self.upload("#!/bin/sh\n")
        error = self.refused(
            script_channel.update_settings, {"enabled": True, "timeout_seconds": 4, "events": {"no.such": True}}, updated_by="a"
        )
        self.assertEqual(set(error.details["fields"]), {"timeout_seconds", "events.no.such"})
        self.assertFalse(script_channel.load_settings()["enabled"])

    def test_removing_the_script_stops_it_running(self) -> None:
        self.upload("#!/bin/sh\n")
        script_channel.update_settings({"enabled": True}, updated_by="admin")
        script_channel.remove(updated_by="admin")
        self.assertFalse(script_channel.wants(NOTIFICATION))
        self.assertFalse(script_channel.load_settings()["enabled"])

    def test_the_audit_log_masks_the_script(self) -> None:
        self.assertEqual(audit_service.mask({"script_content": "#!/bin/sh\nTOKEN=abc\n"})["script_content"], audit_service.MASK)


class RunTests(_ScriptCase):
    def test_a_script_gets_the_event_on_stdin_in_a_file_and_in_variables(self) -> None:
        stdin_copy, file_copy, variables = (self.folder / name for name in ("stdin", "file", "env"))
        self.upload(f'#!/bin/sh\ncat > {stdin_copy}\ncp "$ABR_EVENT_FILE" {file_copy}\nenv > {variables}\n')
        self.assertEqual(run()["exit_code"], 0)
        document = json.loads(stdin_copy.read_text())
        self.assertEqual(document, json.loads(file_copy.read_text()))
        self.assertEqual((document["event"], document["severity"]), ("job.failed", "error"))
        self.assertEqual(document["details"], {"job_name": "nightly", "failed": 1})
        lines = variables.read_text().splitlines()
        self.assertIn("ABR_EVENT=job.failed", lines)
        self.assertIn("ABR_EVENT_NAME=Backup job failed", lines)
        self.assertIn('ABR_SUBJECT=Backup job "nightly" failed', lines)

    def test_none_of_the_servers_variables_reach_a_script(self) -> None:
        variables = self.folder / "env"
        self.upload(f"#!/bin/sh\nenv > {variables}\n")
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb://u:hunter2@db", "ABR_BOOTSTRAP_ADMIN_PASSWORD": "hunter2"}):
            run()
        self.assertNotIn("hunter2", variables.read_text())
        self.assertNotIn("MONGODB_URI", variables.read_text())

    def test_a_large_event_does_not_block_a_script_that_ignores_it(self) -> None:
        self.upload("#!/bin/sh\nexit 0\n")
        big = Notification("job.failed", "s", "b" * 300_000, "error", {"vms": ["v" * 100] * 2000})
        self.assertEqual(run(big)["exit_code"], 0)

    def test_a_failing_script_reports_its_exit_code_and_output(self) -> None:
        self.upload("#!/bin/sh\necho to stdout\necho to stderr >&2\nexit 3\n")
        with self.assertRaises(DeliveryError) as raised:
            run()
        failure = raised.exception
        self.assertEqual((failure.code, failure.retryable, failure.extra["exit_code"]), ("script_failed", False, 3))
        self.assertIn("to stdout", failure.extra["output"])
        self.assertIn("to stderr", failure.extra["output"])

    def test_the_time_limit_stops_the_script_and_what_it_started(self) -> None:
        marker = self.folder / "child"
        self.upload(f"#!/bin/sh\nsleep 300 &\necho $! > {marker}\necho started\nwait\n")
        SYSTEM_STATE[script_channel.STATE_ID]["timeout_seconds"] = 1
        with self.assertRaises(DeliveryError) as raised:
            run()
        self.assertEqual(raised.exception.code, "script_timed_out")
        self.assertIn("started", raised.exception.extra["output"])
        child = int(marker.read_text())
        for _ in range(50):
            if not Path(f"/proc/{child}").exists() or "Z" in Path(f"/proc/{child}/stat").read_text().split(")")[1][:3]:
                break
            time.sleep(0.05)
        else:
            self.fail(f"process {child} outlived the time limit")

    def test_output_is_redacted_and_only_its_end_is_kept(self) -> None:
        self.upload("#!/bin/sh\necho 'password=hunter2'\nhead -c 9000 /dev/zero | tr '\\0' x\necho\necho the end\n")
        output = run()["output"]
        self.assertTrue(output.endswith("the end"))
        self.assertLessEqual(len(output), script_channel.MAX_KEPT_OUTPUT)
        self.upload("#!/bin/sh\necho 'password=hunter2'\n")
        self.assertNotIn("hunter2", run()["output"])

    def test_the_run_folder_is_gone_afterwards(self) -> None:
        marker = self.folder / "where"
        self.upload(f"#!/bin/sh\npwd > {marker}\n")
        run()
        self.assertFalse(Path(marker.read_text().strip()).exists())


class ChoosingTests(_ScriptCase):
    def setUp(self) -> None:
        super().setUp()
        self.upload("#!/bin/sh\n")

    def test_it_runs_only_when_on_and_for_its_own_events(self) -> None:
        self.assertFalse(script_channel.wants(NOTIFICATION))
        script_channel.update_settings({"enabled": True, "events": {"job.failed": False, "job.succeeded": True}}, updated_by="a")
        self.assertFalse(script_channel.wants(NOTIFICATION))
        self.assertTrue(script_channel.wants(Notification("job.succeeded", "s", "b")))

    def test_the_switch_on_the_appliance_stops_everything(self) -> None:
        script_channel.update_settings({"enabled": True}, updated_by="a")
        self.assertTrue(script_channel.wants(NOTIFICATION))
        with patch.dict(os.environ, {script_channel.ENABLE_ENV: "0"}):
            self.assertFalse(script_channel.wants(NOTIFICATION))
            for call in (lambda: self.upload("#!/bin/sh\n"), script_channel.test_notification):
                self.assertEqual(self.refused(call).code, "notification_scripts_disabled")


class ThroughTheServiceTests(_ScriptCase):
    def test_notify_runs_the_script_with_e_mail_off_and_records_the_run(self) -> None:
        self.upload("#!/bin/sh\necho delivered\n")
        script_channel.update_settings({"enabled": True}, updated_by="admin")
        self.assertTrue(notification_service.notify("job.failed", "subject", "body\n", severity="error"))
        notification_service.SCRIPT._queue.join()
        latest = notification_service.SCRIPT.history.items()[0]
        self.assertEqual((latest["status"], latest["exit_code"], latest["output"]), ("sent", 0, "delivered"))
        self.assertEqual(latest["script"], "notify.sh")

    def test_a_failed_run_is_not_repeated(self) -> None:
        marker = self.folder / "runs"
        self.upload(f"#!/bin/sh\necho x >> {marker}\nexit 1\n")
        script_channel.update_settings({"enabled": True}, updated_by="admin")
        notification_service.notify("job.failed", "subject", "body\n")
        notification_service.SCRIPT._queue.join()
        latest = notification_service.SCRIPT.history.items()[0]
        self.assertEqual((latest["status"], latest["attempts"]), ("failed", 1))
        self.assertEqual(len(marker.read_text().splitlines()), 1)

    def test_a_test_run_works_while_the_script_is_off(self) -> None:
        self.upload('#!/bin/sh\necho "$ABR_EVENT"\n')
        result = notification_service.run_script_test()
        self.assertEqual((result["status"], result["output"], result["event"]), ("sent", "test", "test"))
        self.assertEqual(notification_service.SCRIPT.history.items()[0]["id"], result["id"])


class ApiTests(RbacApiCase):
    def setUp(self) -> None:
        in_memory(self)
        super().setUp()

    def put_script(self, role: str = rbac.ADMINISTRATOR):
        return self.client.put(
            "/api/v1/notifications/script/file",
            json={"name": "notify.sh", "script_content": "#!/bin/sh\necho secret-output\n"},
            headers=self.headers(role),
        )

    def test_only_the_administrator_touches_the_script(self) -> None:
        for role in (rbac.BACKUP_ADMINISTRATOR, rbac.BACKUP_OPERATOR, rbac.RESTORE_OPERATOR, rbac.AUDITOR):
            with self.subTest(role=role):
                self.assertEqual(self.put_script(role).status_code, 403)
                headers = self.headers(role)
                self.assertEqual(self.client.get("/api/v1/notifications/script/file", headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/api/v1/notifications/script/test", headers=headers).status_code, 403)
                self.assertEqual(self.client.delete("/api/v1/notifications/script/file", headers=headers).status_code, 403)

    def test_an_upload_is_audited_without_its_content(self) -> None:
        self.assertEqual(self.put_script().status_code, 200)
        record = next(item for item in AUDIT_RECORDS if item["function_id"] == "notification.script")
        self.assertNotIn("secret-output", json.dumps(record))
        self.assertEqual(record["details"]["script"], "notify.sh")
        self.assertEqual(len(record["details"]["sha256"]), 64)

    def test_an_auditor_sees_the_runs_but_not_what_they_printed(self) -> None:
        self.put_script()
        response = self.client.post("/api/v1/notifications/script/test", headers=self.headers(rbac.ADMINISTRATOR))
        self.assertEqual(response.json()["output"], "secret-output")
        admin = self.client.get("/api/v1/notifications/script", headers=self.headers(rbac.ADMINISTRATOR)).json()
        self.assertEqual(admin["runs"][0]["output"], "secret-output")
        auditor = self.client.get("/api/v1/notifications/script", headers=self.headers(rbac.AUDITOR))
        self.assertEqual(auditor.status_code, 200)
        self.assertEqual(auditor.json()["file"]["name"], "notify.sh")
        self.assertEqual(auditor.json()["runs"][0]["status"], "sent")
        self.assertNotIn("secret-output", auditor.text)

    def test_a_script_that_fails_its_test_is_still_an_answer(self) -> None:
        self.client.put(
            "/api/v1/notifications/script/file",
            json={"name": "fail.sh", "script_content": "#!/bin/sh\nexit 7\n"},
            headers=self.headers(rbac.ADMINISTRATOR),
        )
        response = self.client.post("/api/v1/notifications/script/test", headers=self.headers(rbac.ADMINISTRATOR))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["exit_code"]), ("failed", 7))
        self.assertEqual(response.json()["error"]["code"], "script_failed")
