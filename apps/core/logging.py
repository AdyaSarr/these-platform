"""Logging filter: the token of a personal iCal feed (M12) never reaches a journal.

The feed's URL (/ical/<token>.ics) is a bearer secret. This filter, on every
handler (config.settings LOGGING), rewrites it as /ical/[jeton].ics in the
message, its arguments and the request a record may carry (django.request,
django.server). It never refuses a record.
"""

from __future__ import annotations

import logging
import re
from typing import Any

TOKEN_PATH = re.compile(r"/ical/[^/\s\"'?#]+?\.ics")
MASKED = "/ical/[jeton].ics"


def mask(value: Any) -> Any:
    if isinstance(value, str):
        return TOKEN_PATH.sub(MASKED, value)
    return value


class TokenPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(mask(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: mask(v) for k, v in record.args.items()}
        request = getattr(record, "request", None)
        if request is not None and TOKEN_PATH.search(getattr(request, "path", "") or ""):
            record.request = _MaskedRequest(request)
        return True


class _MaskedRequest:
    """The request a record carries, with its path masked (str() and .path)."""

    def __init__(self, request: Any) -> None:
        self._request = request
        self.path = mask(request.path)
        self.path_info = mask(getattr(request, "path_info", ""))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)

    def get_full_path(self) -> str:
        return MASKED

    def __str__(self) -> str:
        return f"<request {MASKED}>"
