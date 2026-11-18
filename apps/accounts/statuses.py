"""The statuses in which each module of a thesis is open (M13a): ONE table.

A module open at a status writes there (its own guard; the right itself is
narrowed by permissions.WRITES_BY_STATUS), sends its e-mails and reminders, and
its rows enter the digests. Rule of the sheet (docs/modules/M13-fin-de-these.md):
the notifications of a module follow its right to write. After the defence,
conferences and ST approvals (M6) and the receipts of the missions begun before
it (M7, ``travel_receipts``, narrowed by the mission in travel.services) and the
documents (M2) stay open; everything else is frozen; nothing is open on a CLOSED
thesis.

Every module reads this table: services, reminders, ``thesis_open`` of the
notifications, the construction of the digests. tests/test_open_thesis_copies.py
refuses any other list of statuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .models import Thesis

S = Thesis.Status
_OPEN = (S.SETUP, S.ACTIVE)
_AND_DEFENDED = (S.SETUP, S.ACTIVE, S.DEFENDED)

MODULE_STATUSES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "team": _OPEN,  # invitations, roles, joining (M1)
        "documents": _AND_DEFENDED,  # the final version, corrections (M2)
        "admin_file": _OPEN,
        "objectives": _OPEN,
        "meetings": _OPEN,
        "conferences": _AND_DEFENDED,
        "travel": _OPEN,
        "travel_receipts": _AND_DEFENDED,
        "feeds": _OPEN,
        "lifecycle": (S.ACTIVE, S.DEFENDED),
    }
)


def open_statuses(module: str) -> tuple[str, ...]:
    return MODULE_STATUSES[module]


def any_open() -> tuple[str, ...]:
    """The statuses in which at least one module is open (the digests run there)."""
    return tuple(s for s in S if any(s in v for v in MODULE_STATUSES.values()))


def is_open_for(thesis: Any, module: str) -> bool:
    """``thesis``: a Thesis or a status; the caller gives a fresh one."""
    status = thesis if isinstance(thesis, str) else thesis.status
    return status in MODULE_STATUSES[module]
