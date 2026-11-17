# The company co-supervisor role (roles-encadrement): role columns of 32 characters,
# the CSI constraint over the new role, one side of the supervision per person and
# thesis, the PhD student alone on their thesis. Adding a constraint fails on rows
# that already break it: nothing is rewritten silently.

import apps.core.db_functions
import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_invitation'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='membership',
            name='accounts_membership_csi_independent',
        ),
        migrations.AlterField(
            model_name='invitation',
            name='role',
            field=models.CharField(choices=[('PHD_STUDENT', 'PhD student'), ('DIRECTOR', 'Thesis director'), ('CO_SUPERVISOR', 'LIRMM co-supervisor'), ('INDUSTRIAL_TUTOR', 'Industrial tutor'), ('INDUSTRIAL_CO_SUPERVISOR', 'Company co-supervisor'), ('CSI_MEMBER', 'CSI member'), ('GUEST', 'Guest')], max_length=32, verbose_name='role'),
        ),
        migrations.AlterField(
            model_name='membership',
            name='role',
            field=models.CharField(choices=[('PHD_STUDENT', 'PhD student'), ('DIRECTOR', 'Thesis director'), ('CO_SUPERVISOR', 'LIRMM co-supervisor'), ('INDUSTRIAL_TUTOR', 'Industrial tutor'), ('INDUSTRIAL_CO_SUPERVISOR', 'Company co-supervisor'), ('CSI_MEMBER', 'CSI member'), ('GUEST', 'Guest')], max_length=32, verbose_name='role'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('role__in', ['CO_SUPERVISOR', 'CSI_MEMBER', 'DIRECTOR', 'INDUSTRIAL_CO_SUPERVISOR', 'INDUSTRIAL_TUTOR', 'PHD_STUDENT'])), expressions=[('user', '='), ('thesis', '='), (apps.core.db_functions.TsTzRange('active_from', 'active_until', django.contrib.postgres.fields.ranges.RangeBoundary()), '&&'), (models.Case(models.When(role='CSI_MEMBER', then=models.Value(1)), default=models.Value(0), output_field=models.IntegerField()), '<>')], name='accounts_membership_csi_independent'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('role__in', ['CO_SUPERVISOR', 'DIRECTOR', 'INDUSTRIAL_CO_SUPERVISOR', 'INDUSTRIAL_TUTOR'])), expressions=[('user', '='), ('thesis', '='), (apps.core.db_functions.TsTzRange('active_from', 'active_until', django.contrib.postgres.fields.ranges.RangeBoundary()), '&&'), (models.Case(models.When(role__in=['CO_SUPERVISOR', 'DIRECTOR'], then=models.Value(1)), default=models.Value(0), output_field=models.IntegerField()), '<>')], name='accounts_membership_one_supervision_side'),
        ),
        migrations.AddConstraint(
            model_name='membership',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(expressions=[('user', '='), ('thesis', '='), (apps.core.db_functions.TsTzRange('active_from', 'active_until', django.contrib.postgres.fields.ranges.RangeBoundary()), '&&'), (models.Case(models.When(role='PHD_STUDENT', then=models.Value(1)), default=models.Value(0), output_field=models.IntegerField()), '<>')], name='accounts_membership_student_alone'),
        ),
    ]
