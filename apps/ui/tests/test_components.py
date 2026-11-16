"""Reusable components (templates/ui/): markup that stays accessible without script."""

from __future__ import annotations

import re

from django.template.loader import render_to_string
from django.test import RequestFactory


def _render(name: str, **context: object) -> str:
    request = RequestFactory().get("/")
    return render_to_string(f"ui/{name}.html", context, request=request)


def test_confirm_is_a_disclosure_with_the_real_submit_inside() -> None:
    html = _render(
        "confirm",
        action="/t/x/meetings/y/cancel/",
        label="Annuler la réunion",
        confirm_label="Confirmer l'annulation",
        message="Les participants seront prévenus. Action irréversible.",
    )
    details = re.search(r'<details class="confirm">(.*)</details>', html, re.S)
    assert details is not None
    body = details.group(1)
    summary = re.search(r'<summary class="btn btn-danger">(.*?)</summary>', body, re.S)
    assert summary is not None and "Annuler la réunion" in summary.group(1)
    form = re.search(
        r'<form method="post" action="/t/x/meetings/y/cancel/"[^>]*>(.*?)</form>', body, re.S
    )
    assert form is not None
    assert 'name="csrfmiddlewaretoken"' in form.group(1)
    assert re.search(r'<button type="submit" class="btn btn-danger">\s*Confirmer', form.group(1))
    assert "Action irréversible." in body
    # The summary is not a submit: opening it never posts anything.
    assert "<button" not in summary.group(1)


def test_badge_never_relies_on_colour_alone() -> None:
    html = _render("badge", tone="danger", label="En retard")
    assert re.search(r'<span class="badge badge-danger">\s*<svg class="icon"', html)
    assert "En retard" in html


def test_badge_tones() -> None:
    for tone in ("success", "warning", "danger", "info", "neutral"):
        assert f"badge-{tone}" in _render("badge", tone=tone, label="x")


def test_empty_state() -> None:
    html = _render("empty", message="Aucune réunion prévue.")
    assert re.search(r'<div class="empty">\s*<svg class="icon"', html)
    assert "Aucune réunion prévue." in html


def test_progress_has_a_text_equivalent() -> None:
    html = _render("progress", value=3, max=10, label="3 / 10 pièces validées")
    assert re.search(r'<progress [^>]*value="3" max="10"[^>]*>', html)
    assert "3 / 10 pièces validées" in html
