"""Two worlds on every GET route of the documents (M2, SR-06): the list, the
upload page, each shared document's page and the download of its version. A
hidden document (page, download) answers like an unknown id. Method:
tests/two_worlds.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.permissions import Action
from apps.accounts.roles import Role
from apps.core.confidentiality import ConfidentialityLevel as L
from tests import two_worlds as w

pytestmark = pytest.mark.django_db(transaction=True)


def _version(document: Any) -> Any:
    from apps.vault.models import DocumentVersion

    return DocumentVersion.objects.get(document=document, version=1)


def _surfaces(team: SimpleNamespace, shared: w.Shared) -> list[str]:
    out = [w.url(team, "vault:documents"), w.url(team, "vault:upload")]
    for d in shared.documents.values():
        out.append(w.url(team, "vault:document", document_id=d.pk))
        out.append(w.url(team, "vault:download", version_id=_version(d).pk))
    return out


def _expected(team: SimpleNamespace, shared: w.Shared, role: Role) -> list[tuple[str, str]]:
    public = shared.documents[L.PUBLIC]
    return [
        (w.url(team, "vault:documents"), w.url(team, "vault:document", document_id=public.pk)),
        (w.url(team, "vault:download", version_id=_version(public).pk), "%PDF-1.7"),
    ]


def _probes(team: SimpleNamespace, hidden: w.Hidden) -> list[tuple[str, str]]:
    out = [
        (w.url(team, "vault:document", document_id=d.pk),
         w.unknown(team, "vault:document", "document_id"))
        for d in hidden.documents
    ]  # fmt: skip
    out += [
        (w.url(team, "vault:download", version_id=v.pk),
         w.unknown(team, "vault:download", "version_id"))
        for v in hidden.versions
    ]  # fmt: skip
    return out


MODULE = w.Module("vault", Action.DOCUMENT_READ, _surfaces, _expected, _probes)


@pytest.mark.parametrize("role", w.ROLES, ids=w.ROLE_IDS)
def test_a_role_cannot_tell_the_worlds_apart(role: Role, time_machine: Any) -> None:
    w.run(MODULE, role, time_machine)
