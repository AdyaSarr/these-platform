"""The token never appears in a journal (M12): Django's, django.request (a 404),
the runserver's request line, an error report, the audit log. The filter
(apps.core.logging.TokenPathFilter) is on every handler."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache

from apps.feeds.tests import helpers as h

pytestmark = pytest.mark.django_db(transaction=True)

# 43 characters, as a token; a witness no random value can contain.
WITNESS = "TEMOINjetonABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"  # pragma: allowlist secret


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    cache.clear()
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def test_the_filter_masks_a_token_in_a_message_and_its_arguments() -> None:
    from apps.core.logging import TokenPathFilter

    token = WITNESS
    record = logging.LogRecord("django.server", logging.INFO, __file__, 1,
                               '"GET %s HTTP/1.1" 200', (f"/ical/{token}.ics",), None)  # fmt: skip
    assert TokenPathFilter().filter(record) is True
    assert token not in record.getMessage() and "/ical/[jeton].ics" in record.getMessage()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, f"Not Found: /ical/{token}.ics",
                               (), None)  # fmt: skip
    TokenPathFilter().filter(record)
    assert token not in record.getMessage()


def test_the_filter_is_on_every_handler() -> None:
    from django.conf import settings

    handlers: dict[str, dict[str, Any]] = settings.LOGGING["handlers"]  # type: ignore[assignment]
    for name, handler in handlers.items():
        assert "token_path" in handler.get("filters", []), name


def test_no_journal_holds_the_token(team: SimpleNamespace,
                                    caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    from apps.vault.models import AuditEvent

    caplog.set_level(logging.DEBUG)
    _feed, token = h.new_feed(team.director, team.thesis)
    h.fetch(token)
    h.fetch(token[:-1] + ("A" if token[-1] != "A" else "B"))  # a 404
    from apps.feeds import services

    services.regenerate(user=team.director, thesis=team.thesis, recent_mfa=True)
    h.fetch(token)  # now revoked: a 404
    captured = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    assert token not in captured
    audit = " ".join(str(e.details_json) for e in AuditEvent.objects.all())
    assert token not in audit


def test_an_error_report_masks_the_path(team: SimpleNamespace) -> None:
    from django.test import RequestFactory

    from apps.core.debug import AlwaysSafeExceptionReporterFilter

    token = WITNESS
    request = RequestFactory().get(f"/ical/{token}.ics")
    text = str(AlwaysSafeExceptionReporterFilter().get_safe_request_meta(request))
    text += AlwaysSafeExceptionReporterFilter().masked_path(request)
    assert token not in text


@pytest.mark.serial
def test_the_runserver_request_line_masks_the_token(
    team: SimpleNamespace, live_server: Any, caplog: pytest.LogCaptureFixture
) -> None:
    import contextlib
    import urllib.error
    import urllib.request

    caplog.set_level(logging.INFO, logger="django.server")
    _feed, token = h.new_feed(team.director, team.thesis)
    for path in (h.feed_url(token), "/ical/" + "z" * 43 + ".ics"):
        with contextlib.suppress(urllib.error.HTTPError):
            urllib.request.urlopen(live_server.url + path, timeout=5)  # noqa: S310  # nosec B310
    captured = " ".join(r.getMessage() for r in caplog.records)
    assert token not in captured and "z" * 43 not in captured
