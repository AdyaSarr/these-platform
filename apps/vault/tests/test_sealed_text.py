"""vault.services, sealed texts: sealing(), open_texts(), padding, AD binding, rights, audit."""

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import crypto, formats, keys, services
from apps.vault.audit import AuditAction, audit
from apps.vault.errors import (
    KekRetirementError,
    LockOrderError,
    UnknownKekError,
    VaultIntegrityError,
)
from apps.vault.formats import KeyDomain
from apps.vault.models import AuditEvent, SealedText

# transaction=True: every service call commits, as in production (no
# ATOMIC_REQUESTS). In one enclosing test transaction, the audit() of a first
# call would precede the KEK lock of the next one, which the lock order forbids.
pytestmark = pytest.mark.django_db(transaction=True)

OBJECTIVE = "objectives.Objective"
OVERHEAD = 1 + 12 + 16  # format byte, nonce, tag


@dataclass
class _Meta:
    label: str


@dataclass
class Fake:
    """Stands for a business object: the vault reads pk, thesis_id and _meta.label only."""

    thesis_id: uuid.UUID
    label: str = OBJECTIVE
    pk: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def _meta(self) -> _Meta:
        return _Meta(self.label)


def _marker() -> str:
    return "TEMOIN-" + secrets.token_hex(8)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create()

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(thesis=thesis, role=role, **kwargs).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(),
    )


def _obj(team: SimpleNamespace, label: str = OBJECTIVE) -> Fake:
    return Fake(thesis_id=team.thesis.pk, label=label)


def _seal(
    team: SimpleNamespace,
    obj: Fake,
    name: str,
    text: str,
    *,
    level: L = L.PUBLIC,
    user: Any = None,
    action: Action = Action.OBJECTIVE_WRITE,
) -> None:
    with services.sealing(
        user=user or team.student, thesis=team.thesis, level=level, action=action
    ) as sealer:
        sealer.seal(obj, name, text)


def _open(
    team: SimpleNamespace,
    user: Any,
    *items: tuple[Fake, str],
    action: Action = Action.OBJECTIVE_READ,
) -> dict[tuple[uuid.UUID, str], Any]:
    return services.open_texts(user, team.thesis, items, action=action)


def _row(obj: Fake, name: str = "title") -> SealedText:
    return SealedText.objects.get(object_id=obj.pk, slot=formats.slot_code(f"{obj.label}.{name}"))


def _events(action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action.value).order_by("seq"))


# --------------------------------------------------------------------------- round trip


