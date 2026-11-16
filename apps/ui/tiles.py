"""Dashboard tiles, computed from the data of the blocks the view kept.

accounts.views.ThesisDashboardView already filtered each block by its own action
and computed its data: a tile only re-reads that data. No block, no tile; the
alert count adds up counts the blocks already display, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# (block key, data key, alert key): the counts that call for attention.
ALERT_SOURCES = (
    ("objectives", "overdue", "objectives_overdue"),
    ("meetings", "overdue_count", "actions_overdue"),
    ("conferences", "pending", "conference_decisions"),
    ("travel", "pending", "mission_decisions"),
    ("travel", "missing_receipts", "missing_receipts"),
)


@dataclass(frozen=True)
class Alerts:
    total: int
    parts: list[tuple[str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Tiles:
    file: Any = None  # admin_file.services.Progress
    objectives: dict[str, Any] | None = None
    meeting: dict[str, Any] | None = None
    alerts: Alerts | None = None

    def __bool__(self) -> bool:
        return any(x is not None for x in (self.file, self.objectives, self.meeting, self.alerts))


def dashboard_tiles(blocks: list[tuple[Any, dict[str, Any]]]) -> Tiles:
    data = {block.key: values for block, values in blocks}
    alerts = None
    if any(key in data for key, _, _ in ALERT_SOURCES):
        parts = [
            (name, int(data[key].get(field_name) or 0))
            for key, field_name, name in ALERT_SOURCES
            if key in data
        ]
        parts = [(name, count) for name, count in parts if count]
        alerts = Alerts(total=sum(count for _, count in parts), parts=parts)
    return Tiles(
        file=data["admin_file"]["progress"] if "admin_file" in data else None,
        objectives=data.get("objectives"),
        meeting=data.get("meetings"),
        alerts=alerts,
    )
