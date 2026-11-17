# A weekly digest covers a week starting on a Monday (M11b).

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_company_co_supervisor_and_sides'),
        ('activity', '0002_m11b_digests'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='activitynotification',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('kind', 'DIGEST_WEEKLY'), _negated=True), ('period_start__iso_week_day', 1), _connector='OR'), name='activity_weekly_digest_monday'),
        ),
    ]
