"""Views of the conferences (/t/<thesis_id>/conferences/): rights per role, POSTs like
a browser, deadlines shown in Paris with their zone, the ST approval (403 before the
second factor), restricted content, external links, card and objective fragment."""

from __future__ import annotations

import datetime as dt
import re
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.conferences import services, views
from apps.conferences.models import Deadline, Preprint, StApproval, Submission, Venue
from apps.conferences.tests.helpers import (
    NOW,
    D,
    L,
    article_version,
    deadline_of,
    edition_of,
    granted,
    marker,
    submission_of,
    venue_of,
)
from apps.objectives.tests.helpers import objective_of

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
SITE_ORIGIN = "http://testserver"
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest", "outsider")
READERS = ("student", "director", "co_supervisor", "tutor")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(password=PASSWORD),
    )


def _client(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    client = Client()
    if mfa:
        mfa_login(client, getattr(team, role), PASSWORD)
    else:
        client.force_login(getattr(team, role))
    return client


def _browser(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    signed = _client(team, role, mfa=mfa)
    client = Client(enforce_csrf_checks=True)
    client.cookies = signed.cookies
    return client


def _browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    page = client.get(page_url)
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    assert match, "no CSRF token in the page"
    return client.post(
        url, {**data, "csrfmiddlewaretoken": match.group(1)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def _url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"conferences:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def _s(team: SimpleNamespace, submission: Submission, name: str, **kw: Any) -> str:
    return _url(team, name, submission_id=submission.pk, **kw)


def _title_tag(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    return match.group(1) if match else ""


# =========================================================================== rights


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("page", ["deadlines", "submissions", "policy", "new_venue"])
def test_pages_per_role(team: SimpleNamespace, role: str, page: str) -> None:
    response = _client(team, role).get(_url(team, page))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_new_submission_page_is_the_students(team: SimpleNamespace, role: str) -> None:
    response = _client(team, role).get(_url(team, "new_submission"))
    assert response.status_code == (200 if role == "student" else 403)


@pytest.mark.parametrize("role", ROLES)
def test_submission_page_per_role(team: SimpleNamespace, role: str) -> None:
    submission = submission_of(team)
    response = _client(team, role).get(_s(team, submission, "submission"))
    assert response.status_code == (200 if role in READERS else 403)


def test_a_submission_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    from apps.conferences.tests.helpers import make_team

    other = make_team()
    theirs = submission_of(other)
    response = _client(team, "student").get(_s(team, theirs, "submission"))
    assert response.status_code == 403


# =========================================================================== catalogue


def test_venue_edition_deadline_posted_like_a_browser(team: SimpleNamespace) -> None:
    client = _browser(team, "director")
    response = _browser_post(
        client,
        _url(team, "new_venue"),
        _url(team, "new_venue"),
        {"kind": "CONFERENCE", "acronym": "SFCA", "name": "Symposium fictif", "url": ""},
    )
    assert response.status_code == 302, response.content[:400]
    venue = Venue.objects.get()
    page = _url(team, "venue", venue_id=venue.pk)
    response = _browser_post(
        client, page, _url(team, "new_edition", venue_id=venue.pk), {"year": "2027"}
    )
    assert response.status_code == 302, response.content[:400]
    edition = venue.editions.get()
    page = _url(team, "edition", edition_id=edition.pk)
    response = _browser_post(
        client,
        page,
        page,
        {"kind": "PAPER", "day": "2027-03-01", "wall": "23:59", "tz_name": "AoE"},
    )
    assert response.status_code == 302, response.content[:400]
    assert Deadline.objects.get().due_at == dt.datetime(2027, 3, 2, 11, 59, tzinfo=dt.UTC)
    html = client.get(page).content.decode()
    assert "02/03/2027 12:59 (Paris)" in html and "01/03/2027 23:59 AoE" in html


def test_a_nonexistent_deadline_time_is_refused(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    page = _url(team, "edition", edition_id=edition.pk)
    response = _client(team, "student").post(
        page,
        {"kind": "PAPER", "day": "2027-03-14", "wall": "02:30", "tz_name": "America/Los_Angeles"},
    )
    assert response.status_code == 400
    assert not Deadline.objects.exists()


def test_external_links_open_apart_without_referrer(team: SimpleNamespace) -> None:
    venue = venue_of(team, url="https://sfca.example.org/" + marker())
    html = _client(team, "student").get(_url(team, "venue", venue_id=venue.pk)).content.decode()
    external = [a for a in re.findall(r"<a\b[^>]*>", html) if 'href="http' in a]
    assert external
    for anchor in external:
        assert 'target="_blank"' in anchor and 'rel="noopener noreferrer"' in anchor


# =========================================================================== submissions


def test_create_a_submission_posted_like_a_browser(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    title = marker()
    client = _browser(team, "student")
    response = _browser_post(
        client,
        _url(team, "new_submission"),
        _url(team, "new_submission"),
        {
            "target": f"edition:{edition.pk}",
            "title": title,
            "abstract": "",
            "external_authors": "",
            "notes": "",
            "objective": "",
            "level": "CONFIDENTIAL",
        },
    )
    assert response.status_code == 302, response.content[:400]
    submission = Submission.objects.get()
    assert submission.st_work is True
    html = client.get(_s(team, submission, "submission")).content.decode()
    assert title in html and title not in _title_tag(html)


def test_restricted_content_for_a_reader_without_the_level(team: SimpleNamespace) -> None:
    title = marker()
    submission = submission_of(team, title=title)  # CONFIDENTIAL
    html = _client(team, "co_supervisor").get(_s(team, submission, "submission")).content.decode()
    assert title not in html and "Contenu restreint" in html


# =========================================================================== ST approval


@pytest.mark.parametrize(
    "view", [views.DecideView, views.LevelView, views.StWorkOffView, views.EditPolicyView]
)
def test_the_guard_comes_before_the_second_factor(view: type) -> None:
    mro = view.__mro__
    assert (
        mro.index(ThesisPermissionRequiredMixin)
        < mro.index(RecentAuthRequiredMixin)
        < mro.index(View)
    )


def test_decide(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    url = _s(team, submission, "decide", approval_id=approval.pk)
    data = {"decision": "GRANTED", "reason": "", "comment": ""}
    for role in ("student", "director"):
        assert _client(team, role).post(url, data).status_code == 403  # never asked for a factor
    response = _client(team, "tutor").post(url, data)
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    assert StApproval.objects.get().decision == "PENDING"
    client = _browser(team, "tutor", mfa=True)
    response = _browser_post(client, _s(team, submission, "submission"), url, data)
    assert response.status_code == 302, response.content[:400]
    assert StApproval.objects.get().decision == "GRANTED"


def test_a_refusal_without_reason_is_a_400(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    approval = services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    url = _s(team, submission, "decide", approval_id=approval.pk)
    response = _client(team, "tutor", mfa=True).post(url, {"decision": "REFUSED", "reason": ""})
    assert response.status_code == 400


def test_request_posted_like_a_browser_and_submit_needs_the_grant(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    version = article_version(team, submission)
    page = _s(team, submission, "submission")
    status = _s(team, submission, "status")
    refused = _client(team, "student").post(
        status, {"status": "SUBMITTED", "version": str(version.pk)}
    )
    assert refused.status_code == 409
    client = _browser(team, "student")
    response = _browser_post(
        client, page, _s(team, submission, "request"),
        {"version": str(version.pk), "purpose": "SUBMISSION"},
    )  # fmt: skip
    assert response.status_code == 302, response.content[:400]
    approval = StApproval.objects.get()
    assert approval.request_due == D(2027, 1, 31)
    services.decide_approval(
        approval=approval, user=team.tutor, decision="GRANTED", recent_mfa=True
    )
    response = _browser_post(
        client, page, status, {"status": "SUBMITTED", "version": str(version.pk)}
    )
    assert response.status_code == 302
    assert Submission.objects.get().status == "SUBMITTED"


def test_a_preprint_without_approval_is_a_409(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    response = _client(team, "student").post(
        _s(team, submission, "preprint"),
        {
            "platform": "EPRINT",
            "posted_on": "2027-01-09",
            "url": "https://eprint.example.org/2027/1",
            "version": str(version.pk),
        },
    )
    assert response.status_code == 409
    assert not Preprint.objects.exists()


def test_preprint_links_open_apart(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    version = article_version(team, submission)
    granted(team, submission, version, purpose="PREPRINT")
    url = "https://eprint.example.org/2027/" + marker()
    services.add_preprint(
        submission=submission, user=team.student, platform="EPRINT", posted_on=D(2027, 1, 9),
        url=url, version=version,
    )  # fmt: skip
    html = _client(team, "tutor").get(_s(team, submission, "submission")).content.decode()
    (anchor,) = [a for a in re.findall(r"<a\b[^>]*>", html) if url in a]
    assert 'target="_blank"' in anchor and 'rel="noopener noreferrer"' in anchor


def test_st_work_off_is_the_tutors(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    url = _s(team, submission, "st_work_off")
    assert _client(team, "student").post(url).status_code == 403
    response = _client(team, "tutor").post(url)
    assert response.status_code == 302 and reverse("mfa_reauthenticate") in response["Location"]
    assert _client(team, "tutor", mfa=True).post(url).status_code == 302
    assert Submission.objects.get().st_work is False


def test_policy(team: SimpleNamespace) -> None:
    url = _url(team, "policy_edit")
    assert _client(team, "student").post(url, {"st_review_days": "10"}).status_code == 403
    response = _client(team, "tutor").post(url, {"st_review_days": "10"})
    assert reverse("mfa_reauthenticate") in response["Location"]
    client = _browser(team, "tutor", mfa=True)
    response = _browser_post(client, _url(team, "policy"), url, {"st_review_days": "45"})
    assert response.status_code == 302
    assert services.review_days(team.thesis) == 45


def test_change_level_asks_for_the_second_factor(team: SimpleNamespace) -> None:
    submission = submission_of(team)
    url = _s(team, submission, "level")
    response = _client(team, "student").post(url, {"level": "INTERNAL_ST"})
    assert reverse("mfa_reauthenticate") in response["Location"]
    assert _client(team, "tutor").post(url, {"level": "INTERNAL_ST"}).status_code == 403
    assert _client(team, "student", mfa=True).post(url, {"level": "INTERNAL_ST"}).status_code == 302
    assert Submission.objects.get().level == "INTERNAL_ST"


def test_an_approval_of_another_submission_is_an_audited_refusal(team: SimpleNamespace) -> None:
    mine = submission_of(team)
    other = submission_of(team, title="Autre article fictif")
    approval = services.request_approval(
        submission=other, user=team.student, version=article_version(team, other)
    )
    url = _s(team, mine, "withdraw_approval", approval_id=approval.pk)
    assert _client(team, "student").post(url).status_code == 403
    from apps.vault.models import AuditEvent

    denied = AuditEvent.objects.filter(action="CONFERENCE_ACCESS_DENIED").last()
    assert denied is not None and denied.details_json["reason"] == "unknown_approval"


# =========================================================================== card, objective


def test_dashboard_card(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission = submission_of(team, edition=edition)
    services.request_approval(
        submission=submission, user=team.student, version=article_version(team, submission)
    )
    page = reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    tutor = _client(team, "tutor").get(page).content.decode()
    assert "SFCA 2027" in tutor and "1 accord ST attend votre décision" in tutor
    student = _client(team, "student").get(page).content.decode()
    assert "accord ST attend" not in student


def test_request_due_on_the_card(team: SimpleNamespace) -> None:
    edition = edition_of(team)
    deadline_of(team, edition)
    submission_of(team, edition=edition)
    page = reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    html = _client(team, "student").get(page).content.decode()
    assert "Accord ST à demander avant le 31/01/2027 (dans 21 jours)" in html


def test_linked_submissions_on_the_objective_page(team: SimpleNamespace) -> None:
    objective = objective_of(team)
    submission_of(team, objective=objective, level=L.PUBLIC, title="Article lié fictif")
    page = reverse(
        "objectives:detail", kwargs={"thesis_id": team.thesis.pk, "objective_id": objective.pk}
    )
    fragment = _url(team, "objective_submissions", objective_id=objective.pk)
    assert fragment in _client(team, "director").get(page).content.decode()
    assert "Article lié fictif" in _client(team, "director").get(fragment).content.decode()
    csi = _client(team, "csi").get(page)
    assert csi.status_code == 200 and fragment not in csi.content.decode()
    assert _client(team, "csi").get(fragment).status_code == 403
