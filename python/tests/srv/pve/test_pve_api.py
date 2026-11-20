# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import datetime
import http.server
import json
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from python.srv.plugins.pve.pve_api import (
    PveApi,
    PveApiError,
    PveAuthError,
    PveCertificateError,
    PvePermissionError,
    PveUnreachableError,
    Upid,
    task_error,
    task_succeeded,
)
from python.tests.srv.pve.fake_api import FakePve

UPID = "UPID:node1:0004B3A2:01C9F3E1:691F0A2B:vzdump:9002:abr@pve!abr:"
TOKEN = dict(token_id="abr@pve!abr", token_secret="5e3c-secret")


def api(fake: FakePve, addresses: list[str] | str = "pve1") -> PveApi:
    return PveApi(addresses, transport=httpx.MockTransport(fake), **TOKEN)


def failure(status: int, reason: str, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {"data": None}, extensions={"reason_phrase": reason.encode()})


class TaskIdTests(unittest.TestCase):
    def test_a_task_id_names_its_node_type_vm_and_user(self):
        upid = Upid.parse(UPID)
        self.assertEqual(
            (upid.node, upid.type, upid.id, upid.user, upid.pid, upid.starttime),
            ("node1", "vzdump", "9002", "abr@pve!abr", 0x4B3A2, 0x691F0A2B),
        )

    def test_other_text_is_not_a_task_id(self):
        for text in ("", "UPID:node", "TASK:node1:1:2:3:vzdump:9002:root@pam:", UPID.replace("0004B3A2", "pid")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Upid.parse(text)

    def test_a_task_succeeded_with_ok_or_warnings_only(self):
        self.assertTrue(task_succeeded({"status": "stopped", "exitstatus": "OK"}))
        self.assertTrue(task_succeeded({"status": "stopped", "exitstatus": "WARNINGS: 2"}))
        self.assertFalse(task_succeeded({"status": "running"}))
        self.assertEqual(
            task_error({"status": "stopped", "exitstatus": "interrupted by signal"}), "interrupted by signal"
        )
        self.assertEqual(task_error({"status": "stopped", "exitstatus": "OK"}), "")
        self.assertEqual(task_error({"status": "running"}), "")


class RequestTests(unittest.TestCase):
    def test_requests_carry_the_token_and_pve_parameters(self):
        fake = FakePve(
            {("POST", "/nodes/node1/vzdump"): {"data": UPID}, ("GET", "/version"): {"data": {"version": "9.2.26"}}}
        )
        with api(fake) as client:
            self.assertEqual(client.get("/version", verbose=True, skip=None), {"version": "9.2.26"})
            upid = client.start_task("/nodes/node1/vzdump", vmid=9002, remove=False, storage="abr", bwlimit=None)
        self.assertEqual(upid, UPID)
        self.assertEqual(fake.requests[0].headers["Authorization"], "PVEAPIToken=abr@pve!abr=5e3c-secret")
        self.assertEqual(fake.requests[0].url.params["verbose"], "1")
        self.assertNotIn("skip", fake.requests[0].url.params)
        self.assertEqual(fake.form(), {"vmid": ["9002"], "remove": ["0"], "storage": ["abr"]})

    def test_a_start_that_returns_no_task_id_fails(self):
        fake = FakePve({("POST", "/nodes/node1/vzdump"): {"data": None}})
        with api(fake) as client, self.assertRaisesRegex(PveApiError, "started no task"):
            client.start_task("/nodes/node1/vzdump", vmid=9002)

    def test_errors_say_what_pve_said(self):
        fake = FakePve(
            {
                ("GET", "/denied"): failure(403, "Permission check failed (/vms/9002, VM.Backup)"),
                ("GET", "/token"): failure(401, "invalid token value!"),
                ("GET", "/vm"): failure(500, "unused", {"data": None, "message": "VM 9002 not running\n"}),
                ("POST", "/params"): failure(
                    400, "Parameter verification failed.", {"data": None, "errors": {"vmid": "invalid format\n"}}
                ),
            }
        )
        with api(fake) as client:
            with self.assertRaisesRegex(
                PvePermissionError, r"GET /denied: Permission check failed \(/vms/9002, VM.Backup\)"
            ):
                client.get("/denied")
            with self.assertRaisesRegex(PveAuthError, "refused AltaStor's API token"):
                client.get("/token")
            with self.assertRaisesRegex(PveApiError, "GET /vm: VM 9002 not running$") as raised:
                client.get("/vm")
            self.assertEqual(raised.exception.status, 500)
            with self.assertRaisesRegex(PveApiError, r"Parameter verification failed. \(vmid: invalid format\)"):
                client.post("/params", vmid="x")

    def test_a_node_that_cannot_be_reached_hands_over_to_the_next(self):
        fake = FakePve({("GET", "/version"): {"data": {"release": "9.2"}}}, down={"pve1"})
        with api(fake, ["pve1", "pve2", "pve3"]) as client:
            self.assertEqual(client.get("/version"), {"release": "9.2"})
            self.assertEqual(client.address, "pve2")
            fake.down = {"pve2"}
            client.get("/version")
            self.assertEqual(client.address, "pve3")
        self.assertEqual([request.url.host for request in fake.requests], ["pve2", "pve3"])

    def test_no_node_answering_is_an_unreachable_cluster(self):
        fake = FakePve(down={"pve1", "pve2"})
        with api(fake, ["pve1", "pve2"]) as client, self.assertRaisesRegex(
            PveUnreachableError, "No node of the PVE cluster answers on port 8006"
        ):
            client.get("/version")


class TaskTests(unittest.TestCase):
    TASK_PATH = "/nodes/node1/tasks/" + quote(UPID, safe="")

    def log_route(self, lines: list[str]):
        def answer(request: httpx.Request) -> httpx.Response:
            start, limit = int(request.url.params["start"]), int(request.url.params["limit"])
            page = [{"n": start + index + 1, "t": text} for index, text in enumerate(lines[start : start + limit])]
            # An empty log answers with one line that is not in the log.
            return httpx.Response(
                200, json={"data": page or [{"n": start + 1, "t": "no content"}], "total": len(lines)}
            )

        return answer

    def test_the_log_comes_in_pages_from_any_line_on(self):
        lines = [f"line {number}" for number in range(1, 1201)]
        fake = FakePve()
        fake.routes[("GET", "/nodes/node1/tasks/" + UPID + "/log")] = self.log_route(lines)
        with api(fake) as client:
            self.assertEqual(client.task_log(UPID), lines)
            self.assertEqual(client.task_log(UPID, 1150), lines[1150:])
            self.assertEqual(client.task_log(UPID, 1200), [])
        self.assertEqual(fake.requests[0].url.raw_path.decode().split("?")[0], "/api2/json" + self.TASK_PATH + "/log")

    def test_an_empty_log_has_no_lines(self):
        fake = FakePve()
        fake.routes[("GET", "/nodes/node1/tasks/" + UPID + "/log")] = self.log_route([])
        with api(fake) as client:
            self.assertEqual(client.task_log(UPID), [])

    def follow(self, steps: list[tuple[str, list[str]]], **options):
        """A task that shows one status and log per poll: running until the last step."""
        fake = FakePve()
        polls = {"count": 0}
        stop_requests: list[str] = []

        def status(request):
            index = min(polls["count"], len(steps) - 1)
            polls["count"] += 1
            state = steps[index][0]
            if isinstance(state, Exception):
                raise state
            return {"data": {"status": "running"} if state == "running" else {"status": "stopped", "exitstatus": state}}

        def log(request):
            index = min(polls["count"] - 1, len(steps) - 1)
            return self.log_route(steps[index][1])(request)

        def stop(request):
            stop_requests.append(request.url.path)
            return {"data": None}

        task = "/nodes/node1/tasks/" + UPID
        fake.routes.update({("GET", task + "/status"): status, ("GET", task + "/log"): log, ("DELETE", task): stop})
        seen: list[str] = []
        sleeps: list[float] = []
        with api(fake) as client:
            final = client.wait_task(UPID, on_log=seen.append, sleep=sleeps.append, **options)
        return final, seen, stop_requests, sleeps

    def test_following_a_task_passes_each_log_line_on_once(self):
        final, seen, stops, sleeps = self.follow(
            [
                ("running", ["INFO: starting"]),
                ("running", ["INFO: starting", "INFO: 50%"]),
                ("OK", ["INFO: starting", "INFO: 50%", "TASK OK"]),
            ]
        )
        self.assertEqual(final, {"status": "stopped", "exitstatus": "OK"})
        self.assertEqual(seen, ["INFO: starting", "INFO: 50%", "TASK OK"])
        self.assertEqual((stops, len(sleeps)), ([], 2))

    def test_following_from_a_line_skips_what_was_seen(self):
        _final, seen, _stops, _sleeps = self.follow([("OK", ["a", "b", "c"])], log_start=2)
        self.assertEqual(seen, ["c"])

    def test_a_stop_is_sent_once_and_the_task_followed_to_its_end(self):
        final, seen, stops, _sleeps = self.follow(
            [
                ("running", ["a"]),
                ("running", ["a"]),
                ("running", ["a", "b"]),
                ("interrupted by signal", ["a", "b", "TASK ERROR: interrupted by signal"]),
            ],
            should_stop=lambda: True,
        )
        self.assertEqual(len(stops), 1)
        self.assertEqual(final["exitstatus"], "interrupted by signal")
        self.assertEqual(seen[-1], "TASK ERROR: interrupted by signal")

    def test_a_short_break_of_the_api_does_not_end_following(self):
        lost = httpx.ConnectError("refused")
        clock = iter(range(0, 1000, 10))
        with self.assertLogs("python.srv.plugins.pve.pve_api", "WARNING") as logs:
            final, seen, _stops, _sleeps = self.follow(
                [("running", ["a"]), (lost, []), (lost, []), ("OK", ["a", "TASK OK"])], clock=lambda: next(clock)
            )
        self.assertEqual((final["exitstatus"], seen), ("OK", ["a", "TASK OK"]))
        self.assertEqual(len(logs.records), 2)
        self.assertIn("the API cannot be reached, trying again", logs.output[0])

    def test_a_long_break_of_the_api_ends_following(self):
        lost = httpx.ConnectError("refused")
        clock = iter(range(0, 10000, 100))
        with self.assertLogs("python.srv.plugins.pve.pve_api", "WARNING"):
            with self.assertRaisesRegex(PveUnreachableError, "Lost the PVE API while following task .* for 300s"):
                self.follow([("running", ["a"])] + [(lost, [])] * 10, clock=lambda: next(clock), unreachable_grace=300)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject: str, key, issuer: str, issuer_key, *, ca: bool) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


