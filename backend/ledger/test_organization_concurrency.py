from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import TransactionTestCase, Client, override_settings, skipUnlessDBFeature
from django.utils import timezone
from .models import Organisation,UserAccess,SecretaryInvite
from .organization_views import token_hash
from .test_organizations import STORAGES

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],STORAGES=STORAGES)
class OrganizationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.org=Organisation.objects.create(name='Concurrent org')
        self.users=[User.objects.create_user('secretary-'+str(i)) for i in range(2)]
        for user in self.users:UserAccess.objects.create(user=user,organization=self.org,role='secretary')

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_leaves_keep_one_secretary(self):
        gate=Barrier(2)
        def leave(pk):
            close_old_connections()
            try:
                client=Client();client.force_login(User.objects.get(pk=pk));gate.wait(timeout=10)
                return client.post('/api/organization/leave/',{},content_type='application/json').status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(leave,[u.pk for u in self.users]))
        self.assertEqual(sorted(results),[200,400])
        self.assertEqual(UserAccess.objects.filter(organization=self.org,active=True,role='secretary').count(),1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_invite_redemption_creates_only_one_account(self):
        token='test-concurrent-token'
        SecretaryInvite.objects.create(organization=self.org,token_hash=token_hash(token),email='invite@example.com',created_by=self.users[0],expires_at=timezone.now()+timedelta(days=1))
        gate=Barrier(2)
        def join(index):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return Client().post('/signup/?invite='+token,{'username':'joined-'+str(index),'email':'invite@example.com','password1':'Long-Secret-Password-928!','password2':'Long-Secret-Password-928!'}).status_code
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(join,[1,2]))
        self.assertEqual(results.count(302),1)
        self.assertEqual(User.objects.filter(username__startswith='joined-').count(),1)
        self.assertIsNotNone(SecretaryInvite.objects.get(token_hash=token_hash(token)).used_at)
