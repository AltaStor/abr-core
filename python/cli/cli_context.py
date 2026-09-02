# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import token_store
from .api.client import AltaStorApiClient
from .cli_output_renderer import CliOutputRenderer
from .tls_trust import ensure_trusted

TOKEN_ENV_VAR = "ABR_TOKEN"


def resolve_token(base_url: str) -> str | None:
    return os.environ.get(TOKEN_ENV_VAR) or token_store.load_token(base_url)


@dataclass(slots=True)
class CliContext:
    base_url: str
    output: str
    api_client: AltaStorApiClient
    output_renderer: CliOutputRenderer

    @classmethod
    def from_args(cls, args: Any) -> "CliContext":
        base_url = str(args.base_url)
        ensure_trusted(base_url)
        return cls(
            base_url=base_url,
            output=str(getattr(args, "output", "table")),
            api_client=AltaStorApiClient(base_url=base_url, token=resolve_token(base_url)),
            output_renderer=CliOutputRenderer(),
        )

    def print_output(self, payload: Any, columns: list[tuple[str, str]] | None = None) -> None:
        self.output_renderer.print_output(self.output, payload, columns)
