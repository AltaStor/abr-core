# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any
from functools import wraps

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import (
    ConnectionFailure,
    ExecutionTimeout,
    InvalidOperation,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
)


logger = logging.getLogger(__name__)

_RETRYABLE = (
    ConnectionFailure,
    NetworkTimeout,
    ExecutionTimeout,
    ServerSelectionTimeoutError,
    InvalidOperation,
)

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.5
_RETRY_MAX_DELAY = 30.0  # cap so backoff can never grow unbounded


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _retry(attempts: int = _RETRY_ATTEMPTS, base_delay: float = _RETRY_BASE_DELAY):
    def decorator(fn):
        @wraps(fn)
        def wrapper(self: Database, *args, **kwargs):
            if attempts <= 0:
                return fn(self, *args, **kwargs)

            delay = base_delay
            last_exc: Exception | None = None

            for attempt in range(1, attempts + 1):
                try:
                    return fn(self, *args, **kwargs)
                except _RETRYABLE as exc:
                    last_exc = exc
                    # Capped exponential backoff with full jitter: spreads retries
                    # so a DB blip across many concurrent callers can't synchronize
                    # into a reconnect storm that pins the CPU.
                    sleep_for = random.uniform(0, min(delay, _RETRY_MAX_DELAY))
                    logger.warning(
                        "Transient DB error in %s (attempt %d/%d): %s — retrying in %.2fs…",
                        fn.__name__, attempt, attempts, exc, sleep_for,
                    )
                    time.sleep(sleep_for)
                    delay = min(delay * 2, _RETRY_MAX_DELAY)
                except PyMongoError as exc:
                    logger.error("Non-retryable DB error in %s: %s", fn.__name__, exc)
                    raise
            logger.error(
                "All %d attempts failed in %s. Last error: %s",
                attempts, fn.__name__, last_exc,
            )
            if last_exc is not None:
                raise last_exc

        return wrapper
    return decorator


