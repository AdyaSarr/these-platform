"""The objectives chart never cuts a label (docs/ui-notes.md: « nov. 20: »).

Each month label is centred on its point; each percent label ends 6 units left of
the plot. With a prudent width per character (0.62 em of the 12-unit font), every
label lies inside the viewBox, for the longest French abbreviations.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from apps.objectives.charts import line_chart

CHAR = 12 * Fraction(62, 100)  # font-size 12 (.chart-tick), a prudent advance per character
MONTHS = ["sept. 2026", "oct. 2026", "nov. 2026", "déc. 2026", "janv. 2027", "févr. 2027",
          "mars 2027", "avr. 2027", "mai 2027", "juin 2027", "juil. 2027", "août 2027"]  # fmt: skip


@pytest.mark.parametrize("count", [1, 2, 6, 12])
def test_every_label_fits_in_the_view_box(count: int) -> None:
    series = [(label, Fraction(1, 2)) for label in MONTHS[:count]]
    chart = line_chart(series)
    assert chart is not None
    for label in chart.x_labels:
        half = len(label.label) * CHAR / 2
        assert Fraction(label.x) - half >= 0, label
        assert Fraction(label.x) + half <= chart.width, label
    for tick in chart.y_ticks:
        assert Fraction(chart.left) - 6 - len(tick.label) * CHAR >= 0, tick
