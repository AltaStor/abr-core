# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from ..compact_help_formatter import CompactHelpFormatter
from ..exit_codes import INVALID_STATE
from ..parsing.common_arguments import add_name_or_id_selector
from ..parsing.secret_arguments import (
    REPO_PASSWORD_ENV_VAR,
    REPO_SECRET_KEY_ENV_VAR,
    add_secret_argument,
    resolve_secret,
)
from ..progress_watch import run_with_elapsed_indicator
from .base import CommandDependencies

IMMUTABILITY_MODES = ("none", "governance", "compliance")
IMMUTABILITY_VALIDATION_TIMEOUT_SECONDS = 21600
STORAGE_VALIDATION_TIMEOUT_SECONDS = 900


def _parse_remote_address(repo_type: str, address: str | None) -> dict[str, str]:
    if not address:
        return {}
    if repo_type == "nfs" and ":" in address:
        server, share = address.split(":", 1)
        return {"server": server, "share": share}
    if repo_type == "smb":
        cleaned = address.replace("\\", "/").lstrip("/")
        parts = cleaned.split("/", 1)
        if len(parts) == 2:
            return {"server": parts[0], "share": parts[1]}
    return {"path": address}


def _configures_immutability(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name, None) is not None
        for name in ("immutable", "immutability_days", "immutability_retention")
    )


def _build_immutability_payload(args: argparse.Namespace) -> dict[str, object]:
    payload: dict[str, object] = {}
    mode = getattr(args, "immutable", None)
    days = getattr(args, "immutability_days", None)
    if mode is not None:
        payload["mode"] = str(mode).strip().lower()
    if days is not None:
        payload["retention_days"] = int(days)
    scope = getattr(args, "immutability_retention", None)
    if scope is not None:
        payload["retention_scope"] = "job_retention" if str(scope) == "job" else "minimum_only"
    return payload


def _build_s3_payload(args: argparse.Namespace) -> dict[str, object]:
    payload: dict[str, object] = {}
    for source_name, target_name in (
        ("bucket", "bucket"),
        ("prefix", "prefix"),
        ("region", "region"),
        ("endpoint_url", "endpoint_url"),
        ("access_key", "access_key"),
        ("secret_key", "secret_key"),
        ("profile", "profile"),
        ("description", "description"),
    ):
        value = getattr(args, source_name, None)
        if value is not None:
            payload[target_name] = value
    if getattr(args, "path_style", None) is not None:
        payload["path_style"] = bool(args.path_style)
    if getattr(args, "verify_tls", None) is not None:
        payload["verify_tls"] = bool(args.verify_tls)
    immutability = _build_immutability_payload(args)
    if immutability:
        payload["immutability"] = immutability
    return payload


def _current_immutability(current: dict[str, object] | None) -> tuple[str, int]:
    stored = (current or {}).get("immutability")
    stored = stored if isinstance(stored, dict) else {}
    try:
        days = int(stored.get("retention_days") or 0)
    except (TypeError, ValueError):
        days = 0
    return str(stored.get("mode") or "none").strip().lower(), days


def _validate_immutability_args(
    args: argparse.Namespace,
    *,
    repo_type: str,
    current: dict[str, object] | None = None,
) -> None:
    mode = getattr(args, "immutable", None)
    days = getattr(args, "immutability_days", None)
    scope = getattr(args, "immutability_retention", None)
    if mode is None and days is None and scope is None:
        return
    if repo_type != "s3":
        raise ValueError(
            "--immutable, --immutability-days and --immutability-retention are only available for s3 repositories"
        )
    if days is not None and int(days) < 1:
        raise ValueError("--immutability-days must be at least 1")

    current_mode, current_days = _current_immutability(current)
    effective_mode = str(mode).strip().lower() if mode is not None else current_mode
    effective_days = int(days) if days is not None else current_days

    if effective_mode == "none":
        if mode is not None and days is not None:
            raise ValueError("--immutable none cannot be combined with --immutability-days")
        if days is not None:
            raise ValueError("--immutability-days requires --immutable governance or --immutable compliance")
        if scope is not None:
            raise ValueError("--immutability-retention requires --immutable governance or --immutable compliance")
        return
    if effective_days < 1:
        raise ValueError(f"--immutable {effective_mode} requires --immutability-days")


