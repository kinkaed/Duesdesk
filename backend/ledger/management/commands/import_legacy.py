"""One-off import of the legacy SQL Server JSON snapshot into the configured PostgreSQL database.

Preserves primary keys and foreign keys, treats serialized datetimes as UTC instants,
and advances PostgreSQL identity sequences past the loaded rows. Refuses to run against a
non-empty ledger unless --force is given, so it cannot silently duplicate a previous load.

Not part of the running application. Remove after the data transfer is complete.
"""
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import DateField, DateTimeField, DecimalField, UUIDField
from django.utils import timezone

from ledger.models import (AuditEvent, Allocation, DuesMonth, ImportBatch, Member, Organisation, Payment, RecoveryAttempt, UserAccess)

ORDER = ['auth_user', 'ledger_organisation', 'ledger_recoveryattempt', 'ledger_member', 'ledger_duesmonth', 'ledger_payment', 'ledger_allocation', 'ledger_useraccess', 'ledger_auditevent', 'ledger_importbatch']

MODELS = {
    'auth_user': User,
    'ledger_organisation': Organisation,
    'ledger_recoveryattempt': RecoveryAttempt,
    'ledger_member': Member,
    'ledger_duesmonth': DuesMonth,
    'ledger_payment': Payment,
    'ledger_allocation': Allocation,
    'ledger_useraccess': UserAccess,
    'ledger_auditevent': AuditEvent,
    'ledger_importbatch': ImportBatch,
}


class Command(BaseCommand):
    help = 'Import the legacy SQL Server JSON snapshot into the configured PostgreSQL database. One-off migration tool.'

    def add_arguments(self, parser):
        parser.add_argument('--input', default=str(settings.BASE_DIR / 'legacy_dump.json'), help='JSON snapshot path.')
        parser.add_argument('--force', action='store_true', help='Allow import into a non-empty ledger.')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('The legacy import targets PostgreSQL only. Configure DATABASE_URL first.')
        input_path = Path(options['input'])
        if not input_path.exists():
            raise CommandError(f'Snapshot not found: {input_path}')
        if not options['force'] and Member.objects.exists():
            raise CommandError('Target database already contains members. Use --force to import anyway.')
        snapshot = json.loads(input_path.read_text(encoding='utf-8'))
        missing = [table for table in ORDER if table not in snapshot.get('tables', {})]
        if missing:
            raise CommandError(f'Snapshot is missing tables: {", ".join(missing)}. Re-run export_legacy.')
        with transaction.atomic():
            for table in ORDER:
                count = self._load_table(MODELS[table], snapshot['tables'][table])
                self.stdout.write(f'{table}: {count} rows')
            if connection.vendor == 'postgresql':
                self._fix_sequences()
        self.stdout.write(self.style.SUCCESS('Legacy records imported and sequences advanced.'))

    def _load_table(self, model, rows):
        count = 0
        for raw in rows:
            instance = model()
            for field in model._meta.concrete_fields:
                column = field.attname
                value = raw.get(column)
                if value is None:
                    setattr(instance, column, None)
                    continue
                if isinstance(field, DateTimeField):
                    value = datetime.fromisoformat(value) if isinstance(value, str) else value
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                elif isinstance(field, DecimalField):
                    value = Decimal(str(value))
                elif isinstance(field, UUIDField):
                    value = UUID(str(value))
                elif isinstance(field, DateField) and isinstance(value, str):
                    value = date.fromisoformat(value)
                setattr(instance, column, value)
            instance.save_base(raw=True)
            count += 1
        return count

    def _fix_sequences(self):
        with connection.cursor() as cursor:
            for table in ORDER:
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
                sequence = cursor.fetchone()[0]
                if not sequence:
                    continue
                cursor.execute(f'SELECT COALESCE(MAX(id), 1) FROM "{table}"')
                maximum = cursor.fetchone()[0]
                cursor.execute('SELECT setval(%s, %s, true)', (sequence, maximum))
                self.stdout.write(f'  sequence {sequence} -> {maximum}')