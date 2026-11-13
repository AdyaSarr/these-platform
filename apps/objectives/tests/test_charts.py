"""objectives.charts: SVG coordinates computed by a pure function, rendered without
script or inline style (strict CSP), values escaped, with an accessible data table."""

import re
from collections.abc import Sequence
from fractions import Fraction

import pytest
from django.template.loader import render_to_string

from apps.objectives.charts import LineChart, line_chart

SERIES = [
    ("déc.", Fraction(0)),
    ("janv.", Fraction(1, 4)),
    ("févr.", Fraction(1, 2)),
    ("mars", Fraction(1)),
]


def test_coordinates_of_a_simple_series() -> None:
    chart = line_chart(SERIES, width=340, height=140)
    assert isinstance(chart, LineChart)
    # Plot box: left 40, right 340 - 16 = 324, top 16, bottom 140 - 32 = 108 (height 92).
    assert (chart.left, chart.right, chart.top, chart.bottom) == ("40.0", "324.0", "16.0", "108.0")
    xs = [p.x for p in chart.points]
    ys = [p.y for p in chart.points]
    assert xs == ["40.0", "134.7", "229.3", "324.0"]  # 284 / 3 apart, rounded half up
    assert ys == ["108.0", "85.0", "62.0", "16.0"]  # 108 - v * 92
    assert chart.segments == ("40.0,108.0 134.7,85.0 229.3,62.0 324.0,16.0",)
    assert [p.percent for p in chart.points] == [0, 25, 50, 100]


def test_a_missing_value_breaks_the_line() -> None:
    chart = line_chart(
        [("a", Fraction(1, 2)), ("b", None), ("c", Fraction(1, 2)), ("d", Fraction(1, 2))]
    )
    assert chart is not None
    assert len(chart.points) == 3  # no dot where there is no value
    assert len(chart.segments) == 1  # "a" alone draws no line; "c"-"d" does
    assert chart.table == [("a", 50), ("b", None), ("c", 50), ("d", 50)]


def test_a_single_point_is_centred() -> None:
    chart = line_chart([("mars", Fraction(3, 10))], width=340, height=140)
    assert chart is not None
    assert chart.points[0].x == "182.0"  # (40 + 324) / 2
    assert chart.segments == ()


def test_no_value_at_all_gives_no_chart() -> None:
    assert line_chart([]) is None
    assert line_chart([("déc.", None), ("janv.", None)]) is None


@pytest.mark.parametrize("value", [Fraction(-1, 100), Fraction(101, 100)])
def test_values_outside_0_and_1_are_refused(value: Fraction) -> None:
    with pytest.raises(ValueError):
        line_chart([("x", value)])


def test_y_ticks_every_quarter() -> None:
    chart = line_chart(SERIES, width=340, height=140)
    assert chart is not None
    assert [(t.y, t.label) for t in chart.y_ticks] == [
        ("108.0", "0 %"),
        ("85.0", "25 %"),
        ("62.0", "50 %"),
        ("39.0", "75 %"),
        ("16.0", "100 %"),
    ]


# --------------------------------------------------------------------------- rendering


def _render(series: Sequence[tuple[str, Fraction | None]]) -> str:
    return render_to_string("objectives/_progress_chart.html", {"chart": line_chart(series)})


def test_rendered_without_script_or_inline_style() -> None:
    html = _render(SERIES)
    assert "<svg" in html and "<polyline" in html
    assert not re.search(r"<script\b", html, re.IGNORECASE)
    assert not re.search(r"""\s(style|on[a-z]+)\s*=""", html, re.IGNORECASE)
    assert "<style" not in html.lower()


def test_labels_are_escaped() -> None:
    html = _render([("<script>alert(1)</script>", Fraction(1, 2)), ('"><img src=x>', Fraction(1))])
    assert "<script>alert" not in html and "<img src=x>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;&lt;img src=x&gt;" in html


def test_an_accessible_title_and_data_table() -> None:
    html = _render(SERIES)
    assert 'role="img"' in html and "aria-labelledby=" in html
    assert re.search(r"<title id=\"[^\"]+\">", html) and "<desc id=" in html
    assert "<caption>" in html
    assert html.count('<th scope="row">') == 4
    assert html.count('<th scope="col">') == 2
    for label, percent in (
        ("déc.", "0 %"),
        ("janv.", "25 %"),
        ("févr.", "50 %"),
        ("mars", "100 %"),
    ):
        assert f'<th scope="row">{label}</th>' in html
        assert f"<td>{percent}</td>" in html


def test_a_missing_value_is_a_dash_in_the_table() -> None:
    html = _render([("déc.", None), ("janv.", Fraction(1, 2))])
    assert "<td>—</td>" in html


def test_no_data_message() -> None:
    html = render_to_string("objectives/_progress_chart.html", {"chart": None})
    assert "<svg" not in html
    assert html.strip()
