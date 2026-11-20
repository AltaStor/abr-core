# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""A fake PVE REST API for tests, served through httpx's mock transport."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from python.srv.plugins.pve.pve_api import PveApi


class FakePve:
    """Answers like PVE's API from a table of paths, and records every request."""

    def __init__(self, routes: dict[tuple[str, str], object] | None = None, down: set[str] | None = None) -> None:
        self.routes = dict(routes or {})
        self.down = set(down or ())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        path = request.url.path.removeprefix("/api2/json")
        answer = self.routes.get((request.method, path))
        if callable(answer):
            answer = answer(request)
        if answer is None:
            return httpx.Response(501, json={"data": None}, extensions={"reason_phrase": b"Method not implemented"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def form(self, index: int = -1) -> dict[str, list[str]]:
        return parse_qs(self.requests[index].content.decode())

    def api(self, addresses: list[str] | str = "pve1") -> PveApi:
        return PveApi(
            addresses, transport=httpx.MockTransport(self), token_id="abr@pve!abr", token_secret="5e3c-secret"
        )
