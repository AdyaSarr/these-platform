# Role columns of 32 characters: INDUSTRIAL_CO_SUPERVISOR has 24 (roles-encadrement).

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('admin_file', '0008_requireditem_submitted_at_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='itemapproval',
            name='role',
            field=models.CharField(choices=[('PHD_STUDENT', 'PhD student'), ('DIRECTOR', 'Thesis director'), ('CO_SUPERVISOR', 'LIRMM co-supervisor'), ('INDUSTRIAL_TUTOR', 'Industrial tutor'), ('INDUSTRIAL_CO_SUPERVISOR', 'Company co-supervisor'), ('CSI_MEMBER', 'CSI member'), ('GUEST', 'Guest')], editable=False, max_length=32),
        ),
        migrations.AlterField(
            model_name='requireditem',
            name='validator_roles',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('PHD_STUDENT', 'PhD student'), ('DIRECTOR', 'Thesis director'), ('CO_SUPERVISOR', 'LIRMM co-supervisor'), ('INDUSTRIAL_TUTOR', 'Industrial tutor'), ('INDUSTRIAL_CO_SUPERVISOR', 'Company co-supervisor'), ('CSI_MEMBER', 'CSI member'), ('GUEST', 'Guest')], max_length=32), size=None, verbose_name='validated by'),
        ),
    ]
