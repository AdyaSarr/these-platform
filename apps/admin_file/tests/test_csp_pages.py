"""Every page and htmx fragment of the administrative file renders without inline
script, inline style or on* handler (strict CSP, docs/decisions.md)."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.helpers import mfa_login
from apps.admin_file import services
from apps.admin_file.models import RequiredItem
from tests.test_csp_rendered_pages import _assert_csp_clean

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
HX = {"HX-Request": "true"}


def test_pages_and_fragments_are_csp_clean() -> None:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    student, director = member(Role.PHD_STUDENT), member(Role.DIRECTOR)
    member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60))
    services.seed_items(thesis)
    item = RequiredItem.objects.get(thesis=thesis, code="adum_enrollment")
    pdf = SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%Piece fictive\n%%EOF\n")

    def url(name: str, **kw: Any) -> str:
        return reverse(f"admin_file:{name}", kwargs={"thesis_id": thesis.pk, **kw})

    as_student = Client()
    as_student.force_login(student)
    pages = {
        "file": as_student.get(url("file")),
        "item": as_student.get(url("item", item_id=item.pk)),
        "item_fragment": as_student.get(url("item", item_id=item.pk), headers=HX),
        "new_item": as_student.get(url("new_item")),
        "submit_fragment": as_student.post(
            url("submit", item_id=item.pk), {"file": pdf}, headers=HX
        ),
        "error_fragment": as_student.post(url("submit", item_id=item.pk), {}, headers=HX),
        "dashboard": as_student.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})),
    }
    as_director = Client()
    mfa_login(as_director, director, PASSWORD)
    pages["director_item"] = as_director.get(url("item", item_id=item.pk), headers=HX)
    pages["approve_fragment"] = as_director.post(url("approve", item_id=item.pk), headers=HX)
    pages["conflict_fragment"] = as_director.post(url("approve", item_id=item.pk), headers=HX)
    for name, response in pages.items():
        assert response.status_code in (200, 400, 409), (name, response.status_code)
        _assert_csp_clean(response.content.decode(), name)
