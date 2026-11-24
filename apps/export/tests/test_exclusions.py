"""What the ZIP never holds (M13b, criteria 7 and 8): secrets, tokens, passwords,
the others' addresses and journal lines, pseudonyms; INTERNAL_ST without an ST
agreement (positive check with one, and once the ST confidentiality is over)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import Thesis
from apps.accounts.reauth import ReauthenticationRequiredError
from apps.export import services as export
from apps.export.models import StExportConsent
from apps.export.tests import helpers as h
from apps.lifecycle.tests.helpers import audited

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_no_secret_token_password_address_or_pseudonym(team: SimpleNamespace) -> None:
    w = h.world(team)
    text = h.everything(h.run(team))
    leaks = [value for value in w.forbidden if value in text]
    assert leaks == []
    for fragment in ("kek_id", "wrapped_dek", "env:CONFIDENTIAL", "ip_hash", "email_hmac"):
        assert fragment not in text, fragment


def test_internal_st_is_left_out_without_an_agreement(team: SimpleNamespace) -> None:
    w = h.world(team)
    archive = h.run(team)
    text = h.everything(archive)
    assert [x for x in w.st_witnesses if x in text] == []
    assert h.read(archive, "manifest.json")["excluded"]["INTERNAL_ST"] >= 3


def test_an_st_agreement_lets_internal_st_in(team: SimpleNamespace) -> None:
    w = h.world(team)
    consent = export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    archive = h.run(team)
    text = h.everything(archive)
    assert [x for x in w.st_witnesses if x not in text] == []  # not vacuous
    assert h.read(archive, "manifest.json")["options"]["st_consent_id"] == str(consent.pk)
    (event,) = audited("THESIS_EXPORTED")
    assert event["st_consent"] == str(consent.pk) and event["st_included"] >= 3


def test_the_end_of_st_confidentiality_lets_internal_st_in(team: SimpleNamespace) -> None:
    w = h.world(team)
    export.set_st_confidential_until(thesis=team.thesis, user=team.tutor,
                                     until=h.D(2027, 1, 19), recent_mfa=True)  # fmt: skip
    text = h.everything(h.run(team))
    assert [x for x in w.st_witnesses if x not in text] == []


def test_a_future_end_keeps_internal_st_out(team: SimpleNamespace) -> None:
    w = h.world(team)
    export.set_st_confidential_until(thesis=team.thesis, user=team.tutor,
                                     until=h.D(2030, 1, 1), recent_mfa=True)  # fmt: skip
    text = h.everything(h.run(team))
    assert [x for x in w.st_witnesses if x in text] == []


# --------------------------------------------------------------------------- the agreement


@pytest.mark.parametrize("attr", ["student", "director", "co_supervisor", "st_co_supervisor",
                                  "csi", "guest", "outsider"])  # fmt: skip
def test_the_tutor_alone_grants_or_dates(team: SimpleNamespace, attr: str) -> None:
    user = getattr(team, attr)
    with pytest.raises(PermissionDenied):
        export.grant_st_consent(thesis=team.thesis, user=user, recent_mfa=True)
    with pytest.raises(PermissionDenied):
        export.set_st_confidential_until(thesis=team.thesis, user=user, until=None,
                                         recent_mfa=True)  # fmt: skip
    assert StExportConsent.objects.count() == 0


def test_granting_and_dating_need_a_recent_second_factor(team: SimpleNamespace) -> None:
    with pytest.raises(ReauthenticationRequiredError):
        export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=False)
    with pytest.raises(ReauthenticationRequiredError):
        export.set_st_confidential_until(thesis=team.thesis, user=team.tutor,
                                         until=h.D(2030, 1, 1), recent_mfa=False)  # fmt: skip


def test_an_agreement_lasts_thirty_days_and_is_journaled(team: SimpleNamespace,
                                                        time_machine: Any) -> None:  # fmt: skip
    w = h.world(team)
    consent = export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    assert consent.valid_until - consent.created_at == dt.timedelta(days=30)
    assert [e["consent"] for e in audited("ST_EXPORT_CONSENT_GRANTED")] == [str(consent.pk)]
    time_machine.move_to(h.NOW + dt.timedelta(days=30, minutes=1), tick=True)
    text = h.everything(h.run(team))
    assert [x for x in w.st_witnesses if x in text] == []


def test_a_revoked_agreement_lets_nothing_in(team: SimpleNamespace) -> None:
    w = h.world(team)
    consent = export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    export.revoke_st_consent(consent=consent, user=team.tutor)
    assert [e["consent"] for e in audited("ST_EXPORT_CONSENT_REVOKED")] == [str(consent.pk)]
    text = h.everything(h.run(team))
    assert [x for x in w.st_witnesses if x in text] == []


def test_dating_is_journaled_with_both_dates(team: SimpleNamespace) -> None:
    export.set_st_confidential_until(thesis=team.thesis, user=team.tutor,
                                     until=h.D(2030, 1, 1), recent_mfa=True)  # fmt: skip
    export.set_st_confidential_until(thesis=team.thesis, user=team.tutor, until=None,
                                     recent_mfa=True)  # fmt: skip
    assert audited("ST_CONFIDENTIALITY_SET") == [
        {"old": None, "new": "2030-01-01"},
        {"old": "2030-01-01", "new": None},
    ]


def test_no_agreement_once_the_thesis_is_closed(team: SimpleNamespace) -> None:
    """Provisional (Q-ST): the tutor's writes are refused in CLOSED."""
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.CLOSED)
    with pytest.raises(PermissionDenied):
        export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    with pytest.raises(PermissionDenied):
        export.set_st_confidential_until(thesis=team.thesis, user=team.tutor, until=None,
                                         recent_mfa=True)  # fmt: skip


def test_an_agreement_in_defended(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    export.grant_st_consent(thesis=team.thesis, user=team.tutor, recent_mfa=True)
    assert StExportConsent.objects.count() == 1
