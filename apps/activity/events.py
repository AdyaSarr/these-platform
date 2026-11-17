"""Writing an activity event (M11a): called by each business service.

record() runs INSIDE the service's transaction and BEFORE its audit() (CLAUDE.md,
lock order: audit() is the last call of a transaction). It is a plain insert, no
vault operation; it also queues the immediate e-mails the event calls for
(notifications.queue_for_event), after commit, with their ids only. A refusal of
the service rolls the event back with everything else.
"""

from __future__ import annotations

from typing import Any

from .choices import EventKind, ObjectType
from .models import Event


def record(*, kind: str, obj: Any, object_type: str, thesis: Any, actor: Any | None) -> Event:
    """Write one event about ``obj`` (of ``object_type``) on ``thesis`` by ``actor``.

    No title, level or text is copied: the event names its object by id only.
    """
    event = Event.objects.create(
        thesis_id=thesis.pk,
        kind=EventKind(kind),
        object_type=ObjectType(object_type),
        object_id=obj.pk,
        actor_id=actor.pk if actor is not None else None,
        created_by_id=actor.pk if actor is not None else None,
    )
    from .notifications import queue_for_event

    queue_for_event(event, obj)
    return event
