# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
from typing import Any

from ..cli_context import CliContext
from ..parsing.secret_arguments import ENCRYPTION_KEY_ENV_VAR, resolve_secret
from ..parsing.validators import (
    STRICT_TIME_PATTERN,
    parse_csv_values,
    parse_duration,
    parse_strict_hhmm,
    parse_weekday_csv,
)

_NEW_SCHEDULE_TYPES = {"daily", "weekly", "monthly", "periodic"}


class _ExecutionScheduleArgs:
    _MAPPED = {"schedule", "time", "day_of_month", "every"}

    def __init__(self, args: Any, prefix: str) -> None:
        self._args = args
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        if name == "days":
            return getattr(self._args, f"{self._prefix}_days", None)
        if name in self._MAPPED:
            return getattr(self._args, f"{self._prefix}_{name}", None)
        if name in {"enabled", "disabled"}:
            return bool(getattr(self._args, f"{self._prefix}_{name}", False))
        return getattr(self._args, name, None)


class JobService:
    def __init__(self, context: CliContext) -> None:
        self.context = context

    @staticmethod
    def _is_empty_value(value: Any) -> bool:
        return value is None or value is False or value == "" or value == []

    @staticmethod
    def ensure_removed_options_unused(args: Any, removed_options: dict[str, str]) -> None:
        for option, message in removed_options.items():
            value = getattr(args, option, None)
            if not JobService._is_empty_value(value):
                raise ValueError(message)

    @staticmethod
    def _coalesce_string(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _normalize_name(value: Any, *, label: str) -> str:
        name = str(value or "")
        if not name.strip():
            raise ValueError(f"{label} must not be empty")
        return name

    @staticmethod
    def _raw_vm_tokens(args: Any) -> list[str]:
        values: list[str] = []

        vm_flags = getattr(args, "vm", None)
        if isinstance(vm_flags, list):
            values.extend(str(item).strip() for item in vm_flags if str(item).strip())
        elif isinstance(vm_flags, str) and vm_flags.strip():
            values.append(vm_flags.strip())

        for field_name in ("vms", "objects"):
            raw = getattr(args, field_name, None)
            if isinstance(raw, str) and raw.strip():
                values.extend(parse_csv_values(raw))

        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped

    @staticmethod
    def _build_task_parameter_updates(args: Any, *, creating: bool = False) -> dict[str, Any]:
        set_encryption = bool(getattr(args, "set_encryption", False))
        reset_encryption = bool(getattr(args, "reset_encryption", False))
        encryption_key = getattr(args, "encryption_key", None)

        if encryption_key is not None and not set_encryption:
            raise ValueError("--encryption-key requires --set-encryption")
        encryption_key = resolve_secret(
            encryption_key,
            env_var=ENCRYPTION_KEY_ENV_VAR,
            prompt="Encryption key",
            needed=set_encryption and creating,
            confirm=True,
        )
        has_key = isinstance(encryption_key, str) and bool(encryption_key.strip())
        if set_encryption and creating and not has_key:
            raise ValueError("--set-encryption requires --encryption-key when creating a job")

        updates: dict[str, Any] = {}
        compression_level = getattr(args, "compression_level", None)
        if compression_level is not None:
            updates["compression_level"] = int(compression_level)
        guest_consistency = getattr(args, "guest_consistency", None)
        if guest_consistency is not None:
            updates["hyperv_consistency"] = str(guest_consistency)
        on_quiesce_failure = getattr(args, "on_quiesce_failure", None)
        if on_quiesce_failure is not None:
            updates["on_quiesce_failure"] = str(on_quiesce_failure).replace("-", "_")

        if set_encryption:
            updates["encryption_enabled"] = True
            if has_key:
                updates["encryption_key"] = encryption_key.strip()
        elif reset_encryption:
            # None marks these keys for removal when merged against a stored job.
            updates["encryption_enabled"] = None
            updates["encryption_key"] = None

        return updates

    @classmethod
    def _has_vm_selection_input(cls, args: Any) -> bool:
        return bool(getattr(args, "vm", None) or getattr(args, "vms", None) is not None or getattr(args, "objects", None) is not None)

    @staticmethod
    def _schedule_detail_fields_present(args: Any) -> bool:
        return any(
            not JobService._is_empty_value(getattr(args, field_name, None))
            for field_name in ("time", "days", "day_of_month", "every")
        )

    @staticmethod
    def _schedule_enabled_override(args: Any) -> bool | None:
        if getattr(args, "enabled", False):
            return True
        if getattr(args, "disabled", False):
            return False
        return None

    EXECUTION_SCHEDULE_PREFIXES = ("active_full", "synthetic_full")

    _SCHEDULE_DETAIL_FLAGS = (
        ("time", "--time"),
        ("days", "--days"),
        ("day_of_month", "--day-of-month"),
        ("every", "--every"),
    )

    _SCHEDULE_TYPE_DETAILS = {
        "daily": ("time",),
        "weekly": ("days", "time"),
        "monthly": ("day_of_month", "time"),
        "periodic": ("every",),
    }

    @classmethod
    def _only_days_detail(cls, args: Any) -> bool:
        if cls._is_empty_value(getattr(args, "days", None)):
            return False
        return all(cls._is_empty_value(getattr(args, name, None)) for name in ("time", "day_of_month", "every"))

    @classmethod
    def _reject_days_a_schedule_cannot_use(cls, stored: Any, prefix: str) -> None:
        if not isinstance(stored, dict):
            return
        schedule_type = str(stored.get("type") or "").strip().lower()
        if schedule_type in {"", "weekly"}:
            return
        flag = f"--{prefix.replace('_', '-')}"
        raise ValueError(
            f"{schedule_type.capitalize()} schedules do not accept {flag}-days. "
            f"Pass {flag}-schedule none to turn this schedule off"
        )

    @staticmethod
    def _stored_schedule_time(stored: dict[str, Any]) -> str:
        stored_time = str(stored.get("time") or "").strip()
        return stored_time if STRICT_TIME_PATTERN.fullmatch(stored_time) else ""

    @classmethod
    def _merge_schedule_details(cls, stored: dict[str, Any], args: Any, *, enabled_override: bool | None) -> dict[str, Any]:
        schedule_type = str(stored.get("type") or "").strip().lower()
        accepted = cls._SCHEDULE_TYPE_DETAILS.get(schedule_type)
        if accepted is None:
            raise ValueError("Schedule details require --schedule")
        label = schedule_type.capitalize()
        for field_name, flag in cls._SCHEDULE_DETAIL_FLAGS:
            if field_name in accepted or cls._is_empty_value(getattr(args, field_name, None)):
                continue
            raise ValueError(f"{label} schedules do not accept {flag}")

        payload: dict[str, Any] = {
            "type": schedule_type,
            "enabled": bool(stored.get("enabled", True)) if enabled_override is None else enabled_override,
        }
        if "time" in accepted:
            given = getattr(args, "time", None)
            payload["time"] = parse_strict_hhmm(str(given)) if given not in {None, ""} else str(stored.get("time") or "")
        if "days" in accepted:
            given = getattr(args, "days", None)
            if given:
                payload["days"] = given if isinstance(given, list) else parse_weekday_csv(str(given))
            else:
                payload["days"] = list(stored.get("days") or [])
        if "day_of_month" in accepted:
            given = getattr(args, "day_of_month", None)
            payload["day_of_month"] = int(given) if given is not None else int(stored.get("day_of_month") or 1)
        if "every" in accepted:
            given = getattr(args, "every", None)
            payload["every"] = parse_duration(str(given)) if given not in {None, ""} else str(stored.get("every") or "")
        return payload

    @classmethod
    def build_execution_schedule_payloads(cls, args: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
        payloads: dict[str, Any] = {}
        job = current if isinstance(current, dict) else {}
        for prefix in cls.EXECUTION_SCHEDULE_PREFIXES:
            key = f"{prefix}_schedule"
            requested = getattr(args, key, None)
            state_only = bool(getattr(args, f"{prefix}_enabled", False) or getattr(args, f"{prefix}_disabled", False))
            prefixed = _ExecutionScheduleArgs(args, prefix)
            has_details = cls._schedule_detail_fields_present(prefixed)
            if cls._is_empty_value(requested) and not has_details and getattr(prefixed, "days", None) is not None:
                cls._reject_days_a_schedule_cannot_use(job.get(key), prefix)
            if cls._is_empty_value(requested) and not state_only and not has_details:
                continue
            if not cls._is_empty_value(requested) and str(requested).strip().lower() in {"none", "off"}:
                payloads[key] = None
                continue
            stored = job.get(key) if isinstance(job.get(key), dict) else None
            stored_type = str((stored or {}).get("type") or "").strip().lower()
            keeps_stored_type = stored is not None and str(requested or "").strip().lower() in {"", stored_type}
            if keeps_stored_type and has_details:
                try:
                    payloads[key] = cls._merge_schedule_details(
                        stored, prefixed, enabled_override=cls._schedule_enabled_override(prefixed)
                    )
                except ValueError as error:
                    raise cls._prefixed_schedule_error(error, prefix) from error
                continue
            if cls._is_empty_value(requested) and has_details:
                if cls._only_days_detail(prefixed):
                    if state_only:
                        payloads[f"{prefix}_enabled"] = cls._schedule_enabled_override(prefixed)
                    continue
                raise cls._prefixed_schedule_error(ValueError("Schedule details require --schedule"), prefix)
            try:
                payloads[key] = cls.build_schedule_payload(prefixed, current_schedule=stored)
            except ValueError as error:
                raise cls._prefixed_schedule_error(error, prefix) from error
        return payloads

    @staticmethod
    def _prefixed_schedule_error(error: ValueError, prefix: str) -> ValueError:
        message = str(error)
        for base_flag in ("--day-of-month", "--days", "--time", "--every", "--schedule"):
            message = message.replace(base_flag, f"--{prefix.replace('_', '-')}{base_flag[1:]}")
        return ValueError(message)

    @staticmethod
    def _gfs_enabled_override(args: Any) -> bool | None:
        if getattr(args, "gfs_enable", False):
            return True
        if getattr(args, "gfs_disable", False):
            return False
        return None

    @classmethod
    def _legacy_schedule_payload(cls, raw: str, *, enabled: bool | None) -> dict[str, Any]:
        value = str(raw or "").strip()
        lowered = value.lower()

        def with_enabled(payload: dict[str, Any]) -> dict[str, Any]:
            if enabled is None:
                return payload
            return {**payload, "enabled": enabled}

        if lowered.startswith("daily "):
            hhmm = parse_strict_hhmm(value[6:].strip())
            return with_enabled({"type": "daily", "time": hhmm})

        if lowered.startswith("weekly "):
            parts = value.split()
            if len(parts) != 3:
                raise ValueError("Legacy weekly schedules must use `weekly DAY HH:MM`")
            return with_enabled({"type": "weekly", "days": parse_weekday_csv(parts[1]), "time": parse_strict_hhmm(parts[2])})

        if lowered.startswith("monthly "):
            parts = value.split()
            if len(parts) != 3:
                raise ValueError("Legacy monthly schedules must use `monthly DAY HH:MM`")
            try:
                day_of_month = int(parts[1])
            except ValueError as exc:
                raise ValueError("Legacy monthly schedules must use a numeric day-of-month") from exc
            if day_of_month < 1 or day_of_month > 31:
                raise ValueError("Monthly schedules require --day-of-month from 1 to 31")
            return with_enabled({"type": "monthly", "day_of_month": day_of_month, "time": parse_strict_hhmm(parts[2])})

        if lowered.startswith("periodic "):
            parts = value.split()
            if len(parts) != 2:
                raise ValueError("Legacy periodic schedules must use `periodic <duration>`")
            return with_enabled({"type": "periodic", "every": parse_duration(parts[1])})

        cron_fields = value.split()
        if len(cron_fields) == 5:
            return with_enabled({"type": "cron", "expression": value})

        raise ValueError("Schedule must use one of: daily, weekly, monthly, periodic, or a legacy cron expression")

    @classmethod
    def build_schedule_payload(cls, args: Any, *, current_schedule: dict[str, Any] | None = None) -> dict[str, Any] | None:
        schedule_value = getattr(args, "schedule", None)
        enabled_override = cls._schedule_enabled_override(args)
        has_schedule_details = cls._schedule_detail_fields_present(args)

        if schedule_value in {None, ""}:
            if has_schedule_details:
                raise ValueError("Schedule details require --schedule")
            if enabled_override is None:
                return None
            if not isinstance(current_schedule, dict):
                raise ValueError("Schedule enable/disable requires the job to already have a schedule")
            payload = copy.deepcopy(current_schedule)
            payload["enabled"] = enabled_override
            return payload

        stored = current_schedule if isinstance(current_schedule, dict) else {}
        given_time = getattr(args, "time", None)
        resolved_time = (
            parse_strict_hhmm(str(given_time))
            if given_time not in {None, ""}
            else cls._stored_schedule_time(stored)
        )

        schedule_text = str(schedule_value).strip()
        if not schedule_text:
            raise ValueError("Schedule must not be empty")

        lowered = schedule_text.lower()
        if lowered not in _NEW_SCHEDULE_TYPES:
            if has_schedule_details:
                raise ValueError("Do not combine legacy schedule text with --time, --days, --day-of-month, or --every")
            return cls._legacy_schedule_payload(schedule_text, enabled=enabled_override)

        if enabled_override is not None:
            enabled = enabled_override
        else:
            enabled = bool(stored.get("enabled", True)) if stored else True
        if lowered == "daily":
            if not resolved_time:
                raise ValueError("Daily schedules require --time")
            if getattr(args, "days", None):
                raise ValueError("Daily schedules do not accept --days")
            if getattr(args, "day_of_month", None) is not None:
                raise ValueError("Daily schedules do not accept --day-of-month")
            if getattr(args, "every", None):
                raise ValueError("Daily schedules do not accept --every")
            return {"type": "daily", "time": resolved_time, "enabled": enabled}

        if lowered == "weekly":
            days = getattr(args, "days", None)
            if not days:
                raise ValueError("Weekly schedules require --days")
            if not resolved_time:
                raise ValueError("Weekly schedules require --time")
            if getattr(args, "day_of_month", None) is not None:
                raise ValueError("Weekly schedules do not accept --day-of-month")
            if getattr(args, "every", None):
                raise ValueError("Weekly schedules do not accept --every")
            parsed_days = days if isinstance(days, list) else parse_weekday_csv(str(days))
            return {"type": "weekly", "days": parsed_days, "time": resolved_time, "enabled": enabled}

        if lowered == "monthly":
            day_of_month = getattr(args, "day_of_month", None)
            if day_of_month is None:
                raise ValueError("Monthly schedules require --day-of-month")
            if not resolved_time:
                raise ValueError("Monthly schedules require --time")
            if getattr(args, "days", None):
                raise ValueError("Monthly schedules do not accept --days")
            if getattr(args, "every", None):
                raise ValueError("Monthly schedules do not accept --every")
            if int(day_of_month) < 1 or int(day_of_month) > 31:
                raise ValueError("Monthly schedules require --day-of-month from 1 to 31")
            return {"type": "monthly", "day_of_month": int(day_of_month), "time": resolved_time, "enabled": enabled}

        if getattr(args, "time", None) not in {None, ""}:
            raise ValueError("Periodic schedules do not accept --time")
        if getattr(args, "days", None):
            raise ValueError("Periodic schedules do not accept --days")
        if getattr(args, "day_of_month", None) is not None:
            raise ValueError("Periodic schedules do not accept --day-of-month")
        every = getattr(args, "every", None)
        if every in {None, ""}:
            raise ValueError("Periodic schedules require --every")
        return {"type": "periodic", "every": parse_duration(str(every)), "enabled": enabled}

    def create_backup_job_payload(self, args: Any) -> dict[str, Any]:
        self.ensure_removed_options_unused(
            args,
            {
                "mode": "`--mode` has been removed from `abr job create`. Backup mode is resolved automatically.",
                "schedule_daily": "`--schedule-daily` has been removed from `abr job create`. Use `--schedule daily --time HH:MM`.",
                "gfs_policy": "`--gfs-policy` has been removed from `abr job create`.",
            },
        )
        name = self._normalize_name(getattr(args, "name", None), label="Job name")
        vm_tokens = self._raw_vm_tokens(args)
        if not vm_tokens:
            raise ValueError("job create requires at least one --vm, --vms, or --objects value")

        repository_name = self._coalesce_string(getattr(args, "repository", None), getattr(args, "repo", None))
        repository_id = self._coalesce_string(getattr(args, "repository_id", None), getattr(args, "repo_id", None))
        if not repository_name and not repository_id:
            raise ValueError("job create requires --repository/--repo or --repository-id/--repo-id")

        schedule = self.build_schedule_payload(args)
        payload: dict[str, Any] = {
            "name": name,
            "vms": vm_tokens,
            "inventory": self._coalesce_string(getattr(args, "inventory", None)),
            "inventory_id": self._coalesce_string(getattr(args, "inventory_id", None)),
            "repo": repository_name,
            "repo_id": repository_id,
            "retention_days": int(getattr(args, "retention_days", 14)),
            "gfs": {
                "weekly": int(getattr(args, "gfs_weekly", 0) or 0),
                "monthly": int(getattr(args, "gfs_monthly", 0) or 0),
                "yearly": int(getattr(args, "gfs_yearly", 0) or 0),
                "enabled": self._gfs_enabled_override(args) is not False,
            },
            "active_full_days": getattr(args, "active_full_days", None) or [],
            "synthetic_full_days": getattr(args, "synthetic_full_days", None) or [],
            **self.build_execution_schedule_payloads(args),
        }
        description = self._coalesce_string(getattr(args, "description", None))
        if description:
            payload["description"] = description
        if schedule is not None:
            payload["schedule"] = schedule
        task_parameter_updates = self._build_task_parameter_updates(args, creating=True)
        # Nothing exists yet to clear, so drop any None (reset) markers.
        non_null_updates = {key: value for key, value in task_parameter_updates.items() if value is not None}
        if non_null_updates:
            payload["task_parameters"] = non_null_updates
        return payload

    def create_copy_job_payload(self, args: Any) -> dict[str, Any]:
        name = self._normalize_name(getattr(args, "name", None), label="Job name")
        source_name = self._coalesce_string(getattr(args, "source", None), getattr(args, "source_job", None))
        source_id = self._coalesce_string(getattr(args, "source_id", None), getattr(args, "source_job_id", None))
        if not source_name and not source_id:
            raise ValueError("job create-copy requires --source/--source-job or --source-id/--source-job-id")

        repository_name = self._coalesce_string(getattr(args, "repository", None), getattr(args, "repo", None))
        repository_id = self._coalesce_string(getattr(args, "repository_id", None), getattr(args, "repo_id", None))
        payload: dict[str, Any] = {
            "name": name,
            "source": source_name,
            "source_id": source_id,
            "repo": repository_name,
            "repo_id": repository_id,
        }
        if getattr(args, "retention_days", None) is not None:
            payload["retention_days"] = int(args.retention_days)
        description = self._coalesce_string(getattr(args, "description", None))
        if description:
            payload["description"] = description
        schedule = self.build_schedule_payload(args)
        if schedule is not None:
            payload["schedule"] = schedule
        else:
            enabled_override = self._schedule_enabled_override(args)
            if enabled_override is not None:
                payload["enabled"] = enabled_override
        return payload

    def build_edit_payload(self, job: dict[str, Any], args: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}

        if getattr(args, "new_name", None):
            payload["name"] = self._normalize_name(args.new_name, label="Job name")

        inventory_name = self._coalesce_string(getattr(args, "inventory", None))
        inventory_id = self._coalesce_string(getattr(args, "inventory_id", None))
        if inventory_name or inventory_id:
            payload["inventory"] = inventory_name
            payload["inventory_id"] = inventory_id

        repository_name = self._coalesce_string(getattr(args, "repository", None), getattr(args, "repo", None))
        repository_id = self._coalesce_string(getattr(args, "repository_id", None), getattr(args, "repo_id", None))
        if repository_name or repository_id:
            payload["repo"] = repository_name
            payload["repo_id"] = repository_id

        if self._has_vm_selection_input(args):
            vm_tokens = self._raw_vm_tokens(args)
            if not vm_tokens:
                raise ValueError("job edit requires at least one VM when --vm, --vms, or --objects is provided")
            payload["vms"] = vm_tokens

        if getattr(args, "retention_days", None) is not None:
            payload["retention_days"] = int(args.retention_days)

        for source_field, payload_field in (
            ("gfs_weekly", "gfs_weekly"),
            ("gfs_monthly", "gfs_monthly"),
            ("gfs_yearly", "gfs_yearly"),
        ):
            if getattr(args, source_field, None) is not None:
                payload[payload_field] = int(getattr(args, source_field))

        gfs_enabled_override = self._gfs_enabled_override(args)
        if gfs_enabled_override is not None:
            payload["gfs_enabled"] = gfs_enabled_override

        if getattr(args, "active_full_days", None) is not None:
            payload["active_full_days"] = args.active_full_days
        if getattr(args, "synthetic_full_days", None) is not None:
            payload["synthetic_full_days"] = args.synthetic_full_days
        payload.update(self.build_execution_schedule_payloads(args, job))

        description = getattr(args, "description", None)
        if description is not None:
            normalized = str(description).strip()
            payload["description"] = normalized or None

        enabled_override = self._schedule_enabled_override(args)
        if enabled_override is not None:
            payload["enabled"] = enabled_override
        stored_schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else None
        if enabled_override is None or self._schedule_detail_fields_present(args) or getattr(args, "schedule", None):
            schedule = self.build_schedule_payload(args, current_schedule=stored_schedule)
            if schedule is not None:
                payload["schedule"] = schedule

        task_parameter_updates = self._build_task_parameter_updates(args)
        if task_parameter_updates:
            payload["task_parameters"] = task_parameter_updates

        if not payload:
            raise ValueError("job edit requires at least one editable option")
        return payload
