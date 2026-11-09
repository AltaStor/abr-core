# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
from typing import Any

from python.srv.services.internal.replication.replication_session import (
    ProgressCallback,
    load_job,
    report_quietly,
)
from python.srv.services.internal.replication.replication_settings import replica_of

logger = logging.getLogger(__name__)

DYNAMIC_MEMORY_KEYS = frozenset({"memory_minimum_mb", "memory_maximum_mb", "memory_buffer"})
HARDWARE_TEXT = {
    "num_cpu": "vCPU",
    "memory_mb": "MB memory",
    "dynamic_memory": "dynamic memory",
    "memory_minimum_mb": "MB minimum memory",
    "memory_maximum_mb": "MB maximum memory",
    "memory_buffer": "% memory buffer",
}


def point_config(point: dict[str, Any]) -> dict[str, Any]:
    from python.srv.services.abr_v41 import _load_restore_manifest_record

    manifest = _load_restore_manifest_record(point)
    config = manifest.get("vm_config") if isinstance(manifest, dict) else None
    return dict(config) if isinstance(config, dict) else {}


def adapter_key(adapter: dict[str, Any], position: int) -> str:
    adapter_id = str(adapter.get("adapter_id") or "").strip().upper()
    if adapter_id:
        return f"id:{adapter_id}"
    label = str(adapter.get("label") or "").strip().lower()
    if label:
        return f"label:{label}"
    return f"index:{position}"


def network_targets(settings: dict[str, Any]) -> dict[str, str]:
    from python.srv.services.abr_v41 import _parse_restore_network_map

    return _parse_restore_network_map(list(settings.get("network_map") or []))


def source_adapters(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [adapter for adapter in config.get("network_adapters") or [] if isinstance(adapter, dict)]


def pair_adapters(wanted: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"source": adapter_key(adapter, position), "replica": str(have["id"])}
        for position, (adapter, have) in enumerate(zip(wanted, current, strict=False))
    ]


def _vlan(adapter: dict[str, Any]) -> int:
    if "vlan_mode" in adapter and str(adapter.get("vlan_mode") or "") != "Access":
        return 0
    return int(adapter.get("vlan_id") or 0)


def _label(adapter: dict[str, Any], position: int) -> str:
    return str(adapter.get("label") or f"Network adapter {position + 1}")


def _shown_labels(adapters: list[dict[str, Any]]) -> list[str]:
    labels = [_label(adapter, position) for position, adapter in enumerate(adapters)]
    return [f"{label} {position + 1}" if labels.count(label) > 1 else label for position, label in enumerate(labels)]


def _target_network(
    adapter: dict[str, Any], label: str, targets: dict[str, str], available: set[str], host: str
) -> tuple[str | None, str | None]:
    source = str(adapter.get("network") or "").strip()
    if not source:
        return None, None
    mapped = targets.get(source)
    if mapped:
        if mapped in available:
            return mapped, None
        return None, (
            f"{label} of the source VM is on {source}, which this job maps to {mapped}, but {mapped} "
            f"is not on host {host}; the replica's adapter is left disconnected"
        )
    if source in available:
        return source, None
    return None, (
        f"{label} of the source VM is on {source}, which this job maps to no network and which is "
        f"not on host {host}; the replica's adapter is left disconnected"
    )


def plan_adapters(
    wanted: list[dict[str, Any]],
    current: list[dict[str, Any]],
    pairs: list[dict[str, str]],
    targets: dict[str, str],
    available: set[str],
    host: str,
) -> dict[str, Any]:
    current_by_id = {str(adapter["id"]): adapter for adapter in current}
    paired: dict[str, str] = {}
    for pair in pairs:
        if str(pair.get("replica") or "") in current_by_id:
            paired.setdefault(str(pair.get("source") or ""), str(pair["replica"]))
    used: set[str] = set()
    kept: list[dict[str, str]] = []
    change: list[dict[str, Any]] = []
    add: list[dict[str, Any]] = []
    notes: list[str] = []
    messages: list[str] = []
    shown = _shown_labels(wanted)
    current_shown = dict(zip([str(adapter["id"]) for adapter in current], _shown_labels(current), strict=True))
    for position, adapter in enumerate(wanted):
        key = adapter_key(adapter, position)
        network, note = _target_network(adapter, shown[position], targets, available, host)
        if note:
            notes.append(note)
        desired = {
            "label": _label(adapter, position),
            "network": network or "",
            "source_network": str(adapter.get("network") or ""),
            "connected": bool(network) and adapter.get("connected", True) is not False,
            "vlan_id": _vlan(adapter),
            "legacy": bool(adapter.get("legacy")),
        }
        replica_id = paired.get(key)
        if replica_id and replica_id not in used:
            used.add(replica_id)
            kept.append({"source": key, "replica": replica_id})
            have = current_by_id[replica_id]
            moved = bool(network) and str(have.get("network") or "") != network
            if moved or bool(have.get("connected")) != desired["connected"] or _vlan(have) != desired["vlan_id"]:
                change.append({"id": replica_id, **desired})
                vlan = f", VLAN {desired['vlan_id']}" if desired["vlan_id"] else ""
                messages.append(
                    f"{shown[position]} set to {network or have.get('network') or 'no network'}"
                    f"{vlan}{'' if desired['connected'] else ', disconnected'} (it was on "
                    f"{have.get('network') or 'no network'}{'' if have.get('connected') else ', disconnected'}), as on "
                    "the source VM"
                )
        else:
            add.append({"source": key, **desired})
            messages.append(
                f"{shown[position]} added "
                + (f"on {network}{'' if desired['connected'] else ', disconnected'}" if network else "disconnected")
                + ", as on the source VM"
            )
    remove = [adapter_id for adapter_id in current_by_id if adapter_id not in used]
    for adapter_id in remove:
        have = current_by_id[adapter_id]
        messages.append(
            f"{current_shown.get(adapter_id) or adapter_id} on {have.get('network') or 'no network'} removed: the "
            "source VM no longer has it"
        )
    return {"kept": kept, "change": change, "add": add, "remove": remove, "notes": notes, "messages": messages}


