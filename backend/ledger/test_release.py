import io
import json
from datetime import date
from unittest.mock import patch
from uuid import uuid4
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import TestCase, override_settings
from openpyxl import load_workbook
from .models import Member, Payment, Allocation, ImportBatch
from .services import record_payment

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReleaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('release-secretary', is_staff=True)
        self.client.force_login(self.user)
        self.member = Member.objects.create(full_name='Release sample', joined=date(2026, 9, 1))
        self.payload = {'member_id': self.member.pk, 'amount': '100', 'start_month': '2026-09',
                        'payment_date': '2026-09-01', 'method': 'Cash', 'request_key': str(uuid4())}

    def test_malformed_requests_never_write(self):
        for value in ['not json', '[]', 'null', '{}']:
            response = self.client.post('/api/payments/', value, content_type='application/json')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_invalid_payment_rolls_back(self):
        response = self.client.post('/api/payments/', json.dumps({**self.payload, 'reference': 'x'*81}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Allocation.objects.exists())

    def test_import_commit_failure_rolls_back_every_row(self):
        csv = f'member_id,amount,start_month,payment_date,method,reference\n{self.member.pk},25,2026-09,2026-09-01,Cash,valid\n{self.member.pk},25,2026-09,2026-09-01,Cash,valid\n'
        token = self.client.post('/api/import/preview/', {'kind':'payments','file':SimpleUploadedFile('payments.csv', csv.encode())}).json()['token']
        self.member.billing_end = date(2026, 9, 1)
        self.member.save()
        response = self.client.post('/api/import/commit/', json.dumps({'token': token}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(ImportBatch.objects.exists())

    def test_health_database_outage_is_service_unavailable(self):
        with patch('ledger.views.connection.cursor', side_effect=OperationalError('private database failure')):
            response = self.client.get('/health/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'private database failure', response.content)

    def test_report_separates_arrears_and_advance_payments(self):
        record_payment({**self.payload, 'amount': '10'}, self.user)
        record_payment({**self.payload, 'amount': '25', 'start_month': '2026-10', 'request_key': str(uuid4())}, self.user)
        with patch('ledger.views.timezone.localdate', return_value=date(2026,9,24)):
            response = self.client.get(f'/api/members/{self.member.pk}/report/')
        book = load_workbook(io.BytesIO(response.content))
        summary = dict(book['Summary'].values)
        self.assertEqual(summary['Outstanding through September 2026'], 15)
        self.assertEqual(summary['Total paid (excludes voids)'], 35)
        rows = list(book['Monthly balances'].values)
        self.assertEqual(rows[1][4], 'Partial')
        self.assertEqual(rows[2][4], 'Advance payment')
        self.assertEqual(rows[2][3], 0)


    def test_import_preview_rejects_overlong_reference_and_notes(self):
        for field, limit in [('reference', 80), ('notes', 500)]:
            csv = f'member_id,amount,start_month,payment_date,method,{field}\n{self.member.pk},25,2026-09,2026-09-01,Cash,{"x" * (limit + 1)}\n'
            response = self.client.post('/api/import/preview/', {'kind':'payments','file':SimpleUploadedFile('payments.csv', csv.encode())})
            self.assertEqual(response.status_code, 400)
            self.assertIn('Row 2:', response.json()['error'])
            self.assertIn(str(limit), response.json()['error'])
            self.assertNotIn('token', response.json())
        self.assertFalse(Payment.objects.exists())
