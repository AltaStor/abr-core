# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The REST API of a Proxmox VE cluster, called with an API token.

Every node answers for the whole cluster: pveproxy passes a request for another node's path on to that node. The client
tries the cluster's nodes in turn when one cannot be reached, so a backup goes on while a node is down.

The certificates are pinned. The client trusts only the certificates it is given, the cluster's root CA and the node
certificates abr-core read over SSH, and checks them in the TLS handshake, before the token is sent. Host names are not
checked, since nodes are often reached by addresses their certificates do not name. Without pinned certificates the
client checks the certificate against the system's CAs and the host name, as for a node with a public certificate.

Backups and restores run as PVE tasks. A task starts at once and returns its UPID; the client follows its status and
log until it ends, and stops it on request. The task runs in a pvedaemon worker on the node and does not depend on the
connection, so a lost connection only pauses following it.
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8006
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 60.0
TASK_POLL_SECONDS = 2.0
# A task outlives a node's pveproxy restart or a network break; following it waits this long for the API to come back.
TASK_UNREACHABLE_GRACE_SECONDS = 300.0
LOG_PAGE_LINES = 500


class PveApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class PveUnreachableError(PveApiError):
    pass


class PveCertificateError(PveApiError):
    pass


class PveAuthError(PveApiError):
    pass


class PvePermissionError(PveApiError):
    pass


@dataclass(frozen=True)
class Upid:
    """A task ID: UPID:node:pid:pstart:starttime:type:id:user:, the numbers in hex."""

    text: str
    node: str
    pid: int
    pstart: int
    starttime: int
    type: str
    id: str
    user: str

    @classmethod
    def parse(cls, text: str) -> "Upid":
        parts = str(text or "").split(":")
        if len(parts) < 9 or parts[0] != "UPID" or parts[-1] != "":
            raise ValueError(f"not a PVE task ID: {text!r}")
        try:
            pid, pstart, starttime = (int(value, 16) for value in parts[2:5])
        except ValueError as exc:
            raise ValueError(f"not a PVE task ID: {text!r}") from exc
        return cls(
            text=text,
            node=parts[1],
            pid=pid,
            pstart=pstart,
            starttime=starttime,
            type=parts[5],
            id=parts[6],
            user=":".join(parts[7:-1]),
        )


def task_succeeded(status: Mapping[str, Any]) -> bool:
    """A task that ended with OK, or with warnings only."""
    exit_status = str(status.get("exitstatus") or "")
    return status.get("status") == "stopped" and (exit_status == "OK" or exit_status.startswith("WARNINGS"))


def task_error(status: Mapping[str, Any]) -> str:
    """Why a task failed, as PVE puts it, or an empty string for a task that succeeded or still runs."""
    if status.get("status") != "stopped" or task_succeeded(status):
        return ""
    return str(status.get("exitstatus") or "unknown error")


def certificate_context(trusted_certificates: Sequence[str] | None) -> ssl.SSLContext:
    """A TLS context that accepts only the given PEM certificates, or the system's CAs without them.

    A trusted node certificate is accepted on its own, without the CA that issued it, which keeps a pinned public
    certificate working; host names are not checked, since the trust is in the certificates themselves.
    """
    if not trusted_certificates:
        return ssl.create_default_context()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata="\n".join(certificate.strip() for certificate in trusted_certificates) + "\n")
    return context


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address and not address.startswith("[") else address


def _form(params: Mapping[str, Any] | None) -> dict[str, str]:
    """PVE parameters as form fields: booleans as 0 and 1, and None left out."""
    form: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        form[key] = ("1" if value else "0") if isinstance(value, bool) else str(value)
    return form


def _is_certificate_failure(exc: BaseException) -> bool:
    cursor: BaseException | None = exc
    seen: list[BaseException] = []
    while cursor is not None and cursor not in seen:
        if isinstance(cursor, ssl.SSLCertVerificationError):
            return True
        seen.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