def _same(current: Any, wanted: Any) -> bool:
    if isinstance(wanted, bool):
        return bool(current) == wanted
    try:
        return int(current) == int(wanted)
    except (TypeError, ValueError):
        return str(current or "") == str(wanted or "")


def hardware_changes(keys: tuple[str, ...], current: dict[str, Any], wanted: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    dynamic = bool(wanted.get("dynamic_memory"))
    changes = {}
    for key in keys:
        if wanted.get(key) is None or (key in DYNAMIC_MEMORY_KEYS and not dynamic):
            continue
        if not _same(current.get(key), wanted[key]):
            changes[key] = (current.get(key), wanted[key])
    return changes


def _hardware_text(changes: dict[str, tuple[Any, Any]]) -> str:
    parts = []
    for key, (before, after) in changes.items():
        if isinstance(after, bool):
            parts.append(f"{HARDWARE_TEXT[key]} {'on' if after else 'off'} (was {'on' if before else 'off'})")
        else:
            parts.append(f"{after} {HARDWARE_TEXT[key]} (was {before if before is not None else 'unknown'})")
    return ", ".join(parts)


def settle_replica(
    job_id: str,
    settings: dict[str, Any],
    state: dict[str, Any],
    point: dict[str, Any],
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
    *,
    creating: bool = False,
) -> dict[str, Any]:
    replica = replica_of(load_job(job_id))
    name = state["name"]
    wanted = point_config(point)
    current = platform.read_config(node, state)
    wanted_adapters = source_adapters(wanted)
    current_adapters = list(current.get("network_adapters") or [])
    recorded = replica.get("adapters")
    pairs = pair_adapters(wanted_adapters, current_adapters) if creating or not isinstance(recorded, list) else recorded
    targets = network_targets(settings)
    names = {str(adapter.get("network") or "") for adapter in wanted_adapters} | set(targets.values())
    available = platform.networks(node, state, sorted(item for item in names if item))
    plan = plan_adapters(wanted_adapters, current_adapters, pairs, targets, available, state["host"])
    hardware = hardware_changes(platform.HARDWARE_KEYS, current, wanted)
    touched = False
    problems: list[str] = []
    adapters = plan["kept"]

    if hardware or plan["change"] or plan["add"] or plan["remove"]:
        touched = True
        if progress_callback is not None:
            progress_callback({"replica_touched": True, "log_entry": False})
        details = ([_hardware_text(hardware)] if hardware else []) + plan["messages"]
        report_quietly(
            progress_callback,
            96,
            f"Bringing the configuration of {name} in line with restore point {point['id']}: {'; '.join(details)}",
            stage="configure",
        )
        try:
            added = platform.apply_config(
                node,
                state,
                {key: wanted[key] for key in platform.HARDWARE_KEYS if wanted.get(key) is not None} if hardware else {},
                remove=plan["remove"],
                change=plan["change"],
                add=plan["add"],
            )
            adapters = plan["kept"] + [
                {"source": item["source"], "replica": str(adapter_id)} for item, adapter_id in zip(plan["add"], added, strict=False)
            ]
        except Exception as exc:
            logger.exception("Configuring replica %s failed", name)
            problems.append(
                f"{name} is at restore point {point['id']}, but its configuration could not be brought in line with it "
                f"({exc}); the next run tries again"
            )
            adapters = pairs

    renamed_to = None
    pending = str(replica.get("pending_name") or "").strip()
    if pending and pending != name:
        renamed_to, problem = _rename(settings, state, pending, node, platform, progress_callback)
        touched = touched or bool(renamed_to)
        if problem:
            problems.append(problem)
    elif pending:
        renamed_to = pending

    unsynced = list(platform.unsynced(current, wanted))
    notes = plan["notes"] + unsynced
    known = {str(item) for item in replica.get("config_warnings") or []}
    fresh = [
        note if note in plan["notes"] else (
            f"Replication does not change this on {renamed_to or name}, and it differs from the source VM in restore point "
            f"{point['id']}: {note}"
        )
        for note in notes
        if note not in known
    ]
    for message in fresh:
        report_quietly(progress_callback, 97, message, stage="configure", log_level="info" if creating else "warn")
    for message in problems:
        report_quietly(progress_callback, 97, message, stage="configure", log_level="warn")
    if touched:
        state = platform.find_replica(node, state["vm_id"]) or state
    return {"state": state, "touched": touched, "adapters": adapters, "config_warnings": notes, "renamed_to": renamed_to}


def _rename(
    settings: dict[str, Any],
    state: dict[str, Any],
    new_name: str,
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
) -> tuple[str | None, str | None]:
    from python.srv.services.abr_v41 import _inventory_vm_name_exists

    if _inventory_vm_name_exists(settings["target_inventory_id"], new_name):
        return None, (
            f"Replica {state['name']} keeps its name: a VM named {new_name} is already on "
            f"{settings['target_inventory_name']}. The next run tries again"
        )
    if progress_callback is not None:
        progress_callback({"replica_touched": True, "log_entry": False})
    report_quietly(progress_callback, 98, f"Renaming replica {state['name']} to {new_name}", stage="configure")
    try:
        platform.rename(node, state, new_name)
    except Exception as exc:
        logger.exception("Renaming replica %s to %s failed", state["name"], new_name)
        return None, f"Replica {state['name']} could not be renamed to {new_name} ({exc}); the next run tries again"
    return new_name, None
