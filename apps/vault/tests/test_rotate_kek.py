"""manage.py rotate_kek: new version and re-wrap, then --retire after the grace period."""

from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import keys, services
from apps.vault.errors import KekLockTimeoutError
from apps.vault.formats import KeyDomain
from apps.vault.models import AuditEvent, DocumentVersion, KeyEncryptionKey

# transaction=True: every service call commits, as in production (no
# ATOMIC_REQUESTS). In one enclosing test transaction, the audit() of a first
# call would precede the KEK lock of the next one, which the lock order forbids.
pytestmark = pytest.mark.django_db(transaction=True)

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
def student() -> Any:
    # Active before T0: the tests travel to T0, which must not precede the membership
    # (with active_from = now, they failed once the real clock passed T0).
    return MembershipFactory.create(
        thesis=ThesisFactory.create(), role=Role.PHD_STUDENT, active_from=T0 - timedelta(days=1)
    ).user


def _store(student: Any, n: int, level: L = L.CONFIDENTIAL) -> list[Any]:
    thesis = student.memberships.get().thesis
    return [
        services.store(
            thesis=thesis,
            upload=SimpleUploadedFile(f"d{i}.pdf", b"%PDF-1.7\n%fictif " + bytes([65 + i])),
            title=f"Document fictif {i}",
            level=level,
            user=student,
        )
        for i in range(n)
    ]


def _rotate(*args: str) -> str:
    out = StringIO()
    call_command("rotate_kek", *args, stdout=out)
    return out.getvalue()


def test_rotation_rewraps_every_document_in_batches(student: Any, time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    documents = _store(student, 5)
    out = _rotate("CONFIDENTIAL", "--batch", "2")
    assert "env:CONFIDENTIAL:2" in out
    assert set(DocumentVersion.objects.values_list("kek_id", flat=True)) == {"env:CONFIDENTIAL:2"}
    batches = AuditEvent.objects.filter(action="KEK_REWRAPPED")
    assert [e.details_json["rows"] for e in batches.order_by("seq")] == [2, 2, 1]
    for document in documents:
        version = document.versions.get()
        assert services.open_version(version.pk, student, recent_mfa=True).data.startswith(
            b"%PDF-1.7"
        )


def test_rotation_keeps_the_blobs_and_the_deks(student: Any) -> None:
    (document,) = _store(student, 1)
    before = document.versions.get()
    _rotate("CONFIDENTIAL")
    after = document.versions.get()
    assert after.ciphertext_path == before.ciphertext_path  # contents untouched
    assert bytes(after.wrapped_dek) != bytes(before.wrapped_dek)  # re-wrapped


def test_other_domains_are_not_touched(student: Any) -> None:
    _store(student, 1, L.PUBLIC)
    _rotate("CONFIDENTIAL")
    assert DocumentVersion.objects.get().kek_id == "env:PUBLIC:1"


def test_retire_waits_for_the_grace_period(student: Any, time_machine: Any) -> None:
    time_machine.move_to(T0, tick=False)
    (document,) = _store(student, 1)
    _rotate("CONFIDENTIAL")
    time_machine.move_to(T0 + timedelta(minutes=5), tick=False)
    with pytest.raises(CommandError, match="grace"):
        _rotate("CONFIDENTIAL", "--retire")
    assert KeyEncryptionKey.objects.get(kek_id="env:CONFIDENTIAL:1").retired_at is None
    time_machine.move_to(T0 + timedelta(minutes=11), tick=False)
    out = _rotate("CONFIDENTIAL", "--retire")
    assert "retired env:CONFIDENTIAL:1" in out
    assert KeyEncryptionKey.objects.get(kek_id="env:CONFIDENTIAL:1").retired_at is not None
    assert services.open_version(
        document.versions.get().pk, student, recent_mfa=True
    ).data.startswith(b"%PDF")


def test_retire_rewraps_rows_sealed_late_under_the_old_version(time_machine: Any) -> None:
    """A row stored under v1 after the rotation's batches (allauth's encrypt() runs
    outside a transaction) is re-wrapped by --retire before retiring v1."""
    time_machine.move_to(T0, tick=False)
    user = UserFactory.create(mfa=False)
    from allauth.mfa.models import Authenticator

    late = services.encrypt_secret("late secret")  # sealed under MFA_SECRETS v1
    _rotate("MFA_SECRETS")
    Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={"secret": late})
    time_machine.move_to(T0 + timedelta(minutes=11), tick=False)
    _rotate("MFA_SECRETS", "--retire")
    stored = Authenticator.objects.get(user=user).data["secret"]
    assert services.decrypt_secret(stored) == "late secret"
    assert KeyEncryptionKey.objects.get(kek_id="env:MFA_SECRETS:1").retired_at is not None


def test_mfa_rotation_reencrypts_the_secrets(time_machine: Any) -> None:
    from allauth.mfa.models import Authenticator

    user = UserFactory.create(mfa=False)
    totp = Authenticator.objects.create(
        user=user, type=Authenticator.Type.TOTP, data={"secret": services.encrypt_secret("t")}
    )
    codes = Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.RECOVERY_CODES,
        data={"seed": services.encrypt_secret("s"), "used_mask": 0},
    )
    _rotate("MFA_SECRETS")
    totp.refresh_from_db()
    codes.refresh_from_db()
    assert services.decrypt_secret(totp.data["secret"]) == "t"
    assert services.decrypt_secret(codes.data["seed"]) == "s"
    assert codes.data["used_mask"] == 0  # other fields kept
    from apps.vault import formats

    assert formats.unpack_secret(totp.data["secret"])[0] == "env:MFA_SECRETS:2"


def test_lock_timeouts_are_retried_with_a_growing_delay(
    student: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.vault.management.commands import rotate_kek

    sleeps: list[float] = []
    monkeypatch.setattr(rotate_kek, "_sleep", sleeps.append)
    real = keys.new_version
    failures = iter([True, True, False])

    def flaky(domain: KeyDomain, **kwargs: Any) -> str:
        if next(failures):
            raise KekLockTimeoutError("busy")
        return real(domain, **kwargs)

    monkeypatch.setattr(keys, "new_version", flaky)
    _rotate("CONFIDENTIAL")
    assert sleeps == [1, 2]
    assert KeyEncryptionKey.objects.filter(domain="CONFIDENTIAL").count() == 2


def test_gives_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.vault.management.commands import rotate_kek

    sleeps: list[float] = []
    monkeypatch.setattr(rotate_kek, "_sleep", sleeps.append)

    def busy(domain: KeyDomain, **kwargs: Any) -> str:
        raise KekLockTimeoutError("busy")

    monkeypatch.setattr(keys, "new_version", busy)
    with pytest.raises(CommandError, match="busy"):
        _rotate("CONFIDENTIAL")
    assert sleeps == [1, 2, 4, 8]


def test_clear_mfa_secret_stops_the_rotation_with_a_clear_message() -> None:
    from allauth.mfa.models import Authenticator

    user = UserFactory.create(mfa=False)
    Authenticator.objects.create(
        user=user,
        type=Authenticator.Type.TOTP,
        data={"secret": "JBSWY3DPEHPK3PXP"},  # pragma: allowlist secret
    )
    with pytest.raises(CommandError, match="not a vault-sealed value"):
        _rotate("MFA_SECRETS")
    stored = Authenticator.objects.get(user=user).data["secret"]
    assert stored == "JBSWY3DPEHPK3PXP"  # fictitious secret, left untouched


def test_unknown_domain_is_refused() -> None:
    with pytest.raises(CommandError):
        _rotate("SECRET")
