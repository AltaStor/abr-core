# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from .hypervisor import Hypervisor


def build_hypervisor_plugin(record: dict[str, object]) -> Hypervisor:
    kind = str(record.get("kind", "") or "")
    if kind == "vcenter":
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("vCenter hypervisor metadata must be a mapping")
        credentials = metadata.get("credentials", {})
        if not isinstance(credentials, dict):
            raise ValueError("vCenter hypervisor credentials must be a mapping")

        from .vcenter.vcenter import VCenterNode

        return VCenterNode(
            host=str(metadata.get("host") or ""),
            user=str(credentials.get("username") or ""),
            password=str(credentials.get("password") or ""),
            port=int(metadata.get("port", 443)),
            verify_ssl=bool(metadata.get("verify_ssl", False)),
        )

    if kind == "mock":
        from .mock import MockHypervisor

        return MockHypervisor()

    raise ValueError(f"Unsupported hypervisor kind: {kind}")
