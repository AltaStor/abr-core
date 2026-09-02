# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from argparse import Namespace

from ..cli_context import CliContext
from ..services import JobService, RestoreService, SystemService


class CommandDependencies:
    def __init__(self, args: Namespace) -> None:
        self.context = CliContext.from_args(args)
        self.jobs = JobService(self.context)
        self.restore = RestoreService(self.context)
        self.system = SystemService(self.context)
