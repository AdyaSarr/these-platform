"""Error reports hide the locals of @sensitive_variables functions, DEBUG or not."""

import sys
import uuid
from types import TracebackType
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter

from apps.core.confidentiality import ConfidentialityLevel
from apps.core.debug import AlwaysSafeExceptionReporterFilter
from apps.vault import crypto
from apps.vault import formats as f
from apps.vault.errors import VaultIntegrityError


def _failed_decryption() -> tuple[bytes, tuple[type[BaseException], BaseException, TracebackType]]:
    fields: dict[str, Any] = {
        "thesis_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "version": 1,
        "level": ConfidentialityLevel.CONFIDENTIAL,
    }
    dek = crypto.new_dek()
    sealed = crypto.aead_encrypt(dek, b"fictitious content", f.content_ad(**fields))
    try:
        crypto.aead_decrypt(dek, sealed, f.content_ad(**(fields | {"version": 2})))
    except VaultIntegrityError:
        exc_type, exc, tb = sys.exc_info()
        assert exc_type is not None and exc is not None and tb is not None
        return dek, (exc_type, exc, tb)
    raise AssertionError("decryption should have failed")


def _crypto_frame_values(reporter: ExceptionReporter) -> list[str]:
    return [
        str(value)
        for frame in reporter.get_traceback_frames()
        if frame["filename"].endswith("vault/crypto.py")
        for _, value in frame["vars"]
    ]


def test_setting_points_to_the_always_safe_filter() -> None:
    assert django_settings.DEFAULT_EXCEPTION_REPORTER_FILTER == (
        "apps.core.debug.AlwaysSafeExceptionReporterFilter"
    )


@pytest.mark.parametrize("debug", [True, False])
def test_report_hides_crypto_locals(settings: Any, debug: bool) -> None:
    settings.DEBUG = debug
    dek, exc_info = _failed_decryption()
    reporter = ExceptionReporter(None, *exc_info)
    assert isinstance(reporter.filter, AlwaysSafeExceptionReporterFilter)
    values = _crypto_frame_values(reporter)
    assert values  # crypto frames were inspected
    substitute = reporter.filter.cleansed_substitute
    assert all(substitute in v for v in values)
    for leak in (repr(dek), dek.hex()):
        assert not any(leak in v for v in values)
    # Only crypto.py frames are guaranteed: the CALLER's frame (here the test,
    # later vault.services) must carry @sensitive_variables itself.


def test_django_default_filter_would_leak_with_debug(settings: Any) -> None:
    """Shows the test above bites: Django's own filter stays inactive with DEBUG=True."""
    settings.DEBUG = True
    dek, exc_info = _failed_decryption()
    reporter = ExceptionReporter(None, *exc_info)
    reporter.filter = SafeExceptionReporterFilter()
    assert any(repr(dek)[:20] in v for v in _crypto_frame_values(reporter))
