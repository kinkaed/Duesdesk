"""A disabled organization is hidden everywhere, not only from ``membership()``.

``disable_stale_organizations`` closes an abandoned workspace by setting
``disabled_at``. That single column has to be honoured by every path that could
otherwise resolve the organization: the tenant queries, the API gate and app
shell, the public login branding lookup, invitation validation and the logo
endpoint. ``membership()`` being right is not enough on its own, so each of those
paths is driven here directly rather than inferred from it.
"""
from datetime import date, timedelta
from uuid import uuid4

from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from .access import membership, organization_for, role_for, visible_members, visible_payments
from .context import site_context
from .models import AuditEvent, Member, Organisation, Payment, SecretaryInvite
from .organization_views import token_hash, valid_invite

STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
LOGO = b'\x89PNG\r\n\x1a\n' + b'0' * 32


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], STORAGES=STORAGES)
class DisabledOrganizationTests(TestCase):
    def make_org(self, name, disabled):
        org = Organisation.objects.create(
            name=name, disabled_at=timezone.now() if disabled else None)
        user = User.objects.create_user(f'user-{org.pk}')
        from .models import UserAccess
        UserAccess.objects.create(user=user, organization=org, role='secretary')
        member = Member.objects.create(organization=org, full_name='Inside Member',
                                       joined=date(2026, 1, 1))
        payment = Payment.objects.create(
            organization=org, member=member, amount_received=100, payment_date=date(2026, 1, 1),
            method='Cash', request_key=uuid4(), created_by=user,
            member_name_snapshot=member.full_name)
        return org, user, member, payment

    def setUp(self):
        self.org, self.user, self.member, self.payment = self.make_org('Closed Co', disabled=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_tenant_queries_resolve_nothing_but_keep_the_row(self):
        self.assertIsNone(membership(self.user))
        self.assertIsNone(organization_for(self.user))
        self.assertIsNone(role_for(self.user))
        self.assertEqual(list(visible_members(self.user)), [])
        self.assertEqual(list(visible_payments(self.user)), [])
        # Hiding the workspace must not erase the membership that is its history.
        from .models import UserAccess
        self.assertTrue(UserAccess.objects.filter(
            user=self.user, organization=self.org, active=True).exists())

    def test_the_shell_and_every_data_endpoint_refuse(self):
        for url in ['/', '/overview/', '/api/session/', '/api/overview/', '/api/members/',
                    '/api/payments/', '/api/accounts/', '/api/audit/', '/export/', '/export/excel/']:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        # A write is refused for the same reason a read is.
        self.assertEqual(self.client.post('/api/settings/', {'name': 'x'},
                                          content_type='application/json').status_code, 403)
        # The receipt is found through visible_payments(), which is empty.
        self.assertEqual(self.client.get(f'/receipts/{self.payment.pk}/').status_code, 404)

    def test_a_denied_request_is_still_filed_under_the_closed_organization(self):
        self.client.get('/api/members/')
        event = AuditEvent.objects.get(action='access.denied')
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.reason, 'no_active_membership')

    def test_the_public_login_lookup_will_not_name_a_closed_workspace(self):
        request = RequestFactory().get(f'/login/?org={self.org.public_id}')
        request.user = AnonymousUser()
        self.assertIsNone(site_context(request)['organisation'])

    def test_a_live_workspace_is_still_resolved_by_the_same_lookup(self):
        live = Organisation.objects.create(name='Open Co', primary='#123456')
        request = RequestFactory().get(f'/login/?org={live.public_id}')
        request.user = AnonymousUser()
        self.assertEqual(site_context(request)['organisation'], live)

    def test_an_invitation_into_a_closed_workspace_is_refused(self):
        raw = 'invite-token-for-a-closed-org'
        SecretaryInvite.objects.create(
            organization=self.org, email='new@example.com', created_by=self.user,
            token_hash=token_hash(raw), expires_at=timezone.now() + timedelta(days=7))
        self.assertIsNone(valid_invite(raw))
        response = Client().get('/signup/?invite=' + raw)
        self.assertEqual(response.status_code, 400)

    def test_an_invitation_into_a_live_workspace_still_works(self):
        live, owner, _, _ = self.make_org('Open Co', disabled=False)
        raw = 'invite-token-for-an-open-org'
        SecretaryInvite.objects.create(
            organization=live, email='new@example.com', created_by=owner,
            token_hash=token_hash(raw), expires_at=timezone.now() + timedelta(days=7))
        self.assertEqual(valid_invite(raw).organization, live)

    def test_the_logo_endpoint_hides_a_closed_workspace(self):
        self.org.logo = LOGO
        self.org.save(update_fields=['logo'])
        self.assertEqual(Client().get(f'/organizations/{self.org.public_id}/logo/').status_code, 404)

    def test_the_logo_endpoint_still_serves_a_live_workspace(self):
        live = Organisation.objects.create(name='Open Co', logo=LOGO)
        response = Client().get(f'/organizations/{live.public_id}/logo/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
