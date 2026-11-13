# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The server-wide vSphere transport and VDDK library (GET and PUT /api/v1/system/vsphere).

Every vSphere inventory without its own value follows them; see plugins/vcenter/disk_transport.py for how a
session resolves what it uses.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from python.srv.core.mongo_db_wrapper import HYPERVISORS, catalog_records, persist_hypervisor
from python.srv.core.utils import utc_now
from python.srv.plugins.vcenter import disk_transport
from python.srv.services.api_errors import api_error
from python.srv.services.inventory_management import inventory_type_from_record

logger = logging.getLogger(__name__)


def _vsphere_inventories() -> list[dict[str, Any]]:
    records = [record for record in catalog_records(HYPERVISORS) if inventory_type_from_record(record) == "vsphere"]
    return sorted(records, key=lambda record: (str(record.get("name") or "").lower(), str(record.get("id") or "")))


def _has_own_settings(record: dict[str, Any]) -> bool:
    return bool(disk_transport.inventory_transport_setting(record) or disk_transport.inventory_library_setting(record))


def vddk_library_or_error(text: str) -> str:
    """A library file this server can hand to abr-vmdk-tool, or a 422 naming what is wrong with it."""
    problem = disk_transport.library_problem(text)
    if problem:
        raise api_error(422, "invalid_vddk_library", problem, fields={"vddk_library": problem})
    if not disk_transport.disk_tool_takes_library_file():
        raise api_error(
            503, "transport_unavailable", disk_transport.library_unsupported_message(), transport=disk_transport.VDDK
        )
    return os.path.normpath(text)


def settings_dto() -> dict[str, Any]:
    settings = disk_transport.global_settings()
    directory, directory_source = disk_transport.vddk_directory()
    default_library = str(directory / disk_transport.VDDK_LIBRARY)
    library = settings["vddk_library"] or default_library
    explicit = disk_transport.explicit_library(settings["vddk_library"], disk_transport.FROM_GLOBAL)
    problems = {
        disk_transport.VDDK: disk_transport.transport_problem(disk_transport.VDDK, library=explicit),
        disk_transport.SOAP: disk_transport.transport_problem(disk_transport.SOAP),
    }
    return {
        "transport": settings["transport"],
        "vddk_library": settings["vddk_library"],
        "effective_vddk_library": library,
        "vddk_library_source": disk_transport.FROM_GLOBAL if settings["vddk_library"] else disk_transport.FROM_DIRECTORY,
        "vddk_library_file": disk_transport.resolved_file_name(library),
        "vddk_directory": str(directory),
        "vddk_directory_source": directory_source,
        "default_vddk_library": default_library,
        "default_vddk_library_file": disk_transport.resolved_file_name(default_library),
        "library_file_supported": disk_transport.disk_tool_takes_library_file(),
        "transports": {
            transport: {"available": problem is None, "problem": problem} for transport, problem in problems.items()
        },
        "inventories": [
            {
                "id": str(record.get("id") or ""),
                "name": str(record.get("name") or ""),
                "transport_setting": disk_transport.inventory_transport_setting(record) or disk_transport.INHERIT,
                "vddk_library": disk_transport.inventory_library_setting(record),
                "has_own_settings": _has_own_settings(record),
            }
            for record in _vsphere_inventories()
        ],
        "updated_at": settings["updated_at"],
        "updated_by": settings["updated_by"],
    }


def update_settings(payload: dict[str, Any], *, updated_by: str | None) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    """Change the server-wide transport and VDDK library.

    A library left out keeps its value; null, an empty one or the path the VDDK directory gives anyway resets it,
    so it follows the directory again. ``apply_to_existing_inventories`` removes the transport and VDDK library of
    every vSphere inventory that has its own, so all of them use the server-wide values from now on. Returns the
    settings, the fields that changed, and the inventories that were handed back to the server-wide values.
    """
    current = disk_transport.global_settings()
    transport, library = current["transport"], current["vddk_library"]
    changed: list[str] = []
    if payload.get("transport") is not None:
        text = str(payload["transport"]).strip().lower()
        if text not in disk_transport.TRANSPORTS:
            message = f"transport must be one of: {', '.join(disk_transport.TRANSPORTS)}"
            raise api_error(422, "invalid_vsphere_settings", message, fields={"transport": message})
        if text != transport:
            transport = text
            changed.append("transport")
    if "vddk_library" in payload:
        text = str(payload["vddk_library"] or "").strip()
        requested = None if disk_transport.is_default_library(text) else vddk_library_or_error(text)
        if requested != library:
            library = requested
            changed.append("vddk_library")
    if "transport" in changed:
        explicit = disk_transport.explicit_library(library, disk_transport.FROM_GLOBAL)
        problem = disk_transport.transport_problem(transport, library=explicit)
        if problem:
            raise api_error(503, "transport_unavailable", problem, transport=transport, fields={"transport": problem})
    handed_back = [record for record in _vsphere_inventories() if _has_own_settings(record)] if payload.get(
        "apply_to_existing_inventories"
    ) else []
    if handed_back:
        problem = disk_transport.transport_problem(
            transport, library=disk_transport.explicit_library(library, disk_transport.FROM_GLOBAL)
        )
        if problem:
            raise api_error(
                503,
                "transport_unavailable",
                f"The inventories cannot be moved to the server-wide transport: {problem}",
                transport=transport,
            )
    if changed or not disk_transport.global_settings_stored():
        disk_transport.store_global_settings(transport, library, updated_by=updated_by)
    updated: list[dict[str, str]] = []
    for record in handed_back:
        metadata = dict(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})
        metadata.pop("vsphere_transport", None)
        metadata.pop("vddk_library", None)
        persist_hypervisor({**record, "metadata": metadata, "updated_at": utc_now()})
        updated.append({"id": str(record.get("id") or ""), "name": str(record.get("name") or "")})
    if changed or updated:
        logger.info(
            "vSphere settings changed by %s: transport %s, VDDK library %s%s",
            updated_by or "an unknown user",
            transport,
            library or "from the VDDK directory",
            f"; {len(updated)} inventory(ies) use them from now on: {', '.join(item['name'] for item in updated)}" if updated else "",
        )
    return settings_dto(), changed, updated


def initialise() -> int:
    """Store the server-wide settings on the first start that has them, keeping what every inventory did.

    The server-wide transport starts as the VDDK and the library follows the VDDK directory, which is how the
    server worked before. Inventories carrying vddk, which every inventory got as its default before there was a
    server-wide value, are handed back to it; one with soap keeps it. Returns how many were handed back.
    """
    if disk_transport.global_settings_stored():
        return 0
    disk_transport.store_global_settings(disk_transport.VDDK, None, updated_by=None)
    handed_back = 0
    for record in _vsphere_inventories():
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        if metadata.get("vsphere_transport") != disk_transport.VDDK:
            continue
        persist_hypervisor({**record, "metadata": {key: value for key, value in metadata.items() if key != "vsphere_transport"}})
        handed_back += 1
    return handed_back
