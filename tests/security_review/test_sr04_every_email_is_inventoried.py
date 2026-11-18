"""SR-04 (docs/security/revue-2026-11.md): the static guard of the e-mails
(apps/core/tests/test_delivery.py, NOTIFYING_MODULES) checks a LIST of modules;
nothing scans the code for other senders. Today accounts (invitation, password
reset) and vault (checkpoint) send directly, as designed but undeclared; a new
module could do the same unseen. Expected safe state: every call of send_mail or
EmailMessage is in core/delivery.py or in an explicit, reviewed list."""

from __future__ import annotations

import re
from pathlib import Path

from apps.core.tests.test_delivery import DIRECT_SENDERS, MAIL_API_READERS

ROOT = Path(__file__).resolve().parents[2]
# The reviewed lists of the static guard (fixed: apps/core/tests/test_delivery.py).
DECLARED: set[str] = set(DIRECT_SENDERS) | set(MAIL_API_READERS)


def test_sr04_no_undeclared_sender() -> None:
    senders = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "apps").rglob("*.py")
        if "tests" not in path.parts
        and re.search(r"\b(send_mail|send_mass_mail|EmailMessage|EmailMultiAlternatives)\b",
                      path.read_text())
    }  # fmt: skip
    assert senders <= DECLARED, sorted(senders - DECLARED)
