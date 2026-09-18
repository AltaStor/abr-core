# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from ..cli_context import CliContext


class RestoreService:
    def __init__(self, context: CliContext) -> None:
        self.context = context

    @staticmethod
    def _coalesce_string(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    return normalized
        return None

    def build_restore_list_params(self, args: Any) -> dict[str, str] | None:
        params = {
            key: value
            for key, value in {
                "vm": self._coalesce_string(getattr(args, "vm", None)),
                "backup": self._coalesce_string(getattr(args, "backup", None)),
                "inventory": self._coalesce_string(getattr(args, "inventory", None)),
            }.items()
            if value is not None
        }
        return params or None

    def build_start_payload(self, args: Any) -> dict[str, Any]:
        explicit_restore_point = self._coalesce_string(getattr(args, "restore_point", None), getattr(args, "point_id", None))
        backup_id = self._coalesce_string(getattr(args, "backup", None))
        vm_selector = self._coalesce_string(getattr(args, "vm", None))
        new_name_raw = getattr(args, "new_name", None)
        legacy_name_raw = getattr(args, "name", None)
        new_name = self._coalesce_string(new_name_raw, legacy_name_raw)
        target_inventory = self._coalesce_string(getattr(args, "target_inventory", None))
        target_inventory_id = self._coalesce_string(getattr(args, "target_inventory_id", None))
        target_host = self._coalesce_string(getattr(args, "target_host", None))
        target_datastore = self._coalesce_string(getattr(args, "datastore", None), getattr(args, "target_datastore", None))
        repo_name = self._coalesce_string(getattr(args, "repo", None))
        repo_id = self._coalesce_string(getattr(args, "repo_id", None))
        network_map = getattr(args, "network_map", None) or []
        datastore_map = getattr(args, "datastore_map", None) or []

        if (new_name_raw is not None or legacy_name_raw is not None) and new_name is None:
            raise ValueError("restore start requires a non-empty --new-name")
        if getattr(args, "restore_point", None) and getattr(args, "point_id", None):
            raise ValueError("Use only one of --restore-point or --point-id")
        if explicit_restore_point and backup_id:
            raise ValueError("Use either --restore-point/--point-id or --backup, not both")
        if not explicit_restore_point and not backup_id:
            raise ValueError("restore start requires --restore-point/--point-id or --backup")
        if explicit_restore_point is None and vm_selector is None and backup_id is not None:
            # Backup chain restore uses the latest restore point in the selected chain by default.
            pass
        if explicit_restore_point is None and backup_id is None and vm_selector is not None:
            raise ValueError("restore start requires --backup when --vm is used without --restore-point")

        in_place = bool(getattr(args, "in_place", False))
        force = bool(getattr(args, "force", False))

        if force and not in_place:
            raise ValueError("--force is only used with --in-place restore; pass --in-place to request it")

        if in_place:
            if not force:
                raise ValueError("--in-place restore requires --force to authorize overwriting the source VM")
            conflicting = [
                flag
                for flag, value in (
                    ("--target-inventory/--target-inventory-id", target_inventory or target_inventory_id),
                    ("--target-host", target_host),
                    ("--datastore/--target-datastore", target_datastore),
                    ("--new-name/--name", new_name),
                )
                if value
            ]
            if conflicting:
                raise ValueError(
                    "--in-place restore cannot be combined with "
                    + ", ".join(conflicting)
                    + " -- in-place restore always uses the source VM's own host, datastore, and name"
                )
        else:
            uses_legacy_in_place = bool(explicit_restore_point) and not any(
                [
                    target_inventory,
                    target_inventory_id,
                    target_host,
                    target_datastore,
                    new_name,
                    repo_name,
                    repo_id,
                ]
            )
            uses_legacy_backup_copy = bool(explicit_restore_point) and not new_name and bool(
                (target_inventory or target_inventory_id) and target_host and target_datastore and (repo_name or repo_id)
            )

            if not uses_legacy_in_place and not uses_legacy_backup_copy:
                if not (target_inventory or target_inventory_id):
                    raise ValueError("restore start requires --target-inventory or --target-inventory-id")
                if not target_host:
                    raise ValueError("restore start requires --target-host")
                if not target_datastore:
                    raise ValueError("restore start requires --datastore or --target-datastore")
                if not new_name:
                    raise ValueError("restore start requires --new-name")

        payload: dict[str, Any] = {
            "point_id": explicit_restore_point,
            "backup": backup_id,
            "vm": vm_selector,
            "name": new_name,
            "restore_mode": "in_place" if in_place else None,
            "target_inventory": target_inventory,
            "target_inventory_id": target_inventory_id,
            "target_host": target_host,
            "target_datastore": target_datastore,
            "network_map": list(network_map),
            "datastore_map": list(datastore_map),
            "repo": repo_name,
            "repo_id": repo_id,
            "power_on": bool(getattr(args, "power_on", False)),
            "encryption_key": str(getattr(args, "encryption_key", None) or "").strip() or None,
            "force": force,
        }
        return {key: value for key, value in payload.items() if value not in (None, [], "")}
