"""State machine of a required item (M3 plan, section 3).

TRANSITIONS lists every allowed (status, event) pair and its target; any other
pair is refused by the services (InvalidTransitionError), whoever asks. APPROVE
targets VALIDATED: the item reaches it once EVERY validator role of the item has
approved the cycle, and stays SUBMITTED until then (two-party items).

tests/test_transitions.py holds an independent transcription and tries every
pair of the product statuses x events.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from .models import ItemStatus


class Event(StrEnum):
    SUBMIT = "SUBMIT"  # the student deposits (cycle + 1)
    WITHDRAW = "WITHDRAW"  # the student takes the deposit back
    APPROVE = "APPROVE"  # one validator role approves the cycle
    REJECT = "REJECT"  # one validator role rejects, with a reason
    REOPEN = "REOPEN"  # a validator or the student reopens, with a reason


_S, _E = ItemStatus, Event

TRANSITIONS: Mapping[tuple[ItemStatus, Event], ItemStatus] = MappingProxyType(
    {
        (_S.TODO, _E.SUBMIT): _S.SUBMITTED,
        (_S.REJECTED, _E.SUBMIT): _S.SUBMITTED,
        (_S.SUBMITTED, _E.WITHDRAW): _S.TODO,
        (_S.SUBMITTED, _E.APPROVE): _S.VALIDATED,
        (_S.SUBMITTED, _E.REJECT): _S.REJECTED,
        (_S.VALIDATED, _E.REOPEN): _S.TODO,
    }
)


class InvalidTransitionError(Exception):
    """The item is not in a state allowing this event (409: reload the page)."""


class FrozenFileError(InvalidTransitionError):
    """The thesis is defended or closed: its administrative file is frozen."""


class StaleSubmissionError(InvalidTransitionError):
    """The attachment changed since the submission: approve the submitted content only."""


def target(status: str, event: Event) -> ItemStatus:
    try:
        return TRANSITIONS[(ItemStatus(status), event)]
    except (KeyError, ValueError):
        raise InvalidTransitionError(f"{event.value} is not allowed from {status}") from None
