#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[3]


def default_disk_tool_path() -> str:
    """The launcher to shell out to, matching how the server resolves it.

    abr-vmdk-tool is built in the separate abr-vmdk-tools repository, so the
    in-tree path is only ever a symlink to it and ALTASTOR_DISK_TOOL_PATH is
    the normal way to point at a build elsewhere.
    """
    configured = os.environ.get("ALTASTOR_DISK_TOOL_PATH", "").strip()
    if configured:
        return configured
    return str(ROOT_DIR / "tools" / "abr-vmdk-tool" / "abr-vmdk-tool.sh")


def env_value(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def env_list(name: str) -> list[str] | None:
    value = env_value(name)
    if not value:
        return None
    return [item for item in value.split() if item]


def env_bool(name: str) -> bool:
    value = (env_value(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def default_backup_dir() -> str:
    return str(ROOT_DIR / "backups" / f"format-matrix-{time.strftime('%Y%m%d-%H%M%S')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up one VM and restore it once for each requested disk type.",
    )
    parser.add_argument("--base-url", default=env_value("BASE_URL") or "http://127.0.0.1:8000")
    parser.add_argument("--esxi-host", default=env_value("ESXI_HOST"), required=env_value("ESXI_HOST") is None)
    parser.add_argument("--esxi-user", default=env_value("ESXI_USER"), required=env_value("ESXI_USER") is None)
    parser.add_argument(
        "--esxi-password",
        default=env_value("ESXI_PASSWORD"),
        required=env_value("ESXI_PASSWORD") is None,
    )
    parser.add_argument("--esxi-port", type=int, default=int(env_value("ESXI_PORT") or "443"))
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        default=env_bool("VERIFY_SSL"),
        help="Enable SSL certificate verification when registering the endpoint.",
    )
    parser.add_argument(
        "--source-vm-name",
        default=env_value("SOURCE_VM_NAME"),
        required=env_value("SOURCE_VM_NAME") is None,
    )
    parser.add_argument(
        "--target-datastore",
        default=env_value("TARGET_DATASTORE"),
        required=env_value("TARGET_DATASTORE") is None,
    )
    parser.add_argument(
        "--target-host-name",
        default=(env_value("TARGET_HOST_NAME") or "").strip(),
        help="Target ESXi host name. If omitted, vm_config.source_host from the manifest is used.",
    )
    parser.add_argument(
        "--disk-types",
        nargs="+",
        default=env_list("FORMATS") or ["original"],
        help="Restore disk types to test. Defaults to original; for example: thick-provisioned thin-provisioned",
    )
    parser.add_argument("--backup-dir", default=env_value("BACKUP_DIR") or default_backup_dir())
    parser.add_argument(
        "--tool-path",
        default=env_value("TOOL_PATH") or default_disk_tool_path(),
    )
    parser.add_argument(
        "--transport-mode",
        default=(env_value("TRANSPORT_MODE") or "").strip(),
        help="Optional VDDK transport mode, for example nbdssl.",
    )
    parser.add_argument("--task-timeout", type=float, default=float(env_value("TASK_TIMEOUT") or "3600"))
    parser.add_argument("--poll-interval", type=float, default=float(env_value("POLL_INTERVAL") or "5"))
    parser.add_argument(
        "--restore-prefix",
        default=(env_value("RESTORE_PREFIX") or "").strip(),
        help="Prefix for restored VM names. Defaults to '<source-vm-name>-restored-format'.",
    )
    parser.add_argument(
        "--register-name",
        default=(env_value("REGISTER_NAME") or "").strip(),
        help="Display name used when registering the ESXi/vCenter endpoint.",
    )
    return parser


def api(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        detail = body
        try:
            detail = json.dumps(json.loads(body), indent=2)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc


def query(params: dict[str, str]) -> str:
    return urllib.parse.urlencode(params)


def wait_task(base_url: str, task_id: str, task_timeout: float, poll_interval: float) -> dict[str, Any]:
    deadline = time.time() + task_timeout
    last_status: tuple[Any, Any, Any] | None = None

    while time.time() < deadline:
        task = api(base_url, "GET", f"/tasks/{task_id}")
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        snapshot = (task.get("status"), result.get("stage"), result.get("progress"))
        if snapshot != last_status:
            progress = result.get("progress")
            progress_text = f"{progress}%" if isinstance(progress, (int, float)) else "-"
            print(
                f"task={task_id} status={task.get('status')} "
                f"stage={result.get('stage') or '-'} progress={progress_text}",
                flush=True,
            )
            last_status = snapshot
        if task.get("status") == "completed":
            return task
        if task.get("status") == "failed":
            raise RuntimeError(f"Task {task_id} failed: {json.dumps(task, indent=2)}")
        time.sleep(poll_interval)

    raise TimeoutError(f"Timed out waiting for task {task_id}")


def create_job(base_url: str, job_type: str, hypervisor_id: str) -> str:
    job = api(
        base_url,
        "POST",
        "/job",
        {
            "job_type": job_type,
            "hypervisor_id": hypervisor_id,
            "metadata": {"created_via": "python/tests/manual/backup_restore_formats.py"},
        },
    )
    return str(job["id"])


def create_task(
    *,
    base_url: str,
    task_type: str,
    hypervisor_id: str,
    resource_type: str,
    resource_external_id: str,
    job_id: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return api(
        base_url,
        "POST",
        "/task",
        {
            "task_type": task_type,
            "hypervisor_id": hypervisor_id,
            "resource_type": resource_type,
            "resource_external_id": resource_external_id,
            "job_id": job_id,
            "parameters": parameters,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")
    restore_prefix = args.restore_prefix or f"{args.source_vm_name}-restored-format"
    register_name = args.register_name or f"{args.esxi_host}-format-matrix"

    print(f"Checking server health at {base_url}", flush=True)
    api(base_url, "GET", "/health")

    print(f"Registering ESXi/vCenter endpoint {args.esxi_host}", flush=True)
    registered = api(
        base_url,
        "POST",
        "/register/vcenter",
        {
            "name": register_name,
            "host": args.esxi_host,
            "username": args.esxi_user,
            "password": args.esxi_password,
            "port": args.esxi_port,
            "verify_ssl": args.verify_ssl,
            "metadata": {"created_via": "python/tests/manual/backup_restore_formats.py"},
        },
    )
    hypervisor_id = str(registered["id"])
    print(f"Registered hypervisor_id={hypervisor_id}", flush=True)

    api(base_url, "POST", f"/poll_resources?{query({'id': hypervisor_id})}")
    inventory = api(base_url, "GET", f"/get_resources?{query({'id': hypervisor_id})}")
    resources = inventory.get("resources", {})
    vms = resources.get("virtual_machines", [])
    source_vm = next((vm for vm in vms if vm.get("name") == args.source_vm_name), None)
    if not source_vm:
        names = ", ".join(str(vm.get("name")) for vm in vms)
        raise RuntimeError(f"VM {args.source_vm_name!r} not found. Discovered VMs: {names}")

    source_vm_id = str(source_vm["external_id"])
    print(f"Using source VM {args.source_vm_name} external_id={source_vm_id}", flush=True)

    backup_job_id = create_job(base_url, "backup", hypervisor_id)
    backup_parameters: dict[str, Any] = {
        "destination_dir": args.backup_dir,
        "tool_path": args.tool_path,
    }
    if args.transport_mode:
        backup_parameters["transport_mode"] = args.transport_mode

    print(f"Starting VM backup into {args.backup_dir}", flush=True)
    backup_task = create_task(
        base_url=base_url,
        task_type="backup",
        hypervisor_id=hypervisor_id,
        resource_type="virtual_machine",
        resource_external_id=source_vm_id,
        job_id=backup_job_id,
        parameters=backup_parameters,
    )
    backup_result = wait_task(base_url, str(backup_task["id"]), args.task_timeout, args.poll_interval)["result"]
    manifest_path = str(backup_result["manifest_path"])
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    target_host_name = args.target_host_name or str(manifest.get("vm_config", {}).get("source_host") or "")
    if not target_host_name:
        raise RuntimeError("TARGET_HOST_NAME is empty and backup manifest did not include vm_config.source_host")

    print(f"Backup manifest: {manifest_path}", flush=True)
    print(f"Target host: {target_host_name}", flush=True)
    print(f"Target datastore: {args.target_datastore}", flush=True)

    restore_job_id = create_job(base_url, "restore", hypervisor_id)
    summary: list[dict[str, Any]] = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    for disk_format in args.disk_types:
        safe_format = disk_format.replace("/", "-").replace(" ", "-")
        target_vm_name = f"{restore_prefix}-{safe_format}-{timestamp}"
        parameters: dict[str, Any] = {
            "manifest_path": manifest_path,
            "target_vm_name": target_vm_name,
            "target_host_name": target_host_name,
            "target_datastore_name": args.target_datastore,
            "tool_path": args.tool_path,
        }
        if args.transport_mode:
            parameters["transport_mode"] = args.transport_mode
        if disk_format != "original":
            parameters["disk_type"] = disk_format

        print(f"Restoring {target_vm_name} with disk_type={disk_format}", flush=True)
        restore_task = create_task(
            base_url=base_url,
            task_type="restore",
            hypervisor_id=hypervisor_id,
            resource_type="virtual_machine",
            resource_external_id=source_vm_id,
            job_id=restore_job_id,
            parameters=parameters,
        )
        restored = wait_task(base_url, str(restore_task["id"]), args.task_timeout, args.poll_interval)
        restored_result = restored["result"]
        summary.append(
            {
                "format": disk_format,
                "target_vm_name": target_vm_name,
                "task_id": restore_task["id"],
                "disks": [
                    {
                        "index": disk.get("index"),
                        "requested_disk_type": disk.get("requested_disk_type"),
                        "resolved_disk_type_code": disk.get("resolved_disk_type_code"),
                        "resolved_disk_type_name": disk.get("resolved_disk_type_name"),
                        "restored_datastore_path": disk.get("restored_datastore_path"),
                    }
                    for disk in restored_result.get("disks", [])
                ],
            }
        )

    print("\nBackup/restore format matrix completed", flush=True)
    print(json.dumps({"hypervisor_id": hypervisor_id, "manifest_path": manifest_path, "restores": summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
