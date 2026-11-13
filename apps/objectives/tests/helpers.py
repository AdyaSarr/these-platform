"""Shared fixtures of the objectives tests. Fictitious data only (CLAUDE.md rule 10)."""

from __future__ import annotations

import datetime as dt
import secrets
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services
from apps.objectives.models import Category, Objective, ThesisYear

D = dt.date


def make_team(**thesis_fields: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "cifre_start_date": D(2026, 12, 1),
        "first_enrollment_date": D(2026, 12, 15),
        "planned_end_date": D(2029, 11, 30),
    }
    fields.update(thesis_fields)
    thesis = ThesisFactory.create(**fields)

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(),
    )


def year_of(team: SimpleNamespace, number: int = 1) -> ThesisYear:
    return ThesisYear.objects.get(thesis=team.thesis, number=number)


def marker() -> str:
    return "TEMOIN-" + secrets.token_hex(6)


def objective_of(team: SimpleNamespace, **overrides: Any) -> Objective:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "year": year_of(team),
        "title": "Caractériser une fuite fictive",
        "description": "Description fictive.",
        "success_criterion": "Un critère fictif mesurable.",
        "category": Category.RESEARCH,
        "due_date": D(2027, 6, 30),
        "weight": 2,
        "level": L.PUBLIC,
    }
    fields.update(overrides)
    return services.create_objective(**fields)


def approved(team: SimpleNamespace, **overrides: Any) -> Objective:
    objective = objective_of(team, **overrides)
    return services.approve_objective(objective=objective, user=team.director, recent_mfa=True)


def texts(user: Any, objective: Objective) -> dict[str, Any]:
    return services.objective_texts(user, team_thesis(objective), [objective])[objective.pk]


def team_thesis(objective: Objective) -> Any:
    from apps.accounts.models import Thesis

    return Thesis.objects.get(pk=objective.thesis_id)
