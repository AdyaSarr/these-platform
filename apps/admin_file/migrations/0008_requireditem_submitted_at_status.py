# A deposit date only while deposited (frontend step 2, point 12).

from django.db import migrations, models


def clear_dates_off_a_deposit(apps, schema_editor):
    """Before the CHECK: an item validated between 0007 and this migration kept
    its deposit date (approve did not clear it then)."""
    RequiredItem = apps.get_model("admin_file", "RequiredItem")
    RequiredItem.objects.exclude(status="SUBMITTED").exclude(submitted_at=None).update(
        submitted_at=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ("admin_file", "0007_requireditem_submitted_at"),
    ]

    operations = [
        migrations.RunPython(clear_dates_off_a_deposit, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="requireditem",
            constraint=models.CheckConstraint(
                condition=models.Q(("submitted_at__isnull", True), ("status", "SUBMITTED"), _connector="OR"),
                name="admin_file_item_submitted_at_status",
            ),
        ),
    ]
