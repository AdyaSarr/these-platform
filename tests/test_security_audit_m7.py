"""Security audit of the travel module (M7), completed at the end-of-module review.

Merge gate: every GET page of M7 is covered by the two-worlds test of the presence
minimisation (apps/travel/tests/test_minimisation.py). Its DEFERRED list (pages not
rendered yet, each with a reason) may hold entries while the module is being
built; it must be empty before the merge into main: hence the merge_gate marker (left out
of the default run, `uv run pytest -m merge_gate` before the merge, CLAUDE.md).
"""

from __future__ import annotations

import pytest

from apps.travel.tests.test_minimisation import DEFERRED


@pytest.mark.merge_gate
def test_every_m7_page_is_covered_by_the_two_worlds_test() -> None:
    assert DEFERRED == {}, (
        "pages of M7 still outside the two-worlds test: render them in "
        f"test_minimisation._surfaces() before the merge: {sorted(DEFERRED)}"
    )
