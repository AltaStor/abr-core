# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import time
from typing import Any

from python.srv.core.mongo_db_wrapper import (
    TASK_LOCK,
    TASKS,
    catalog_records,
    persist_task,
)
from python.srv.core.utils import utc_now
from python.srv.services.internal.replication.replication_session import (
    ProgressCallback,
    ReplicationError,
    disk_folder,
    is_own_snapshot,
    is_replication_task,
    load_job,
    marker_prefix,
    marker_session,
    replica_tag,
    report_progress,
    report_quietly,
    save_replica,
    wait_seconds,
)
from python.srv.services.internal.replication.replication_settings import replica_of

logger = logging.getLogger(__name__)


def _session_status(session_id: str) -> str:
    task = TASKS.get(session_id)
    return str(task.get("status") or "") if isinstance(task, dict) else ""


def unfinished_marked_vm(state: dict[str, Any], job_id: str, replica_session: str) -> bool:
    session_id = marker_session(state["marker_text"], job_id)
    if replica_session and session_id == replica_session:
        return False
    return _session_status(session_id) != "completed"


def clean_first_run_leftovers(
    job: dict[str, Any], settings: dict[str, Any], node: Any, platform: Any, progress_callback: ProgressCallback
) -> bool:
    job_id = str(job["id"])
    replica = replica_of(job)
    found = False
    replica_session = str(replica.get("created_by_session") or "")
    for state in platform.replicas_with_marker(node, marker_prefix(job_id)):
        if state["vm_id"] == replica.get("vm_id"):
            continue
        if state["powered_on"] or not unfinished_marked_vm(state, job_id, replica_session):
            continue
        report_progress(progress_callback, 2, f"Removing {state['name']}, a replica an interrupted run of this job did not finish")
        platform.destroy_replica(node, state)
        found = True

    if getattr(platform, "PLATFORM", "") == "hyper-v":
        found = _remove_detached_staging_files(job_id, settings, node, platform, progress_callback) or found

    if replica.get("vm_id") and not replica.get("point_id"):
        state = platform.find_replica(node, replica["vm_id"])
        if state is not None:
            if marker_prefix(job_id) not in state["marker_text"]:
                raise ReplicationError(
                    f"Replica creation was not completed. Delete VM {state['name']} and create a new Replication Job"
                )
            if state["powered_on"]:
                raise ReplicationError(
                    f"Replica creation was not completed and VM {state['name']} is powered on. Delete VM "
                    f"{state['name']} and create a new Replication Job"
                )
            report_progress(progress_callback, 3, f"Removing {state['name']}, a replica an earlier run did not finish creating")
            platform.destroy_replica(node, state)
        save_replica(job_id, {})
        found = True

    if not replica_of(load_job(job_id)).get("vm_id") and getattr(platform, "PLATFORM", "") == "hyper-v":
        folder = platform.replica_folder(settings["target_datastore_name"] or settings["target_datastore"], settings["replica_name"])
        leftovers = [
            item["path"]
            for item in platform.tagged_files(node, settings["target_host_name"] or settings["target_host"], folder, replica_tag(job_id))
            if not item["attached"]
        ]
        if leftovers:
            report_progress(progress_callback, 4, f"Removing {len(leftovers)} disk file(s) an interrupted run of this job left in {folder}")
            platform.remove_files(node, settings["target_host_name"] or settings["target_host"], leftovers)
            found = True
    return found


def _paths_in_swap_plans(job_id: str) -> set[str]:
    paths: set[str] = set()
    for task in catalog_records(TASKS):
        if not isinstance(task, dict) or str(task.get("job_id") or "") != job_id or not is_replication_task(task):
            continue
        plan = (task.get("result") or {}).get("swap_plan")
        for change in (plan or {}).get("changes") or [] if isinstance(plan, dict) else []:
            for key in ("new_path", "old_path"):
                if change.get(key):
                    paths.add(str(change[key]).lower())
    return paths


def _stem(path: str) -> str:
    name = str(path).replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return name.rsplit(".", 1)[0]


def _remove_detached_staging_files(
    job_id: str, settings: dict[str, Any], node: Any, platform: Any, progress_callback: ProgressCallback
) -> bool:
    replica = replica_of(load_job(job_id))
    host = str(replica.get("host") or "") or settings["target_host_name"] or settings["target_host"]
    protected = _paths_in_swap_plans(job_id) | {str(disk.get("path") or "").lower() for disk in replica.get("disks") or []}
    kept = {(disk_folder(path).lower(), _stem(path)) for path in protected if path}
    leftovers = [
        path
        for path in platform.detached_staging_files(
            node, host, settings["target_datastore_name"] or settings["target_datastore"], replica_tag(job_id)
        )
        if path.lower() not in protected
        and not any(disk_folder(path).lower() == folder and _stem(path).startswith(stem) for folder, stem in kept)
    ]
    if not leftovers:
        return False
    report_progress(
        progress_callback,
        3,
        f"Removing {len(leftovers)} disk file(s) an interrupted rebuild of this job left in "
        f"{', '.join(sorted({path.rsplit(chr(92), 1)[0] if chr(92) in path else disk_folder(path) for path in leftovers}))}",
    )
    left = platform.delete_disk_files(node, host, leftovers)
    if left:
        report_quietly(progress_callback, 3, f"Could not remove {', '.join(left)}; remove them by hand", log_level="warn")
    return True


