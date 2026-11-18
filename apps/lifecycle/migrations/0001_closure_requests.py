# Two-person closure requests and the lifecycle e-mails (M13a).

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0006_thesis_end_dates'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ClosureRequest',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('requester_role', models.CharField(max_length=32)),
                ('reason', models.CharField(choices=[('defended', 'The thesis was defended'), ('student_withdrew', 'The PhD student stops the thesis'), ('cifre_terminated', 'Early end of the CIFRE contract'), ('other', 'Another reason')], max_length=32, verbose_name='reason')),
                ('expires_at', models.DateTimeField()),
                ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('withdrawn_at', models.DateTimeField(blank=True, null=True)),
                ('expired_at', models.DateTimeField(blank=True, null=True)),
                ('confirmed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('requested_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('thesis', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='closure_requests', to='accounts.thesis')),
            ],
        ),
        migrations.CreateModel(
            name='LifecycleNotification',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='updated at')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('DEFENDED', 'Defence declared'), ('DEFENSE_CANCELLED', 'Defence declaration cancelled'), ('CLOSURE_REQUESTED', 'Closure requested')], max_length=20)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('SKIPPED', 'Not sent: no longer concerned or due')], default='PENDING', max_length=10)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('closure_request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='lifecycle.closurerequest')),
                ('created_by', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='created by')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('thesis', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.thesis')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddConstraint(
            model_name='closurerequest',
            constraint=models.UniqueConstraint(condition=models.Q(('confirmed_at__isnull', True), ('expired_at__isnull', True), ('withdrawn_at__isnull', True)), fields=('thesis',), name='lifecycle_one_open_request'),
        ),
        migrations.AddConstraint(
            model_name='closurerequest',
            constraint=models.CheckConstraint(condition=models.Q(('requester_role__in', ['PHD_STUDENT', 'DIRECTOR'])), name='lifecycle_requester_side'),
        ),
        migrations.AddConstraint(
            model_name='closurerequest',
            constraint=models.CheckConstraint(condition=models.Q(('reason__in', ['defended', 'student_withdrew', 'cifre_terminated', 'other'])), name='lifecycle_known_reason'),
        ),
    ]
