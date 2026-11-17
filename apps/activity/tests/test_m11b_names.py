"""Never a separator followed by nothing (M11b, review of M11a).

The label of a catalogue piece is empty in the database by design (its name comes
from the catalogue, by its code; a piece added by the student must have one:
constraint admin_file_item_code_label). A catalogue-shaped code absent from the
catalogue — the test factories' only — has no name: the bell, the feed and the
digest page then show the kind alone, without « · » after it."""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.roles import Role
from apps.activity import digests, services
from apps.activity.choices import Category, Frequency
from apps.activity.labels import joined
from apps.activity.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)

DANGLING = re.compile(r"·\s*(<|$)", re.M)


def test_joined_drops_the_empty_parts() -> None:
    assert joined("Pièce déposée", "") == "Pièce déposée"
    assert joined("Pièce déposée", "  ", None, "Contrat") == "Pièce déposée · Contrat"
    assert joined("", "") == ""


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_a_nameless_piece_leaves_no_dangling_separator(team: SimpleNamespace,
                                                       time_machine: Any) -> None:  # fmt: skip
    from apps.admin_file import services as admin_file
    from apps.admin_file.services import label_of
    from apps.admin_file.tests.factories import RequiredItemFactory

    services.set_preference(team.director, team.thesis, Category.ACTION, Frequency.DAILY)
    piece = RequiredItemFactory.create(thesis=team.thesis, validator_roles=[Role.DIRECTOR])
    assert label_of(piece) == ""  # a code absent from the catalogue
    admin_file.submit(item=piece, user=team.student)
    time_machine.move_to(h.NOW + dt.timedelta(days=1), tick=True)
    digests.run()
    client = h.client_for(team.director)
    pages = [client.get(h.url(team, "feed")).content.decode()]
    from apps.activity.models import ActivityNotification

    digest = ActivityNotification.objects.get(recipient=team.director, kind="DIGEST_DAILY")
    pages.append(client.get(h.url(team, "digest", digest_id=digest.pk)).content.decode())
    for html in pages:
        assert "Pièce déposée" in html
        assert not DANGLING.search(html), DANGLING.search(html)
