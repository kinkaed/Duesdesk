from datetime import date
from decimal import Decimal
from uuid import uuid4
from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from .models import Member, Payment, Allocation
from .services import record_payment, plan_payment, parse_amount

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class PaymentTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='secretary',password='TestingOnly25!',is_staff=True)
        self.member=Member.objects.create(full_name='Test Member',joined=date(2026,1,1))
        self.payload={'member_id':self.member.pk,'amount':'100','start_month':'2026-09','payment_date':'2026-09-01','method':'Cash','request_key':str(uuid4())}

    def test_four_month_payment_and_one_receipt(self):
        payment=record_payment(self.payload,self.user)
        allocations=list(payment.allocations.select_related('dues_month').order_by('dues_month__month'))
        self.assertEqual(len(allocations),4)
        self.assertEqual([a.dues_month.month.month for a in allocations],[9,10,11,12])
        self.assertEqual(sum(a.amount for a in allocations),Decimal('100'))
        self.assertEqual(Payment.objects.count(),1)

    def test_partial_then_top_up_skips_paid_months(self):
        record_payment({**self.payload,'amount':'10'},self.user)
        plan=plan_payment(self.member,Decimal('50'),date(2026,9,1))
        self.assertEqual([p['amount'] for p in plan],[Decimal('15'),Decimal('25'),Decimal('10')])
        self.assertEqual([p['status'] for p in plan],['Paid','Paid','Partial'])

    def test_year_rollover(self):
        plan=plan_payment(self.member,Decimal('50'),date(2026,12,1))
        self.assertEqual(plan[1]['month'],date(2027,1,1))

    def test_duplicate_submit_is_idempotent(self):
        a=record_payment(self.payload,self.user)
        b=record_payment(self.payload,self.user)
        self.assertEqual(a.pk,b.pk)
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(Allocation.objects.count(),4)

    def test_invalid_amounts(self):
        for value in ['0','-1','NaN','Infinity','25.001','3001','x']:
            with self.assertRaises(ValueError): parse_amount(value)

    def test_pre_join_and_inactive_rejected(self):
        with self.assertRaises(ValueError): plan_payment(self.member,Decimal('25'),date(2025,12,1))
        self.member.status='Inactive';self.member.save()
        with self.assertRaises(ValueError):record_payment(self.payload,self.user)
        self.assertEqual(Payment.objects.count(),0)

    def test_auth_and_secretary_access(self):
        self.assertEqual(self.client.get('/api/overview/').status_code,401)
        user=User.objects.create_user(username='viewer')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/api/overview/').status_code,403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/overview/?month=2026-09').status_code,200)

    def test_receipt_and_monthly_reporting(self):
        payment=record_payment(self.payload,self.user)
        self.client.force_login(self.user)
        response=self.client.get('/api/overview/?month=2026-10').json()
        self.assertEqual(Decimal(response['metrics']['collections']),0)
        self.assertEqual(Decimal(response['metrics']['assigned']),25)
        self.assertEqual(response['members'][0]['status'],'Paid')
        receipt=self.client.get(f'/receipts/{payment.pk}/')
        self.assertContains(receipt,'December 2026')
        self.assertContains(receipt,payment.receipt_number)

    def test_csrf_is_required(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response=client.post('/api/payments/',self.payload,content_type='application/json')
        self.assertEqual(response.status_code,403)

    def test_csv_neutralizes_formula(self):
        self.member.full_name='=1+1';self.member.save()
        self.client.force_login(self.user)
        response=self.client.get('/export/?month=2026-09')
        self.assertIn("'=1+1",response.content.decode())
