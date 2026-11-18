"""Rate limits of the feed (M12): 60 per hour per token (its fingerprint) and 300
per hour per address (its audit pseudonym), counted BEFORE the lookup, so a valid
token and an unknown one get the same answer. Fixed one-hour windows in the cache
(shared: Redis in production). The address is vault.audit.client_ip, the one of
allauth's limits and of the audit log (SR-02)."""

from __future__ import annotations

import time
from typing import Any

from django.core.cache import cache

from apps.vault import crypto
from apps.vault.audit import client_ip

PER_TOKEN = 60
PER_ADDRESS = 300
WINDOW = 3600


def _count(key: str) -> int:
    bucket = f"feeds:{key}:{int(time.time()) // WINDOW}"
    cache.add(bucket, 0, WINDOW)
    try:
        return int(cache.incr(bucket))
    except ValueError:  # expired between add and incr
        cache.set(bucket, 1, WINDOW)
        return 1


def over_limit(token_hash: str, request: Any) -> bool:
    ip = client_ip(request) or "-"
    address = crypto.pseudonym(ip, purpose="ip")
    by_token = _count(f"token:{token_hash}")
    by_address = _count(f"ip:{address}")
    return by_token > PER_TOKEN or by_address > PER_ADDRESS
