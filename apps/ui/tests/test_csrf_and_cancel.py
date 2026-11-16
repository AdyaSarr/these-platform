"""The two last notes of docs/ui-notes.md.

1. A CSRF failure shows our own page (CSRF_FAILURE_VIEW): the sentence of the
   note, no technical detail (never Django's reason), the layout of the other
   pages, status 403, and nothing of the request copied into the page (path,
   query string, posted fields).
2. « Annuler la réunion » asks for a confirmation (<details>, like « Retirer »).
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.accounts.roles import Role

from .helpers import signed_in

MARKER = "TEMOIN-FUITE-csrf"
SENTENCE = "Votre navigateur bloque les cookies de ce site ou la page a expiré\u00a0: rechargez-la."


def test_the_setting_names_our_view() -> None:
    assert settings.CSRF_FAILURE_VIEW == "apps.core.views.csrf_failure"


def _refused(client: Client, url: str, **extra: Any) -> Any:
    response = client.post(url, {"note": MARKER, "next": f"/{MARKER}/"}, **extra)
    assert response.status_code == 403
    return response


@pytest.mark.django_db
def test_an_anonymous_failure() -> None:
    browser = Client(enforce_csrf_checks=True)
    response = _refused(browser, reverse("account_login") + f"?{MARKER}=1")
    page = html_module.unescape(response.content.decode())
    assert SENTENCE in page
    assert '<header class="site-header"' in page  # the layout of the other pages
    assert MARKER not in page


@pytest.mark.django_db
def test_a_signed_in_failure_copies_nothing_of_the_request() -> None:
    client, thesis = signed_in(Role.PHD_STUDENT)
    browser = Client(enforce_csrf_checks=True)
    browser.cookies = client.cookies
    url = reverse("accounts:team_invite", kwargs={"thesis_id": thesis.pk}) + f"?q={MARKER}"
    response = _refused(browser, url, HTTP_ORIGIN="http://testserver")
    page = html_module.unescape(response.content.decode())
    assert SENTENCE in page
    assert MARKER not in page
    assert str(thesis.pk) not in page  # not even the path of the refused page
    assert thesis.title not in page
    # The theme form comes back to "/", not to the refused path.
    assert re.search(r'name="next" value="/"', page)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "extra", [{}, {"HTTP_ORIGIN": "https://ailleurs.example.org"}], ids=["no-token", "origin"]
)
def test_no_technical_detail(extra: dict[str, str]) -> None:
    browser = Client(enforce_csrf_checks=True)
    page = _refused(browser, reverse("account_login"), **extra).content.decode()
    for detail in ("CSRF", "csrf_token", "Referer", "Origin", "ailleurs.example.org", "Forbidden"):
        assert detail not in page.replace("csrfmiddlewaretoken", ""), detail


# --------------------------------------------------------------------------- 2


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    from apps.meetings.tests.helpers import meeting_of
    from apps.travel.tests.helpers import make_team

    time_machine.move_to(dt.datetime(2027, 1, 5, 9, 0, tzinfo=dt.UTC), tick=False)
    team = make_team()
    team.meeting = meeting_of(team)
    return team


@pytest.mark.django_db(transaction=True)
def test_cancelling_a_meeting_asks_for_a_confirmation(team: SimpleNamespace) -> None:
    client = Client()
    client.force_login(team.student)
    kwargs = {"thesis_id": team.thesis.pk, "meeting_id": team.meeting.pk}
    page = html_module.unescape(
        client.get(reverse("meetings:detail", kwargs=kwargs)).content.decode()
    )
    url = reverse("meetings:cancel", kwargs=kwargs)
    confirm = re.search(
        rf'<details class="confirm">(?:(?!</details>).)*?{url}.*?</details>', page, re.S
    )
    assert confirm is not None
    assert re.search(
        r'<summary class="btn btn-danger">.*?Annuler la réunion</summary>', confirm.group(0), re.S
    )
    assert page.count(f'action="{url}"') == 1  # the only form that cancels