class Database:
    def __init__(
        self,
        client: MongoClient,
        db_name: str,
        *,
        uri: str,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._uri = uri
        self._db_name = db_name
        self._client_options = dict(client_options or {})
        self._db = client[db_name]

    @classmethod
    def connect(
        cls,
        uri: str = "mongodb://localhost:27017",
        db_name: str = "backup_db",
        *,
        server_selection_timeout_ms: int = 3000,
        connect_timeout_ms: int = 3000,
        socket_timeout_ms: int = 10000,
        max_pool_size: int = 50,
    ) -> Database:
        logger.info("Connecting to MongoDB at %s (db=%s)…", uri, db_name)
        client_options = {
            "serverSelectionTimeoutMS": server_selection_timeout_ms,
            "connectTimeoutMS": connect_timeout_ms,
            "socketTimeoutMS": socket_timeout_ms,
            "maxPoolSize": max_pool_size,
        }
        client = MongoClient(uri, **client_options)
        client.admin.command("ping")
        logger.info("MongoDB connection established.")

        instance = cls(client, db_name, uri=uri, client_options=client_options)
        instance._ensure_indexes()
        return instance

    def _ensure_indexes(self) -> None:
        specs = {
            "nodes": "node_id",
            "vms": "vm_id",
            "disks": "disk_id",
            "storages": "stor_id",
            "repositories": "id",
            "jobs": "id",
            "backups": "id",
            "tasks": "id",
            "users": "id",
            "sessions": "token",
            "hypervisors": "id",
            "resources": "resource_key",
        }
        for collection, field in specs.items():
            self._db[collection].create_index([(field, ASCENDING)], unique=True, name=f"uq_{field}")
        self._db["users"].create_index([("name", ASCENDING)], unique=True, name="uq_name")
        self._db["sessions"].create_index([("user_id", ASCENDING)], name="ix_user_id")
        self._db["vms"].create_index("node_id")
        self._db["disks"].create_index("vm_id")
        self._db["resources"].create_index("hypervisor_id")
        self._db["resources"].create_index([("hypervisor_id", ASCENDING), ("resource_type", ASCENDING)])
        logger.debug("Indexes verified for %d collections.", len(specs))

    @_retry()
    def upsert(self, collection: str, id_field: str, document: dict[str, Any]) -> None:
        doc_id = document.get(id_field)
        if not doc_id:
            return
        now = _utc_now()
        payload = copy.deepcopy(document)
        created_at = payload.pop("created_at", now)
        payload["updated_at"] = now
        self._db[collection].update_one(
            {id_field: doc_id},
            {"$set": payload, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )
        logger.debug("Upserted '%s' in '%s'.", doc_id, collection)

    @_retry()
    def get(self, collection: str, id_field: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._db[collection].find_one({id_field: doc_id}, {"_id": 0})
        return doc if isinstance(doc, dict) else None

    @_retry()
    def find(self, collection: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self._db[collection].find(query, {"_id": 0}))

    @_retry()
    def delete(self, collection: str, id_field: str, doc_id: str) -> bool:
        result = self._db[collection].delete_one({id_field: doc_id})
        deleted = result.deleted_count > 0
        if deleted:
            logger.debug("Deleted '%s' from '%s'.", doc_id, collection)
        return deleted

    def get_pending_jobs(self) -> list[dict[str, Any]]:
        return self.find("jobs", {"status": "active"})

    def get_disks_for_vm(self, vm_id: str) -> list[dict[str, Any]]:
        return self.find("disks", {"vm_id": vm_id})

    @_retry()
    def update_job_status(self, job_id: str, status: str) -> None:
        self._db["jobs"].update_one(
            {"job_id": job_id},
            {"$set": {"status": status, "updated_at": _utc_now()}}
        )
        logger.debug("Job '%s' status → '%s'.", job_id, status)

    @_retry()
    def replace_hypervisor_resources(self, hypervisor_id: str, resources: list[dict[str, Any]]) -> None:
        self._db["resources"].delete_many({"hypervisor_id": hypervisor_id})
        now = _utc_now()
        payload: list[dict[str, Any]] = []
        for resource in resources:
            item = copy.deepcopy(resource)
            item["hypervisor_id"] = hypervisor_id
            item["resource_key"] = f"{hypervisor_id}:{resource['resource_type']}:{resource['external_id']}"
            item["updated_at"] = now
            item.setdefault("created_at", now)
            payload.append(item)
        if payload:
            self._db["resources"].insert_many(payload, ordered=False)
        logger.debug(
            "Replaced %d resource(s) for hypervisor '%s'.", len(payload), hypervisor_id
        )

    @_retry()
    def get_hypervisor_resources(self, hypervisor_id: str) -> list[dict[str, Any]]:
        return list(
            self._db["resources"].find(
                {"hypervisor_id": hypervisor_id},
                {"_id": 0, "hypervisor_id": 0, "resource_key": 0},
            )
        )

    @_retry()
    def delete_hypervisor_resources(self, hypervisor_id: str) -> None:
        self._db["resources"].delete_many({"hypervisor_id": hypervisor_id})
        logger.debug("Deleted resources for hypervisor '%s'.", hypervisor_id)

    @_retry()
    def get_queued_tasks(self, limit: int | None = None) -> list[dict[str, Any]]:
        cursor = self._db["tasks"].find(
            {"status": "queued"},
            {"_id": 0},
        ).sort("created_at", ASCENDING)
        if limit is not None:
            cursor = cursor.limit(limit)
        return list(cursor)

    @_retry()
    def claim_task(self, task_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        doc = self._db["tasks"].find_one_and_update(
            {"id": task_id, "status": "queued"},
            {"$set": {"status": "running", "updated_at": now}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if not isinstance(doc, dict):
            return None
        doc["status"] = "running"
        doc["updated_at"] = now
        return doc

    def ping(self) -> None:
        self._client.admin.command("ping")

    def close(self) -> None:
        self._client.close()
        logger.info("MongoDB connection closed.")

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_) -> None:
        self.close()
