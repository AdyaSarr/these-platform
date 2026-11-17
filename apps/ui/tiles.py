"""Dashboard tiles, computed from the data of the blocks the view kept.

accounts.views.ThesisDashboardView already filtered each block by its own action
and computed its data: a tile only re-reads that data. No block, no tile; the
alert count adds up counts the blocks already display, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# What falls to THE VIEWER only (apps/ui/tests/test_alerts_mine.py): their
# overdue actions (the list of the Meetings card, titles already opened there),
# the approvals they decide, their missing receipts; each element gets a line and
# a link (review A). The overdue objectives stay in the Objectives tile.
ALERT_SOURCES: tuple[tuple[str, str, str], ...] = (
    # (block key, list of the block's data, alert key): the count is its length.
    ("meetings", "my_overdue", "actions_overdue"),  # (action, title opened by the card)
    ("conferences", "pending_approvals", "conference_decisions"),
    ("travel", "pending_orders", "mission_decisions"),
    ("travel", "missing_missions", "missing_receipts"),
)


@dataclass(frozen=True)
class Alerts:
    total: int
    # (alert key, count, elements): a line and a link per element (review A).
    parts: list[tuple[str, int, list[Any]]] = field(default_factory=list)


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
            (name, len(items), items)
            for key, list_key, name in ALERT_SOURCES
            if key in data and (items := list(data[key].get(list_key) or []))
        ]
        alerts = Alerts(total=sum(count for _, count, _ in parts), parts=parts)
    return Tiles(
        file=data["admin_file"]["progress"] if "admin_file" in data else None,
        objectives=data.get("objectives"),
        meeting=data.get("meetings"),
        alerts=alerts,
    )
