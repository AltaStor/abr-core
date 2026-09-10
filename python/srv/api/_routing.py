# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from collections.abc import Callable, Sequence

from fastapi import APIRouter, Depends

from .auth_guard import require_session

RouteSpec = tuple[str, Callable, list[str]]


def build_router(prefix: str, tag: str, *, protected: bool = True) -> APIRouter:
    dependencies = [Depends(require_session)] if protected else []
    return APIRouter(prefix=f"/api/v1{prefix}", tags=[tag], dependencies=dependencies)


def register(router: APIRouter, routes: Sequence[RouteSpec]) -> None:
    for path, handler, methods in routes:
        router.add_api_route(path, handler, methods=methods, response_model_exclude_unset=True)
