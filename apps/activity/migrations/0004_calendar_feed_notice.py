# The calendar feed expiry notice: a new event kind and object type (M12).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activity', '0003_weekly_digest_monday'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='kind',
            field=models.CharField(choices=[('DOCUMENT_ADDED', 'New document'), ('DOCUMENT_VERSION_ADDED', 'New version of a document'), ('DOCUMENT_GRANTED', 'Document shared with you'), ('DOCUMENT_LINKED', 'Document linked'), ('ITEM_SUBMITTED', 'Piece submitted'), ('ITEM_VALIDATED', 'Piece validated'), ('ITEM_REJECTED', 'Piece refused'), ('ITEM_REOPENED', 'Piece reopened'), ('OBJECTIVE_CREATED', 'New objective'), ('OBJECTIVE_CHANGED', 'Objective changed'), ('PROGRESS_REPORTED', 'Progress reported'), ('OBJECTIVE_COMMENTED', 'Comment on an objective'), ('MEETING_PLANNED', 'Meeting planned'), ('MEETING_CHANGED', 'Meeting changed'), ('MEETING_CANCELLED', 'Meeting cancelled'), ('MINUTES_PUBLISHED', 'Minutes published'), ('ACTION_ASSIGNED', 'Action assigned'), ('SUBMISSION_CREATED', 'New submission'), ('SUBMISSION_STATUS_CHANGED', 'Submission status changed'), ('APPROVAL_REQUESTED', 'ST approval requested'), ('APPROVAL_DECIDED', 'ST approval decided'), ('MISSION_PLANNED', 'Mission planned'), ('MISSION_CHANGED', 'Mission changed'), ('MISSION_CANCELLED', 'Mission cancelled'), ('ORDER_REQUESTED', 'Mission approval requested'), ('ORDER_DECIDED', 'Mission approval decided'), ('MEMBER_JOINED', 'New team member'), ('MEMBER_ROLE_CHANGED', 'Role changed'), ('MEMBER_LEFT', 'Member left'), ('OBJECTIVE_OVERDUE', 'Objective overdue'), ('CALENDAR_FEED_EXPIRING', 'Calendar subscription expiring')], editable=False, max_length=32),
        ),
        migrations.AlterField(
            model_name='event',
            name='object_type',
            field=models.CharField(choices=[('DOCUMENT', 'DOCUMENT'), ('DOCUMENT_GRANT', 'DOCUMENT_GRANT'), ('MEETING_DOCUMENT', 'MEETING_DOCUMENT'), ('SUBMISSION_DOCUMENT', 'SUBMISSION_DOCUMENT'), ('ITEM', 'ITEM'), ('OBJECTIVE', 'OBJECTIVE'), ('OBJECTIVE_COMMENT', 'OBJECTIVE_COMMENT'), ('MEETING', 'MEETING'), ('ACTION', 'ACTION'), ('SUBMISSION', 'SUBMISSION'), ('APPROVAL', 'APPROVAL'), ('MISSION', 'MISSION'), ('ORDER', 'ORDER'), ('MEMBERSHIP', 'MEMBERSHIP'), ('FEED', 'FEED')], editable=False, max_length=24),
        ),
    ]