def _validate_create_args(args: argparse.Namespace) -> None:
    repo_type = str(args.type or "").lower()
    _validate_immutability_args(args, repo_type=repo_type)
    if getattr(args, "create_bucket", None) and repo_type != "s3":
        raise ValueError("--create-bucket is only available for s3 repositories")
    if repo_type == "local":
        if not getattr(args, "path", None):
            raise ValueError("local repositories require --path")
        return
    if repo_type == "s3":
        s3_payload = _build_s3_payload(args)
        if not str(s3_payload.get("bucket") or "").strip():
            raise ValueError("s3 repositories require --bucket")
        if not str(s3_payload.get("region") or "").strip():
            raise ValueError("s3 repositories require --region")
        endpoint_url = str(s3_payload.get("endpoint_url") or "").strip()
        if endpoint_url:
            parsed = urlparse(endpoint_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("s3 repositories require a valid --endpoint-url")
        access_key = str(s3_payload.get("access_key") or "").strip()
        secret_key = str(s3_payload.get("secret_key") or "")
        if bool(access_key) != bool(secret_key):
            raise ValueError("s3 repositories require both --access-key and --secret-key, or neither")
        return

    address = getattr(args, "address", None)
    server = getattr(args, "server", None)
    share = getattr(args, "share", None)
    if not address and not (server and share):
        raise ValueError(f"{repo_type} repositories require --address or --server and --share")
    if repo_type == "smb" and (not getattr(args, "username", None) or not getattr(args, "password", None)):
        raise ValueError("smb repositories require --username and --password")


def _validate_edit_args(current: dict[str, object], args: argparse.Namespace) -> None:
    repo_type = str(current.get("type") or "").lower()
    _validate_immutability_args(args, repo_type=repo_type, current=current)
    if getattr(args, "create_bucket", None) and repo_type != "s3":
        raise ValueError("--create-bucket is only available for s3 repositories")
    if repo_type != "s3":
        return
    s3_payload = _build_s3_payload(args)
    if not s3_payload:
        return
    endpoint_url = str(s3_payload.get("endpoint_url") or "").strip()
    if endpoint_url:
        parsed = urlparse(endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("s3 repositories require a valid --endpoint-url")


def _print_repository_warnings(result: object) -> None:
    if not isinstance(result, dict):
        return
    for warning in result.get("warnings") or []:
        print(f"Warning: {warning}", file=sys.stderr)


def _print_immutability_effect(result: object) -> None:
    if not isinstance(result, dict):
        return
    effect = result.get("immutability_effect")
    if not isinstance(effect, dict):
        return
    current = effect.get("current") if isinstance(effect.get("current"), dict) else {}
    mode = str(current.get("mode") or "none")
    days = int(current.get("retention_days") or 0)
    headline = "disabled" if mode == "none" or days < 1 else f"{mode}, {days} day(s)"
    print(f"Immutability: {headline}", file=sys.stderr)
    for line in effect.get("effect") or []:
        print(f"  {line}", file=sys.stderr)
    for line in effect.get("limitations") or []:
        print(f"  Limitation: {line}", file=sys.stderr)
    for line in effect.get("warnings") or []:
        print(f"  Warning: {line}", file=sys.stderr)


def _print_immutability_checks(result: object) -> None:
    if not isinstance(result, dict):
        return
    for check in result.get("checks") or []:
        if not isinstance(check, dict):
            continue
        message = str(check.get("message") or "").strip()
        suffix = f": {message}" if message else ""
        print(f"  [{check.get('status')}] {check.get('name')}{suffix}", file=sys.stderr)


class RepositoryCommands:
    def register(self, registry) -> None:
        repo = registry.add_parser("repo", help="Repository management", description="Repository commands", formatter_class=CompactHelpFormatter)
        commands = repo.add_subparsers(dest="repo_command", required=True)

        add = commands.add_parser(
            "add",
            help="Add backup repository.",
            description=(
                "Add backup repository.\n\n"
                f"The SMB password is read from {REPO_PASSWORD_ENV_VAR} and the S3 secret key from "
                f"{REPO_SECRET_KEY_ENV_VAR}, or prompted for when they are not set; avoid --password VALUE "
                "and --secret-key VALUE, which show them to every local user.\n\n"
                "Examples:\n"
                '  abr repo add --name "Local-Repo-1" --type local --path /mnt/backups/repo1\n'
                '  abr repo add --name "HV-Local" --type local --path /mnt/backups/hv --server backup01 '
                '--share hv --username "CORP\\backupsvc"\n'
                '  abr repo add --name "SMB-Repo" --type smb --address //fileserver/backups '
                "--username backupsvc\n"
                '  abr repo add --name "S3-Repo" --type s3 --bucket altastor-backups --region us-east-1 '
                "--access-key AKIA...\n"
                f'  {REPO_SECRET_KEY_ENV_VAR}=... abr repo add --name "S3-WORM" --type s3 --bucket altastor-objectlock '
                "--region us-east-1 --access-key AKIA... --immutable governance --immutability-days 30"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add.add_argument("--name", required=True, help="Repository name")
        add.add_argument("--type", required=True, choices=("local", "nfs", "smb", "s3"), help="Repository type")
        add.add_argument("--path", help="Local repository path")
        add.add_argument("--address", help="NFS host:/export or SMB //host/share")
        add.add_argument(
            "--server",
            help="Remote repository server; for a local repository, the SMB server Hyper-V hosts reach its folder on",
        )
        add.add_argument(
            "--share",
            help="Remote repository share/export; for a local repository, the SMB share (and folder) of --path",
        )
        add.add_argument("--username", help="Repository username (for a local repository, the SMB share's account)")
        add_secret_argument(add, "--password", env_var=REPO_PASSWORD_ENV_VAR, help_text="Repository password")
        add.add_argument("--description", help="Repository description")
        add.add_argument("--bucket", help="S3 bucket name")
        add.add_argument("--prefix", help="S3 key prefix inside the bucket")
        add.add_argument("--region", help="S3 region")
        add.add_argument("--endpoint-url", dest="endpoint_url", help="Custom S3-compatible endpoint URL")
        add.add_argument("--access-key", dest="access_key", help="S3 access key")
        add_secret_argument(add, "--secret-key", env_var=REPO_SECRET_KEY_ENV_VAR, help_text="S3 secret key")
        add.add_argument("--profile", help="AWS profile name")
        add_path_style = add.add_mutually_exclusive_group()
        add_path_style.add_argument("--path-style", dest="path_style", action="store_true", default=None, help="Use path-style S3 addressing")
        add_path_style.add_argument("--virtual-host-style", dest="path_style", action="store_false", help="Use virtual-hosted style S3 addressing")
        add_tls = add.add_mutually_exclusive_group()
        add_tls.add_argument("--verify-tls", dest="verify_tls", action="store_true", default=None, help="Verify TLS certificates for S3 endpoints")
        add_tls.add_argument("--no-verify-tls", dest="verify_tls", action="store_false", help="Disable TLS verification for S3 endpoints")
        add.add_argument(
            "--immutable",
            choices=IMMUTABILITY_MODES,
            help="S3 Object Lock mode applied to new backups (s3 repositories only)",
        )
        add.add_argument(
            "--immutability-days",
            dest="immutability_days",
            type=int,
            help=(
                "Minimum days S3 Object Lock holds each new backup. The period actually written "
                "follows the job's full-backup cycle and its retention, and is never shorter than "
                "this. Required with --immutable governance/compliance."
            ),
        )
        add.add_argument(
            "--immutability-retention",
            dest="immutability_retention",
            choices=("job", "minimum"),
            help=(
                "Whether the Object Lock period runs to the end of the job's full-backup cycle, so one "
                "chain shares one date (job, the default), or is only --immutability-days counted from "
                "each backup (minimum)"
            ),
        )
        add.add_argument(
            "--create-bucket",
            dest="create_bucket",
            action="store_true",
            default=None,
            help=(
                "Create the S3 bucket when it does not exist. Without this the repository is refused. "
                "A bucket created this way cannot hold immutable backups, because Object Lock is only "
                "settable while a bucket is being created"
            ),
        )
        add.set_defaults(handler=self.create)

        list_parser = commands.add_parser(
            "list",
            help="List backup repositories.",
            description="List backup repositories.\n\nExamples:\n  abr repo list",
            formatter_class=CompactHelpFormatter,
        )
        list_parser.set_defaults(handler=self.list)

        for name, help_text, handler in (
            ("info", "Show repository details.", self.info),
            ("remove", "Remove/unlink a repository from ABR management.", self.remove),
            ("rescan", "Refresh repository metadata/catalog state.", self.rescan),
            ("verify", "Verify repository availability and integrity.", self.verify),
        ):
            description = (
                f"{help_text}\n\n"
                "Examples:\n"
                f'  abr repo {name} --name "Local-Repo-1"\n'
                f"  abr repo {name} --id <repo-id>"
            )
            parser = commands.add_parser(name, help=help_text, description=description, formatter_class=CompactHelpFormatter)
            add_name_or_id_selector(parser, name_help="Repository name", id_help="Repository ID")
            parser.set_defaults(handler=handler)

        edit = commands.add_parser(
            "edit",
            help="Edit repository settings.",
            description=(
                "Edit repository settings.\n\n"
                "Examples:\n"
                '  abr repo edit --name "Local-Repo-1" --description "Primary on-prem repository"\n'
                '  abr repo edit --name "Local-Repo-1" --server backup01 --share repo1 --username "CORP\\backupsvc" '
                "--password\n"
                '  abr repo edit --id <repo-id> --new-name "Local-Repo-1-Renamed"\n'
                '  abr repo edit --name "S3-WORM" --immutable governance --immutability-days 30\n'
                '  abr repo edit --name "S3-WORM" --immutable none'
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(edit, name_help="Repository name", id_help="Repository ID")
        edit.add_argument("--new-name", help="Updated repository name")
        edit.add_argument("--path", help="Updated local repository path")
        edit.add_argument("--address", help="Updated remote repository address")
        edit.add_argument(
            "--server", help="Updated remote repository server, or a local repository's SMB server (\"\" removes it)"
        )
        edit.add_argument(
            "--share", help="Updated remote repository share/export, or a local repository's SMB share (\"\" removes it)"
        )
        edit.add_argument("--username", help="Updated repository username")
        add_secret_argument(edit, "--password", env_var=REPO_PASSWORD_ENV_VAR, help_text="Updated repository password")
        edit.add_argument("--description", help="Updated repository description")
        edit.add_argument("--bucket", help="Updated S3 bucket name")
        edit.add_argument("--prefix", help="Updated S3 key prefix")
        edit.add_argument("--region", help="Updated S3 region")
        edit.add_argument("--endpoint-url", dest="endpoint_url", help="Updated custom S3 endpoint URL")
        edit.add_argument("--access-key", dest="access_key", help="Updated S3 access key")
        add_secret_argument(edit, "--secret-key", env_var=REPO_SECRET_KEY_ENV_VAR, help_text="Updated S3 secret key")
        edit.add_argument("--profile", help="Updated AWS profile name")
        edit_path_style = edit.add_mutually_exclusive_group()
        edit_path_style.add_argument("--path-style", dest="path_style", action="store_true", default=None, help="Use path-style S3 addressing")
        edit_path_style.add_argument("--virtual-host-style", dest="path_style", action="store_false", help="Use virtual-hosted style S3 addressing")
        edit_tls = edit.add_mutually_exclusive_group()
        edit_tls.add_argument("--verify-tls", dest="verify_tls", action="store_true", default=None, help="Verify TLS certificates for S3 endpoints")
        edit_tls.add_argument("--no-verify-tls", dest="verify_tls", action="store_false", help="Disable TLS verification for S3 endpoints")
        edit.add_argument(
            "--immutable",
            choices=IMMUTABILITY_MODES,
            help="Updated S3 Object Lock mode applied to new backups (s3 repositories only)",
        )
        edit.add_argument(
            "--immutability-days",
            dest="immutability_days",
            type=int,
            help=(
                "Updated minimum days S3 Object Lock holds each new backup; the period actually "
                "written follows the job's backup cycle and is never shorter than this"
            ),
        )
        edit.add_argument(
            "--immutability-retention",
            dest="immutability_retention",
            choices=("job", "minimum"),
            help=(
                "Updated rule for the Object Lock period: run to the end of the full-backup cycle (job) "
                "or only --immutability-days counted from each backup (minimum)"
            ),
        )
        edit.add_argument(
            "--create-bucket",
            dest="create_bucket",
            action="store_true",
            default=None,
            help=(
                "Create the S3 bucket when it does not exist. Without this the repository is refused. "
                "A bucket created this way cannot hold immutable backups, because Object Lock is only "
                "settable while a bucket is being created"
            ),
        )
        edit.set_defaults(handler=self.edit)

        verify_immutability = commands.add_parser(
            "verify-immutability",
            help="Validate that an S3 repository can hold immutable backups.",
            description=(
                "Validate that an S3 repository can hold immutable backups.\n\n"
                "Checks Object Lock, versioning and permissions, then proves them by writing a small locked test\n"
                "backup and confirming the storage refuses to delete it. The probe stays until its lock expires.\n\n"
                "Examples:\n"
                '  abr repo verify-immutability --name "S3-WORM"\n'
                "  abr repo verify-immutability --id <repo-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(verify_immutability, name_help="Repository name", id_help="Repository ID")
        verify_immutability.set_defaults(handler=self.verify_immutability)

        apply_immutability = commands.add_parser(
            "apply-immutability",
            help="Put stored backups under the repository's current immutability setting.",
            description=(
                "Put stored backups under the repository's current immutability setting.\n\n"
                "Applies it to every restore point already stored, counting the period from now. Object Lock\n"
                "cannot be lifted, so this cannot be undone; refusals are reported per restore point.\n\n"
                "Examples:\n"
                '  abr repo apply-immutability --name "S3-WORM"\n'
                "  abr repo apply-immutability --id <repo-id>"
            ),
            formatter_class=CompactHelpFormatter,
        )
        add_name_or_id_selector(apply_immutability, name_help="Repository name", id_help="Repository ID")
        apply_immutability.set_defaults(handler=self.apply_immutability)

    @staticmethod
    def _resolve(deps: CommandDependencies, args: argparse.Namespace) -> dict[str, object]:
        return deps.context.api_client.get("api/v1/repositories", params={"name": getattr(args, "name", None), "id": getattr(args, "id", None)})

    @staticmethod
    def _resolve_id(deps: CommandDependencies, args: argparse.Namespace) -> str:
        if getattr(args, "id", None):
            if getattr(args, "name", None):
                items = deps.context.api_client.get("api/v1/repositories").get("items", [])
                matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
                if not matches or str(matches[0].get("id")) != str(args.id):
                    raise ValueError("Repository selector mismatch: --name and --id refer to different records")
            return str(args.id)
        if not getattr(args, "name", None):
            raise ValueError("Repository selection requires --name or --id")
        items = deps.context.api_client.get("api/v1/repositories").get("items", [])
        matches = [item for item in items if str(item.get("name") or "").lower() == str(args.name).lower()]
        if not matches:
            raise ValueError(f"Repository not found: {args.name}")
        if len(matches) > 1:
            raise ValueError(f"Multiple repositories matched name: {args.name}")
        return str(matches[0]["id"])

    def create(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        repo_type = str(args.type or "").lower()
        args.password = resolve_secret(
            getattr(args, "password", None),
            env_var=REPO_PASSWORD_ENV_VAR,
            prompt="Repository password",
            needed=repo_type in {"smb", "local"} and bool(getattr(args, "username", None)),
        )
        args.secret_key = resolve_secret(
            getattr(args, "secret_key", None),
            env_var=REPO_SECRET_KEY_ENV_VAR,
            prompt="S3 secret key",
            needed=repo_type == "s3" and bool(str(getattr(args, "access_key", None) or "").strip()),
        )
        _validate_create_args(args)
        payload = {
            "name": args.name,
            "type": args.type,
            "path": args.path,
            "server": args.server,
            "share": args.share,
            "username": args.username,
            "password": args.password,
            "description": args.description,
        }
        payload.update({k: v for k, v in _parse_remote_address(args.type, args.address).items() if payload.get(k) in {None, ""}})
        if args.type == "s3":
            payload["s3"] = _build_s3_payload(args)
            if getattr(args, "create_bucket", None):
                payload["create_bucket"] = True
        if args.type == "s3":
            immutability = _configures_immutability(args)
            result = run_with_elapsed_indicator(
                lambda: deps.context.api_client.post(
                    "api/v1/repositories",
                    json=payload,
                    timeout=(
                        IMMUTABILITY_VALIDATION_TIMEOUT_SECONDS
                        if immutability
                        else STORAGE_VALIDATION_TIMEOUT_SECONDS
                    ),
                ),
                label=(
                    "Validating repository immutability"
                    if immutability
                    else "Validating repository storage"
                ),
            )
        else:
            result = deps.context.api_client.post("api/v1/repositories", json=payload)
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("path", "PATH"), ("status", "STATUS")])
        _print_repository_warnings(result)
        _print_immutability_effect(result)
        return 0

    def list(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        payload = deps.context.api_client.get("api/v1/repositories")
        deps.context.print_output(payload, [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("path", "PATH"), ("status", "STATUS")])
        return 0

    def info(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.get(f"api/v1/repositories/{self._resolve_id(deps, args)}"))
        return 0

    def edit(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        repo_id = self._resolve_id(deps, args)
        current = deps.context.api_client.get(f"api/v1/repositories/{repo_id}")
        args.password = resolve_secret(
            getattr(args, "password", None), env_var=REPO_PASSWORD_ENV_VAR, prompt="Repository password"
        )
        args.secret_key = resolve_secret(
            getattr(args, "secret_key", None), env_var=REPO_SECRET_KEY_ENV_VAR, prompt="S3 secret key"
        )
        _validate_edit_args(current, args)
        payload = {key: value for key, value in {
            "name": args.new_name,
            "path": args.path,
            "server": args.server,
            "share": args.share,
            "username": args.username,
            "password": args.password,
            "description": args.description,
        }.items() if value is not None}
        if args.address:
            payload.update(_parse_remote_address(str(current.get("type") or ""), args.address))
        if str(current.get("type") or "").lower() == "s3":
            s3_payload = _build_s3_payload(args)
            if s3_payload:
                payload["s3"] = s3_payload
            if getattr(args, "create_bucket", None):
                payload["create_bucket"] = True
        changes_connection = any(key != "description" for key in payload.get("s3") or {})
        if _configures_immutability(args) or (changes_connection and _current_immutability(current)[0] != "none"):
            result = run_with_elapsed_indicator(
                lambda: deps.context.api_client.patch(
                    f"api/v1/repositories/{repo_id}", json=payload, timeout=IMMUTABILITY_VALIDATION_TIMEOUT_SECONDS
                ),
                label="Validating repository immutability",
            )
        elif changes_connection:
            result = run_with_elapsed_indicator(
                lambda: deps.context.api_client.patch(
                    f"api/v1/repositories/{repo_id}", json=payload, timeout=STORAGE_VALIDATION_TIMEOUT_SECONDS
                ),
                label="Validating repository storage",
            )
        else:
            result = deps.context.api_client.patch(f"api/v1/repositories/{repo_id}", json=payload)
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("type", "TYPE"), ("path", "PATH"), ("status", "STATUS")])
        _print_repository_warnings(result)
        _print_immutability_effect(result)
        return 0

    def remove(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.delete(f"api/v1/repositories/{self._resolve_id(deps, args)}"), [("id", "ID"), ("removed", "REMOVED")])
        return 0

    def rescan(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        deps.context.print_output(deps.context.api_client.post(f"api/v1/repositories/{self._resolve_id(deps, args)}/rescan"), [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("last_scan_at", "LAST SCAN")])
        return 0

    def verify(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        repository_id = self._resolve_id(deps, args)
        result = run_with_elapsed_indicator(
            lambda: deps.context.api_client.post(f"api/v1/repositories/{repository_id}/verify", timeout=21600),
            label="Verifying repository",
        )
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("message", "MESSAGE")])
        if isinstance(result, dict) and result.get("status") == "error":
            return INVALID_STATE
        return 0

    def apply_immutability(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        repository_id = self._resolve_id(deps, args)
        result = run_with_elapsed_indicator(
            lambda: deps.context.api_client.post(
                f"api/v1/repositories/{repository_id}/apply-immutability",
                timeout=IMMUTABILITY_VALIDATION_TIMEOUT_SECONDS,
            ),
            label="Applying immutability to stored backups",
        )
        deps.context.print_output(
            result,
            [
                ("id", "ID"),
                ("name", "NAME"),
                ("status", "STATUS"),
                ("updated_restore_points", "UPDATED"),
                ("failed_restore_points", "FAILED"),
                ("message", "MESSAGE"),
            ],
        )
        if isinstance(result, dict):
            for failure in result.get("failures") or []:
                print(
                    f"  {failure.get('restore_point_id')} / {failure.get('backup_name')}: {failure.get('message')}",
                    file=sys.stderr,
                )
            if result.get("status") == "error":
                return INVALID_STATE
        return 0

    def verify_immutability(self, args: argparse.Namespace) -> int:
        deps = CommandDependencies(args)
        repository_id = self._resolve_id(deps, args)
        result = run_with_elapsed_indicator(
            lambda: deps.context.api_client.post(
                f"api/v1/repositories/{repository_id}/verify-immutability", timeout=21600
            ),
            label="Validating repository immutability",
        )
        deps.context.print_output(result, [("id", "ID"), ("name", "NAME"), ("status", "STATUS"), ("message", "MESSAGE")])
        _print_immutability_checks(result)
        _print_immutability_effect(result)
        if isinstance(result, dict) and result.get("status") == "error":
            return INVALID_STATE
        return 0
