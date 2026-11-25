"""M9b: the texts sealed at a submission's level live in other tables too (the
approval comments, the preprint URLs, the publication details of M9b). They are
REGISTERED (apps.conferences.sealed, filled by each app's AppConfig.ready), and a
change of level of the submission reseals everything registered; M6 never
imports the apps that register.

The static rule: every model with a foreign key (or one-to-one) to Submission,
sealed texts (formats.TEXT_SLOTS) and no level of its own is registered, with
exactly its sealed fields."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied

from apps.conferences import sealed, services
from apps.conferences.models import Submission
from apps.vault import formats
from apps.vault.models import SealedText

ROOT = Path(__file__).resolve().parents[3]


def _sealed_fields(model: Any) -> set[str]:
    label = model._meta.label
    return {
        name.rsplit(".", 1)[1] for name in formats.TEXT_SLOTS if name.rsplit(".", 1)[0] == label
    }


def _expected() -> dict[tuple[Any, str], set[str]]:
    out = {}
    for model in apps.get_models():
        if model is Submission:
            continue
        fields = _sealed_fields(model)
        if not fields or any(f.name == "level" for f in model._meta.concrete_fields):
            continue  # no text, or texts at the model's own level (a mission, say)
        for field in model._meta.concrete_fields:
            if field.is_relation and field.related_model is Submission:
                out[(model, field.name)] = fields
    return out


def test_every_model_sealed_at_the_submission_level_is_registered() -> None:
    expected = _expected()
    labels = {model._meta.label for model, _link in expected}
    # Not vacuous: M6's own two, and the publication details of M9b.
    assert {"conferences.StApproval", "conferences.Preprint",
            "portfolio.PublicationDetails"} <= labels  # fmt: skip
    registered = {(d.model, d.link): set(d.fields) for d in sealed.registered()}
    assert registered == expected


def test_conferences_never_imports_the_portfolio() -> None:
    for path in sorted((ROOT / "apps" / "conferences").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(n.startswith("apps.portfolio") for n in names), path


def test_a_registration_names_real_sealed_fields() -> None:
    with pytest.raises(ValueError):
        sealed.register(Submission, ("title",))  # Submission reseals its own texts
    from apps.conferences.models import StApproval

    with pytest.raises(ValueError):
        sealed.register(StApproval, ("no_such_slot",))


@pytest.mark.django_db(transaction=True)
def test_a_change_of_level_reseals_the_publication_details(time_machine: Any) -> None:
    from apps.portfolio import services as portfolio
    from apps.portfolio.tests import helpers as h

    time_machine.move_to(h.NOW, tick=True)
    team: SimpleNamespace = h.make_team()
    submission = h.publication(team, h.L.CONFIDENTIAL, title="Article fictif")
    details = h.details(team, submission, doi="10.12345/fictif.2027", bibtex_key="fictif27")
    services.change_level(submission=submission, user=team.student,
                          level=h.L.INTERNAL_LIRMM, recent_mfa=True)  # fmt: skip
    levels = set(SealedText.objects.filter(object_id=details.pk).values_list("level", flat=True))
    assert levels == {"INTERNAL_LIRMM"}
    row = portfolio.publication_detail(team.co_supervisor, Submission.objects.get(pk=submission.pk))
    assert row.texts["doi"] == "10.12345/fictif.2027"
    with pytest.raises(PermissionDenied):
        portfolio.publication_for(team.tutor, team.thesis, submission.pk, operation="detail")
