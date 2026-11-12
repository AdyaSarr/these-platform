"""Database functions shared by several apps.

Referenced by migrations: do not rename or move.
"""

from django.contrib.postgres.fields import DateTimeRangeField
from django.db.models import Func


class TsTzRange(Func):
    """PostgreSQL ``tstzrange(lower, upper, bounds)``; a NULL upper bound means +infinity."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()
