"""PDFs of the demo: built in memory, never read from disk."""

import re

import pytest

from apps.demo.pdf import PHRASE, fictional_pdf
from apps.vault.validation import validate_upload


def test_structure_and_offsets() -> None:
    data = fictional_pdf("Attestation d'emploi")
    assert data.startswith(b"%PDF-1.4\n")
    assert data.rstrip().endswith(b"%%EOF")
    startxref = int(re.search(rb"startxref\n(\d+)\n", data).group(1))  # type: ignore[union-attr]
    assert data[startxref:].startswith(b"xref\n0 6\n")
    entries = re.findall(rb"(\d{10}) 00000 n \n", data[startxref:])
    assert len(entries) == 5
    for number, offset in enumerate(entries, start=1):
        assert data[int(offset) :].startswith(f"{number} 0 obj".encode())
    length = int(re.search(rb"/Length (\d+)", data).group(1))  # type: ignore[union-attr]
    stream = data.split(b"stream\n", 1)[1]
    assert stream[length:].startswith(b"\nendstream")


def test_contains_the_phrase_and_the_title() -> None:
    data = fictional_pdf("Note (fictive)")
    assert PHRASE == "Document fictif de démonstration"
    assert PHRASE.encode("cp1252") in data
    assert rb"Note \(fictive\)" in data  # parentheses escaped


def test_accepted_by_the_vault_validation() -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    checked = validate_upload(SimpleUploadedFile("demo.pdf", fictional_pdf("Titre")))
    assert checked.mime_type == "application/pdf"


@pytest.mark.parametrize("title", ["", "x" * 121, "emoji 🙂", "back\\slash", "a\nb"])
def test_unexpected_titles_are_refused(title: str) -> None:
    with pytest.raises(ValueError):
        fictional_pdf(title)
