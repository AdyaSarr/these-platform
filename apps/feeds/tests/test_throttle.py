"""SR-02: behind the proxy, the limit per address counts each client apart (the
address is vault.audit.client_ip, the one allauth's limits use)."""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from apps.feeds import throttle

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_limits() -> None:
    cache.clear()


def _through_the_proxy(address: str) -> Any:
    return RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=address)


def test_behind_the_proxy_each_client_has_its_own_count(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
    monkeypatch.setattr(throttle, "PER_ADDRESS", 2)
    addresses = ["203.0.113.1"] * 3 + ["203.0.113.2"]
    answers = [
        throttle.over_limit(f"token-{n}", _through_the_proxy(a)) for n, a in enumerate(addresses)
    ]
    assert answers == [False, False, True, False]


def test_the_throttle_reads_the_shared_client_ip() -> None:
    from apps.vault.audit import client_ip

    assert vars(throttle)["client_ip"] is client_ip
