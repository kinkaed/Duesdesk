from io import StringIO
import json
import uuid

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import AuditEvent, Member, Organisation, Payment, UserAccess, Allocation, DuesMonth
from google_auth.testsupport import GoogleTestMixin


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DisableAccountTests(GoogleTestMixin, TestCase):
    def setUp(self):
        self.password='A-Very-Strong-Private-Phrase-42!'
        self.client=Client()
        self.signup_with_google({'username':'founder','email':'founder@example.com','password1':self.password,'password2':self.password,'organization_name':'Disable Co'},client=self.client)
        self.org=Organisation.objects.get(name='Disable Co')
        self.founder=User.objects.get(username='founder')
        self.target=User.objects.create_user('colleague',email='colleague@example.com',password=self.password)
        self.access=UserAccess.objects.create(organization=self.org,user=self.target,role='secretary')
        self.member=Member.objects.create(organization=self.org,full_name='Ama Mensah',joined=timezone.now().date())

    def post(self,url,data=None):
        return self.client.post(url,data or {},content_type='application/json')

    def disable(self,user):
        return self.post(f'/api/accounts/{user.pk}/disable/')

    def record_payment(self):
        month=DuesMonth.objects.create(organization=self.org,month=timezone.now().date().replace(day=1))
        payment=Payment.objects.create(organization=self.org,member=self.member,amount_received=100,payment_date=timezone.now().date(),method='Cash',request_key=uuid.uuid4(),created_by=self.founder,member_name_snapshot=self.member.full_name)
        Allocation.objects.create(organization=self.org,payment=payment,dues_month=month,amount=100)
        return payment

    def test_disable_active_secretary_succeeds(self):
        response=self.disable(self.target)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['already_disabled'],False)
        self.target.refresh_from_db()
        self.access.refresh_from_db()
        self.assertFalse(self.access.active)
        self.assertFalse(self.target.is_active)

    def test_disable_is_idempotent_on_repeat(self):
        self.assertEqual(self.disable(self.target).status_code,200)
        second=self.disable(self.target)
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.json()['already_disabled'],True)
        # A double click must not spam the audit history with duplicate events.
        self.assertEqual(AuditEvent.objects.filter(action='account.disabled',entity_id=str(self.target.pk)).count(),1)

    def sign_in(self,username):
        client=Client()
        client.post('/login/',{'username':username,'password':self.password})
        return client

    def test_disabled_account_cannot_log_in_or_reach_data(self):
        self.record_payment()
        self.assertEqual(self.disable(self.target).status_code,200)
        self.assertFalse(self.target.__class__.objects.get(pk=self.target.pk).is_active)
        fresh=self.sign_in('colleague')
        # Login is refused outright, so there is no session to reach any data with.
        self.assertEqual(fresh.get('/api/members/').status_code,401)
        self.assertEqual(fresh.get('/api/overview/').status_code,401)

    def test_history_survives_disable(self):
        payment=self.record_payment()
        self.assertEqual(self.disable(self.target).status_code,200)
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertEqual(payment.created_by_id,self.founder.pk)
        self.assertTrue(AuditEvent.objects.filter(action='account.disabled').exists())
        response=self.client.get('/api/audit/')
        self.assertEqual(response.status_code,200)
        self.assertIn('account.disabled',response.content.decode())

    def test_no_write_reuses_a_fixed_key(self):
        # Every write must target an existing row or insert a fresh one. A fixed or
        # reused key is what produces a unique violation, so assert none exist.
        self.assertEqual(self.disable(self.target).status_code,200)
        self.access.refresh_from_db()
        # auth_user is updated in place by primary key, never re-inserted.
        self.assertEqual(User.objects.filter(pk=self.target.pk).count(),1)
        self.assertFalse(User.objects.get(pk=self.target.pk).is_active)
        # The audit event is a single fresh insert.
        self.assertEqual(AuditEvent.objects.filter(action='account.disabled').count(),1)

    def test_cannot_disable_self(self):
        response=self.disable(self.founder)
        self.assertEqual(response.status_code,400)
        self.assertIn("You can't disable your own account",response.json()['error'])
        self.assertTrue(UserAccess.objects.get(user=self.founder).active)
        self.assertTrue(User.objects.get(pk=self.founder.pk).is_active)

    def test_organization_can_never_be_left_without_a_secretary(self):
        # Only an active secretary in the same organization may disable an account,
        # and never themselves. So the final secretary always has a second lock
        # against removal: nobody else is left who could press the button.
        self.assertEqual(self.disable(self.target).status_code,200)
        remaining=UserAccess.objects.filter(organization=self.org,role='secretary',active=True)
        self.assertEqual([a.user.username for a in remaining],['founder'])
        self.assertEqual(self.disable(self.founder).status_code,400)
        self.assertEqual(UserAccess.objects.filter(organization=self.org,role='secretary',active=True).count(),1)

    def test_cannot_disable_across_organizations(self):
        other=Organisation.objects.create(name='Other Co')
        stranger=User.objects.create_user('stranger',email='stranger@example.com',password=self.password)
        UserAccess.objects.create(organization=other,user=stranger,role='secretary')
        self.assertEqual(self.disable(stranger).status_code,404)
        self.assertTrue(UserAccess.objects.get(user=stranger).active)

    def test_auditor_cannot_disable(self):
        auditor=User.objects.create_user('auditor',email='auditor@example.com',password=self.password)
        UserAccess.objects.create(organization=self.org,user=auditor,role='auditor')
        self.client.force_login(auditor)
        self.assertEqual(self.disable(self.target).status_code,403)
        self.assertTrue(UserAccess.objects.get(user=self.target).active)

    def test_assign_access_restores_a_disabled_login(self):
        self.assertEqual(self.disable(self.target).status_code,200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        call_command('assign_access','colleague','--organization-id',str(self.org.pk),'--role','secretary',stdout=StringIO())
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertTrue(UserAccess.objects.get(user=self.target).active)
        self.assertEqual(self.sign_in('colleague').get('/api/members/').status_code,200)

    # Enabling is the counterpart to disabling above. It exists so a secretary can
    # undo a mistake from the product instead of needing a server shell.

    def enable(self,user):
        return self.post(f'/api/accounts/{user.pk}/enable/')

    def test_enable_restores_a_disabled_account(self):
        self.assertEqual(self.disable(self.target).status_code,200)
        restored=self.enable(self.target)
        self.assertEqual(restored.status_code,200)
        self.assertEqual(restored.json()['already_enabled'],False)
        self.target.refresh_from_db()
        self.access.refresh_from_db()
        self.assertTrue(self.access.active)
        self.assertTrue(self.target.is_active)
        # The account is genuinely usable again, not just flagged in the database.
        self.assertEqual(self.sign_in('colleague').get('/api/members/').status_code,200)
        self.assertTrue(AuditEvent.objects.filter(action='account.enabled',entity_id=str(self.target.pk)).exists())

    def test_enable_is_idempotent_on_repeat(self):
        self.assertEqual(self.disable(self.target).status_code,200)
        first=self.enable(self.target)
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json()['already_enabled'],False)
        second=self.enable(self.target)
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.json()['already_enabled'],True)
        self.assertEqual(AuditEvent.objects.filter(action='account.enabled',entity_id=str(self.target.pk)).count(),1)

    def test_enable_never_changes_the_role(self):
        # Enabling restores access only. It must not be a way to promote an
        # account, so an auditor comes back as an auditor.
        auditor=User.objects.create_user('restored',email='restored@example.com',password=self.password)
        access=UserAccess.objects.create(organization=self.org,user=auditor,role='auditor')
        self.assertEqual(self.disable(auditor).status_code,200)
        self.assertEqual(self.enable(auditor).status_code,200)
        access.refresh_from_db()
        self.assertTrue(access.active)
        self.assertEqual(access.role,'auditor')
        self.assertFalse(auditor.__class__.objects.get(pk=auditor.pk).is_superuser)
        self.assertEqual(json.loads(AuditEvent.objects.filter(action='account.enabled',entity_id=str(auditor.pk)).first().details)['role'],'auditor')

    def test_cannot_enable_across_organizations(self):
        other=Organisation.objects.create(name='Other Co')
        stranger=User.objects.create_user('stranger',email='stranger@example.com',password=self.password)
        access=UserAccess.objects.create(organization=other,user=stranger,role='secretary')
        access.active=False;access.save(update_fields=['active'])
        self.assertEqual(self.enable(stranger).status_code,404)
        self.assertFalse(UserAccess.objects.get(user=stranger).active)

    def test_auditor_cannot_enable(self):
        auditor=User.objects.create_user('auditor',email='auditor@example.com',password=self.password)
        UserAccess.objects.create(organization=self.org,user=auditor,role='auditor')
        target=User.objects.create_user('target',email='target@example.com',password=self.password)
        access=UserAccess.objects.create(organization=self.org,user=target,role='auditor')
        access.active=False;access.save(update_fields=['active'])
        target.is_active=False;target.save(update_fields=['is_active'])
        self.client.force_login(auditor)
        response=self.enable(target)
        self.assertEqual(response.status_code,403)
        self.assertFalse(UserAccess.objects.get(user=target).active)
        self.assertFalse(User.objects.get(pk=target.pk).is_active)

    def test_non_superuser_cannot_enable_a_superuser(self):
        root=User.objects.create_user('root',email='root@example.com',password=self.password,is_superuser=True)
        access=UserAccess.objects.create(organization=self.org,user=root,role='secretary')
        # Reached without going through disable, which refuses a superuser target
        # outright, so the stored state is set directly to model the legacy case.
        access.active=False;access.save(update_fields=['active'])
        root.is_active=False;root.save(update_fields=['is_active'])
        response=self.enable(root)
        self.assertEqual(response.status_code,400)
        self.assertIn('superuser',response.json()['error'])
        self.assertFalse(UserAccess.objects.get(user=root).active)
        self.assertFalse(User.objects.get(pk=root.pk).is_active)


