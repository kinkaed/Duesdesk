from .models import Organisation, UserAccess
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import date
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch
from django.test import TestCase, Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.core import mail
from openpyxl import load_workbook
from .models import Member, UserAccess, Payment, AuditEvent, ImportBatch
from .services import next_month, parse_month, record_payment, report_horizon, void_payment
from .views import member_rows
from . import views as ledger_views

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class OperationalTests(TestCase):
    def setUp(self):
        self.org=Organisation.objects.create(name="Test Organization")
        self.secretary=User.objects.create_user('sec',password='A-Fresh-Strong-Password!',is_staff=True,email='sec@example.com')
        UserAccess.objects.create(organization=self.org,user=self.secretary,role="secretary")
        self.member=Member.objects.create(organization=self.org,full_name='Sample',joined=date(2026,9,1))
        self.other=Member.objects.create(organization=self.org,full_name='Other',joined=date(2026,9,1))
        self.auditor=User.objects.create_user('auditor',password='A-Fresh-Strong-Password!',email='auditor@example.com')
        UserAccess.objects.create(organization=self.org,user=self.auditor,role='auditor')
        self.payload={'member_id':self.member.pk,'amount':'100','start_month':'2026-09','payment_date':'2026-09-01','method':'Cash','request_key':str(uuid4())}
        self.client.force_login(self.secretary)

    def post(self,url,data):return self.client.post(url,json.dumps(data),content_type='application/json')

    def test_members_are_records_and_never_accounts(self):
        # A member is a row in the organization, not a login.
        self.assertEqual(Member.objects.count(),2)
        self.assertFalse(User.objects.filter(email__endswith='@example.com').exclude(email__in=['sec@example.com','auditor@example.com']).exists())
        self.assertFalse(hasattr(self.member,'user'))
        roles={choice[0] for choice in UserAccess._meta.get_field('role').choices}
        self.assertNotIn('member',roles)
        # No route may mint a member account.
        self.assertEqual(self.post('/api/accounts/',{'username':'m1','email':'m1@example.com','password':'An-Excellent-Private-Phrase!','role':'member','member_id':self.member.pk}).status_code,400)
        self.assertFalse(User.objects.filter(username='m1').exists())
        # Public signup only ever creates secretary access.
        visitor=Client()
        response=visitor.post('/signup/',{'username':'fresh','email':'fresh@example.com','password1':'Another-Strong-Phrase-42!','password2':'Another-Strong-Phrase-42!','organization_name':'Fresh Association'})
        self.assertEqual(response.status_code,302,response.content[:300])
        self.assertEqual(User.objects.get(username='fresh').access.role,'secretary')

    def test_auditor_read_only(self):
        self.client.force_login(self.auditor)
        self.assertEqual(len(self.client.get('/api/members/').json()['members']),2)
        self.assertEqual(self.post('/api/members/',{'name':'No'}).status_code,403)
        self.assertEqual(self.client.get('/api/audit/').status_code,200)
        self.assertEqual(self.client.get('/api/settings/').status_code,403)

    def test_void_preserves_receipt_restores_balance_and_audit(self):
        p=record_payment(self.payload,self.secretary)
        void_payment(p.pk,self.secretary,'Wrong covered period')
        self.assertEqual(Payment.objects.count(),1)
        row=member_rows(date(2026,9,1),self.secretary)[1] if self.member.full_name=='Z' else next(x for x in member_rows(date(2026,9,1),self.secretary) if x['id']==self.member.pk)
        self.assertEqual(Decimal(row['balance']),25)
        self.assertEqual(AuditEvent.objects.filter(action='payment.voided').count(),1)
        self.assertContains(self.client.get(f'/receipts/{p.pk}/'),'VOID')
        with self.assertRaises(ValueError):void_payment(p.pk,self.secretary,'Duplicate void')

    def test_changed_duplicate_request_is_rejected(self):
        record_payment(self.payload,self.secretary)
        with self.assertRaises(ValueError):record_payment({**self.payload,'start_month':'2026-10'},self.secretary)

    def test_receipt_name_survives_member_rename(self):
        p=record_payment(self.payload,self.secretary)
        self.member.full_name='Changed Name';self.member.save()
        self.assertContains(self.client.get(f'/receipts/{p.pk}/'),'Sample')

    def test_ending_membership_preserves_old_arrears(self):
        self.member.billing_end=date(2026,10,1);self.member.status='Inactive';self.member.save()
        row=next(x for x in member_rows(date(2026,12,1),self.secretary) if x['id']==self.member.pk)
        self.assertEqual(Decimal(row['arrears']),50)
        self.assertEqual(row['status'],'Not due')

    def test_edit_member_and_audit(self):
        data={'name':'Updated','phone':'024','email':'','joined':'2026-09-01','status':'Active'}
        self.assertEqual(self.post(f'/api/members/{self.member.pk}/',data).status_code,200)
        self.assertTrue(AuditEvent.objects.filter(action='member.updated').exists())

    def test_excel_is_scoped_and_formula_safe(self):
        self.member.full_name='=SUM(1,2)';self.member.save()
        self.client.force_login(self.auditor)
        response=self.client.get('/export/excel/?month=2026-09')
        book=load_workbook(io.BytesIO(response.content));sheet=book.active
        self.assertEqual(sheet.max_row,3)
        cells=[sheet.cell(row=r,column=2).value for r in range(2,sheet.max_row+1)]
        self.assertIn("'=SUM(1,2)",cells)
        for row in range(2,sheet.max_row+1):
            if sheet.cell(row=row,column=2).value=="'=SUM(1,2)":
                self.assertEqual(sheet.cell(row=row,column=2).data_type,'s')

    def test_import_preview_commit_and_duplicate_prevention(self):
        file=SimpleUploadedFile('members.csv',b'name,joined\nImported Person,2026-09-01\n')
        response=self.client.post('/api/import/preview/',{'kind':'members','file':file})
        self.assertEqual(response.status_code,200,response.content)
        token=response.json()['token']
        self.assertEqual(Member.objects.count(),2)
        self.assertEqual(self.post('/api/import/commit/',{'token':token}).status_code,200)
        self.assertEqual(Member.objects.count(),3)
        self.assertEqual(self.post('/api/import/commit/',{'token':token}).status_code,400)
        self.assertEqual(ImportBatch.objects.count(),1)

    def test_import_invalid_row_does_not_write(self):
        file=SimpleUploadedFile('members.csv',b'name,joined\nGood,2026-09-01\nBad,not-a-date\n')
        self.assertEqual(self.client.post('/api/import/preview/',{'kind':'members','file':file}).status_code,400)
        self.assertEqual(Member.objects.count(),2)

    def test_import_payments_atomic_and_recalculates(self):
        file=SimpleUploadedFile('payments.csv',f'member_id,amount,start_month,payment_date,method\n{self.member.pk},10,2026-09,2026-09-01,Cash\n{self.member.pk},15,2026-09,2026-09-01,Cash\n'.encode())
        token=self.client.post('/api/import/preview/',{'kind':'payments','file':file}).json()['token']
        self.assertEqual(self.post('/api/import/commit/',{'token':token}).status_code,200)
        row=next(x for x in member_rows(date(2026,9,1),self.secretary) if x['id']==self.member.pk)
        self.assertEqual(row['status'],'Paid')

    def test_user_creation_validates_password_and_role(self):
        data={'username':'newuser','email':'new@example.com','password':'short','role':'auditor'}
        self.assertEqual(self.post('/api/accounts/',data).status_code,400)
        data['password']='An-Excellent-Private-Phrase!'
        self.assertEqual(self.post('/api/accounts/',data).status_code,201)
        new=User.objects.get(username='newuser')
        self.assertFalse(new.is_superuser)
        # A secretary cannot be minted here; they must accept an invitation.
        self.assertEqual(self.post('/api/accounts/',{'username':'sec2','email':'sec2@example.com','password':'An-Excellent-Private-Phrase!','role':'secretary'}).status_code,400)
        self.assertFalse(User.objects.filter(username='sec2').exists())
        self.assertIsNone(new.access.member)
        self.assertNotIn(data['password'],''.join(AuditEvent.objects.values_list('details',flat=True)))

    def test_accounts_list_keeps_disabled_role_and_does_not_scale_queries(self):
        # A disabled account must still report the role it holds, otherwise a
        # secretary cannot tell what access they would be restoring.
        self.assertEqual(self.post(f'/api/accounts/{self.auditor.pk}/disable/',{}).status_code,200)
        users={u['username']:u for u in self.client.get('/api/accounts/').json()['users']}
        self.assertEqual(users['auditor']['role'],'auditor')
        self.assertFalse(users['auditor']['active'])
        self.assertEqual(users['sec']['role'],'secretary')
        self.assertTrue(users['sec']['active'])
        # The role must come from the joined access row, so the list has to cost the
        # same whatever the size of the organization. A per-user membership() lookup
        # would pass the assertions above while still querying once per account.
        def account_queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get('/api/accounts/')
            return len(captured.captured_queries)
        before=account_queries()
        for index in range(4):
            extra=User.objects.create_user(f'extra{index}',email=f'extra{index}@example.com',password='A-Fresh-Strong-Password!')
            UserAccess.objects.create(organization=self.org,user=extra,role='auditor')
        self.assertEqual(account_queries(),before)

    def test_disabling_user_blocks_existing_session(self):
        self.assertEqual(self.post(f'/api/accounts/{self.auditor.pk}/disable/',{}).status_code,200)
        self.client.force_login(self.auditor)
        # Disabling clears is_active too, so the old session is dead outright (401)
        # rather than a live session that merely fails a role check (403).
        self.assertEqual(self.client.get('/api/overview/').status_code,401)
        # A read-only auditor is still refused writes, checked with a live account.
        self.auditor.is_active=True;self.auditor.save(update_fields=['is_active'])
        self.auditor.access.active=True;self.auditor.access.save(update_fields=['active'])
        self.client.force_login(self.auditor)
        self.assertEqual(self.post(f'/api/accounts/{self.secretary.pk}/disable/',{}).status_code,403)

    def test_password_recovery_and_throttle(self):
        self.client.logout()
        self.client.post('/account/reset/',{'email':'auditor@example.com'})
        self.assertEqual(len(mail.outbox),1)
        self.client.post('/account/reset/',{'email':'auditor@example.com'})
        self.assertEqual(len(mail.outbox),1)

    def test_login_locks_after_failed_attempts(self):
        self.client.logout()
        for _ in range(5):self.client.post('/login/',{'username':'auditor','password':'wrong'})
        response=self.client.post('/login/',{'username':'auditor','password':'A-Fresh-Strong-Password!'})
        self.assertEqual(response.status_code,429)

    def test_security_headers(self):
        response=self.client.get('/')
        self.assertIn("frame-ancestors 'none'",response['Content-Security-Policy'])
        self.assertEqual(response['Cache-Control'],'no-store, private')


    def test_single_member_report_and_void_totals(self):
        payment = record_payment(self.payload, self.secretary)
        record_payment({**self.payload, 'member_id': self.other.pk, 'request_key': str(uuid4())}, self.secretary)
        url = f'/api/members/{self.member.pk}/report/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.content))
        summary = dict(workbook['Summary'].values)
        self.assertEqual(summary['Total paid (excludes voids)'], 100)
        self.assertEqual(workbook['Payments'].max_row, 2)
        self.assertEqual(workbook['Months covered'].max_row, 5)
        self.assertNotIn('Other', str(list(workbook['Summary'].values)))
        self.assertEqual(workbook['Payments']['C2'].value, 100)
        self.assertEqual(workbook['Months covered']['C2'].value, 25)
        void_payment(payment.pk, self.secretary, 'Wrong payment recorded')
        workbook = load_workbook(io.BytesIO(self.client.get(url).content))
        self.assertEqual(dict(workbook['Summary'].values)['Total paid (excludes voids)'], 0)
        self.assertEqual(workbook['Payments']['F2'].value, 'VOID')
        # The action name is part of the contract the audit history reports on, so
        # the rename from member.report_exported is asserted rather than assumed:
        # the old spelling must no longer be written.
        self.assertTrue(AuditEvent.objects.filter(action='member.export.generated', entity_id=str(self.member.pk)).exists())
        self.assertFalse(AuditEvent.objects.filter(action='member.report_exported').exists())

    def test_member_report_access_empty_and_formula_safety(self):
        url = f'/api/members/{self.member.pk}/report/'
        self.member.full_name = '=1+1'
        self.member.save()
        workbook = load_workbook(io.BytesIO(self.client.get(url).content))
        self.assertEqual(workbook['Summary']['B4'].data_type, 's')
        self.assertEqual(workbook['Payments'].max_row, 1)
        self.assertEqual(self.client.get('/api/members/999999/report/').status_code, 404)
        for user in [self.auditor]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_denied_json_endpoints_return_a_readable_error(self):
        # A bare 403 left the SPA unable to parse a body at all, so every
        # role-gated JSON endpoint must answer with the api() error shape.
        self.client.force_login(self.auditor)
        denied = ['/api/settings/', '/api/accounts/', '/api/import/template/', '/api/invites/',
                  f'/api/members/{self.member.pk}/report/']
        for url in denied:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response['Content-Type'], 'application/json', url)
            self.assertTrue(response.json()['error'], url)
        # Not @api-decorated, so it carries its own role check.
        response = self.post('/api/branding/preview/', {})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()['error'])

    def test_unknown_report_kind_is_rejected_not_silently_balances(self):
        default = self.client.get('/export/?month=2026-09')
        self.assertEqual(default.status_code, 200)
        self.assertIn(b'Member ID', default.content)
        payments = self.client.get('/export/?month=2026-09&kind=payments')
        self.assertEqual(payments.status_code, 200)
        self.assertIn(b'Receipt', payments.content)
        unknown = self.client.get('/export/?month=2026-09&kind=arrears')
        self.assertEqual(unknown.status_code, 400)
        self.assertTrue(unknown.json()['error'])
        self.assertEqual(self.client.get('/export/excel/?month=2026-09&kind=arrears').status_code, 400)

    def test_replayed_payment_save_answers_ok_not_created(self):
        first = self.post('/api/payments/', self.payload)
        self.assertEqual(first.status_code, 201)
        replay = self.post('/api/payments/', self.payload)
        self.assertEqual(replay.status_code, 200)
        # Same payment is reported, only the status changes. The amount string
        # keeps its pre-existing formatting (in-memory vs stored 2dp Decimal).
        self.assertEqual({k: v for k, v in replay.json().items() if k != 'amount'},
                         {k: v for k, v in first.json().items() if k != 'amount'})
        self.assertEqual(Decimal(replay.json()['amount']), Decimal(first.json()['amount']))
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='payment.recorded').count(), 1)
        # A changed payload under the same key stays a conflict, not a replay.
        self.assertEqual(self.post('/api/payments/', {**self.payload, 'amount': '50'}).status_code, 400)

    def test_serve_rejects_unknown_app_env_before_django_loads(self):
        backend = Path(__file__).resolve().parent.parent
        # Everything above the Django import is the guard, so this also pins the
        # requirement that validation happens before settings are read.
        guard = (backend / 'serve.py').read_text(encoding='utf-8').split('from django.core.wsgi')[0]

        def check(value):
            previous = os.environ.get('APP_ENV')
            os.environ['APP_ENV'] = value
            try:
                namespace = {}
                exec(compile(guard, 'serve.py', 'exec'), namespace)
                return namespace['validate_app_env'](os.environ['APP_ENV'])
            finally:
                if previous is None:
                    os.environ.pop('APP_ENV', None)
                else:
                    os.environ['APP_ENV'] = previous

        # A wrongly cased value is normalized, not silently treated as local.
        self.assertEqual(check('production'), 'production')
        self.assertEqual(check('  Production  '), 'production')
        self.assertEqual(check('local'), 'local')
        for bad in ['prod', 'staging', '', 'produciton', 'production2']:
            with self.assertRaises(SystemExit):
                check(bad)
        # The real entrypoint exits with the message instead of serving.
        result = subprocess.run([sys.executable, 'serve.py'], cwd=backend, capture_output=True, text=True,
                                timeout=60, env={**os.environ, 'APP_ENV': 'prod'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('APP_ENV must be one of', result.stderr + result.stdout)

    def test_render_lifecycle_runs_migrations_only_before_deploy(self):
        # Read as text: the project takes no YAML dependency for its own tests.
        spec = (Path(__file__).resolve().parent.parent.parent / 'render.yaml').read_text(encoding='utf-8')

        def field(name):
            return next(line for line in spec.splitlines() if line.strip().startswith(f'{name}:'))

        # Migrations must not run during the build: every build would migrate.
        self.assertNotIn('migrate', field('buildCommand'))
        self.assertIn('verify_static', field('buildCommand'))
        pre = field('preDeployCommand')
        self.assertIn('migrate --noinput', pre)
        self.assertIn('deployment_check', pre)
        self.assertEqual(field('startCommand').split(':', 1)[1].strip(), 'cd backend && python serve.py')

    def test_read_only_post_skips_the_organization_write_lock(self):
        body = {'member_id': self.member.pk, 'amount': '50', 'start_month': '2026-09'}
        self.assertTrue(ledger_views.payment_preview.read_only)
        self.assertFalse(getattr(ledger_views.payments, 'read_only', False))
        with patch('ledger.views.Organisation.objects.select_for_update') as locked:
            preview = self.post('/api/payments/preview/', body)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(len(preview.json()['allocations']), 2)
        self.assertFalse(locked.called, 'a read-only POST must not take the write lock')
        # Recording a payment still serializes on the organization row.
        with patch('ledger.views.Organisation.objects.select_for_update') as locked:
            self.assertEqual(self.post('/api/payments/', self.payload).status_code, 201)
        self.assertTrue(locked.called, 'record_payment must keep its lock')
        # Role denial for a read-only endpoint is unchanged.
        self.client.force_login(self.auditor)
        self.assertEqual(self.post('/api/payments/preview/', body).status_code, 403)

    def test_report_month_is_bounded_by_organization_data(self):
        today = timezone.localdate().replace(day=1)
        covered = next_month(next_month(next_month(today)))
        beyond = next_month(covered)
        # Dates the user types into records keep the original wide range.
        self.assertEqual(parse_month('2100-12'), date(2100, 12, 1))
        self.assertEqual(report_horizon(self.org), today)
        for url in ['/api/overview/', '/export/', f'/api/members/{self.member.pk}/report/']:
            self.assertEqual(self.client.get(f'{url}?month={beyond:%Y-%m}').status_code, 400, url)
        refused = self.client.get('/api/overview/?month=2100-12')
        self.assertIn('Reports are available up to', refused.json()['error'])
        self.assertEqual(self.client.get(f'/api/overview/?month={today:%Y-%m}').status_code, 200)
        # The horizon grows with the organization's own data, not a fixed date.
        record_payment({**self.payload, 'start_month': f'{today:%Y-%m}',
                        'payment_date': f'{today:%Y-%m}-01', 'request_key': str(uuid4())}, self.secretary)
        self.assertEqual(report_horizon(self.org), covered)
        self.assertEqual(self.client.get(f'/api/overview/?month={covered:%Y-%m}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/overview/?month={beyond:%Y-%m}').status_code, 400)

    def test_member_report_uses_the_requested_month(self):
        url = f'/api/members/{self.member.pk}/report/'
        today = timezone.localdate().replace(day=1)
        tail = next_month(next_month(next_month(next_month(today))))
        # A future last billable month is organization data, so it sets the horizon
        # the report is allowed to reach.
        self.member.billing_end = tail.replace(day=28)
        self.member.save(update_fields=['billing_end'])
        self.assertEqual(report_horizon(self.org), tail)

        def summary(month):
            return dict(load_workbook(io.BytesIO(self.client.get(f'{url}?month={month:%Y-%m}').content))['Summary'].values)

        # Nothing paid yet: arrears cover every month up to the selected one.
        self.assertEqual(summary(today)['Outstanding through ' + today.strftime('%B %Y')], 25)
        self.assertEqual(summary(tail)['Outstanding through ' + tail.strftime('%B %Y')], 125)
        self.assertIn('through ' + tail.strftime('%B %Y'), summary(tail)['Report coverage'])
        # A payment covering later months is still reported for an earlier month.
        record_payment({**self.payload, 'start_month': f'{today:%Y-%m}',
                        'payment_date': f'{today:%Y-%m}-01', 'request_key': str(uuid4())}, self.secretary)
        book = load_workbook(io.BytesIO(self.client.get(f'{url}?month={today:%Y-%m}').content))
        self.assertEqual(book['Payments'].max_row, 2)
        self.assertEqual(book['Months covered'].max_row, 5)
        self.assertEqual(dict(book['Summary'].values)['Outstanding through ' + today.strftime('%B %Y')], 0)
        # The filename contract is unchanged: it carries the generation date.
        response = self.client.get(f'{url}?month={tail:%Y-%m}')
        self.assertIn(f'filename="{self.member.code}-payment-report-{timezone.localdate().isoformat()}.xlsx"',
                      response['Content-Disposition'])
