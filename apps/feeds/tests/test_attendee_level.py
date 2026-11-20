"""An invitation does not open the feed to a meeting one no longer reads (M12, SR-12).

The feed lists the meetings one attends, through the rule of the lists
(``readable_by``: MEETING_READ and the level). ``_check_attendees`` refuses an
attendee who cannot read the level, at creation and at a change of level; but a
change of ROLE keeps the attendee row. The rule of the lists is then the only
barrier: mutant M10 of scripts/mutants.toml (``readable_by`` removed) must fail
here. The two-worlds test never invites the observed role to a hidden meeting.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts import services as accounts
from apps.accounts.models import Membership
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.feeds.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_new_role_hides_a_meeting_of_a_level_it_does_not_read(time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    user = team.co_supervisor  # reads INTERNAL_LIRMM
    shown = h.meeting(team, L.PUBLIC, days=3, attendees=[team.student, user])
    hidden = h.meeting(team, L.INTERNAL_LIRMM, days=4, attendees=[team.student, user])
    membership = Membership.objects.active().get(thesis=team.thesis, user=user)
    accounts.change_role(membership, Role.INDUSTRIAL_CO_SUPERVISOR, by=team.student)
    # Still an attendee of both; the new role reads PUBLIC and INTERNAL_ST only.
    assert hidden.attendees.filter(user=user).exists()
    _feed, token = h.new_feed(user, team.thesis)  # the former feed was revoked
    body = h.fetch(token).content.decode()
    assert f"UID:{shown.pk}@" in body  # not vacuous: the feed lists its meetings
    assert f"UID:{hidden.pk}@" not in body
    assert "TEMOIN-FUITE" not in body
