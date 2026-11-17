# M11b: e-mails deferred to a digest (DEFERRED, DIGESTED, digest), preferences in three categories.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_company_co_supervisor_and_sides'),
        ('activity', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='notificationpreference',
            name='activity_preference_info_only',
        ),
        migrations.AddField(
            model_name='activitynotification',
            name='category',
            field=models.CharField(blank=True, choices=[('INFO', 'Information'), ('ACTION', 'Asks you to act'), ('DEADLINE', 'Deadline reminders')], editable=False, max_length=16),
        ),
        migrations.AddField(
            model_name='activitynotification',
            name='digest',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='activity.activitynotification'),
        ),
        migrations.AddField(
            model_name='activitynotification',
            name='includes_info',
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AlterField(
            model_name='activitynotification',
            name='status',
            field=models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due'), ('DEFERRED', 'Waiting for a digest'), ('DIGESTED', 'Sent in a digest')], default='PENDING', max_length=10),
        ),
        migrations.AlterField(
            model_name='event',
            name='actor',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='event',
            name='kind',
            field=models.CharField(choices=[('DOCUMENT_ADDED', 'New document'), ('DOCUMENT_VERSION_ADDED', 'New version of a document'), ('DOCUMENT_GRANTED', 'Document shared with you'), ('DOCUMENT_LINKED', 'Document linked'), ('ITEM_SUBMITTED', 'Piece submitted'), ('ITEM_VALIDATED', 'Piece validated'), ('ITEM_REJECTED', 'Piece refused'), ('ITEM_REOPENED', 'Piece reopened'), ('OBJECTIVE_CREATED', 'New objective'), ('OBJECTIVE_CHANGED', 'Objective changed'), ('PROGRESS_REPORTED', 'Progress reported'), ('OBJECTIVE_COMMENTED', 'Comment on an objective'), ('MEETING_PLANNED', 'Meeting planned'), ('MEETING_CHANGED', 'Meeting changed'), ('MEETING_CANCELLED', 'Meeting cancelled'), ('MINUTES_PUBLISHED', 'Minutes published'), ('ACTION_ASSIGNED', 'Action assigned'), ('SUBMISSION_CREATED', 'New submission'), ('SUBMISSION_STATUS_CHANGED', 'Submission status changed'), ('APPROVAL_REQUESTED', 'ST approval requested'), ('APPROVAL_DECIDED', 'ST approval decided'), ('MISSION_PLANNED', 'Mission planned'), ('MISSION_CHANGED', 'Mission changed'), ('MISSION_CANCELLED', 'Mission cancelled'), ('ORDER_REQUESTED', 'Mission approval requested'), ('ORDER_DECIDED', 'Mission approval decided'), ('MEMBER_JOINED', 'New team member'), ('MEMBER_ROLE_CHANGED', 'Role changed'), ('MEMBER_LEFT', 'Member left'), ('OBJECTIVE_OVERDUE', 'Objective overdue')], editable=False, max_length=32),
        ),
        migrations.AddConstraint(
            model_name='notificationpreference',
            constraint=models.CheckConstraint(condition=models.Q(('category', 'INFO'), models.Q(('category', 'ACTION'), models.Q(('frequency', 'FEED_ONLY'), _negated=True)), models.Q(('category', 'DEADLINE'), ('frequency__in', ['IMMEDIATE', 'DAILY'])), _connector='OR'), name='activity_preference_frequencies'),
        ),
    ]
