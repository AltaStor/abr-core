# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.routing import APIRoute

from python.srv.services import audit_service, rbac

from .auth_guard import authorize, require_session

# path, handler, methods, function ID
RouteSpec = tuple[str, Callable, list[str], str]

FUNCTION_ID_KEY = "x-function-id"


def function_id_of(route: APIRoute) -> str | None:
    return (route.openapi_extra or {}).get(FUNCTION_ID_KEY)


class AuditedRoute(APIRoute):
    """A route that writes an audit record for each request it serves,
    allowed, denied or failed."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()
        function_id = function_id_of(self)
        if function_id is None:
            return handler

        async def audited_handler(request: Request) -> Response:
            response: Response | None = None
            error: BaseException | None = None
            try:
                response = await handler(request)
                return response
            except Exception as exc:
                error = exc
                raise
            finally:
                try:
                    body = await request.body()
                except Exception:
                    body = b""
                await run_in_threadpool(
                    audit_service.record_request,
                    function_id=function_id,
                    context=getattr(request.state, "audit", None),
                    headers=request.headers,
                    client=request.client.host if request.client else None,
                    query=dict(request.query_params),
                    body=body,
                    status=response.status_code if response is not None else getattr(error, "status_code", 500),
                    response_body=getattr(response, "body", None),
                    exc=error,
                )

        return audited_handler


def build_router(prefix: str, tag: str, *, protected: bool = True) -> APIRouter:
    dependencies = [Depends(require_session)] if protected else []
    return APIRouter(prefix=f"/api/v1{prefix}", tags=[tag], dependencies=dependencies, route_class=AuditedRoute)


def register(router: APIRouter, routes: Sequence[RouteSpec]) -> None:
    for path, handler, methods, function_id in routes:
        function = rbac.FUNCTIONS_BY_ID[function_id]
        dependencies = [] if function_id in rbac.PUBLIC_FUNCTIONS else [Depends(authorize(function_id))]
        router.add_api_route(
            path,
            handler,
            methods=methods,
            response_model_exclude_unset=True,
            dependencies=dependencies,
            openapi_extra={FUNCTION_ID_KEY: function_id},
            description=_with_function_note(handler.__doc__, function),
        )


def _with_function_note(doc: str | None, function: rbac.Function) -> str:
    note = f"Function `{function.id}`: {function.name}."
    if function.id in rbac.PUBLIC_FUNCTIONS:
        note += " Needs no session."
    return f"{note}\n\n{inspect.cleandoc(doc)}" if doc else note
