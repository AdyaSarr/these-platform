"""Abbreviated dates in French: « 18 nov. », never « 18 Nov ».

Django's format "M" title-cases a three-letter month (« Nov », « Déc ») and "b"
gives « nov » without its full stop; "N" is the French abbreviation (« janv. »,
« sept. », « nov. »; apps/core/month_abbreviations.py completes January and
February). strftime's %b / %B follow the locale of the process, never the
platform's: banned too. Abbreviated day and month: the |short_date filter of
apps/ui (a no-break space between them)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from django.template import Context, Template
from django.utils import translation

BASE_DIR = Path(__file__).resolve().parent.parent

# A format string of the |date and |time filters, or of formats.date_format().
TEMPLATE_FORMAT = re.compile(r"""\|(?:date|time):(["'])(.*?)\1""")
PYTHON_FORMAT = re.compile(r"""(?:date_format|time_format)\([^,()]+,\s*(["'])(.*?)\1""")
STRFTIME_MONTH = re.compile(r"""strftime\(\s*(["'])[^"']*%[bBh]""")
BANNED = set("Mb")
NAMED = re.compile(r"[A-Z_]+_FORMAT")  # resolved by the locale: checked below


def _sources() -> list[Path]:
    paths = list((BASE_DIR / "templates").rglob("*.html"))
    paths += list((BASE_DIR / "apps").glob("*/templates/**/*.html"))
    paths += [p for p in (BASE_DIR / "apps").rglob("*.py") if "/tests/" not in str(p)]
    return sorted(paths)


def _unescaped(fmt: str) -> set[str]:
    """The format characters of a Django date format (a backslash escapes one)."""
    return set(re.sub(r"\\.", "", fmt))


def _offences(source: str) -> list[str]:
    found = [
        fmt
        for regex in (TEMPLATE_FORMAT, PYTHON_FORMAT)
        for _quote, fmt in regex.findall(source)
        if not NAMED.fullmatch(fmt) and _unescaped(fmt) & BANNED
    ]
    return found + [m.group(0) for m in STRFTIME_MONTH.finditer(source)]


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(BASE_DIR)))
def test_no_english_month_abbreviation(path: Path) -> None:
    assert _offences(path.read_text(encoding="utf-8")) == []


def test_the_scan_sees_what_it_must() -> None:
    assert _offences('{{ d|date:"j M" }}') == ["j M"]
    assert _offences("{{ d|date:'d b Y' }}") == ["d b Y"]
    assert _offences('formats.date_format(day, "M Y")') == ["M Y"]
    assert _offences('x.strftime("%d %b")') == ['strftime("%d %b']
    fine = '{{ d|date:"j N" }} {{ d|date:"\\M j" }} {{ d|date:"SHORT_DATE_FORMAT" }}'
    assert _offences(fine) == []


@pytest.mark.parametrize(
    "name", ["DATE_FORMAT", "DATETIME_FORMAT", "SHORT_DATE_FORMAT", "SHORT_DATETIME_FORMAT",
             "MONTH_DAY_FORMAT", "YEAR_MONTH_FORMAT"],
)  # fmt: skip
def test_french_named_formats_have_no_english_abbreviation(name: str) -> None:
    from django.utils import formats

    with translation.override("fr"):
        assert not _unescaped(formats.get_format(name)) & BANNED, name


def _short(value: object) -> str:
    with translation.override("fr"):
        return Template("{% load ui %}{{ value|short_date }}").render(Context({"value": value}))


@pytest.mark.parametrize(
    ("month", "expected"),
    [(1, "janv."), (2, "févr."), (3, "mars"), (6, "juin"), (7, "juil."), (9, "sept."),
     (11, "nov."), (12, "déc.")],
)  # fmt: skip
def test_short_date_is_the_french_abbreviation(month: int, expected: str) -> None:
    assert _short(dt.date(2026, month, 18)) == f"18\u00a0{expected}"


def test_short_date_of_an_instant_is_its_paris_day() -> None:
    # 23:30 UTC on 30 November = 00:30 on 1 December in Paris (TIME_ZONE).
    instant = dt.datetime(2026, 11, 30, 23, 30, tzinfo=dt.UTC)
    assert _short(instant) == "1\u00a0déc."


def test_short_date_of_nothing_is_empty() -> None:
    assert _short(None) == ""


def test_the_next_meeting_tile_uses_short_date() -> None:
    source = (BASE_DIR / "templates" / "accounts" / "dashboard.html").read_text(encoding="utf-8")
    assert "meeting.starts_at|short_date" in source
