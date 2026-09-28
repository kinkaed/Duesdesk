import uuid
from django.core.management.color import no_style
from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Org = apps.get_model('ledger', 'Organisation')
    org, _ = Org.objects.using(alias).get_or_create(pk=1, defaults={'name':'Membership Association'})
    # Explicit legacy pk=1 must not collide with PostgreSQL's next generated id.
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Org]):
            cursor.execute(sql)
    for item in Org.objects.using(alias).all():
        item.public_id = uuid.uuid4()
        item.save(using=alias, update_fields=['public_id'])
    for name in ['Member','Payment','Allocation','DuesMonth','UserAccess','AuditEvent','ImportBatch']:
        apps.get_model('ledger', name).objects.using(alias).filter(organization__isnull=True).update(organization_id=org.pk)
    User = apps.get_model('auth', 'User')
    Access = apps.get_model('ledger', 'UserAccess')
    for user in User.objects.using(alias).exclude(pk__in=Access.objects.using(alias).values('user_id')):
        Access.objects.using(alias).create(user_id=user.pk, organization_id=org.pk, role='secretary' if user.is_staff or user.is_superuser else 'auditor')


class Migration(migrations.Migration):
    dependencies = [('ledger','0005_secretaryinvite_allocation_organization_and_more')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)] + [
        migrations.AlterField(model_name=name, name='organization', field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='ledger.organisation'))
        for name in ['member','payment','allocation','duesmonth','useraccess','auditevent','importbatch']
    ] + [migrations.AlterField(model_name='organisation', name='public_id', field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False))]
