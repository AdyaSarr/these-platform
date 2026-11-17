"""Fictitious theses and members for the visual-layer tests (CLAUDE.md rule 10)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from django.test import Client
from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory

# Roles whose membership must end (the CSI is renewed each year, guests are temporary).
BOUNDED = {Role.CSI_MEMBER, Role.GUEST}


def member(thesis: Thesis, role: Role) -> Any:
    kwargs: dict[str, Any] = {}
    if role in BOUNDED:
        kwargs["active_until"] = timezone.now() + dt.timedelta(days=60)
    return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user


def signed_in(role: Role, thesis: Thesis | None = None) -> tuple[Client, Thesis]:
    thesis = thesis or ThesisFactory.create()
    if role is not Role.PHD_STUDENT:
        member(thesis, Role.PHD_STUDENT)
    client = Client()
    client.force_login(member(thesis, role))
    return client, thesis


def nav_keys(html: str) -> list[str]:
    """The data-nav keys of the sidebar links, in order."""
    match = re.search(r'<nav id="sidebar"[^>]*>(.*?)</nav>', html, re.S)
    return re.findall(r'data-nav="([\w-]+)"', match.group(1)) if match else []


def without_sidebar(html: str) -> str:
    """The page without its sidebar: for a test about the content of a page, which
    the navigation (links to every module the role may open) must not satisfy."""
    return re.sub(r'<nav id="sidebar".*?</nav>', "", html, count=1, flags=re.S)
