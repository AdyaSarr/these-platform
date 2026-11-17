# M11b: e-mails deferred to a digest (DEFERRED, DIGESTED, digest), preferences in three categories.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activity', '0002_m11b_digests'),
        ('admin_file', '0009_role_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='itemnotification',
            name='admin_file_notification_status',
        ),
        migrations.RemoveConstraint(
            model_name='reminderlog',
            name='admin_file_reminder_status',
        ),
        migrations.AddField(
            model_name='itemnotification',
            name='digest',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='activity.activitynotification'),
        ),
        migrations.AddField(
            model_name='reminderlog',
            name='digest',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='activity.activitynotification'),
        ),
        migrations.AlterField(
            model_name='itemnotification',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due'), ('DEFERRED', 'Waiting for a digest'), ('DIGESTED', 'Sent in a digest')], default='PENDING', editable=False, max_length=10),
        ),
        migrations.AlterField(
            model_name='reminderlog',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due'), ('DEFERRED', 'Waiting for a digest'), ('DIGESTED', 'Sent in a digest')], default='PENDING', editable=False, max_length=10),
        ),
        migrations.AddConstraint(
            model_name='itemnotification',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['PENDING', 'SENT', 'FAILED', 'SKIPPED', 'DEFERRED', 'DIGESTED'])), name='admin_file_notification_status'),
        ),
        migrations.AddConstraint(
            model_name='reminderlog',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ['PENDING', 'SENT', 'FAILED', 'SKIPPED', 'DEFERRED', 'DIGESTED'])), name='admin_file_reminder_status'),
        ),
    ]
