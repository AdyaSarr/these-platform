"""M11b: the objectives send no e-mail of their own (the Monday summary is folded
into the activity digests). A static guard, as tests NOTIFYING_MODULES does for
the modules that notify: nothing in apps/objectives (tests aside) may send an
e-mail but through apps.core.delivery."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"\b(send_mail|send_mass_mail|mail_admins|mail_managers|EmailMessage|"
    r"EmailMultiAlternatives|get_connection|smtplib)\b"
)
SOURCES = sorted(
    p for p in ROOT.rglob("*.py") if "tests" not in p.parts and "migrations" not in p.parts
)


def test_there_are_sources() -> None:
    assert len(SOURCES) > 5


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_e_mail_is_sent_outside_core_delivery(path: Path) -> None:
    found = FORBIDDEN.findall(path.read_text(encoding="utf-8"))
    assert found == [], f"{path}: {found}"
