"""The texts sealed at a submission's level outside the Submission table (M9b).

A change of level of a submission (services.change_level) reseals its own texts
and every text REGISTERED here: the approval comments and preprint URLs of M6,
the publication details of M9b. Each app registers its models in its
AppConfig.ready(), so that M6 never imports the apps built on it.

apps/conferences/tests/test_sealed_registry.py checks that every model with a
foreign key to Submission, sealed texts (vault.formats.TEXT_SLOTS) and no level of
its own is registered, with exactly its sealed fields.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from apps.vault import formats


class Dependent(NamedTuple):
    model: Any  # a model class
    link: str  # its foreign key to Submission
    fields: tuple[str, ...]  # its sealed fields, at the submission's level


_REGISTERED: list[Dependent] = []


def register(model: Any, fields: tuple[str, ...], *, link: str = "submission") -> None:
    """Called once per model at start-up (AppConfig.ready); idempotent."""
    from .models import Submission

    label = model._meta.label
    known = {
        name.rsplit(".", 1)[1] for name in formats.TEXT_SLOTS if name.rsplit(".", 1)[0] == label
    }
    if model is Submission or not fields or not set(fields) <= known:
        raise ValueError(f"{label}: not texts sealed at a submission's level")
    field = model._meta.get_field(link)
    if not field.is_relation or field.related_model is not Submission:
        raise ValueError(f"{label}.{link}: not a link to a submission")
    dependent = Dependent(model, link, tuple(fields))
    if dependent not in _REGISTERED:
        _REGISTERED.append(dependent)


def registered() -> tuple[Dependent, ...]:
    return tuple(_REGISTERED)
