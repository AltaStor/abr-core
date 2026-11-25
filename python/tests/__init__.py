# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

"""Test package for BackupServer."""

import os

# No test reaches the shares broker of the machine it runs on: a machine set up for a dev server has one, and a
# local repository a test adds would be shared through it for real. Tests of the broker's callers patch its client.
os.environ["ABR_SHARES_BROKER_SOCKET"] = "/nonexistent/abr-tests/altastor-backup-shares.sock"
