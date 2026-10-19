# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests
import urllib3

from ..errors import CliStructuredError
from ..exit_codes import GENERAL_ERROR

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_VERSION_HEADER = "X-ABR-API-Version"


def _load_apiver(path: Path) -> tuple[int, int]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return int(values["ABR_API_MAJOR"]), int(values["ABR_API_MINOR"])


CLIENT_API_MAJOR, CLIENT_API_MINOR = _load_apiver(Path(__file__).parents[1] / "APIVER")


class AltaStorApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        token: str | None = None,
        verify: bool | str = False,
        command: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.verify = verify
        self.command = command
        self._version_checked = False

    def _check_api_version(self, response: requests.Response) -> None:
        if self._version_checked:
            return
        self._version_checked = True
        server_header = response.headers.get(API_VERSION_HEADER)
        if not server_header:
            return
        try:
            server_major_str, server_minor_str = server_header.split(".", 1)
            server_major, server_minor = int(server_major_str), int(server_minor_str)
        except ValueError:
            return
        if server_major != CLIENT_API_MAJOR:
            raise CliStructuredError(
                code="api_version_incompatible",
                message=(
                    f"Incompatible API version: server speaks {server_major}.{server_minor}, "
                    f"client speaks {CLIENT_API_MAJOR}.{CLIENT_API_MINOR}. Major versions must match "
                    "— upgrade the CLI or the server."
                ),
                exit_code=GENERAL_ERROR,
            )
        if server_minor != CLIENT_API_MINOR:
            if server_minor > CLIENT_API_MINOR:
                logger.warning(
                    "Server API is newer (%s.%s) than this CLI (%s.%s) — consider upgrading the CLI.",
                    server_major, server_minor, CLIENT_API_MAJOR, CLIENT_API_MINOR,
                )
            else:
                logger.warning(
                    "Server API is older (%s.%s) than this CLI (%s.%s) — the server needs an upgrade.",
                    server_major, server_minor, CLIENT_API_MAJOR, CLIENT_API_MINOR,
                )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        refresh: bool = False,
    ) -> Any:
        headers: dict[str, str] = {
            "X-Client-Type": "cli",
            API_VERSION_HEADER: f"{CLIENT_API_MAJOR}.{CLIENT_API_MINOR}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Names the command in the server's audit log; a refresh is a read the
        # CLI repeats on its own (progress polling), which the log leaves out.
        if self.command:
            headers["X-ABR-Command"] = self.command
        if refresh:
            headers["X-ABR-Refresh"] = "1"
        request_kwargs: dict[str, Any] = {
            "json": json,
            "params": params,
            "timeout": timeout or self.timeout,
            "headers": headers,
            "verify": self.verify,
        }

        response = requests.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            **request_kwargs,
        )
        self._check_api_version(response)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def get(
        self, path: str, params: dict[str, Any] | None = None, timeout: int | None = None, *, refresh: bool = False
    ) -> Any:
        return self._request("GET", path, params=params, timeout=timeout, refresh=refresh)

    def post(
        self,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._request("POST", path, json=json, params=params, timeout=timeout)

    def patch(
        self,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._request("PATCH", path, json=json, params=params, timeout=timeout)

    def put(
        self,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._request("PUT", path, json=json, params=params, timeout=timeout)

    def delete(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        return self._request("DELETE", path, json=json, params=params, timeout=timeout)