def forget_unfinished_replica(job_id: str, node: Any, platform: Any) -> None:
    replica = replica_of(load_job(job_id))
    if not replica.get("vm_id") or replica.get("point_id"):
        return
    try:
        state = platform.find_replica(node, replica["vm_id"])
    except Exception:
        logger.warning("Could not check whether the unfinished replica of job %s is still there", job_id, exc_info=True)
        return
    if state is None:
        save_replica(job_id, {})


def clean_update_leftovers(
    job_id: str, state: dict[str, Any], node: Any, platform: Any, progress_callback: ProgressCallback
) -> tuple[dict[str, Any], bool]:
    found = False
    deadline = time.time() + wait_seconds()
    while state["busy"]:
        if time.time() > deadline:
            tasks = ", ".join(state.get("busy_tasks") or []) or "a disk merge"
            raise ReplicationError(
                f"{state['name']} is still busy with {tasks} on the target after {wait_seconds()}s; the replica is "
                "updated once it is finished"
            )
        report_progress(progress_callback, 2, f"Waiting for {', '.join(state.get('busy_tasks') or ['a task'])} on {state['name']} to finish")
        time.sleep(5)
        state = platform.find_replica(node, state["vm_id"]) or state
        found = True
    if state["consolidation_needed"]:
        report_progress(progress_callback, 3, f"Consolidating the disks of {state['name']}, which an interrupted run left unconsolidated")
        platform.consolidate(node, state, wait_seconds())
        state = platform.find_replica(node, state["vm_id"]) or state
        found = True
    for name in [name for name in state["snapshots"] if is_own_snapshot(name, job_id)]:
        report_progress(progress_callback, 4, f"Reverting {state['name']} to snapshot {name}, which an interrupted run left behind")
        platform.revert_snapshot(node, state, name, wait_seconds())
        state = platform.find_replica(node, state["vm_id"]) or state
        found = True
    return state, found


def undo_swap(node: Any, platform: Any, state: dict[str, Any], staging: dict[str, Any], plan: dict[str, Any]) -> None:
    current = platform.find_replica(node, state["vm_id"]) or state
    paths = {disk["path"] for disk in current["disks"]}
    moved = [change for change in plan["changes"] if change["new_path"] and change["new_path"] in paths]
    if moved and hasattr(platform, "put_back_disks"):
        platform.put_back_disks(node, current, plan["changes"])
    still_staged = platform.find_replica(node, staging["vm_id"])
    if still_staged is not None and still_staged["disks"]:
        platform.remove_staging(node, still_staged)
    else:
        if still_staged is not None:
            platform.discard_staging_shell(node, still_staged)
        platform.delete_disk_files(node, plan["host"], [change["new_path"] for change in plan["changes"] if change["new_path"]])


def finish_swap(node: Any, platform: Any, plan: dict[str, Any], progress_callback: ProgressCallback) -> None:
    staging = platform.find_replica(node, plan["staging_vm_id"])
    if staging is not None:
        platform.discard_staging_shell(node, staging)
    elif plan.get("staging_configuration_location"):
        platform.discard_staging_shell(
            node,
            {"vm_id": plan["staging_vm_id"], "name": plan["staging_name"], "host": plan["host"], "configuration_location": plan["staging_configuration_location"]},
        )
    old = [change["old_path"] for change in plan["changes"] if change["old_path"] and change["old_path"] != change["new_path"]]
    left = platform.delete_disk_files(node, plan["host"], old)
    in_use = {disk_folder(change["new_path"]) for change in plan["changes"] if change["new_path"]}
    for folder in sorted({disk_folder(path) for path in old} - in_use):
        if "-staging-" in folder.rsplit("/", 1)[-1] and hasattr(platform, "remove_folder"):
            platform.remove_folder(node, plan["host"], folder)
    if left:
        report_quietly(
            progress_callback, 95, f"Could not remove the replaced disk file(s) {', '.join(left)}; remove them by hand", log_level="warn"
        )


