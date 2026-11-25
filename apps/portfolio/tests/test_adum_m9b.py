"""The ADUM summary extended (M9b): publications, communications, teaching and
outreach, with the rules of M9a (PUBLIC only by default, a box for the internal
ones marked « à vérifier », second factor, journal in numbers, CSV neutralized);
PUBLISHED only by default, a box for the accepted ones marked « à paraître »
(Q4). The BibTeX export of the published submissions: the student only, strict
escaping, tested with hostile fields."""

from __future__ import annotations

import csv
import io
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.portfolio import services
from apps.portfolio.tests import helpers as h
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _csv(team: SimpleNamespace, section: str, **kw: Any) -> list[list[str]]:
    options: dict[str, Any] = {"include_internal": False, "include_accepted": False}
    options.update(kw)
    _name, content = services.adum_csv(thesis=team.thesis, user=team.student, year=1,
                                       recent_mfa=True, section=section, **options)  # fmt: skip
    text = content.decode("utf-8").removeprefix("\ufeff")
    assert "\r\n" in text
    return list(csv.reader(io.StringIO(text), delimiter=";"))


def _venue(team: SimpleNamespace, kind: str, acronym: str) -> Any:
    from apps.conferences.tests.helpers import venue_of

    return venue_of(team, kind=kind, acronym=acronym, name=f"Lieu fictif {acronym}")


# --------------------------------------------------------------------------- sections


def test_publications_and_communications_by_category(team: SimpleNamespace) -> None:
    article = h.publication(team, title="Article de revue fictif",
                            venue=_venue(team, "JOURNAL", "RFSM"))  # fmt: skip
    h.details(team, article, adum_category="JOURNAL_ARTICLE", volume="3", pages="10-20")
    poster = h.publication(team, title="Poster fictif", presentation_kind="POSTER")
    talk = h.publication(team, title="Exposé fictif", venue=_venue(team, "SEMINAR", "SEMF"),
                         presentation_kind="TALK")  # fmt: skip
    h.details(team, talk, adum_category="SEMINAR", presented_on=h.D(2027, 1, 14))
    summary = services.adum_summary(team.student, team.thesis, year=1)
    assert [r.title for r in summary.publications] == ["Article de revue fictif"]
    assert {r.title for r in summary.communications} == {"Poster fictif", "Exposé fictif"}
    # Without details, the category is the suggested one (the poster).
    (shown,) = [r for r in summary.communications if r.submission.pk == poster.pk]
    assert shown.category == "POSTER"
    assert summary.publications[0].texts["pages"] == "10-20"


def test_published_only_by_default_accepted_on_request(team: SimpleNamespace) -> None:
    h.publication(team, title="Publié fictif")
    h.publication(team, title="Accepté fictif", status="ACCEPTED")
    h.publication(team, title="Soumis fictif", status="SUBMITTED")
    summary = services.adum_summary(team.student, team.thesis, year=1)
    assert [r.title for r in summary.publications + summary.communications] == ["Publié fictif"]
    assert summary.accepted_left_out == 1
    full = services.adum_summary(team.student, team.thesis, year=1, include_accepted=True)
    titles = {r.title: r.accepted for r in full.publications + full.communications}
    assert titles == {"Publié fictif": False, "Accepté fictif": True}
    body = h.client_for(team.student).get(h.url(team, "adum") + "?accepted=1").content.decode()
    assert "à paraître" in body


def test_internal_left_out_by_default_and_marked(team: SimpleNamespace) -> None:
    h.publication(team, title="Public fictif")
    h.publication(team, L.INTERNAL_ST, title="TEMOIN-FUITE-ARTICLE-ST")
    h.teaching(team, L.INTERNAL_LIRMM, course="TEMOIN-FUITE-COURS-LIRMM")
    h.outreach(team, L.CONFIDENTIAL, title="TEMOIN-FUITE-ACTION-CONF")
    summary = services.adum_summary(team.student, team.thesis, year=1)
    texts = [r.title for r in summary.publications + summary.communications]
    texts += [r.texts["course"] for r in summary.teachings]
    texts += [r.texts["title"] for r in summary.outreach]
    assert texts == ["Public fictif"] and summary.left_out_other == 3
    body = h.client_for(team.student).get(h.url(team, "adum")).content.decode()
    assert "TEMOIN-FUITE" not in body
    full = services.adum_summary(team.student, team.thesis, year=1, include_internal=True)
    assert len(full.publications + full.communications) == 2 and full.left_out_other == 0
    marked = h.client_for(team.student).get(h.url(team, "adum") + "?internal=1").content.decode()
    assert marked.count("niveau interne : à vérifier avant saisie") == 3


