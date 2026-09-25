from django.conf import settings
from django.db import migrations


def assign_missing_secretary_access(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    UserAccess = apps.get_model('ledger', 'UserAccess')
    database = schema_editor.connection.alias
    assigned = set(UserAccess.objects.using(database).values_list('user_id', flat=True))
    missing = [
        UserAccess(user_id=user_id, role='secretary')
        for user_id in User.objects.using(database).values_list('pk', flat=True)
        if user_id not in assigned
    ]
    if missing:
        UserAccess.objects.using(database).bulk_create(missing, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0003_receipt_snapshots'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_missing_secretary_access, migrations.RunPython.noop),
    ]
