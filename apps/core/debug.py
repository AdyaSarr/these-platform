"""Error-report filter that hides sensitive values in every environment.

Django's ``SafeExceptionReporterFilter`` only acts when DEBUG is False: with
DEBUG=True, the technical 500 page would show the local variables of
``@sensitive_variables`` functions (keys, decrypted content) and the POST
parameters of ``@sensitive_post_parameters`` views. This subclass makes it act
always. Everything else (which settings are masked, the substitute) is Django's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.views.debug import SafeExceptionReporterFilter

if TYPE_CHECKING:
    from django.http import HttpRequest


class AlwaysSafeExceptionReporterFilter(SafeExceptionReporterFilter):
    def is_active(self, request: HttpRequest | None) -> bool:
        return True