def finish_interrupted_swap(
    job: dict[str, Any],
    session_id: str,
    state: dict[str, Any],
    node: Any,
    platform: Any,
    progress_callback: ProgressCallback,
    *,
    retrying: bool = False,
) -> tuple[dict[str, Any], bool]:
    interrupted = (_own_swap_plan(session_id) if retrying else None) or _interrupted_swap(str(job["id"]), session_id)
    if interrupted is None:
        return state, False
    plan_session, plan = interrupted
    if plan.get("replica_vm_id") != state["vm_id"]:
        _forget_swap_plan(plan_session)
        return state, False
    paths = {disk["slot"]: disk["path"] for disk in state["disks"]}
    done = [change for change in plan["changes"] if paths.get(change["slot"], "") == change["new_path"]]
    if not done:
        report_progress(progress_callback, 2, f"An interrupted run had rebuilt the disks of {state['name']} but not switched to them; removing the rebuilt ones")
        staging = platform.find_replica(node, plan["staging_vm_id"])
        undo_swap(node, platform, state, staging or {"vm_id": plan["staging_vm_id"]}, plan)
        _forget_swap_plan(plan_session)
        return platform.find_replica(node, state["vm_id"]) or state, True
    if len(done) != len(plan["changes"]):
        report_progress(progress_callback, 2, f"Finishing the disk switch an interrupted run of this job started on {state['name']}")
        platform.swap_disks(node, state, {"disks": []}, [change for change in plan["changes"] if change not in done])
    report_progress(progress_callback, 2, f"An interrupted run had switched {state['name']} to its rebuilt disks; finishing it")
    finish_swap(node, platform, plan, progress_callback)
    finished = platform.find_replica(node, state["vm_id"]) or state
    save_replica(str(job["id"]), metadata_after_swap(replica_of(job), plan, finished))
    _forget_swap_plan(plan_session)
    return finished, True


def _interrupted_swap(job_id: str, session_id: str) -> tuple[str, dict[str, Any]] | None:
    candidates = [
        task
        for task in catalog_records(TASKS)
        if isinstance(task, dict)
        and str(task.get("job_id") or "") == job_id
        and str(task.get("id") or "") != session_id
        and is_replication_task(task)
        and str(task.get("status") or "") not in ("completed", "queued", "running")
        and isinstance((task.get("result") or {}).get("swap_plan"), dict)
        and (task.get("result") or {}).get("swap_plan")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda task: str(task.get("created_at") or ""))
    latest = candidates[-1]
    return str(latest["id"]), dict(latest["result"]["swap_plan"])


def _own_swap_plan(session_id: str) -> tuple[str, dict[str, Any]] | None:
    task = TASKS.get(session_id)
    plan = (task.get("result") or {}).get("swap_plan") if isinstance(task, dict) else None
    return (session_id, dict(plan)) if isinstance(plan, dict) and plan else None


def _forget_swap_plan(session_id: str) -> None:
    with TASK_LOCK:
        stored = TASKS.get(session_id)
        if not isinstance(stored, dict):
            return
        result = dict(stored.get("result") or {})
        result["swap_plan"] = {}
        updated = dict(stored, result=result)
        TASKS[session_id] = updated
    persist_task(updated)


def metadata_after_swap(
    replica: dict[str, Any], plan: dict[str, Any], finished: dict[str, Any]
) -> dict[str, Any]:
    by_slot = {disk["slot"]: disk for disk in finished["disks"]}
    disks = [
        {
            "key": change["key"],
            "slot": change["slot"],
            "path": change["new_path"],
            "size_bytes": by_slot.get(change["slot"], {}).get("size_bytes", 0),
            "source_capacity_bytes": change["source_capacity_bytes"],
        }
        for change in plan["changes"]
        if change["new_path"]
    ]
    return {
        **replica,
        "vm_name": finished["name"],
        "host": finished["host"],
        "point_id": plan["point_id"],
        "point_created_at": plan["point_created_at"],
        "last_success_at": utc_now(),
        "fingerprint": finished["fingerprint"],
        "start_marker": finished.get("start_marker"),
        "disks": disks,
    }


def remove_replica_vms(job: dict[str, Any], node: Any, platform: Any) -> dict[str, Any]:
    job_id = str(job["id"])
    replica = replica_of(job)
    state = platform.find_replica(node, replica["vm_id"]) if replica.get("vm_id") else None
    if state is not None and state["powered_on"]:
        raise ReplicationError(
            f"Replica {state['name']} is powered on, so it may be in use after a failover; it is not deleted. Turn it "
            "off first, or delete the job without --delete-replica to keep the VM"
        )
    replica_session = marker_session(state["marker_text"], job_id) if state is not None else ""
    replica_session = replica_session or str(replica.get("created_by_session") or "")
    marked = [found for found in platform.replicas_with_marker(node, marker_prefix(job_id)) if found["vm_id"] != replica.get("vm_id")]
    staged = [found for found in marked if not found["powered_on"] and unfinished_marked_vm(found, job_id, replica_session)]
    kept = [found["name"] for found in marked if found not in staged]
    removed = []
    left: list[str] = []
    for found in ([state] if state is not None else []) + staged:
        folders = {disk_folder(disk["path"]) for disk in found["disks"] if disk["path"]}
        left.extend(platform.destroy_replica(node, found))
        for folder in sorted(folders):
            if "-staging-" in folder.replace("\\", "/").rsplit("/", 1)[-1] and hasattr(platform, "remove_folder"):
                platform.remove_folder(node, found["host"], folder)
        removed.append(found["name"])
    return {
        "replica_deleted": state is not None,
        "replica_vm_name": state["name"] if state is not None else str(replica.get("vm_name") or ""),
        "removed_vms": removed,
        "kept_vms": kept,
        "files_left": left,
    }
