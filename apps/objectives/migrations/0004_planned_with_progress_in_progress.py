"""Data: an objective « Planned » with a progress above 0 % becomes « In progress ».

The old progress form sent its first choice (« Planned ») when the status was left
untouched. Not a result: no mark, no revision, no audit event (nothing was decided,
the status is brought in line with the progress already reported). The CHECK that
forbids it from now on comes in the next migration, once these rows are fixed.
"""

from django.db import migrations


def planned_with_progress_to_in_progress(apps, schema_editor):  # type: ignore[no-untyped-def]
    Objective = apps.get_model("objectives", "Objective")
    Objective.objects.filter(status="PLANNED", progress__gt=0).update(status="IN_PROGRESS")


class Migration(migrations.Migration):
    dependencies = [("objectives", "0003_weekly_summary")]

    operations = [
        # Reverse: nothing to undo (which rows were PLANNED is not kept, on purpose).
        migrations.RunPython(planned_with_progress_to_in_progress, migrations.RunPython.noop),
    ]