def test_round_trip(team: SimpleNamespace) -> None:
    obj = _obj(team)
    with services.sealing(
        user=team.student, thesis=team.thesis, level=L.INTERNAL_LIRMM, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        sealer.seal(obj, "title", "Caractériser la fuite par canal auxiliaire (fictif)")
        sealer.seal(obj, "description", "Ligne 1\nLigne 2\t— accents : éèàç, emoji 🔐")
    opened = _open(team, team.student, (obj, "title"), (obj, "description"))
    assert opened == {
        (obj.pk, "title"): "Caractériser la fuite par canal auxiliaire (fictif)",
        (obj.pk, "description"): "Ligne 1\nLigne 2\t— accents : éèàç, emoji 🔐",
    }
    row = _row(obj)
    assert row.thesis_id == team.thesis.pk
    assert row.level == L.INTERNAL_LIRMM
    assert row.kek_id == "env:INTERNAL_LIRMM:1"
    assert row.created_by_id == team.student.pk


def test_empty_text_is_sealed_too(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "")
    assert len(_row(obj).ciphertext) == OVERHEAD + 64
    assert _open(team, team.student, (obj, "title")) == {(obj.pk, "title"): ""}


def test_missing_text_is_absent_from_the_result(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    assert _open(team, team.student, (obj, "title"), (obj, "description")) == {
        (obj.pk, "title"): "Titre fictif"
    }
    assert services.open_text(team.student, obj, "description", action=Action.OBJECTIVE_READ) == ""
    assert (
        services.open_text(team.student, obj, "title", action=Action.OBJECTIVE_READ)
        == "Titre fictif"
    )


def test_open_texts_without_items_returns_nothing(team: SimpleNamespace) -> None:
    assert _open(team, team.student) == {}


def test_replacing_keeps_one_row_under_a_fresh_dek(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Premier titre fictif")
    first = _row(obj)
    _seal(team, obj, "title", "Premier titre fictif")  # same text again
    second = _row(obj)
    assert SealedText.objects.count() == 1
    assert second.pk == first.pk
    assert bytes(second.wrapped_dek) != bytes(first.wrapped_dek)
    assert bytes(second.ciphertext) != bytes(first.ciphertext)


def test_same_text_in_two_objects_gives_unrelated_ciphertexts(team: SimpleNamespace) -> None:
    a, b = _obj(team), _obj(team)
    _seal(team, a, "title", "Titre identique fictif")
    _seal(team, b, "title", "Titre identique fictif")
    assert bytes(_row(a).ciphertext)[1:13] != bytes(_row(b).ciphertext)[1:13]  # nonces
    assert bytes(_row(a).ciphertext) != bytes(_row(b).ciphertext)


def test_erase(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    with services.sealing(
        user=team.student, thesis=team.thesis, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        assert sealer.erase(obj, "title") is True
        assert sealer.erase(obj, "title") is False
    assert not SealedText.objects.exists()


# --------------------------------------------------------------------------- padding


def test_titles_of_5_and_60_bytes_have_ciphertexts_of_equal_length(team: SimpleNamespace) -> None:
    short, long_ = _obj(team), _obj(team)
    _seal(team, short, "title", "a" * 5)
    _seal(team, long_, "title", "b" * 60)
    assert len(_row(short).ciphertext) == len(_row(long_).ciphertext) == OVERHEAD + 64


@pytest.mark.parametrize(("length", "padded"), [(63, 64), (64, 128), (200, 256)])
def test_ciphertext_length_reveals_only_the_64_byte_bucket(
    team: SimpleNamespace, length: int, padded: int
) -> None:
    obj = _obj(team)
    _seal(team, obj, "description", "x" * length)
    assert len(_row(obj, "description").ciphertext) == OVERHEAD + padded


def _reseal_raw(row: SealedText, plaintext: bytes) -> None:
    """Re-encrypt ``plaintext`` AS IS (no padding added) under the row's own DEK and AD."""
    level = L(row.level)
    dek = keys.unwrap(
        row.kek_id,
        bytes(row.wrapped_dek),
        formats.text_dek_wrap_ad(
            thesis_id=row.thesis_id,
            object_id=row.object_id,
            slot=row.slot,
            level=level,
            kek_id=row.kek_id,
        ),
    )
    ad = formats.text_ad(
        thesis_id=row.thesis_id, object_id=row.object_id, slot=row.slot, level=level
    )
    sealed = formats.pack_blob(crypto.aead_encrypt(dek, plaintext, ad))
    SealedText.objects.filter(pk=row.pk).update(ciphertext=sealed)


@pytest.mark.parametrize(
    "plaintext",
    [
        b"abc\x81" + b"\x00" * 60,  # wrong marker
        b"abc\x80" + b"\x00" * 59 + b"\x01",  # garbage after the marker
        b"a" * 64,  # no padding at all
        b"\x80" + b"\x00" * 127,  # 128 bytes of padding: never produced
    ],
)
def test_altered_padding_is_refused_and_audited(team: SimpleNamespace, plaintext: bytes) -> None:
    """Authentic AEAD (re-encrypted with the right DEK and AD), malformed padding."""
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    _reseal_raw(_row(obj), plaintext)
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (obj, "title"))
    (event,) = _events(AuditAction.SEALED_TEXT_INTEGRITY_FAILED)
    assert event.details_json == {
        "slot": "objectives.Objective.title",
        "object_ids": [str(obj.pk)],
        "error": "VaultIntegrityError",
    }


def test_padding_with_a_valid_marker_still_opens(team: SimpleNamespace) -> None:
    """Control for the test above: the same re-encryption with good padding opens."""
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    _reseal_raw(_row(obj), formats.pad_text(b"Autre titre"))
    assert _open(team, team.student, (obj, "title")) == {(obj.pk, "title"): "Autre titre"}


# --------------------------------------------------------------------------- AD binding


def _transplant(source: SealedText, target: SealedText) -> None:
    SealedText.objects.filter(pk=target.pk).update(
        ciphertext=source.ciphertext, wrapped_dek=source.wrapped_dek, kek_id=source.kek_id
    )


def test_ciphertext_moved_to_another_object_is_refused(team: SimpleNamespace) -> None:
    a, b = _obj(team), _obj(team)
    _seal(team, a, "title", "Titre A fictif")
    _seal(team, b, "title", "Titre B fictif")
    _transplant(_row(a), _row(b))
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (b, "title"))


def test_ciphertext_moved_to_another_field_is_refused(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    _seal(team, obj, "description", "Description fictive")
    _transplant(_row(obj, "title"), _row(obj, "description"))
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (obj, "description"))


def test_ciphertext_moved_to_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    other_student = MembershipFactory.create(thesis=other, role=Role.PHD_STUDENT).user
    mine, theirs = _obj(team), Fake(thesis_id=other.pk)
    _seal(team, mine, "title", "Titre fictif")
    with services.sealing(
        user=other_student, thesis=other, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        sealer.seal(theirs, "title", "Titre de l'autre thèse")
    _transplant(_row(theirs), _row(mine))
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (mine, "title"))


def test_row_moved_to_another_object_id_is_refused(team: SimpleNamespace) -> None:
    obj, elsewhere = _obj(team), _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    SealedText.objects.filter(pk=_row(obj).pk).update(object_id=elsewhere.pk)
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (elsewhere, "title"))


def test_level_rewritten_in_the_database_is_refused(team: SimpleNamespace) -> None:
    """Level and kek_id rewritten together (the constraint forbids one alone)."""
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif", level=L.INTERNAL_LIRMM)
    SealedText.objects.filter(pk=_row(obj).pk).update(
        level=L.CONFIDENTIAL, kek_id="env:CONFIDENTIAL:1"
    )
    with pytest.raises(VaultIntegrityError):
        _open(team, team.student, (obj, "title"))


def test_unknown_kek_is_audited_apart(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    SealedText.objects.filter(pk=_row(obj).pk).update(kek_id="env:PUBLIC:9")
    with pytest.raises(UnknownKekError):
        _open(team, team.student, (obj, "title"))
    (event,) = _events(AuditAction.SEALED_TEXT_KEK_UNAVAILABLE)
    assert event.details_json["error"] == "UnknownKekError"
    assert not _events(AuditAction.SEALED_TEXT_INTEGRITY_FAILED)


# --------------------------------------------------------------------------- rights: seal


@pytest.mark.parametrize("role", ["director", "co_supervisor", "tutor", "csi", "guest", "outsider"])
def test_only_the_write_right_seals_an_objective(team: SimpleNamespace, role: str) -> None:
    user = getattr(team, role)
    with pytest.raises(PermissionDenied):
        _seal(team, _obj(team), "title", "Titre fictif", user=user)
    assert not SealedText.objects.exists()
    (event,) = _events(AuditAction.SEALED_TEXT_ACCESS_DENIED)
    assert event.actor_id == user.pk
    assert event.details_json == {"operation": "seal", "level": "PUBLIC"}


def test_supervisor_seals_a_comment_with_the_comment_right(team: SimpleNamespace) -> None:
    obj = _obj(team, "objectives.ObjectiveComment")
    _seal(
        team, obj, "body", "Commentaire fictif", user=team.director, action=Action.OBJECTIVE_COMMENT
    )
    assert services.open_text(team.director, obj, "body", action=Action.OBJECTIVE_COMMENT) == (
        "Commentaire fictif"
    )


@pytest.mark.parametrize(
    ("role", "level"), [("tutor", L.INTERNAL_LIRMM), ("co_supervisor", L.CONFIDENTIAL)]
)
def test_nobody_seals_at_a_level_they_cannot_read(
    team: SimpleNamespace, role: str, level: L
) -> None:
    obj = _obj(team, "objectives.ObjectiveComment")
    with pytest.raises(PermissionDenied):
        _seal(
            team,
            obj,
            "body",
            "Fictif",
            user=getattr(team, role),
            level=level,
            action=Action.OBJECTIVE_COMMENT,
        )
    assert not SealedText.objects.exists()
    assert _events(AuditAction.SEALED_TEXT_ACCESS_DENIED)


def test_object_of_another_thesis_is_refused_by_the_sealer(team: SimpleNamespace) -> None:
    stranger = Fake(thesis_id=ThesisFactory.create().pk)
    with pytest.raises(ValueError, match="another thesis"):
        _seal(team, stranger, "title", "Fictif")
    assert not SealedText.objects.exists()


@pytest.mark.parametrize(
    ("label", "name"),
    [
        (OBJECTIVE, "weight"),  # not a text slot
        ("objectives.Unknown", "title"),
        ("objectives.objective", "title"),  # label_lower is not the label
    ],
)
def test_unknown_slot_is_refused(team: SimpleNamespace, label: str, name: str) -> None:
    with pytest.raises(ValueError, match="no text slot"):
        _seal(team, _obj(team, label), name, "Fictif")


def test_object_without_a_uuid_pk_is_refused(team: SimpleNamespace) -> None:
    obj = _obj(team)
    obj.pk = 42  # type: ignore[assignment]
    with pytest.raises(TypeError):
        _seal(team, obj, "title", "Fictif")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("avec\x00nul", ValueError),
        ("surrogate \ud800 seul", ValueError),
        ("é" * 8_192, ValueError),  # 16 384 bytes in UTF-8: one too many
        (b"bytes", TypeError),
        (None, TypeError),
    ],
)
def test_invalid_text_is_refused_and_nothing_is_stored(
    team: SimpleNamespace, text: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _seal(team, _obj(team), "title", text)
    assert not SealedText.objects.exists()


def test_largest_text_is_accepted(team: SimpleNamespace) -> None:
    obj = _obj(team)
    text = "x" + "é" * 8_191  # 16 383 bytes
    _seal(team, obj, "description", text)
    assert len(_row(obj, "description").ciphertext) == OVERHEAD + 16_384
    assert (
        services.open_text(team.student, obj, "description", action=Action.OBJECTIVE_READ) == text
    )


# --------------------------------------------------------------------------- rights: open


@pytest.fixture
def levels(team: SimpleNamespace) -> dict[L, Fake]:
    objects = {level: _obj(team) for level in (L.PUBLIC, L.INTERNAL_LIRMM, L.CONFIDENTIAL)}
    for level, obj in objects.items():
        _seal(team, obj, "title", f"Titre {level.value} fictif", level=level)
    return objects


@pytest.mark.parametrize(
    ("role", "readable"),
    [
        ("student", {L.PUBLIC, L.INTERNAL_LIRMM, L.CONFIDENTIAL}),
        ("director", {L.PUBLIC, L.INTERNAL_LIRMM, L.CONFIDENTIAL}),
        ("co_supervisor", {L.PUBLIC, L.INTERNAL_LIRMM}),
        ("tutor", {L.PUBLIC, L.CONFIDENTIAL}),
        ("csi", {L.PUBLIC}),
    ],
)
def test_levels_a_role_cannot_read_come_back_restricted(
    team: SimpleNamespace, levels: dict[L, Fake], role: str, readable: set[L]
) -> None:
    opened = _open(team, getattr(team, role), *[(obj, "title") for obj in levels.values()])
    for level, obj in levels.items():
        expected = f"Titre {level.value} fictif" if level in readable else services.RESTRICTED
        assert opened[(obj.pk, "title")] == expected, level


@pytest.mark.parametrize("role", ["guest", "outsider"])
def test_without_the_read_right_nothing_is_opened(
    team: SimpleNamespace, levels: dict[L, Fake], role: str
) -> None:
    with pytest.raises(PermissionDenied):
        _open(team, getattr(team, role), (levels[L.PUBLIC], "title"))
    (event,) = _events(AuditAction.SEALED_TEXT_ACCESS_DENIED)
    assert event.details_json == {"operation": "read"}


def test_csi_cannot_open_comments(team: SimpleNamespace) -> None:
    """OBJECTIVE_READ is not OBJECTIVE_COMMENT: the comment thread stays closed."""
    comment = _obj(team, "objectives.ObjectiveComment")
    _seal(
        team,
        comment,
        "body",
        "Commentaire interne fictif",
        user=team.director,
        action=Action.OBJECTIVE_COMMENT,
    )
    with pytest.raises(PermissionDenied):
        _open(team, team.csi, (comment, "body"), action=Action.OBJECTIVE_COMMENT)
    assert _events(AuditAction.SEALED_TEXT_ACCESS_DENIED)


def test_object_of_another_thesis_is_refused_and_audited(team: SimpleNamespace) -> None:
    stranger = Fake(thesis_id=ThesisFactory.create().pk)
    with pytest.raises(PermissionDenied):
        _open(team, team.student, (stranger, "title"))
    (event,) = _events(AuditAction.SEALED_TEXT_ACCESS_DENIED)
    assert event.details_json == {"operation": "read", "reason": "other_thesis"}


# --------------------------------------------------------------------------- audit of reads


def test_confidential_reads_are_audited_once_per_call(
    team: SimpleNamespace, levels: dict[L, Fake]
) -> None:
    confidential = levels[L.CONFIDENTIAL]
    _seal(team, confidential, "description", "Description confidentielle", level=L.CONFIDENTIAL)
    opened = _open(
        team,
        team.director,
        (confidential, "title"),
        (confidential, "description"),
        (levels[L.PUBLIC], "title"),
        (levels[L.INTERNAL_LIRMM], "title"),
    )
    assert len(opened) == 4
    (event,) = _events(AuditAction.SEALED_TEXT_READ)
    assert event.actor_id == team.director.pk
    assert event.thesis_id == team.thesis.pk
    assert event.details_json == {
        "level": "CONFIDENTIAL",
        "count": 2,
        "slots": ["objectives.Objective.description", "objectives.Objective.title"],
        "object_ids": [str(confidential.pk)],
    }


def test_reads_below_confidential_are_not_audited(
    team: SimpleNamespace, levels: dict[L, Fake]
) -> None:
    _open(team, team.director, (levels[L.PUBLIC], "title"), (levels[L.INTERNAL_LIRMM], "title"))
    assert not _events(AuditAction.SEALED_TEXT_READ)


def test_a_restricted_confidential_text_is_not_a_read(
    team: SimpleNamespace, levels: dict[L, Fake]
) -> None:
    _open(team, team.co_supervisor, (levels[L.CONFIDENTIAL], "title"))
    assert not _events(AuditAction.SEALED_TEXT_READ)


def test_no_byte_of_a_text_in_the_database_or_the_audit_log(team: SimpleNamespace) -> None:
    marker = _marker()
    obj = _obj(team)
    _seal(team, obj, "title", f"{marker} titre", level=L.CONFIDENTIAL)
    _open(team, team.director, (obj, "title"))
    with pytest.raises(PermissionDenied):
        _seal(team, obj, "title", marker, user=team.director)
    with connection.cursor() as cursor:
        cursor.execute("SELECT row_to_json(t)::text FROM vault_sealedtext t")
        rows = [r[0] for r in cursor.fetchall()]
        cursor.execute("SELECT ciphertext, wrapped_dek FROM vault_sealedtext")
        raw = [bytes(b) for r in cursor.fetchall() for b in r]
        cursor.execute("SELECT row_to_json(t)::text FROM vault_auditevent t")
        events = [r[0] for r in cursor.fetchall()]
    assert rows and raw and events
    for blob in [*rows, *events]:
        assert marker not in blob and "titre" not in blob
    for blob in raw:
        assert marker.encode() not in blob


# --------------------------------------------------------------------------- locks


def test_sealing_after_audit_in_the_same_transaction_is_refused(team: SimpleNamespace) -> None:
    with pytest.raises(LockOrderError), transaction.atomic():
        audit(AuditAction.THESIS_CREATED, actor=team.student, thesis=team.thesis)
        with services.sealing(
            user=team.student, thesis=team.thesis, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
        ):
            pass


def test_seal_after_audit_inside_the_block_is_refused(team: SimpleNamespace) -> None:
    """The KEK lock is already held, so keys.wrap would not refuse: the sealer does."""
    with (
        pytest.raises(LockOrderError),
        services.sealing(
            user=team.student, thesis=team.thesis, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
        ) as sealer,
    ):
        sealer.seal(_obj(team), "title", "Avant l'audit")
        audit(AuditAction.THESIS_CREATED, actor=team.student, thesis=team.thesis)
        sealer.seal(_obj(team), "description", "Après l'audit")
    assert not SealedText.objects.exists()  # the whole transaction rolled back


def test_the_sealer_is_dead_after_its_block(team: SimpleNamespace) -> None:
    with services.sealing(
        user=team.student, thesis=team.thesis, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        sealer.seal(_obj(team), "title", "Trop tard")


def test_an_exception_rolls_every_seal_back(team: SimpleNamespace) -> None:
    with (
        pytest.raises(ZeroDivisionError),
        services.sealing(
            user=team.student, thesis=team.thesis, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
        ) as sealer,
    ):
        sealer.seal(_obj(team), "title", "Fictif")
        _ = 1 / 0
    assert not SealedText.objects.exists()


# --------------------------------------------------------------------------- level change


def test_reseal_moves_a_text_to_the_sealer_level(team: SimpleNamespace) -> None:
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif", level=L.PUBLIC)
    before = _row(obj)
    with services.sealing(
        user=team.student, thesis=team.thesis, level=L.CONFIDENTIAL, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        assert sealer.reseal(obj, "title") is True
        assert sealer.reseal(obj, "title") is False  # already there
        assert sealer.reseal(obj, "description") is False  # no text
    after = _row(obj)
    assert (after.level, after.kek_id) == (L.CONFIDENTIAL, "env:CONFIDENTIAL:1")
    assert bytes(after.wrapped_dek) != bytes(before.wrapped_dek)
    assert _open(team, team.student, (obj, "title")) == {(obj.pk, "title"): "Titre fictif"}
    assert _open(team, team.csi, (obj, "title")) == {(obj.pk, "title"): services.RESTRICTED}


# --------------------------------------------------------------------------- rotation


def test_rotation_rewraps_texts_and_retirement_waits_for_them(
    team: SimpleNamespace, settings: Any
) -> None:
    settings.VAULT_RETIRE_GRACE = timedelta(0)
    objects = [_obj(team) for _ in range(3)]
    for i, obj in enumerate(objects):
        _seal(team, obj, "title", f"Titre fictif {i}", level=L.CONFIDENTIAL)
    ciphertexts = {o.pk: bytes(_row(o).ciphertext) for o in objects}
    new = keys.new_version(KeyDomain.CONFIDENTIAL)
    with pytest.raises(KekRetirementError, match="referenced by 3"):
        keys.retire("env:CONFIDENTIAL:1")
    assert services.rewrap_batch(KeyDomain.CONFIDENTIAL, limit=2) == 2
    assert services.rewrap_batch(KeyDomain.CONFIDENTIAL, limit=2) == 1
    assert services.rewrap_batch(KeyDomain.CONFIDENTIAL, limit=2) == 0
    for i, obj in enumerate(objects):
        row = _row(obj)
        assert row.kek_id == new
        assert bytes(row.ciphertext) == ciphertexts[obj.pk]  # only the DEK is re-wrapped
        assert _open(team, team.student, (obj, "title"))[(obj.pk, "title")] == f"Titre fictif {i}"
    keys.retire("env:CONFIDENTIAL:1")


def test_rotation_of_one_domain_leaves_the_others(team: SimpleNamespace) -> None:
    public, lirmm = _obj(team), _obj(team)
    _seal(team, public, "title", "Public fictif", level=L.PUBLIC)
    _seal(team, lirmm, "title", "LIRMM fictif", level=L.INTERNAL_LIRMM)
    keys.new_version(KeyDomain.PUBLIC)
    assert services.rewrap_batch(KeyDomain.PUBLIC) == 1
    assert _row(public).kek_id == "env:PUBLIC:2"
    assert _row(lirmm).kek_id == "env:INTERNAL_LIRMM:1"


# --------------------------------------------------------------------------- database


def _valid_row(team: SimpleNamespace, **overrides: Any) -> SealedText:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "slot": 0x0101,
        "object_id": uuid.uuid4(),
        "level": L.PUBLIC,
        "kek_id": "env:PUBLIC:1",
        "wrapped_dek": secrets.token_bytes(60),
        "ciphertext": secrets.token_bytes(OVERHEAD + 64),
    }
    fields.update(overrides)
    return SealedText(**fields)


def test_a_valid_row_passes_the_constraints(team: SimpleNamespace) -> None:
    _valid_row(team).save()


@pytest.mark.parametrize(
    "overrides",
    [
        {"ciphertext": secrets.token_bytes(OVERHEAD + 63)},  # not padded
        {"ciphertext": secrets.token_bytes(OVERHEAD + 65)},
        {"ciphertext": secrets.token_bytes(OVERHEAD)},  # no padded block at all
        {"ciphertext": secrets.token_bytes(OVERHEAD + 16_384 + 64)},  # beyond the largest
        {"wrapped_dek": secrets.token_bytes(59)},
        {"kek_id": "env:CONFIDENTIAL:1"},  # KEK of another level
        {"kek_id": "env:MFA_SECRETS:1"},
        {"level": "SECRET"},
        {"slot": 0x0001},
        {"slot": 0x10000},
    ],
)
def test_constraints_refuse_malformed_rows(
    team: SimpleNamespace, overrides: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _valid_row(team, **overrides).save()


def test_one_row_per_slot_and_object(team: SimpleNamespace) -> None:
    object_id = uuid.uuid4()
    _valid_row(team, object_id=object_id).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _valid_row(team, object_id=object_id).save()


def test_str_shows_no_content(team: SimpleNamespace) -> None:
    row = _valid_row(team)
    assert str(row) == f"sealed text {row.pk}"


def test_sealed_texts_are_not_in_the_django_admin() -> None:
    assert SealedText not in admin.site._registry


@pytest.mark.parametrize(
    "func",
    [
        services.open_texts,
        services.open_text,
        services.TextSealer.seal,
        services.TextSealer.reseal,
        services._open_text_row,
        services._seal_text_row,
        services._rewrap_texts,
    ],
)
def test_functions_holding_a_plaintext_or_a_dek_hide_their_locals(func: Any) -> None:
    assert func.__code__.co_name == "sensitive_variables_wrapper"


def test_reseal_of_an_altered_text_rolls_back_and_is_audited(team: SimpleNamespace) -> None:
    obj, other = _obj(team), _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    _seal(team, other, "title", "Autre titre fictif")
    _transplant(_row(other), _row(obj))
    with (
        pytest.raises(VaultIntegrityError),
        services.sealing(
            user=team.student,
            thesis=team.thesis,
            level=L.CONFIDENTIAL,
            action=Action.OBJECTIVE_WRITE,
        ) as sealer,
    ):
        sealer.reseal(obj, "title")
    assert _row(obj).level == L.PUBLIC  # rolled back
    (event,) = _events(AuditAction.SEALED_TEXT_INTEGRITY_FAILED)
    assert event.details_json["object_ids"] == [str(obj.pk)]


# --------------------------------------------------------------------------- each AD on its own
#
# The transplant tests above move ciphertext AND wrapped DEK: the DEK-wrap AD
# refuses first, so they would pass even if the content AD bound nothing. Below,
# the OTHER layer is made consistent with the target, so that a single AD is
# left to refuse (mutation-tested: dropping the slot from either AD fails here).


def _dek_of(row: SealedText) -> bytes:
    level = L(row.level)
    ad = formats.text_dek_wrap_ad(
        thesis_id=row.thesis_id,
        object_id=row.object_id,
        slot=row.slot,
        level=level,
        kek_id=row.kek_id,
    )
    return keys.unwrap(row.kek_id, bytes(row.wrapped_dek), ad)


def _targets(team: SimpleNamespace, case: str) -> tuple[Fake, str, Fake, str, Any]:
    """(source obj, source field, target obj, target field, target thesis)."""
    source = _obj(team)
    _seal(team, source, "title", "Titre source fictif")
    if case == "field":
        _seal(team, source, "description", "Description cible fictive")
        return source, "title", source, "description", team.thesis
    if case == "object":
        target = _obj(team)
        _seal(team, target, "title", "Titre cible fictif")
        return source, "title", target, "title", team.thesis
    other = ThesisFactory.create()
    other_student = MembershipFactory.create(thesis=other, role=Role.PHD_STUDENT).user
    target = Fake(thesis_id=other.pk)
    with services.sealing(
        user=other_student, thesis=other, level=L.PUBLIC, action=Action.OBJECTIVE_WRITE
    ) as sealer:
        sealer.seal(target, "title", "Titre cible fictif")
    team.other_student = other_student
    return source, "title", target, "title", other


@pytest.mark.parametrize("case", ["field", "object", "thesis"])
def test_content_ad_alone_binds_the_slot_the_object_and_the_thesis(
    team: SimpleNamespace, case: str
) -> None:
    """Source DEK re-wrapped under the TARGET's wrap AD: only the content AD can refuse."""
    source, source_field, target, target_field, thesis = _targets(team, case)
    src, dst = _row(source, source_field), _row(target, target_field)
    dek = _dek_of(src)
    with transaction.atomic():
        kek_id, wrapped = keys.wrap(
            KeyDomain.PUBLIC,
            dek,
            lambda k: formats.text_dek_wrap_ad(
                thesis_id=dst.thesis_id,
                object_id=dst.object_id,
                slot=dst.slot,
                level=L.PUBLIC,
                kek_id=k,
            ),
        )
    SealedText.objects.filter(pk=dst.pk).update(
        ciphertext=src.ciphertext, wrapped_dek=wrapped, kek_id=kek_id
    )
    reader = team.student if case != "thesis" else team.other_student
    with pytest.raises(VaultIntegrityError):
        services.open_texts(reader, thesis, [(target, target_field)], action=Action.OBJECTIVE_READ)


@pytest.mark.parametrize("case", ["field", "object", "thesis"])
def test_wrap_ad_alone_binds_the_slot_the_object_and_the_thesis(
    team: SimpleNamespace, case: str
) -> None:
    """Source wrapped DEK kept, content re-encrypted under the TARGET's content AD with
    that DEK: only the DEK-wrap AD can refuse."""
    source, source_field, target, target_field, thesis = _targets(team, case)
    src, dst = _row(source, source_field), _row(target, target_field)
    content_ad = formats.text_ad(
        thesis_id=dst.thesis_id, object_id=dst.object_id, slot=dst.slot, level=L.PUBLIC
    )
    sealed = crypto.aead_encrypt(_dek_of(src), formats.pad_text(b"Texte forge"), content_ad)
    SealedText.objects.filter(pk=dst.pk).update(
        ciphertext=formats.pack_blob(sealed), wrapped_dek=src.wrapped_dek, kek_id=src.kek_id
    )
    reader = team.student if case != "thesis" else team.other_student
    with pytest.raises(VaultIntegrityError):
        services.open_texts(reader, thesis, [(target, target_field)], action=Action.OBJECTIVE_READ)


def test_the_isolation_harness_opens_when_nothing_is_moved(team: SimpleNamespace) -> None:
    """Control: the same re-wrap and re-encryption aimed at the row itself open fine."""
    obj = _obj(team)
    _seal(team, obj, "title", "Titre fictif")
    row = _row(obj)
    content_ad = formats.text_ad(
        thesis_id=row.thesis_id, object_id=row.object_id, slot=row.slot, level=L.PUBLIC
    )
    sealed = crypto.aead_encrypt(_dek_of(row), formats.pad_text(b"Texte forge"), content_ad)
    SealedText.objects.filter(pk=row.pk).update(ciphertext=formats.pack_blob(sealed))
    assert _open(team, team.student, (obj, "title")) == {(obj.pk, "title"): "Texte forge"}
