# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Notification settings and histories kept in memory for a test."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import SYSTEM_STATE
from python.srv.services import notification_service


def forget_history() -> None:
    for channel in notification_service.CHANNELS:
        channel.history._items = None


def in_memory(case: unittest.TestCase) -> None:
    """Keep the settings in memory. Importing the server applies defaults.env,
    so without this they would go to whatever catalog answers on its URI."""
    database = patch("python.srv.core.mongo_db_wrapper.get_database", return_value=None)
    database.start()
    case.addCleanup(database.stop)
    SYSTEM_STATE.clear()
    forget_history()
    case.addCleanup(SYSTEM_STATE.clear)
    case.addCleanup(forget_history)