class DisableUnderUserMemberConstraintTests(GoogleTestMixin, TransactionTestCase):
    """Runs outside a wrapping transaction so it can add and drop a real index."""

    reset_sequences = False

    def setUp(self):
        self.password = 'A-Very-Strong-Private-Phrase-42!'
        self.client = Client()
        self.signup_with_google({'username': 'founder', 'email': 'founder@example.com', 'password1': self.password, 'password2': self.password, 'organization_name': 'Constraint Co'},client=self.client)
        self.org = Organisation.objects.get(name='Constraint Co')
        self.founder = User.objects.get(username='founder')
        self.target = User.objects.create_user('colleague', email='colleague@example.com', password=self.password)
        self.access = UserAccess.objects.create(organization=self.org, user=self.target, role='secretary')

    def test_disable_survives_a_unique_constraint_on_user_and_member(self):
        # Production reportedly carries a unique constraint on
        # ledger_useraccess(user_id, member_id). Reproduce it exactly, then confirm
        # the disable still succeeds, proving the path never collides on it.
        with connection.cursor() as cursor:
            cursor.execute('CREATE UNIQUE INDEX uq_useraccess_user_member ON ledger_useraccess (user_id, member_id)')
        try:
            response = self.client.post(f'/api/accounts/{self.target.pk}/disable/', {}, content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['already_disabled'], False)
            self.access.refresh_from_db()
            self.assertFalse(self.access.active)
        finally:
            with connection.cursor() as cursor:
                cursor.execute('DROP INDEX IF EXISTS uq_useraccess_user_member')
