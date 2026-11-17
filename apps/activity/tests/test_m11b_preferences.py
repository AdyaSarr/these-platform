"""M11b: three categories per (person, thesis), each with its allowed frequencies.

Informations: immediate, daily, weekly, feed only. À faire: immediate, daily,
weekly (never feed only). Échéances: immediate, daily (never weekly, never feed
only). Refused by the service AND by the database; a forged POST is refused too.
Defaults: immediate for À faire and Échéances.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.roles import Role
from apps.activity import services
from apps.activity.choices import Category, Frequency
from apps.activity.models import NotificationPreference
from apps.activity.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)

C, F = Category, Frequency
ALLOWED: dict[str, set[str]] = {
    C.INFO: {F.IMMEDIATE, F.DAILY, F.WEEKLY, F.FEED_ONLY},
    C.ACTION: {F.IMMEDIATE, F.DAILY, F.WEEKLY},
    C.DEADLINE: {F.IMMEDIATE, F.DAILY},
}
PAIRS = [(c, f) for c in C for f in F]


@pytest.fixture
def team() -> SimpleNamespace:
    return h.make_team()


def _id(pair: tuple[str, str]) -> str:
    return f"{pair[0]}-{pair[1]}"


@pytest.mark.parametrize("pair", PAIRS, ids=_id)
def test_the_service_allows_exactly_the_matrix(
    team: SimpleNamespace, pair: tuple[str, str]
) -> None:
    category, frequency = pair
    if frequency in ALLOWED[category]:
        services.set_preference(team.director, team.thesis, category, frequency)
        assert services.frequency_of(team.director, team.thesis, category) == frequency
    else:
        with pytest.raises(ValidationError):
            services.set_preference(team.director, team.thesis, category, frequency)
        assert not NotificationPreference.objects.filter(category=category).exists()


@pytest.mark.parametrize("pair", [p for p in PAIRS if p[1] not in ALLOWED[p[0]]], ids=_id)
def test_the_database_refuses_what_the_service_refuses(
    team: SimpleNamespace, pair: tuple[str, str]
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationPreference.objects.create(
            user=team.director, thesis=team.thesis, category=pair[0], frequency=pair[1]
        )


@pytest.mark.parametrize("role", [r for r in h.ROLE_ATTRS if r is not Role.GUEST],
                         ids=lambda r: r.value)  # fmt: skip
def test_defaults_are_immediate_for_action_and_deadline(team: SimpleNamespace, role: Role) -> None:
    user = getattr(team, h.ROLE_ATTRS[role])
    for category in (C.ACTION, C.DEADLINE):
        assert services.frequency_of(user, team.thesis, category) == F.IMMEDIATE


@pytest.mark.parametrize(
    ("field", "value"), [("action", "FEED_ONLY"), ("deadline", "WEEKLY"), ("deadline", "FEED_ONLY")]
)
def test_a_forged_post_is_refused(team: SimpleNamespace, field: str, value: str) -> None:
    browser = h.browser_for(team.director)
    data = {"info": "WEEKLY", "action": "IMMEDIATE", "deadline": "IMMEDIATE", field: value}
    response = h.browser_post(browser, h.url(team, "preferences"), h.url(team, "preferences"),
                              data)  # fmt: skip
    assert response.status_code == 200  # the form again, with its error
    assert not NotificationPreference.objects.exists()


def test_the_page_offers_the_three_categories(team: SimpleNamespace) -> None:
    html = h.client_for(team.director).get(h.url(team, "preferences")).content.decode()
    for name in ("info", "action", "deadline"):
        assert f'name="{name}"' in html, name
    deadline = html[html.index('name="deadline"') :]
    deadline = deadline[: deadline.index("</select>")]
    assert 'value="WEEKLY"' not in deadline and 'value="FEED_ONLY"' not in deadline
    action = html[html.index('name="action"') :]
    action = action[: action.index("</select>")]
    assert 'value="FEED_ONLY"' not in action
    assert "pour l'instant" not in html  # « Toujours envoyés immédiatement, pour l'instant »


def test_the_page_saves_the_three(team: SimpleNamespace) -> None:
    browser = h.browser_for(team.director)
    data = {"info": "FEED_ONLY", "action": "WEEKLY", "deadline": "DAILY"}
    response = h.browser_post(browser, h.url(team, "preferences"), h.url(team, "preferences"),
                              data)  # fmt: skip
    assert response.status_code == 302
    for category, frequency in ((C.INFO, F.FEED_ONLY), (C.ACTION, F.WEEKLY), (C.DEADLINE, F.DAILY)):
        assert services.frequency_of(team.director, team.thesis, category) == frequency


def test_every_e_mail_of_every_module_has_a_category() -> None:
    """The classification of the sheet (M11b), exhaustive."""
    from apps.activity.routing import CATEGORY_OF, SOURCES

    for source in SOURCES.values():
        kinds = set(source.kinds())
        assert kinds == set(CATEGORY_OF[source.key]), source.key
        assert set(CATEGORY_OF[source.key].values()) <= set(C)


def test_the_classification_of_the_sheet() -> None:
    from apps.activity.routing import CATEGORY_OF

    expected = {
        "admin_file.reminder": {"J14": C.DEADLINE, "J3": C.DEADLINE, "OVERDUE": C.DEADLINE},
        "admin_file.notification": {
            "REOPENED": C.INFO, "DUE_PROPOSED": C.ACTION, "DUE_ACCEPTED": C.DEADLINE,
            "DUE_REFUSED": C.DEADLINE, "DUE_CHANGED": C.DEADLINE,
        },
        "meetings": {
            "INVITED": C.DEADLINE, "CHANGED": C.DEADLINE, "CANCELLED": C.DEADLINE,
            "MINUTES": C.ACTION, "REMINDER": C.DEADLINE, "ACTION_OVERDUE": C.DEADLINE,
        },
        "conferences": {
            "DEADLINE": C.DEADLINE, "REQUEST_DUE": C.DEADLINE, "APPROVAL_PENDING": C.DEADLINE,
            "APPROVAL_REQUESTED": C.ACTION, "APPROVAL_DECIDED": C.INFO, "POLICY_CHANGED": C.INFO,
        },
        "travel": {
            "ORDER_TO_REQUEST": C.DEADLINE, "RECEIPTS_MISSING": C.DEADLINE,
            "ORDER_PENDING": C.DEADLINE, "ORDER_REQUESTED": C.ACTION, "ORDER_DECIDED": C.INFO,
            "RULES_CHANGED": C.INFO,
        },
    }  # fmt: skip
    for key, table in expected.items():
        assert dict(CATEGORY_OF[key]) == table, key
