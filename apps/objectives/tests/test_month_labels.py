"""Month labels of the progress chart: French abbreviations (janv., févr., ... déc.),
from Django's localized formatting (format "N"), never "Jui"/"Jul"."""

from __future__ import annotations

import datetime as dt

from django.utils import translation

from apps.objectives.views import month_label

EXPECTED = [
    "janv. 2026", "févr. 2026", "mars 2026", "avr. 2026", "mai 2026", "juin 2026",
    "juil. 2026", "août 2026", "sept. 2026", "oct. 2026", "nov. 2026", "déc. 2026",
]  # fmt: skip


def test_the_twelve_months_in_french() -> None:
    with translation.override("fr"):
        assert [month_label(dt.date(2026, m, 1)) for m in range(1, 13)] == EXPECTED


def test_june_and_july_are_told_apart() -> None:
    with translation.override("fr"):
        june, july = month_label(dt.date(2026, 6, 1)), month_label(dt.date(2026, 7, 1))
    assert june != july and "Jui" not in june + july and "Jul" not in june + july


def test_any_day_of_the_month_gives_the_month() -> None:
    with translation.override("fr"):
        assert month_label(dt.date(2027, 2, 28)) == "févr. 2027"
