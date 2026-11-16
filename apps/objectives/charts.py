"""SVG line chart of the monthly progress: coordinates computed here (pure, exact).

The template (objectives/_progress_chart.html) only places these strings: no
script, no inline style (strict CSP, docs/decisions.md); colours come from CSS
classes. Labels are plain text, escaped by the template. Coordinates are
rounded half up to one decimal, from Fractions (no float drift).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from .stats import as_percent

MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 40, 16, 16, 32
TICKS = (Fraction(0), Fraction(1, 4), Fraction(1, 2), Fraction(3, 4), Fraction(1))


def _fmt(value: Fraction) -> str:
    """One decimal, half up: Fraction(1363, 10) -> "136.3"."""
    if value < 0:
        raise ValueError("coordinates are never negative")
    tenths = value * 10 + Fraction(1, 2)
    q = tenths.numerator // tenths.denominator
    return f"{q // 10}.{q % 10}"


@dataclass(frozen=True)
class ChartPoint:
    x: str
    y: str
    label: str
    percent: int


@dataclass(frozen=True)
class Tick:
    y: str
    label: str


@dataclass(frozen=True)
class XLabel:
    x: str
    label: str


@dataclass(frozen=True)
class LineChart:
    width: int
    height: int
    left: str
    right: str
    top: str
    bottom: str
    label_y: str  # baseline of the month labels
    segments: tuple[str, ...]  # "x,y x,y ..." for each run of consecutive values
    points: tuple[ChartPoint, ...]
    y_ticks: tuple[Tick, ...]
    x_labels: tuple[XLabel, ...]
    table: list[tuple[str, int | None]] = field(default_factory=list)


def line_chart(
    series: Sequence[tuple[str, Fraction | None]], *, width: int = 600, height: int = 240
) -> LineChart | None:
    """Chart of ``series`` = [(label, value in [0, 1] or None)]; None if no value at all."""
    if not any(value is not None for _label, value in series):
        return None
    for _label, value in series:
        if value is not None and not 0 <= value <= 1:
            raise ValueError("chart values are fractions between 0 and 1")
    left, right = Fraction(MARGIN_LEFT), Fraction(width - MARGIN_RIGHT)
    top, bottom = Fraction(MARGIN_TOP), Fraction(height - MARGIN_BOTTOM)
    n = len(series)

    def x_of(i: int) -> Fraction:
        return (left + right) / 2 if n == 1 else left + (right - left) * i / (n - 1)

    def y_of(value: Fraction) -> Fraction:
        return bottom - value * (bottom - top)

    points: list[ChartPoint] = []
    segments: list[str] = []
    run: list[str] = []
    for i, (label, value) in enumerate(series):
        if value is None:
            if len(run) > 1:
                segments.append(" ".join(run))
            run = []
            continue
        x, y = _fmt(x_of(i)), _fmt(y_of(value))
        run.append(f"{x},{y}")
        points.append(ChartPoint(x=x, y=y, label=label, percent=as_percent(value) or 0))
    if len(run) > 1:
        segments.append(" ".join(run))
    return LineChart(
        width=width,
        height=height,
        left=_fmt(left),
        right=_fmt(right),
        top=_fmt(top),
        bottom=_fmt(bottom),
        label_y=_fmt(bottom + 20),
        segments=tuple(segments),
        points=tuple(points),
        y_ticks=tuple(Tick(y=_fmt(y_of(t)), label=f"{as_percent(t)}\u00a0%") for t in TICKS),
        x_labels=tuple(
            XLabel(x=_fmt(x_of(i)), label=label) for i, (label, _v) in enumerate(series)
        ),
        table=[(label, as_percent(value)) for label, value in series],
    )
