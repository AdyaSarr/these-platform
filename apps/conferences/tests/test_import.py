"""Strict local JSON import of the conference catalogue (rule 11: external data is
untrusted): size, encoding, duplicate keys, NaN and floats, unknown keys, types,
dates, zones and URLs refused before anything is written; dry run by default; an
existing row is never modified; no network at all; counts only on the output."""

from __future__ import annotations

import datetime as dt
import json
import socket
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.conferences import importer
from apps.conferences.models import Deadline, Edition, Venue
from apps.conferences.tests.helpers import NOW, deadline_of, edition_of, make_team, venue_of
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the import must never open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _catalogue(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format": 1,
        "venues": [
            {
                "kind": "CONFERENCE",
                "acronym": "SFCA",
                "name": "Symposium fictif sur les canaux auxiliaires",
                "url": "https://sfca.example.org/",
                "editions": [
                    {
                        "year": 2027,
                        "location": "Ville fictive",
                        "event_start": "2027-09-06",
                        "event_end": "2027-09-09",
                        "deadlines": [
                            {
                                "kind": "ABSTRACT",
                                "date": "2027-02-22",
                                "time": "23:59",
                                "tz": "AoE",
                            },
                            {"kind": "PAPER", "date": "2027-03-01", "time": "23:59", "tz": "AoE"},
                        ],
                    }
                ],
            },
            {"kind": "JOURNAL", "acronym": "RFSM", "name": "Revue fictive de sécurité matérielle"},
        ],
    }
    data.update(overrides)
    return data


def _file(tmp_path: Path, content: Any, name: str = "catalogue.json") -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


def _run(team: SimpleNamespace, path: Path, *extra: str) -> str:
    out = StringIO()
    call_command(
        "conferences_import", str(path), "--thesis", str(team.thesis.pk),
        "--as", team.student.email, *extra, stdout=out,
    )  # fmt: skip
    return out.getvalue()


# --------------------------------------------------------------------------- success


def test_dry_run_by_default_writes_nothing(team: SimpleNamespace, tmp_path: Path) -> None:
    before = AuditEvent.objects.count()
    out = _run(team, _file(tmp_path, _catalogue()))
    assert out.strip() == (
        "dry run: 2 venue(s), 1 edition(s), 2 deadline(s) to create, 0 conflict(s); "
        "nothing written (--apply to write)"
    )
    assert not Venue.objects.exists() and AuditEvent.objects.count() == before


def test_apply_creates_through_the_services(team: SimpleNamespace, tmp_path: Path) -> None:
    out = _run(team, _file(tmp_path, _catalogue()), "--apply")
    assert out.strip().startswith("applied: 2 venue(s), 1 edition(s), 2 deadline(s)")
    assert Venue.objects.filter(thesis=team.thesis).count() == 2
    paper = Deadline.objects.get(kind="PAPER")
    assert (paper.due_at, paper.tz_name) == (dt.datetime(2027, 3, 2, 11, 59, tzinfo=dt.UTC), "AoE")
    assert AuditEvent.objects.filter(action="DEADLINE_ADDED").count() == 2
    (imported,) = [e.details_json for e in AuditEvent.objects.filter(action="CONFERENCES_IMPORTED")]
    assert imported == {"venues": 2, "editions": 1, "deadlines": 2, "conflicts": 0}
    assert all(
        e.actor_id == team.student.pk for e in AuditEvent.objects.filter(action="DEADLINE_ADDED")
    )


def test_an_existing_row_is_never_modified(team: SimpleNamespace, tmp_path: Path) -> None:
    venue = venue_of(team, name="Nom saisi à la main", url="")
    edition = edition_of(team, venue=venue, location="Lieu saisi à la main")
    deadline = deadline_of(team, edition)  # the same PAPER deadline
    out = _run(team, _file(tmp_path, _catalogue()), "--apply")
    venue.refresh_from_db()
    edition.refresh_from_db()
    assert (venue.name, venue.url, edition.location) == (
        "Nom saisi à la main",
        "",
        "Lieu saisi à la main",
    )
    assert Deadline.objects.filter(edition=edition).count() == 2  # only the abstract added
    assert Deadline.objects.get(pk=deadline.pk).due_at == deadline.due_at
    assert "1 venue(s), 0 edition(s), 1 deadline(s)" in out and "2 conflict(s)" in out


def test_running_twice_creates_nothing_the_second_time(
    team: SimpleNamespace, tmp_path: Path
) -> None:
    path = _file(tmp_path, _catalogue())
    _run(team, path, "--apply")
    out = _run(team, path, "--apply")
    assert out.strip().startswith("applied: 0 venue(s), 0 edition(s), 0 deadline(s)")
    assert Edition.objects.count() == 1 and Deadline.objects.count() == 2


