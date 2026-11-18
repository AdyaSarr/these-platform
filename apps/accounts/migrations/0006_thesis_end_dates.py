# The dates of the end of a thesis: defence, closure, end of the director's access (M13a).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_company_co_supervisor_and_sides'),
    ]

    operations = [
        migrations.AddField(
            model_name='thesis',
            name='access_until',
            field=models.DateField(blank=True, null=True, verbose_name="director's access until"),
        ),
        migrations.AddField(
            model_name='thesis',
            name='closed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='closed at'),
        ),
        migrations.AddField(
            model_name='thesis',
            name='defended_on',
            field=models.DateField(blank=True, null=True, verbose_name='defence date'),
        ),
    ]
