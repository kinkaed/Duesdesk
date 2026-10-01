from datetime import date
from decimal import Decimal
from uuid import uuid4
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.db.models import Sum
from .models import Organisation, UserAccess, Member, Payment


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class FirstPaymentApiTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Payment regression')
        user = User.objects.create_user('secretary', password='TestPassword!')
        UserAccess.objects.create(user=user, organization=self.org, role='secretary')
        self.client.force_login(user)

    def add_member(self, **extra):
        response = self.client.post('/api/members/', {'name':'New member','joined':'2026-01-01','status':'Active',**extra}, content_type='application/json')
        self.assertEqual(response.status_code, 201)
        return response.json()['id']

    def pay(self, member, amount):
        payload = {'member_id':member,'amount':amount,'start_month':'2026-01','payment_date':'2026-01-01','method':'Cash','request_key':str(uuid4())}
        preview = self.client.post('/api/payments/preview/',payload,content_type='application/json')
        saved = self.client.post('/api/payments/',payload,content_type='application/json')
        return preview, saved

    def test_new_member_can_pay_any_positive_amount_within_supported_range(self):
        for amount in ('10.50','25','50','100','125.75','3000'):
            with self.subTest(amount=amount):
                member = self.add_member()
                preview, saved = self.pay(member, amount)
                self.assertEqual(preview.status_code, 200, preview.content)
                self.assertEqual(saved.status_code, 201, saved.content)
                payment = Payment.objects.get(pk=saved.json()['id'])
                self.assertEqual(payment.amount_received, Decimal(amount))
                self.assertEqual(payment.allocations.aggregate(total=Sum('amount'))['total'], Decimal(amount))

    def test_existing_member_can_pay_more_than_one_month(self):
        member = self.add_member()
        self.pay(member, '25')
        preview, saved = self.pay(member, '100')
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(len(preview.json()['allocations']), 4)
        self.assertEqual(Payment.objects.filter(member_id=member).count(), 2)

    def test_explicit_end_date_explains_why_preview_and_save_are_blocked(self):
        member = self.add_member(billing_end='2026-01')
        preview, saved = self.pay(member, '100')
        for response in (preview, saved):
            self.assertEqual(response.status_code, 400)
            self.assertIn('clear or extend the membership end month', response.json()['error'])
        self.assertFalse(Payment.objects.exists())
        response = self.client.post(f'/api/members/{member}/', {'name':'New member','joined':'2026-01-01','status':'Active','billing_end':''}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.pay(member, '100')[1].status_code, 201)
