"""One-off export of existing SQL Server records to a JSON snapshot for PostgreSQL migration.

Reads the legacy database with pyodbc (install `backend/requirements-legacy.txt`),
preserves primary keys and foreign keys, and writes the naive SQL Server datetimes as
UTC instants so they load correctly into PostgreSQL `timestamptz`.

Not part of the running application. Remove after the data transfer is complete.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db.models import BooleanField, DateTimeField, DecimalField, UUIDField
from django.utils import timezone as django_timezone

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
    help = 'Export existing SQL Server records (pyodbc) to a JSON snapshot for PostgreSQL. One-off migration tool.'

    def add_arguments(self, parser):
        parser.add_argument('--output', default=str(settings.BASE_DIR / 'legacy_dump.json'), help='JSON snapshot path.')

    def handle(self, *args, **options):
        try:
            import pyodbc
        except ImportError:
            raise CommandError('pyodbc is required for the legacy export. Install backend/requirements-legacy.txt.')
        snapshot = {'tables': {}, 'order': ORDER, 'generated_at': django_timezone.now().isoformat()}
        connection = pyodbc.connect(self._connection_string())
        connection.autocommit = True
        try:
            for table in ORDER:
                self.stdout.write(f'Exporting {table}...')
                rows = self._read_table(connection, MODELS[table])
                snapshot['tables'][table] = rows
                self.stdout.write(f'  {len(rows)} rows')
        finally:
            connection.close()
        output = Path(options['output'])
        output.write_text(json.dumps(snapshot), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Wrote {output}'))

    def _connection_string(self):
        import os
        override = os.environ.get('LEGACY_CONNECTION_STRING')
        if override:
            return override
        driver = os.environ.get('LEGACY_DB_DRIVER', 'ODBC Driver 18 for SQL Server')
        host = os.environ.get('LEGACY_DB_HOST', 'localhost')
        port = os.environ.get('LEGACY_DB_PORT', '')
        server = f'{host},{port}' if port else host
        database = os.environ.get('LEGACY_DB_NAME', 'FinancialSecretaryTest')
        user = os.environ.get('LEGACY_DB_USER', '')
        password = os.environ.get('LEGACY_DB_PASSWORD', '')
        if user:
            return f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={user};PWD={password};Encrypt=no;TrustServerCertificate=yes'
        return f'DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes'

    def _read_table(self, connection, model):
        table = model._meta.db_table
        cursor = connection.cursor()
        cursor.execute(f'SELECT * FROM [{table}]')
        selects = []
        columns = []
        for d in cursor.description:
            columns.append(d[0])
            if d[1] is bytearray:
                selects.append(f'CONVERT(varchar(40), [{d[0]}], 126) AS [{d[0]}]')
            else:
                selects.append(f'[{d[0]}]')
        cursor.execute(f'SELECT {", ".join(selects)} FROM [{table}] ORDER BY [id]')
        rows = []
        for raw in cursor.fetchall():
            source = dict(zip(columns, raw))
            row = {}
            for field in model._meta.concrete_fields:
                column = field.attname
                value = source.get(column)
                if value is None:
                    row[column] = None
                    continue
                if isinstance(field, DateTimeField):
                    if not isinstance(value, datetime):
                        value = datetime.fromisoformat(str(value))
                    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
                    row[column] = value.isoformat()
                elif isinstance(field, BooleanField):
                    row[column] = bool(value)
                elif isinstance(field, UUIDField):
                    row[column] = str(value).strip('{}')
                elif isinstance(field, DecimalField):
                    row[column] = str(Decimal(str(value)))
                elif isinstance(value, (str, int, bool)):
                    row[column] = value
                else:
                    row[column] = str(value)
            rows.append(row)
        return rows