# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError


@dataclass(slots=True)
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload

    def to_http_exception(self) -> HTTPException:
        return HTTPException(status_code=self.status_code, detail=self.to_payload())


def api_error(status_code: int, code: str, message: str, **details: Any) -> ApiError:
    return ApiError(status_code=status_code, code=code, message=message, details=details)


def normalize_http_exception(exc: HTTPException) -> HTTPException:
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        return exc
    if isinstance(detail, dict) and {"code", "message"} <= set(detail):
        payload = {"error": detail}
        return HTTPException(status_code=exc.status_code, detail=payload)
    message = str(detail or "Request failed")
    return api_error(exc.status_code, "runtime_error", message).to_http_exception()


def validation_error_payload(exc: RequestValidationError) -> dict[str, Any]:
    errors = []
    for item in exc.errors():
        loc = [str(value) for value in item.get("loc", ())]
        errors.append(
            {
                "location": loc,
                "message": str(item.get("msg") or "Invalid value"),
                "type": str(item.get("type") or "value_error"),
            }
        )
    return api_error(
        422,
        "validation_error",
        "Request validation failed",
        issues=errors,
    ).to_payload()
