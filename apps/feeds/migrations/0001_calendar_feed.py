# Personal iCal feeds: a token fingerprint, one live feed per (person, thesis) (M12).

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
            name='CalendarFeed',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token_hash', models.CharField(editable=False, max_length=64, unique=True)),
                ('expires_on', models.DateField(editable=False)),
                ('revoked_at', models.DateTimeField(editable=False, null=True)),
                ('last_used_on', models.DateField(editable=False, null=True)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('thesis', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.thesis')),
                ('user', models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(condition=models.Q(('revoked_at__isnull', True)), fields=('user', 'thesis'), name='feeds_one_live_feed')],
            },
        ),
    ]
