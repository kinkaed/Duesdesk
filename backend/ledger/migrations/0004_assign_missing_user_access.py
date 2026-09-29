from django.conf import settings
from django.db import migrations


def assign_missing_secretary_access(apps, schema_editor):
    """Intentionally does nothing.

    This step used to grant 'secretary' to every account that had no access
    row. It ran before the organization column existed, so it over-privileged
    ordinary accounts, and because 0006 only backfills accounts that still have
    no access row, it also pre-empted the least-privilege policy there.

    Leaving it empty lets 0006 assign 'secretary' to staff and superusers and
    'auditor' to everyone else. Historical migrations must not be edited once
    they have run anywhere, so this is fixed in place rather than superseded by
    a corrective migration, which would only add new rows and never change the
    ones already created.
    """
    return None


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0003_receipt_snapshots'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_missing_secretary_access, migrations.RunPython.noop),
    ]
