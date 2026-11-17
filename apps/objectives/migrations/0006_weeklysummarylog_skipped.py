# SKIPPED: the weekly summary goes through core.delivery.deliver_once (M11a).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('objectives', '0005_planned_has_no_progress'),
    ]

    operations = [
        migrations.AlterField(
            model_name='weeklysummarylog',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due')], default='PENDING', editable=False, max_length=10),
        ),
    ]
