# Hand-written (reviewed): hash chain of the audit log, checkpoints, triggers.
#
# 1. chain_format (1) and prev_hash / hash added (hashes nullable for the backfill);
# 2. existing rows (M1, fictitious development data) renumbered 1..N in seq
#    order and chained, with the same functions as vault.audit (format v1);
# 3. columns made NOT NULL, seq no longer an identity column;
# 4. triggers: no UPDATE / DELETE / TRUNCATE; INSERT must extend the chain
#    (under the same advisory lock as vault.audit.audit()).

from django.db import migrations, models

# pg_advisory_xact_lock(22081, 1) below is vault.locks.AUDIT_NAMESPACE (0x5641) and
# AUDIT_CHAIN_KEY: a test checks that they match. A fixed string, no formatting.
TRIGGERS = """
CREATE FUNCTION vault_audit_forbid() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'vault: % on % is forbidden (append-only audit log)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE FUNCTION vault_auditevent_check_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    last_seq bigint;
    last_hash varchar(64);
BEGIN
    -- Same lock as vault.audit.audit(): direct SQL inserts are queued too, and
    -- the SELECT below (a new snapshot) sees the link committed just before.
    PERFORM pg_advisory_xact_lock(22081, 1);
    SELECT seq, hash INTO last_seq, last_hash
        FROM vault_auditevent ORDER BY seq DESC LIMIT 1;
    IF NEW.seq IS DISTINCT FROM COALESCE(last_seq, 0) + 1
       OR NEW.prev_hash IS DISTINCT FROM COALESCE(last_hash, repeat('0', 64)) THEN
        RAISE EXCEPTION 'vault: audit event % does not extend the chain', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION vault_auditcheckpoint_check_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.hash IS DISTINCT FROM (SELECT hash FROM vault_auditevent WHERE seq = NEW.seq) THEN
        RAISE EXCEPTION 'vault: checkpoint % does not match its audit event', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER vault_auditevent_no_update_delete
    BEFORE UPDATE OR DELETE ON vault_auditevent
    FOR EACH ROW EXECUTE FUNCTION vault_audit_forbid();
CREATE TRIGGER vault_auditevent_no_truncate
    BEFORE TRUNCATE ON vault_auditevent
    FOR EACH STATEMENT EXECUTE FUNCTION vault_audit_forbid();
CREATE TRIGGER vault_auditevent_link
    BEFORE INSERT ON vault_auditevent
    FOR EACH ROW EXECUTE FUNCTION vault_auditevent_check_link();

CREATE TRIGGER vault_auditcheckpoint_no_update_delete
    BEFORE UPDATE OR DELETE ON vault_auditcheckpoint
    FOR EACH ROW EXECUTE FUNCTION vault_audit_forbid();
CREATE TRIGGER vault_auditcheckpoint_no_truncate
    BEFORE TRUNCATE ON vault_auditcheckpoint
    FOR EACH STATEMENT EXECUTE FUNCTION vault_audit_forbid();
CREATE TRIGGER vault_auditcheckpoint_event
    BEFORE INSERT ON vault_auditcheckpoint
    FOR EACH ROW EXECUTE FUNCTION vault_auditcheckpoint_check_event();
"""

DROP_TRIGGERS = """
DROP TRIGGER vault_auditcheckpoint_event ON vault_auditcheckpoint;
DROP TRIGGER vault_auditcheckpoint_no_truncate ON vault_auditcheckpoint;
DROP TRIGGER vault_auditcheckpoint_no_update_delete ON vault_auditcheckpoint;
DROP TRIGGER vault_auditevent_link ON vault_auditevent;
DROP TRIGGER vault_auditevent_no_truncate ON vault_auditevent;
DROP TRIGGER vault_auditevent_no_update_delete ON vault_auditevent;
DROP FUNCTION vault_auditcheckpoint_check_event();
DROP FUNCTION vault_auditevent_check_link();
DROP FUNCTION vault_audit_forbid();
"""


def chain_existing_rows(apps, schema_editor):  # type: ignore[no-untyped-def]
    # The FROZEN v1 code (never modified): this migration hashes exactly as it did.
    from apps.vault.chain_v1 import GENESIS_HASH, chain_hash, payload_bytes

    with schema_editor.connection.cursor() as cursor:
        # Renumber 1..N in the current seq order (two steps: no collision).
        cursor.execute("UPDATE vault_auditevent SET seq = -seq")
        cursor.execute(
            "UPDATE vault_auditevent AS e SET seq = r.n FROM "
            "(SELECT seq, row_number() OVER (ORDER BY seq DESC) AS n FROM vault_auditevent) AS r "
            "WHERE e.seq = r.seq"
        )
    AuditEvent = apps.get_model("vault", "AuditEvent")
    prev_hash = GENESIS_HASH
    for row in AuditEvent.objects.order_by("seq").iterator():
        row.prev_hash = prev_hash
        row.hash = chain_hash(prev_hash, payload_bytes(row))
        AuditEvent.objects.filter(seq=row.seq).update(prev_hash=row.prev_hash, hash=row.hash)
        prev_hash = row.hash


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_invitation"),
        ("vault", "0003_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditevent",
            name="chain_format",
            field=models.PositiveSmallIntegerField(db_default=1, default=1, editable=False),
        ),
        migrations.AddField(
            model_name="auditevent",
            name="prev_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="auditevent",
            name="hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(chain_existing_rows, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="auditevent",
            name="prev_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.AlterField(
            model_name="auditevent",
            name="seq",
            field=models.BigIntegerField(editable=False, primary_key=True, serialize=False),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("seq__gte", 1)), name="vault_audit_seq_from_1"
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("chain_format__in", [1])),
                name="vault_audit_known_chain_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("hash__regex", "^[0-9a-f]{64}$"), ("prev_hash__regex", "^[0-9a-f]{64}$")
                ),
                name="vault_audit_hashes_hex",
            ),
        ),
        migrations.CreateModel(
            name="AuditCheckpoint",
            fields=[
                ("seq", models.BigIntegerField(editable=False, primary_key=True, serialize=False)),
                ("hash", models.CharField(editable=False, max_length=64)),
                ("timestamp", models.DateTimeField(editable=False)),
                ("signature", models.CharField(editable=False, max_length=128)),
            ],
            options={
                "verbose_name": "audit checkpoint",
                "verbose_name_plural": "audit checkpoints",
                "ordering": ["seq"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("seq__gte", 1)), name="vault_checkpoint_seq_from_1"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("hash__regex", "^[0-9a-f]{64}$"),
                            ("signature__regex", "^[0-9a-f]{128}$"),
                        ),
                        name="vault_checkpoint_hex",
                    ),
                ],
            },
        ),
        migrations.RunSQL(TRIGGERS, DROP_TRIGGERS),
    ]
