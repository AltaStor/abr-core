# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import requests

from python.cli import abr_cli


def _response(status_code: int, payload: dict[str, object]) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


class HttpStatusToExitCodeTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_a_503_from_the_server_maps_to_the_invalid_state_exit_code(
        self, request_mock: object
    ) -> None:
        request_mock.return_value = _response(
            503,
            {
                "error": {
                    "code": "catalog_unavailable",
                    "message": "The catalog database is unavailable; the change was not applied.",
                }
            },
        )

        with self.assertRaises(SystemExit) as ctx:
            abr_cli.main(
                [
                    "--base-url",
                    "http://example.test",
                    "repo",
                    "add",
                    "--name",
                    "outage-probe",
                    "--type",
                    "local",
                    "--path",
                    "/tmp",
                ]
            )

        self.assertEqual(ctx.exception.code, 4)

    @patch("python.cli.api.client.requests.request")
    def test_a_409_from_the_server_still_maps_to_the_same_exit_code(
        self, request_mock: object
    ) -> None:
        request_mock.return_value = _response(
            409,
            {"error": {"code": "repository_path_overlaps", "message": "Repository path overlaps"}},
        )

        with self.assertRaises(SystemExit) as ctx:
            abr_cli.main(
                [
                    "--base-url",
                    "http://example.test",
                    "repo",
                    "add",
                    "--name",
                    "outage-probe",
                    "--type",
                    "local",
                    "--path",
                    "/tmp",
                ]
            )

        self.assertEqual(ctx.exception.code, 4)


if __name__ == "__main__":
    unittest.main()
