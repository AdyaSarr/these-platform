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

    def masked_path(self, request: HttpRequest) -> str:
        """The request's path, the token of an iCal feed masked (M12)."""
        from .logging import mask

        return str(mask(request.path))

    def get_safe_request_meta(self, request: HttpRequest) -> dict[str, object]:
        """META with the token of an iCal feed masked in every value (M12)."""
        from .logging import mask

        meta = super().get_safe_request_meta(request)
        return {key: mask(value) for key, value in meta.items()}
