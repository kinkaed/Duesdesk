from .models import Organisation, UserAccess
import io
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4
from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import mail
from openpyxl import load_workbook
from .models import Member, UserAccess, Payment, AuditEvent, ImportBatch
from .services import record_payment, void_payment
from .views import member_rows

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
        self.assertTrue(AuditEvent.objects.filter(action='member.report_exported', entity_id=str(self.member.pk)).exists())

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
