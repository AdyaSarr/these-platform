"""Labels of the documents a viewer reads: a title that repeats is numbered,
« Justificatif 2026-07-14 (2) », in the order the documents were stored.

Counted among the documents THIS viewer may read (vault.services.readable_documents,
the list's own rule), never among all of them: a « (2) » must not tell that a hidden
document has the same title. Only a number is added: nothing of the hidden part.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def numbered_titles(documents: Iterable[Any]) -> dict[Any, str]:
    """{document pk: label}; a label never equals another title of the set."""
    documents = list(documents)
    taken = {d.title for d in documents}
    groups: dict[str, list[Any]] = defaultdict(list)
    for document in documents:
        groups[document.title].append(document)
    labels: dict[Any, str] = {}
    for title, same in groups.items():
        same.sort(key=lambda d: (d.created_at, str(d.pk)))
        labels[same[0].pk] = title
        number = 1
        for document in same[1:]:
            number += 1
            while f"{title} ({number})" in taken:
                number += 1
            label = f"{title} ({number})"
            taken.add(label)
            labels[document.pk] = label
    return labels


# Level badges: a word and an icon (ui/badge.html), the tone only adds to them.
LEVEL_TONES = {
    "PUBLIC": "neutral",
    "INTERNAL_LIRMM": "info",
    "INTERNAL_ST": "info",
    "CONFIDENTIAL": "warning",
}
