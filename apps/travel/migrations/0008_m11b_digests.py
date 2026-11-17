# M11b: e-mails deferred to a digest (DEFERRED, DIGESTED, digest), preferences in three categories.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_company_co_supervisor_and_sides'),
        ('activity', '0002_m11b_digests'),
        ('travel', '0007_ics_sequence'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='travelnotification',
            name='travel_notification_choices',
        ),
        migrations.AddField(
            model_name='travelnotification',
            name='digest',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='activity.activitynotification'),
        ),
        migrations.AlterField(
            model_name='travelnotification',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned'), ('DEFERRED', 'Waiting for a digest'), ('DIGESTED', 'Sent in a digest')], default='PENDING', max_length=10),
        ),
        migrations.AddConstraint(
            model_name='travelnotification',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ['ORDER_TO_REQUEST', 'ORDER_PENDING', 'RECEIPTS_MISSING', 'ORDER_REQUESTED', 'ORDER_DECIDED', 'RULES_CHANGED']), ('window__in', ['', 'EARLY', 'DUE', 'J7', 'J1', 'AFTER_GRACE', 'AFTER_14']), ('status__in', ['PENDING', 'SENT', 'FAILED', 'SKIPPED', 'DEFERRED', 'DIGESTED'])), name='travel_notification_choices'),
        ),
    ]
