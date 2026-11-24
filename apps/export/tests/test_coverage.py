"""Every model of the project is either exported or deliberately left out (M13b,
criterion 15): a model added later fails here until it is classified."""

from __future__ import annotations

from django.apps import apps

from apps.export.coverage import EXPORTED, NOT_EXPORTED


def _project_models() -> set[str]:
    return {m._meta.label for m in apps.get_models() if m.__module__.startswith("apps.")}


def test_every_model_is_classified_once() -> None:
    assert set(EXPORTED) & set(NOT_EXPORTED) == set()
    assert set(EXPORTED) | set(NOT_EXPORTED) == _project_models()


def test_every_exclusion_says_why() -> None:
    assert all(reason.strip() for reason in NOT_EXPORTED.values())


def test_the_secrets_are_never_exported() -> None:
    for label in ("vault.KeyEncryptionKey", "vault.SealedText", "feeds.CalendarFeed",
                  "accounts.Invitation", "vault.AuditCheckpoint"):  # fmt: skip
        assert label in NOT_EXPORTED, label
