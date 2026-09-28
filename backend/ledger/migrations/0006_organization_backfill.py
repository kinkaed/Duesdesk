import uuid
from django.core.management.color import no_style
from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Org = apps.get_model('ledger', 'Organisation')
    # Adopt the organization that already exists. Assuming pk=1 would create a
    # second, generic organization whenever a legacy database's organisation row
    # has a different id, silently re-homing every existing record and stranding
    # the real organization's name, contact and receipt footer.
    org = Org.objects.using(alias).order_by('pk').first()
    if org is None:
        # A fresh database has no organization. Create the historical default at
        # id 1 explicitly: a generated id is only 1 if the sequence has never
        # been used, which is not guaranteed on an existing installation.
        org = Org.objects.using(alias).create(pk=1, name='Membership Association')
    # Explicit legacy pks must not collide with PostgreSQL's next generated id.
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Org]):
            cursor.execute(sql)
    for item in Org.objects.using(alias).filter(public_id__isnull=True):
        item.public_id = uuid.uuid4()
        item.save(using=alias, update_fields=['public_id'])
    for name in ['Member','Payment','Allocation','DuesMonth','UserAccess','AuditEvent','ImportBatch']:
        apps.get_model('ledger', name).objects.using(alias).filter(organization__isnull=True).update(organization_id=org.pk)
    User = apps.get_model('auth', 'User')
    Access = apps.get_model('ledger', 'UserAccess')
    for user in User.objects.using(alias).exclude(pk__in=Access.objects.using(alias).values('user_id')):
        Access.objects.using(alias).create(user_id=user.pk, organization_id=org.pk, role='secretary' if user.is_staff or user.is_superuser else 'auditor')
    if schema_editor.connection.vendor == 'postgresql':
        # Django declares foreign keys DEFERRABLE INITIALLY DEFERRED, so the
        # membership inserts above leave pending trigger events. Force them to be
        # checked now, otherwise the SET NOT NULL in the following AlterField
        # operations is refused with "cannot ALTER TABLE ... pending trigger events".
        with schema_editor.connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')


class Migration(migrations.Migration):
    dependencies = [('ledger','0005_secretaryinvite_allocation_organization_and_more')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)] + [
        migrations.AlterField(model_name=name, name='organization', field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='ledger.organisation'))
        for name in ['member','payment','allocation','duesmonth','useraccess','auditevent','importbatch']
    ] + [migrations.AlterField(model_name='organisation', name='public_id', field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False))]