def test_teaching_and_outreach_of_the_year(team: SimpleNamespace) -> None:
    h.teaching(team, course="Cours de l'année fictif", hours="12,5")
    h.teaching(team, course="Cours ancien fictif", start=h.D(2026, 8, 3))
    h.teaching(team, course="Cours à venir fictif", start=h.D(2027, 3, 1))
    h.outreach(team, title="Action de l'année fictive")
    h.outreach(team, title="Action à venir fictive", date=h.D(2027, 3, 1))
    summary = services.adum_summary(team.student, team.thesis, year=1)
    assert [r.texts["course"] for r in summary.teachings] == ["Cours de l'année fictif"]
    assert [r.texts["title"] for r in summary.outreach] == ["Action de l'année fictive"]
    rows = _csv(team, "teaching")
    assert rows[1][3] == "12,5"


@pytest.mark.parametrize("section", ["publications", "communications", "teaching", "outreach"])
def test_the_csv_neutralizes_formulas(team: SimpleNamespace, section: str) -> None:
    formula = '=HYPERLINK("https://example.org")'
    if section in ("publications", "communications"):
        form = "PAPER" if section == "publications" else "POSTER"
        h.publication(team, title=formula, presentation_kind=form,
                      external_authors="+Auteur fictif")  # fmt: skip
    elif section == "teaching":
        h.teaching(team, course=formula, institution="-Université fictive")
    else:
        h.outreach(team, title=formula, audience="@public fictif")
    rows = _csv(team, section)
    cells = [cell for row in rows[1:] for cell in row]
    assert cells and not any(c.startswith(("=", "+", "-", "@")) for c in cells), cells
    assert f"'{formula}" in cells


def test_the_csv_needs_a_second_factor_and_journals_numbers(team: SimpleNamespace) -> None:
    h.publication(team, title="TEMOIN-FUITE-TITRE-CSV")
    with pytest.raises(ReauthenticationRequiredError):
        services.adum_csv(thesis=team.thesis, user=team.student, year=1, include_internal=False,
                          recent_mfa=False, section="publications")  # fmt: skip
    _csv(team, "publications", include_accepted=True)
    event = AuditEvent.objects.filter(action="PORTFOLIO_EXPORTED").get()
    assert event.details_json == {"year": 1, "rows": 1, "internal": False, "accepted": True,
                                  "section": "publications"}  # fmt: skip
    with pytest.raises(ValidationError):
        _csv(team, "skills")


def test_the_csv_of_the_trainings_is_unchanged(team: SimpleNamespace) -> None:
    h.training(team, title="Atelier fictif")
    rows = _csv(team, "trainings")
    assert rows[0][0] == "Intitulé" and rows[1][0] == "Atelier fictif"


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_the_summary_and_the_exports_are_the_students(team: SimpleNamespace, attr: str) -> None:
    user = getattr(team, attr)
    with pytest.raises(PermissionDenied):
        services.adum_summary(user, team.thesis, year=1)
    with pytest.raises(PermissionDenied):
        services.publications_bibtex(thesis=team.thesis, user=user, include_internal=False,
                                     include_accepted=False, recent_mfa=True)  # fmt: skip
    assert h.client_for(user).post(h.url(team, "adum_bibtex")).status_code == 403


# --------------------------------------------------------------------------- BibTeX


HOSTILE = (
    "}, author = {Pirate",
    "\\input{/etc/passwd}",
    "\\write18{rm -rf /}",
    "@comment{",
    "50 % & $x^2$ #1 ~a_b",
    "texte\u202e inversé\u200b",
    "{" * 40 + "}" * 3,
    "x" * 290,
)


def _fields(entry: str) -> list[str]:
    return re.findall(r"^  [a-z]+ = \{(.*)\},?$", entry, flags=re.M)


def _balanced(value: str) -> bool:
    plain = re.sub(r"\\textbackslash\{\}|\\textasciitilde\{\}|\\textasciicircum\{\}|\\[{}$&%#_]",
                   "", value)  # fmt: skip
    return "{" not in plain and "}" not in plain and "\\" not in plain


