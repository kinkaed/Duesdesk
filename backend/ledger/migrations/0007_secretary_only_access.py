# Members are records, not accounts. No member role exists from here on.
from django.db import migrations, models


def revoke_member_access(apps, schema_editor):
    alias = schema_editor.connection.alias
    Access = apps.get_model('ledger', 'UserAccess')
    # Withdraw any member login left over from before this policy. Accounts are
    # kept so payments and audit events that reference them stay intact, but the
    # membership is deactivated so no member data remains reachable by that login.
    Access.objects.using(alias).filter(role='member').update(active=False)
    # The member link is reserved for a future portal, so it is not a login path
    # and is left empty here rather than pointing a real account at a member.


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0006_organization_backfill'),
    ]

    operations = [
        migrations.RunPython(revoke_member_access, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='useraccess',
            name='role',
            field=models.CharField(choices=[('secretary', 'Secretary'), ('auditor', 'Auditor')], max_length=12),
        ),
    ]
