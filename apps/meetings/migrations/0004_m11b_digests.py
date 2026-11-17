# M11b: e-mails deferred to a digest (DEFERRED, DIGESTED, digest), preferences in three categories.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activity', '0002_m11b_digests'),
        ('meetings', '0003_notification_skipped_label'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='meetingnotification',
            name='meetings_notification_status',
        ),
        migrations.AddField(
            model_name='meetingnotification',
            name='digest',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='activity.activitynotification'),
        ),
        migrations.AlterField(
            model_name='meetingnotification',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due'), ('DEFERRED', 'Waiting for a digest'), ('DIGESTED', 'Sent in a digest')], default='PENDING', max_length=10),
        ),
        migrations.AddConstraint(
            model_name='meetingnotification',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['PENDING', 'SENT', 'FAILED', 'SKIPPED', 'DEFERRED', 'DIGESTED'])), name='meetings_notification_status'),
        ),
    ]
