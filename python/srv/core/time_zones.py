# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Time zone names as clients send them.

Browsers name zones the way CLDR does, which keeps some names IANA has since
replaced: Chrome, Firefox and Node report "Europe/Kiev", not "Europe/Kyiv".
Debian 13 moved those old names to the tzdata-legacy package, which an
appliance does not have, so zoneinfo there does not know them. A job saved
from a browser in Kyiv was refused, and one carried over from an older
appliance ran on UTC.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Every zone a browser reports by an old name, with its current one. The Web
# UI keeps the same table (RENAMED_ZONES in src/lib/time.ts).
RENAMED_ZONES: dict[str, str] = {
    "Africa/Asmera": "Africa/Asmara",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca",
    "America/Coral_Harbour": "America/Atikokan",
    "America/Cordoba": "America/Argentina/Cordoba",
    "America/Godthab": "America/Nuuk",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Jujuy": "America/Argentina/Jujuy",
    "America/Louisville": "America/Kentucky/Louisville",
    "America/Mendoza": "America/Argentina/Mendoza",
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Europe/Kiev": "Europe/Kyiv",
    "Pacific/Enderbury": "Pacific/Kanton",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Truk": "Pacific/Chuuk",
}


def load_zone(name: str) -> ZoneInfo:
    """The zone `name` names, found by its current name when it is an old
    one, so the result's key is the name to store. Raises
    ZoneInfoNotFoundError for a zone this server does not know, and
    ValueError for a malformed name, as ZoneInfo does."""
    current = RENAMED_ZONES.get(name)
    if current is not None:
        try:
            return ZoneInfo(current)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo(name)
