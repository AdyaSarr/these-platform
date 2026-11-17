"""Team page (frontend step 2, point 9): one card per member, the actions in a
menu (<details>, no script), the current role preselected in the role list,
« Retirer » behind a confirmation (<details class="confirm">), nothing truncated.

No permission change: the menu exists under the very conditions the old forms
did (can_change_role, can_extend, can_remove, never on the student's card), and
the views re-check every POST.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.accounts.tests.factories import ThesisFactory

from .helpers import member, signed_in

pytestmark = pytest.mark.django_db

BASE_DIR = Path(__file__).resolve().parents[3]


def _team_page(role: Role) -> tuple[str, list[Membership]]:
    thesis = ThesisFactory.create()
    member(thesis, Role.DIRECTOR)
    member(thesis, Role.CO_SUPERVISOR)
    member(thesis, Role.INDUSTRIAL_TUTOR)
    member(thesis, Role.CSI_MEMBER)
    client, thesis = signed_in(role, thesis)
    response = client.get(reverse("accounts:team", kwargs={"thesis_id": thesis.pk}))
    assert response.status_code == 200
    memberships = list(Membership.objects.active().filter(thesis=thesis))
    return response.content.decode(), memberships


def _cards(html: str) -> dict[str, str]:
    return dict(
        re.findall(
            r'<li class="card member" data-member="([0-9a-f-]+)">(.*?)</li>'
            r'\s*(?=<li class="card member"|</ul>)',
            html,
            re.S,
        )
    )


def test_one_card_per_active_member() -> None:
    html, memberships = _team_page(Role.DIRECTOR)
    cards = _cards(html)
    assert set(cards) == {str(m.pk) for m in memberships}
    assert "<table" not in html.split('class="members"', 1)[1].split("</ul>", 1)[0]
    for m in memberships:
        card = cards[str(m.pk)]
        assert m.user.email in card and str(m.get_role_display()) in card


def test_a_reader_gets_no_menu() -> None:
    html, _ = _team_page(Role.DIRECTOR)
    assert 'class="menu"' not in html and "/remove/" not in html


def _student_cards() -> tuple[dict[str, str], list[Membership], str]:
    html, memberships = _team_page(Role.PHD_STUDENT)
    return _cards(html), memberships, html


def test_the_student_manages_every_card_but_their_own() -> None:
    cards, memberships, _ = _student_cards()
    for m in memberships:
        has_menu = '<details class="menu">' in cards[str(m.pk)]
        assert has_menu is (m.role != Role.PHD_STUDENT), m.role


def test_the_current_role_is_preselected() -> None:
    cards, memberships, _ = _student_cards()
    for m in memberships:
        if m.role == Role.PHD_STUDENT:
            continue
        select = re.search(
            r'<select name="role" id="role-[^"]+">(.*?)</select>', cards[str(m.pk)], re.S
        )
        assert select is not None, m.role
        selected = re.findall(r'<option value="(\w+)" selected>', select.group(1))
        assert selected == [m.role], m.role
        assert 'value="PHD_STUDENT"' not in select.group(1)


def test_remove_asks_for_a_confirmation() -> None:
    cards, memberships, _ = _student_cards()
    for m in memberships:
        if m.role == Role.PHD_STUDENT:
            continue
        card = cards[str(m.pk)]
        url = reverse(
            "accounts:team_remove", kwargs={"thesis_id": m.thesis_id, "membership_id": m.pk}
        )
        confirm = re.search(r'<details class="confirm">.*?</details>', card, re.S)
        assert confirm is not None
        assert re.search(
            r'<summary class="btn btn-danger">.*?Retirer</summary>', confirm.group(0), re.S
        )
        # The only form that removes is inside the confirmation.
        assert card.count(f'action="{url}"') == 1 and f'action="{url}"' in confirm.group(0)


def test_csi_extension_on_the_csi_card_only() -> None:
    cards, memberships, _ = _student_cards()
    for m in memberships:
        card = cards[str(m.pk)]
        assert ('name="days"' in card) is (m.role == Role.CSI_MEMBER), m.role


def test_every_field_has_its_label_and_ids_are_unique() -> None:
    _, _, html = _student_cards()
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert [i for i, n in Counter(ids).items() if n > 1] == []
    for field_id in re.findall(r'<(?:select|input) [^>]*id="((?:role|days)-[^"]+)"', html):
        assert f'<label for="{field_id}"' in html, field_id


def test_the_csi_end_date_is_on_its_card() -> None:
    cards, memberships, _ = _student_cards()
    csi = next(m for m in memberships if m.role == Role.CSI_MEMBER)
    assert csi.active_until is not None and csi.active_until > timezone.now() - dt.timedelta(1)
    assert 'data-until="' in cards[str(csi.pk)]


def test_nothing_is_truncated() -> None:
    css = (BASE_DIR / "static" / "css" / "components.css").read_text(encoding="utf-8")
    rules = re.findall(r"(\.member[\w\s.,:>-]*)\{([^}]*)\}", css)
    assert rules
    assert not any("ellipsis" in body or "nowrap" in body for _, body in rules)
    assert any("overflow-wrap: anywhere" in body for _, body in rules)


def test_revoking_an_invitation_asks_for_a_confirmation() -> None:
    """Same confirmation as « Retirer » (review of point 9)."""
    from apps.accounts import services

    thesis = ThesisFactory.create()
    client, thesis = signed_in(Role.PHD_STUDENT, thesis)
    student = Membership.objects.get(thesis=thesis, role=Role.PHD_STUDENT)
    invitation = services.create_invitation(
        thesis=thesis,
        email="invite.fictif@example.org",
        role=Role.DIRECTOR,
        organization=student.organization,
        by=student.user,
    )
    html = client.get(reverse("accounts:team", kwargs={"thesis_id": thesis.pk})).content.decode()
    url = reverse(
        "accounts:team_revoke_invitation",
        kwargs={"thesis_id": thesis.pk, "invitation_id": invitation.pk},
    )
    confirm = re.search(
        rf'<details class="confirm">(?:(?!</details>).)*?{url}.*?</details>', html, re.S
    )
    assert confirm is not None
    assert re.search(
        r'<summary class="btn btn-danger">.*?Révoquer</summary>', confirm.group(0), re.S
    )
    assert html.count(f'action="{url}"') == 1  # the only form that revokes
