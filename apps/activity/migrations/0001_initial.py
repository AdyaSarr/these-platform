# The activity app (M11a): events, reads, preferences, e-mails. Random UUID keys.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0005_company_co_supervisor_and_sides'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Event',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('DOCUMENT_ADDED', 'New document'), ('DOCUMENT_VERSION_ADDED', 'New version of a document'), ('DOCUMENT_GRANTED', 'Document shared with you'), ('DOCUMENT_LINKED', 'Document linked'), ('ITEM_SUBMITTED', 'Piece submitted'), ('ITEM_VALIDATED', 'Piece validated'), ('ITEM_REJECTED', 'Piece refused'), ('ITEM_REOPENED', 'Piece reopened'), ('OBJECTIVE_CREATED', 'New objective'), ('OBJECTIVE_CHANGED', 'Objective changed'), ('PROGRESS_REPORTED', 'Progress reported'), ('OBJECTIVE_COMMENTED', 'Comment on an objective'), ('MEETING_PLANNED', 'Meeting planned'), ('MEETING_CHANGED', 'Meeting changed'), ('MEETING_CANCELLED', 'Meeting cancelled'), ('MINUTES_PUBLISHED', 'Minutes published'), ('ACTION_ASSIGNED', 'Action assigned'), ('SUBMISSION_CREATED', 'New submission'), ('SUBMISSION_STATUS_CHANGED', 'Submission status changed'), ('APPROVAL_REQUESTED', 'ST approval requested'), ('APPROVAL_DECIDED', 'ST approval decided'), ('MISSION_PLANNED', 'Mission planned'), ('MISSION_CHANGED', 'Mission changed'), ('MISSION_CANCELLED', 'Mission cancelled'), ('ORDER_REQUESTED', 'Mission approval requested'), ('ORDER_DECIDED', 'Mission approval decided'), ('MEMBER_JOINED', 'New team member'), ('MEMBER_ROLE_CHANGED', 'Role changed'), ('MEMBER_LEFT', 'Member left')], editable=False, max_length=32)),
                ('object_type', models.CharField(choices=[('DOCUMENT', 'DOCUMENT'), ('DOCUMENT_GRANT', 'DOCUMENT_GRANT'), ('MEETING_DOCUMENT', 'MEETING_DOCUMENT'), ('SUBMISSION_DOCUMENT', 'SUBMISSION_DOCUMENT'), ('ITEM', 'ITEM'), ('OBJECTIVE', 'OBJECTIVE'), ('OBJECTIVE_COMMENT', 'OBJECTIVE_COMMENT'), ('MEETING', 'MEETING'), ('ACTION', 'ACTION'), ('SUBMISSION', 'SUBMISSION'), ('APPROVAL', 'APPROVAL'), ('MISSION', 'MISSION'), ('ORDER', 'ORDER'), ('MEMBERSHIP', 'MEMBERSHIP')], editable=False, max_length=24)),
                ('object_id', models.UUIDField(editable=False)),
                ('actor', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('thesis', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='accounts.thesis')),
            ],
        ),
        migrations.CreateModel(
            name='ActivityNotification',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('EVENT', 'One element'), ('DIGEST_DAILY', 'Daily digest'), ('DIGEST_WEEKLY', 'Weekly digest')], editable=False, max_length=16)),
                ('cycle', models.PositiveIntegerField(editable=False, null=True)),
                ('period_start', models.DateField(editable=False, null=True)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due')], default='PENDING', max_length=10)),
                ('attempts', models.PositiveSmallIntegerField(default=0, editable=False)),
                ('sent_at', models.DateTimeField(editable=False, null=True)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('recipient', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('thesis', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='accounts.thesis')),
                ('event', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='activity.event')),
            ],
        ),
        migrations.CreateModel(
            name='EventRead',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reads', to='activity.event')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='NotificationPreference',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('category', models.CharField(choices=[('INFO', 'Information'), ('ACTION', 'Asks you to act'), ('DEADLINE', 'Deadline reminders')], max_length=16)),
                ('frequency', models.CharField(choices=[('IMMEDIATE', 'At once, one e-mail per element'), ('DAILY', 'Daily digest'), ('WEEKLY', 'Weekly digest (Monday)'), ('FEED_ONLY', 'No e-mail: the Activity page only')], max_length=16)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('thesis', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='accounts.thesis')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['thesis', '-created_at'], name='activity_event_recent'),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['object_type', 'object_id'], name='activity_event_object'),
        ),
        migrations.AddConstraint(
            model_name='activitynotification',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'EVENT')), fields=('event', 'recipient'), name='activity_notification_event_once'),
        ),
        migrations.AddConstraint(
            model_name='activitynotification',
            constraint=models.UniqueConstraint(condition=models.Q(('kind', 'EVENT'), _negated=True), fields=('thesis', 'kind', 'period_start', 'recipient'), name='activity_notification_digest_once'),
        ),
        migrations.AddConstraint(
            model_name='activitynotification',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('kind', 'EVENT'), ('event__isnull', False)), models.Q(models.Q(('kind', 'EVENT'), _negated=True), ('period_start__isnull', False)), _connector='OR'), name='activity_notification_shape'),
        ),
        migrations.AddConstraint(
            model_name='eventread',
            constraint=models.UniqueConstraint(fields=('event', 'user'), name='activity_read_once'),
        ),
        migrations.AddConstraint(
            model_name='notificationpreference',
            constraint=models.UniqueConstraint(fields=('user', 'thesis', 'category'), name='activity_one_preference'),
        ),
        migrations.AddConstraint(
            model_name='notificationpreference',
            constraint=models.CheckConstraint(condition=models.Q(('category', 'INFO')), name='activity_preference_info_only'),
        ),
    ]
