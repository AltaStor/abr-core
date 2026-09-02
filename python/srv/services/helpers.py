# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

import hashlib


def deterministic_seed(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)