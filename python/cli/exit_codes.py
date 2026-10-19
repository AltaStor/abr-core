# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

# Stable CLI process exit codes.
SUCCESS = 0
GENERAL_ERROR = 1
INVALID_USAGE = 2
NOT_FOUND = 3
INVALID_STATE = 4
UNAUTHORIZED = 5
VALIDATION_FAILED = 6
# The signed-in user's role does not allow the command, or the account is disabled.
PERMISSION_DENIED = 7