# --------------------------------------------------------------------------- refused


def _refused(team: SimpleNamespace, tmp_path: Path, content: Any, fragment: str) -> None:
    with pytest.raises(CommandError) as exc:
        _run(team, _file(tmp_path, content), "--apply")
    assert fragment in str(exc.value)
    assert not Venue.objects.exists() and not Deadline.objects.exists()


def test_too_big(team: SimpleNamespace, tmp_path: Path) -> None:
    _refused(team, tmp_path, " " * (importer.MAX_BYTES + 1), "too large")


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (b"\xff\xfe{}", "UTF-8"),
        ("{not json", "not valid JSON"),
        ('{"format": 1, "format": 1, "venues": []}', "duplicate key"),
        ('{"format": 1, "venues": [], "x": NaN}', "NaN"),
        ('{"format": 1.0, "venues": []}', "float"),
        ("[1, 2]", "an object"),
    ],
)
def test_malformed_files(
    team: SimpleNamespace, tmp_path: Path, content: Any, fragment: str
) -> None:
    _refused(team, tmp_path, content, fragment)


def _venue(**overrides: Any) -> dict[str, Any]:
    venue = dict(_catalogue()["venues"][0])
    venue.update(overrides)
    return venue


@pytest.mark.parametrize(
    ("venue", "fragment"),
    [
        (_venue(color="red"), "venues[0]: unknown key 'color'"),
        (_venue(kind="PARTY"), "venues[0].kind"),
        (_venue(acronym=12), "venues[0].acronym"),
        (_venue(url="http://sfca.example.org/"), "venues[0].url"),
        (_venue(url="javascript:alert(1)"), "venues[0].url"),
        (_venue(editions=[{"year": "2027"}]), "venues[0].editions[0].year"),
        (_venue(editions=[{"year": 2027, "event_start": "6/9/2027"}]), "event_start"),
        (
            _venue(
                editions=[
                    {
                        "year": 2027,
                        "deadlines": [
                            {
                                "kind": "PAPER",
                                "date": "2027-03-01",
                                "time": "23:59",
                                "tz": "Etc/GMT+12",
                            }
                        ],
                    }
                ]
            ),
            "deadlines[0].tz",
        ),
        (
            _venue(
                editions=[
                    {
                        "year": 2027,
                        "deadlines": [
                            {
                                "kind": "PAPER",
                                "date": "2027-03-14",
                                "time": "02:30",
                                "tz": "America/Los_Angeles",
                            }
                        ],
                    }
                ]
            ),
            "deadlines[0]",
        ),
        (
            _venue(
                editions=[{"year": 2027, "deadlines": [{"kind": "PAPER", "date": "2027-03-01"}]}]
            ),
            "deadlines[0]: missing key 'time'",
        ),
    ],
)
def test_strict_schema(team: SimpleNamespace, tmp_path: Path, venue: Any, fragment: str) -> None:
    _refused(team, tmp_path, _catalogue(venues=[venue]), fragment)


def test_a_journal_has_no_edition(team: SimpleNamespace, tmp_path: Path) -> None:
    journal = {
        "kind": "JOURNAL",
        "acronym": "RFSM",
        "name": "Revue fictive",
        "editions": [{"year": 2027}],
    }
    _refused(team, tmp_path, _catalogue(venues=[journal]), "venues[0].editions")


def test_counts_are_bounded(team: SimpleNamespace, tmp_path: Path) -> None:
    many = [{"kind": "WORKSHOP", "acronym": f"W{i}", "name": "Atelier fictif"} for i in range(101)]
    _refused(team, tmp_path, _catalogue(venues=many), "at most 100")


def test_the_format_and_the_top_level_keys(team: SimpleNamespace, tmp_path: Path) -> None:
    _refused(team, tmp_path, {"format": 2, "venues": []}, "format")
    _refused(team, tmp_path, {"venues": []}, "missing key 'format'")


def test_the_actor_is_the_student_of_the_thesis(team: SimpleNamespace, tmp_path: Path) -> None:
    path = _file(tmp_path, _catalogue())
    with pytest.raises(CommandError):
        call_command(
            "conferences_import", str(path), "--thesis", str(team.thesis.pk),
            "--as", team.director.email, "--apply", stdout=StringIO(),
        )  # fmt: skip
    assert not Venue.objects.exists()


def test_errors_never_echo_values(team: SimpleNamespace, tmp_path: Path) -> None:
    hidden_value = "VALEUR-A-NE-PAS-AFFICHER"
    with pytest.raises(CommandError) as exc:
        _run(team, _file(tmp_path, _catalogue(venues=[_venue(kind=hidden_value)])))
    assert hidden_value not in str(exc.value)