def test_bibtex_escapes_hostile_fields(team: SimpleNamespace) -> None:
    for n, text in enumerate(HOSTILE):
        submission = h.publication(team, title=text, external_authors=text,
                                   venue=_venue(team, "JOURNAL", f"RF{n}"))  # fmt: skip
        h.details(team, submission, adum_category="JOURNAL_ARTICLE", volume="1", pages="1-2")
    name, content = services.publications_bibtex(
        thesis=team.thesis, user=team.student, include_internal=False, include_accepted=False,
        recent_mfa=True,
    )  # fmt: skip
    assert name == "publications.bib"
    text = content.decode("utf-8")
    entries = re.split(r"\n(?=@)", text.strip())
    assert len(entries) == len(HOSTILE)
    assert all(re.match(r"^@article\{[A-Za-z0-9:_-]+,\n", e) for e in entries), entries[0]
    for entry in entries:
        values = _fields(entry)
        assert values and all(_balanced(v) for v in values), entry
        assert entry.endswith("\n}")
    assert "\\input" not in text and "\\write18" not in text
    assert "\u202e" not in text and "\u200b" not in text
    assert sum(1 for line in text.splitlines() if line.startswith("@")) == len(HOSTILE)


def test_bibtex_keys_types_and_options(team: SimpleNamespace) -> None:
    article = h.publication(team, title="Article fictif", venue=_venue(team, "JOURNAL", "RFSM"))
    h.details(team, article, adum_category="JOURNAL_ARTICLE", bibtex_key="fictif:2027",
              doi="10.12345/fictif", hal_id="hal-01234567")  # fmt: skip
    paper = h.publication(team, title="Acte fictif")
    h.details(team, paper, adum_category="CONFERENCE_PAPER", bibtex_key="fictif:2027")
    h.publication(team, title="Poster fictif", presentation_kind="POSTER")
    h.publication(team, L.INTERNAL_LIRMM, title="TEMOIN-FUITE-BIB-INTERNE")
    h.publication(team, title="TEMOIN-FUITE-BIB-ACCEPTE", status="ACCEPTED")
    _name, content = services.publications_bibtex(
        thesis=team.thesis, user=team.student, include_internal=False, include_accepted=False,
        recent_mfa=True,
    )  # fmt: skip
    text = content.decode()
    assert "@article{fictif:2027," in text and "@inproceedings{fictif:2027-2," in text
    assert "@misc{" in text and "doi = {10.12345/fictif}" in text
    assert "TEMOIN-FUITE" not in text
    _name, everything = services.publications_bibtex(
        thesis=team.thesis, user=team.student, include_internal=True, include_accepted=True,
        recent_mfa=True,
    )  # fmt: skip
    assert b"TEMOIN-FUITE-BIB-INTERNE" in everything and b"TEMOIN-FUITE-BIB-ACCEPTE" in everything
    events = AuditEvent.objects.filter(action="PORTFOLIO_EXPORTED").order_by("seq")
    assert [e.details_json for e in events] == [
        {"section": "bibtex", "rows": 3, "internal": False, "accepted": False},
        {"section": "bibtex", "rows": 5, "internal": True, "accepted": True},
    ]


def test_bibtex_needs_a_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        services.publications_bibtex(thesis=team.thesis, user=team.student,
                                     include_internal=False, include_accepted=False,
                                     recent_mfa=False)  # fmt: skip
    assert not AuditEvent.objects.filter(action="PORTFOLIO_EXPORTED").exists()


def test_the_bibtex_view(team: SimpleNamespace) -> None:
    h.publication(team, title="Article fictif")
    page = h.url(team, "adum")
    stale = h.browser_post(h.browser_for(team.student), page, h.url(team, "adum_bibtex"), {})
    assert stale.status_code == 302 and "reauthenticate" in stale["Location"]
    response = h.browser_post(h.mfa_browser(team.student), page, h.url(team, "adum_bibtex"), {})
    assert response.status_code == 200
    assert response["Content-Type"] == "application/x-bibtex; charset=utf-8"
    assert response["Content-Disposition"] == 'attachment; filename="publications.bib"'
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert b"Article fictif" in response.content
