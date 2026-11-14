"""Strict import of a local JSON catalogue of conferences (CLAUDE.md rule 11).

The file is untrusted: at most MAX_BYTES, UTF-8 without BOM, JSON without
duplicate keys, NaN, Infinity or floats; a fixed schema checked field by field
(known keys only, exact types, ISO dates, HH:MM times, a known zone, https URLs),
bounded counts. Everything is validated before anything is written; then the
catalogue services create what is missing, inside one transaction, and never
modify an existing row (a difference is counted as a conflict, left untouched).
No network: the file is read from the local disk only. Messages name the JSON
path, never a value of the file.

Schema (format 1):
    {"format": 1, "venues": [
        {"kind": "CONFERENCE", "acronym": "...", "name": "...", "url": "https://...",
         "editions": [{"year": 2027, "location": "...", "event_start": "2027-09-06",
                       "event_end": "2027-09-09",
                       "deadlines": [{"kind": "PAPER", "date": "2027-03-01",
                                      "time": "23:59", "tz": "AoE"}]}]}]}
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.models import Thesis, User
from apps.core import timezones
from apps.vault.audit import AuditAction, audit

from . import services
from .models import Deadline, DeadlineKind, Edition, Venue, VenueKind

MAX_BYTES = 256 * 1024
MAX_VENUES, MAX_EDITIONS, MAX_DEADLINES = 100, 20, 20
FORMAT = 1
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"\d{2}:\d{2}")
_KEY = re.compile(r"[a-z_]{1,32}")


class ImportFileError(Exception):
    """The file is refused; the message names a JSON path, never a value."""


@dataclass(frozen=True)
class DeadlineSpec:
    kind: str
    day: dt.date
    wall: dt.time
    tz: str
    due_at: dt.datetime


@dataclass(frozen=True)
class EditionSpec:
    year: int
    location: str
    event_start: dt.date | None
    event_end: dt.date | None
    deadlines: list[DeadlineSpec] = field(default_factory=list)


@dataclass(frozen=True)
class VenueSpec:
    kind: str
    acronym: str
    name: str
    url: str
    editions: list[EditionSpec] = field(default_factory=list)


@dataclass
class Counts:
    venues: int = 0
    editions: int = 0
    deadlines: int = 0
    conflicts: int = 0


# --------------------------------------------------------------------------- reading


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImportFileError("duplicate key in an object")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ImportFileError("NaN and Infinity are refused")


def _no_float(text: str) -> Any:
    raise ImportFileError("a float is refused: integers only")


def read(path: Path) -> list[VenueSpec]:
    """Read, decode, parse and validate the file; nothing touches the database."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        raise ImportFileError("the file cannot be read") from None
    if len(data) > MAX_BYTES:
        raise ImportFileError(f"the file is too large (at most {MAX_BYTES} bytes)")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ImportFileError("the file must be UTF-8 without BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ImportFileError("the file is not UTF-8") from None
    try:
        document = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constant,
            parse_float=_no_float,
        )
    except json.JSONDecodeError as exc:
        raise ImportFileError(f"not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    return parse(document)


# --------------------------------------------------------------------------- schema


def _object(value: Any, path: str, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImportFileError(f"{path or 'the file'} must be an object")
    for key in value:
        if key not in required | optional:
            shown = f" '{key}'" if isinstance(key, str) and _KEY.fullmatch(key) else ""
            raise ImportFileError(f"{path}: unknown key{shown}")
    for key in sorted(required):
        if key not in value:
            raise ImportFileError(f"{path}: missing key '{key}'")
    return value


def _list(value: Any, path: str, limit: int, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise ImportFileError(f"{path}: expected a list")
    if len(value) > limit:
        raise ImportFileError(f"{path}: at most {limit} {what}")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ImportFileError(f"{path}: expected a string")
    return value


def _choice(value: Any, path: str, choices: list[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ImportFileError(f"{path}: expected one of {', '.join(choices)}")
    return value


def _date(value: Any, path: str) -> dt.date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ImportFileError(f"{path}: expected a date YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ImportFileError(f"{path}: expected a date YYYY-MM-DD") from None


def _time(value: Any, path: str) -> dt.time:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ImportFileError(f"{path}: expected a time HH:MM")
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        raise ImportFileError(f"{path}: expected a time HH:MM") from None


def _clean(cleaner: Any, value: str, path: str) -> str:
    try:
        return str(cleaner(value))
    except ValidationError:
        raise ImportFileError(f"{path}: refused (length, characters or https)") from None


def _deadline(value: Any, path: str) -> DeadlineSpec:
    obj = _object(value, path, {"kind", "date", "time", "tz"}, set())
    kind = _choice(obj["kind"], f"{path}.kind", DeadlineKind.values)
    day = _date(obj["date"], f"{path}.date")
    if day is None:
        raise ImportFileError(f"{path}.date: expected a date YYYY-MM-DD")
    wall = _time(obj["time"], f"{path}.time")
    tz = _string(obj["tz"], f"{path}.tz")
    try:
        timezones.zone(tz)
    except ValidationError:
        raise ImportFileError(f"{path}.tz: unknown time zone (AoE, UTC or IANA)") from None
    try:
        due_at = timezones.local_to_utc(day, wall, tz)
    except ValidationError as exc:
        raise ImportFileError(f"{path}: {' '.join(exc.messages)}") from None
    return DeadlineSpec(kind, day, wall, tz, due_at)


def _edition(value: Any, path: str) -> EditionSpec:
    obj = _object(value, path, {"year"}, {"location", "event_start", "event_end", "deadlines"})
    year = obj["year"]
    if type(year) is not int or not 2000 <= year <= 2100:
        raise ImportFileError(f"{path}.year: expected an integer between 2000 and 2100")
    location = _clean(
        lambda v: services._clean_line(v, "location"),
        _string(obj.get("location", ""), f"{path}.location"),
        f"{path}.location",
    )
    start = _date(obj.get("event_start"), f"{path}.event_start")
    end = _date(obj.get("event_end"), f"{path}.event_end")
    if start is not None and end is not None and start > end:
        raise ImportFileError(f"{path}: the event ends before it starts")
    deadlines = [
        _deadline(item, f"{path}.deadlines[{i}]")
        for i, item in enumerate(
            _list(obj.get("deadlines", []), f"{path}.deadlines", MAX_DEADLINES, "deadlines")
        )
    ]
    return EditionSpec(year, location, start, end, deadlines)


def _venue(value: Any, path: str) -> VenueSpec:
    obj = _object(value, path, {"kind", "acronym", "name"}, {"url", "editions"})
    kind = _choice(obj["kind"], f"{path}.kind", VenueKind.values)
    acronym = _clean(
        lambda v: services._clean_line(v, "acronym"),
        _string(obj["acronym"], f"{path}.acronym"),
        f"{path}.acronym",
    )
    name = _clean(
        lambda v: services._clean_line(v, "name"),
        _string(obj["name"], f"{path}.name"),
        f"{path}.name",
    )
    url = _clean(services._clean_url, _string(obj.get("url", ""), f"{path}.url"), f"{path}.url")
    raw = _list(obj.get("editions", []), f"{path}.editions", MAX_EDITIONS, "editions")
    if kind == VenueKind.JOURNAL and raw:
        raise ImportFileError(f"{path}.editions: a journal has no edition")
    editions = [_edition(item, f"{path}.editions[{i}]") for i, item in enumerate(raw)]
    years = [e.year for e in editions]
    if len(years) != len(set(years)):
        raise ImportFileError(f"{path}.editions: an edition is listed twice")
    return VenueSpec(kind, acronym, name, url, editions)


def parse(document: Any) -> list[VenueSpec]:
    top = _object(document, "", {"format", "venues"}, set())
    if type(top["format"]) is not int or top["format"] != FORMAT:
        raise ImportFileError(f"format: only format {FORMAT} is known")
    venues = [
        _venue(item, f"venues[{i}]")
        for i, item in enumerate(_list(top["venues"], "venues", MAX_VENUES, "venues"))
    ]
    keys = [(v.kind, v.acronym) for v in venues]
    if len(keys) != len(set(keys)):
        raise ImportFileError("venues: a venue is listed twice")
    return venues


# --------------------------------------------------------------------------- writing


def _plan_or_apply(venues: list[VenueSpec], *, thesis: Thesis, user: User, apply: bool) -> Counts:
    counts = Counts()
    for spec in venues:
        venue = Venue.objects.filter(thesis=thesis, kind=spec.kind, acronym=spec.acronym).first()
        if venue is None:
            counts.venues += 1
            if apply:
                venue = services.create_venue(
                    thesis=thesis, user=user, kind=spec.kind, acronym=spec.acronym,
                    name=spec.name, url=spec.url,
                )  # fmt: skip
        elif (venue.name, venue.url) != (spec.name, spec.url):
            counts.conflicts += 1  # left untouched
        for edition_spec in spec.editions:
            edition = (
                Edition.objects.filter(venue=venue, year=edition_spec.year).first()
                if venue is not None
                else None
            )
            if edition is None:
                counts.editions += 1
                if apply and venue is not None:  # created above when apply
                    edition = services.create_edition(
                        venue=venue, user=user, year=edition_spec.year,
                        location=edition_spec.location, event_start=edition_spec.event_start,
                        event_end=edition_spec.event_end,
                    )  # fmt: skip
            elif (edition.location, edition.event_start, edition.event_end) != (
                edition_spec.location,
                edition_spec.event_start,
                edition_spec.event_end,
            ):
                counts.conflicts += 1  # left untouched
            for d in edition_spec.deadlines:
                exists = (
                    edition is not None
                    and Deadline.objects.filter(
                        edition=edition, kind=d.kind, due_at=d.due_at, cancelled_at__isnull=True
                    ).exists()
                )
                if exists:
                    continue
                counts.deadlines += 1
                if apply and edition is not None:  # created above when apply
                    services.add_deadline(
                        edition=edition, user=user, kind=d.kind, day=d.day, wall=d.wall,
                        tz_name=d.tz,
                    )  # fmt: skip
    return counts


def run(path: Path, *, thesis: Thesis, user: User, apply: bool) -> Counts:
    """Validate everything, then (with ``apply``) write in one transaction."""
    venues = read(path)
    if not apply:
        return _plan_or_apply(venues, thesis=thesis, user=user, apply=False)
    with transaction.atomic():
        counts = _plan_or_apply(venues, thesis=thesis, user=user, apply=True)
        audit(
            AuditAction.CONFERENCES_IMPORTED,
            actor=user,
            thesis=thesis,
            venues=counts.venues,
            editions=counts.editions,
            deadlines=counts.deadlines,
            conflicts=counts.conflicts,
        )
    return counts