class PveApi:
    def __init__(
        self,
        addresses: str | Sequence[str],
        *,
        token_id: str,
        token_secret: str,
        port: int = DEFAULT_PORT,
        trusted_certificates: Sequence[str] | None = None,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.addresses = [addresses] if isinstance(addresses, str) else [str(item) for item in addresses if item]
        if not self.addresses:
            raise ValueError("the PVE API needs at least one node address")
        self.port = int(port)
        self._authorization = f"PVEAPIToken={token_id}={token_secret}"
        self._current = 0
        timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_SECONDS)
        if transport is not None:
            self._client = httpx.Client(transport=transport, timeout=timeout)
        else:
            self._client = httpx.Client(verify=certificate_context(trusted_certificates), timeout=timeout)

    def __enter__(self) -> "PveApi":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def address(self) -> str:
        """The node address the last request went to."""
        return self.addresses[self._current]

    def _url(self, address: str, path: str) -> str:
        return f"https://{_url_host(address)}:{self.port}/api2/json{path}"

    def request(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Calls the API and returns the whole JSON answer, whose "data" holds the result."""
        form = _form(params)
        order = list(range(self._current, len(self.addresses))) + list(range(self._current))
        failures: list[tuple[str, BaseException]] = []
        for index in order:
            address = self.addresses[index]
            try:
                response = self._client.request(
                    method,
                    self._url(address, path),
                    params=form if method in ("GET", "DELETE") else None,
                    data=form if method in ("POST", "PUT") else None,
                    headers={"Authorization": self._authorization},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing was sent: the next node can take the request, a task start included.
                logger.debug("PVE API at %s:%s cannot be reached: %s", address, self.port, exc)
                failures.append((address, exc))
                continue
            except httpx.TimeoutException as exc:
                raise PveUnreachableError(
                    f"The PVE API at {address}:{self.port} did not answer {method} {path} in time: {exc}", path=path
                ) from exc
            except httpx.TransportError as exc:
                raise PveUnreachableError(
                    f"The connection to the PVE API at {address}:{self.port} broke during {method} {path}: {exc}",
                    path=path,
                ) from exc
            self._current = index
            return self._answer(response, method, path)
        raise self._unreachable(failures, path)

    def _unreachable(self, failures: list[tuple[str, BaseException]], path: str) -> PveApiError:
        refused = [address for address, exc in failures if _is_certificate_failure(exc)]
        if refused:
            return PveCertificateError(
                f"The TLS certificate of the PVE API at {', '.join(refused)} is not the one AltaStor trusts for this "
                "cluster. If the node's certificate was renewed or replaced, run inventory rescan, which reads it "
                "again over SSH",
                path=path,
            )
        detail = "; ".join(f"{address}: {exc}" for address, exc in failures)
        return PveUnreachableError(f"No node of the PVE cluster answers on port {self.port} ({detail})", path=path)

    def _answer(self, response: httpx.Response, method: str, path: str) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code < 400:
            if not isinstance(body, dict):
                raise PveApiError(
                    f"{method} {path}: the PVE API answered without JSON", status=response.status_code, path=path
                )
            return body
        message = str((body or {}).get("message") or "").strip() if isinstance(body, dict) else ""
        message = message or response.reason_phrase or f"HTTP {response.status_code}"
        errors = (body or {}).get("errors") if isinstance(body, dict) else None
        if isinstance(errors, dict) and errors:
            message += " (" + "; ".join(f"{key}: {str(value).strip()}" for key, value in sorted(errors.items())) + ")"
        text = f"{method} {path}: {message}"
        if response.status_code == 401:
            raise PveAuthError(
                f"{text}. The PVE API refused AltaStor's API token: it was removed, expired or replaced",
                status=401,
                path=path,
            )
        if response.status_code == 403:
            raise PvePermissionError(text, status=403, path=path)
        raise PveApiError(text, status=response.status_code, path=path)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params)["data"]

    def post(self, path: str, **params: Any) -> Any:
        return self.request("POST", path, params)["data"]

    def put(self, path: str, **params: Any) -> Any:
        return self.request("PUT", path, params)["data"]

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params)["data"]

    # ---- tasks ------------------------------------------------------------------------------------------------------

    @staticmethod
    def _task_path(upid: str) -> str:
        return f"/nodes/{quote(Upid.parse(upid).node, safe='')}/tasks/{quote(upid, safe='')}"

    def start_task(self, path: str, *, method: str = "POST", **params: Any) -> str:
        """Starts a task, such as POST /nodes/{node}/vzdump or DELETE /nodes/{node}/qemu/{vmid}; returns its UPID."""
        upid = self.request(method, path, params)["data"]
        try:
            Upid.parse(str(upid or ""))
        except ValueError as exc:
            raise PveApiError(f"{method} {path} started no task: the PVE API answered {upid!r}", path=path) from exc
        return str(upid)

    def task_status(self, upid: str) -> dict[str, Any]:
        """status is "running" or "stopped"; a stopped task has its exitstatus, "OK" when it succeeded."""
        return dict(self.get(f"{self._task_path(upid)}/status"))

    def task_log(self, upid: str, start: int = 0, limit: int | None = None) -> list[str]:
        """The task's log lines from line start on, counted from 0; at most limit of them when given."""
        lines: list[str] = []
        while True:
            page_size = LOG_PAGE_LINES if limit is None else min(LOG_PAGE_LINES, limit - len(lines))
            answer = self.request("GET", f"{self._task_path(upid)}/log", {"start": start, "limit": page_size})
            total = int(answer.get("total") or 0)
            # An empty log answers with one "no content" line; the total tells it apart from a real line.
            page = [str(item.get("t") or "") for item in answer.get("data") or []][: max(total - start, 0)]
            lines.extend(page)
            start += len(page)
            if len(page) < page_size or start >= total or (limit is not None and len(lines) >= limit):
                return lines

    def stop_task(self, upid: str) -> None:
        self.delete(self._task_path(upid))

    def tasks(self, node: str, **filters: Any) -> list[dict[str, Any]]:
        """The node's task history, newest first: vmid, typefilter, userfilter, since, until, source, limit."""
        return [dict(item) for item in self.get(f"/nodes/{quote(node, safe='')}/tasks", **filters) or []]

    def wait_task(
        self,
        upid: str,
        *,
        on_log: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        log_start: int = 0,
        poll_seconds: float = TASK_POLL_SECONDS,
        unreachable_grace: float = TASK_UNREACHABLE_GRACE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Follows a task until it ends and returns its last status.

        Every new log line goes to on_log. When should_stop answers true the task is stopped once, and followed on
        until it has ended, so that its cleanup has run. log_start skips the lines a caller has already seen, as when
        abr-core takes up a task again after a restart.
        """
        stop_sent = False
        unreachable_since: float | None = None
        seen = log_start
        while True:
            try:
                status = self.task_status(upid)
                lines = self.task_log(upid, seen)
                unreachable_since = None
            except PveUnreachableError as exc:
                now = clock()
                unreachable_since = now if unreachable_since is None else unreachable_since
                if now - unreachable_since >= unreachable_grace:
                    raise PveUnreachableError(
                        f"Lost the PVE API while following task {upid} for {int(now - unreachable_since)}s: {exc}",
                        path=exc.path,
                    ) from exc
                logger.warning("PVE task %s: the API cannot be reached, trying again: %s", upid, exc)
                sleep(poll_seconds)
                continue
            seen += len(lines)
            if on_log is not None:
                for line in lines:
                    on_log(line)
            if status.get("status") != "running":
                return status
            if not stop_sent and should_stop is not None and should_stop():
                logger.info("Stopping PVE task %s", upid)
                try:
                    self.stop_task(upid)
                except PveApiError as exc:
                    # A task that ended meanwhile cannot be stopped; its status says how it ended.
                    logger.warning("Could not stop PVE task %s: %s", upid, exc)
                stop_sent = True
            sleep(poll_seconds)
