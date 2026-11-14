"""Give a due date to every mandatory catalogue item that has none (before 0005
forbids it), with the catalogue's rule AS IT STANDS ON 2026-11-14.

The rule is copied here on purpose, not imported from admin_file.catalog: this
migration must give the same result whenever it runs, whatever the catalogue
becomes later (tests/test_due_date_migration.py replays it from 0002). An
existing date is never changed; custom items (the student's rule) are left as
they are. No audit event: a data repair, like objectives 0004.
"""

import datetime as dt

from django.db import migrations

CIFRE_START = "cifre_start_date"
FIRST_ENROLLMENT = "first_enrollment_date"

# code -> (thesis date it counts from, days after it), frozen on 2026-11-14.
FROZEN_RULES = {
    "employment_contract": (CIFRE_START, 0),
    "cifre_agreement": (CIFRE_START, 30),
    "collaboration_contract": (CIFRE_START, 90),
    "funding_proof": (FIRST_ENROLLMENT, 0),
    "fsd_zrr_clearance": (CIFRE_START, 0),
    "adum_enrollment": (FIRST_ENROLLMENT, 0),
    "doctoral_charter": (FIRST_ENROLLMENT, 30),
    "training_agreement": (FIRST_ENROLLMENT, 90),
    "training_plan": (FIRST_ENROLLMENT, 180),
    "cvec_certificate": (FIRST_ENROLLMENT, 0),
}
CUSTOM_CODE_REGEX = r"^custom-[0-9a-f]{12}$"


def fill(apps, schema_editor):  # type: ignore[no-untyped-def]
    item_model = apps.get_model("admin_file", "RequiredItem")
    missing = (
        item_model.objects.filter(mandatory=True, due_date__isnull=True)
        .exclude(code__regex=CUSTOM_CODE_REGEX)
        .select_related("thesis")
        .order_by("pk")
    )
    unknown = []
    filled = 0
    for item in missing:
        rule = FROZEN_RULES.get(item.code)
        if rule is None:
            unknown.append(item.code)
            continue
        field, days = rule
        item.due_date = getattr(item.thesis, field) + dt.timedelta(days=days)
        item.save(update_fields=["due_date"])
        filled += 1
    if unknown:
        raise RuntimeError(
            "mandatory items without a due date and without a frozen rule: "
            + ", ".join(sorted(set(unknown)))
            + " (give them a date, then migrate again)"
        )
    if filled:
        print(f"\n  admin_file 0004: {filled} catalogue due date(s) filled in")  # noqa: T201


class Migration(migrations.Migration):
    dependencies = [("admin_file", "0003_reopening_proposals_notifications")]

    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