class PinnedCertificateTests(unittest.TestCase):
    """A local HTTPS server with a node certificate issued by a cluster CA, as pveproxy has."""

    @classmethod
    def setUpClass(cls):
        ca_key, node_key, other_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
        cls.ca = _certificate("Proxmox Virtual Environment", ca_key, "Proxmox Virtual Environment", ca_key, ca=True)
        cls.node = _certificate("node1.example.test", node_key, "Proxmox Virtual Environment", ca_key, ca=False)
        cls.other = _certificate("other", other_key, "other", other_key, ca=True)
        folder = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        (folder / "node.pem").write_text(_pem(cls.node))
        (folder / "node.key").write_bytes(
            node_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        cls.headers: list[dict[str, str]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.headers.append(dict(self.headers))
                body = json.dumps({"data": {"release": "9.2"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(folder / "node.pem", folder / "node.key")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.headers.clear()

    def call(self, trusted: list[str]) -> object:
        with PveApi("127.0.0.1", port=self.port, trusted_certificates=trusted, **TOKEN) as client:
            return client.get("/version")

    def test_the_cluster_ca_is_trusted_for_a_node_it_issued_whatever_its_name(self):
        self.assertEqual(self.call([_pem(self.ca)]), {"release": "9.2"})

    def test_a_pinned_node_certificate_is_trusted_without_its_ca(self):
        self.assertEqual(self.call([_pem(self.node)]), {"release": "9.2"})
        self.assertEqual(self.headers[0]["Authorization"], "PVEAPIToken=abr@pve!abr=5e3c-secret")

    def test_another_certificate_is_refused_before_the_token_is_sent(self):
        with self.assertRaisesRegex(PveCertificateError, "is not the one AltaStor trusts for this cluster"):
            self.call([_pem(self.other)])
        self.assertEqual(self.headers, [])

    def test_without_pins_the_system_cas_decide(self):
        with self.assertRaises(PveCertificateError):
            self.call([])
        self.assertEqual(self.headers, [])


if __name__ == "__main__":
    unittest.main()
