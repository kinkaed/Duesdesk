"""What a refusal looks like from outside: the status code, the wording, and the event.

A refused request has three jobs that are easy to get wrong individually and
impossible to notice when only one is tested:

* the answer has to be JSON. A Django-rendered HTML 404 on a JSON endpoint is
  returned to api.ts, which has no status-specific handling for it and reports an
  infrastructure failure instead of "not found";
* the wording has to be ours. `int()` and `Model.objects.get()` raise Python and
  ORM messages that were being handed straight to the user, which is both a leak
  of internals and an answer that helps nobody act;
* the event has to exist. A refusal and its audit event cannot share the
  transaction that produced the refusal: that transaction ends by undoing the
  refusal, and the event goes with it. The refusals worth recording are exactly
  the ones that happen inside one.

These tests are arranged around those three, plus the two idempotency gaps where
a double click used to manufacture history nobody made.
"""
import json
from datetime import date
from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from django.core import signing
from django.db.models import Sum
from django.test import Client, TestCase

from .models import (
    Allocation,
    AuditEvent,
    Member,
    Organisation,
    Payment,
    SecretaryInvite,
    UserAccess,
)
from .services import record_payment
from .views import PAYMENT_MAX_PAGE

PASSWORD = 'TestingOnly25!'


class RefusalBase(TestCase):
    """Two organizations with one member each, so every id has a wrong owner."""

    def setUp(self):
        self.a = Organisation.objects.create(name='Organization A')
        self.b = Organisation.objects.create(name='Organization B')
        self.sec_a = User.objects.create_user('secretary-a', password=PASSWORD,
                                              email='a@example.test')
        self.sec_b = User.objects.create_user('secretary-b', password=PASSWORD,
                                              email='b@example.test')
        UserAccess.objects.create(organization=self.a, user=self.sec_a, role='secretary')
        UserAccess.objects.create(organization=self.b, user=self.sec_b, role='secretary')
        self.member_a = Member.objects.create(organization=self.a, full_name='Alice',
                                              joined=date(2026, 1, 1))
        self.member_b = Member.objects.create(organization=self.b, full_name='Bob',
                                              joined=date(2026, 1, 1))
        self.client.force_login(self.sec_a)

    def post(self, url, data=None):
        return self.client.post(url, data if data is not None else {},
                                content_type='application/json')

    def payment_body(self, **overrides):
        return {**overrides} or {}

    def record(self, member, key=None, **overrides):
        return {'member_id': member.pk, 'amount': '50', 'start_month': '2026-09',
                'payment_date': '2026-09-01', 'method': 'Cash',
                'request_key': key or str(uuid4()), **overrides}


class AuditSurvivesItsOwnRollback(RefusalBase):
    """DD-01: the event a refusal is raised for has to outlive the refusal."""

    def test_a_conflicting_idempotency_key_is_recorded_despite_the_rollback(self):
        key = str(uuid4())
        first = self.post('/api/payments/', self.record(self.member_a, key))
        self.assertEqual(first.status_code, 201, first.content)
        # Same key, different amount. Refused, and the payment transaction that
        # raised the refusal rolls itself back.
        conflict = self.post('/api/payments/',
                             self.record(self.member_a, key, amount='75'))
        self.assertEqual(conflict.status_code, 400, conflict.content)
        self.assertEqual(Payment.objects.filter(request_key=key).count(), 1,
                         'the refused retry must not add or alter a payment')
        self.assertEqual(
            list(AuditEvent.objects.filter(action='payment.replayed')
                 .values_list('outcome', 'reason')),
            [('replayed', 'request_key_conflict')],
            'the replay has to be in the history, and it has to say why')

    def test_an_identical_retry_is_not_recorded_a_second_time(self):
        # The counterpart to the test above: the event marks a refusal, so a
        # replay that was granted must not add one.
        key = str(uuid4())
        self.assertEqual(self.post('/api/payments/', self.record(self.member_a, key)).status_code, 201)
        again = self.post('/api/payments/', self.record(self.member_a, key))
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(AuditEvent.objects.filter(action='payment.replayed').count(), 0)

    def test_an_expired_import_preview_is_recorded(self):
        expired = self.post('/api/import/commit/', {'token': 'not-a-real-token'})
        self.assertEqual(expired.status_code, 400, expired.content)
        self.assertEqual(
            list(AuditEvent.objects.filter(action='import.rejected')
                 .values_list('outcome', 'reason')),
            [('rejected', 'preview_expired')])

    def test_a_reimported_file_is_recorded(self):
        rows = [{'name': 'Alice', 'joined': '2026-01-01'}]
        token = signing.dumps({'rows': rows, 'kind': 'members', 'user': self.sec_a.pk,
                               'organization': self.a.pk}, salt='csv-import')
        self.assertEqual(self.post('/api/import/commit/', {'token': token}).status_code, 200)
        # Refused before the batch exists, but still inside api()'s write lock,
        # which is what used to take the event with it.
        again = self.post('/api/import/commit/', {'token': token})
        self.assertEqual(again.status_code, 400, again.content)
        self.assertEqual(
            list(AuditEvent.objects.filter(action='import.rejected')
                 .values_list('outcome', 'reason')),
            [('replayed', 'duplicate_file')])

    def test_a_batch_that_fails_halfway_is_recorded_as_failed(self):
        # The write that explodes is inside the batch transaction, so the ImportBatch
        # row and every member created before it go away together.
        rows = [{'name': 'Imported One', 'joined': '2026-01-01'},
                {'name': 'Imported Two', 'joined': '2026-01-01'}]
        token = signing.dumps({'rows': rows, 'kind': 'members', 'user': self.sec_a.pk,
                               'organization': self.a.pk}, salt='csv-import')
        # raise_request_exception off so the 500 is a response we can assert on
        # rather than an error in the test.
        crashing = Client(raise_request_exception=False)
        crashing.force_login(self.sec_a)
        with mock.patch.object(Member, 'save', side_effect=RuntimeError('db exploded')):
            failed = crashing.post('/api/import/commit/', data=json.dumps({'token': token}),
                                   content_type='application/json')
        self.assertEqual(failed.status_code, 500)
        self.assertFalse(Member.objects.filter(full_name='Imported One').exists(),
                         'the batch rolls back as one unit')
        self.assertEqual(
            list(AuditEvent.objects.filter(action='import.failed')
                 .values_list('outcome', 'reason')),
            [('failure', 'RuntimeError')],
            'a bulk write that dies halfway is the event an auditor needs most')


class TheAnswerIsJson(RefusalBase):
    """DD-03/DD-05/DD-06: a JSON endpoint answers in JSON, in our words."""

    def test_a_missing_record_is_a_json_404_on_every_json_endpoint(self):
        # pk=0 rather than 424242: these tests do not assume any row exists.
        # The point is the shape of the answer, not which ids happen to be taken.
        cases = [
            ('get', '/api/members/0/'),
            ('get', '/api/members/0/report/'),
            ('get', '/api/members/0/report/?month=2026-09'),
            ('post', '/api/members/0/'),
            ('post', '/api/accounts/0/disable/'),
            ('post', '/api/accounts/0/enable/'),
            ('post', '/api/invites/0/revoke/'),
            # A reason long enough to pass validation, so the lookup is what
            # refuses it. Validating the body before the lookup is deliberate:
            # neither order tells a caller whether the payment exists.
            ('post', '/api/payments/0/void/', {'reason': 'Written off in error'}),
            ('post', '/api/payments/', self.record(self.member_a, member_id=0)),
            ('post', '/api/payments/preview/', {'member_id': 0, 'amount': '50',
                                                 'start_month': '2026-09'}),
        ]
        for method, url, *body in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(
                    url, *([json.dumps(body[0])] if body else []),
                    content_type='application/json')
                self.assertEqual(response.status_code, 404, f'{method.upper()} {url}')
                self.assertEqual(response['Content-Type'], 'application/json',
                                 f'{method.upper()} {url} rendered a page instead')
                self.assertEqual(response.json(),
                                 {'error': 'That record does not exist.'})

    def test_the_audit_endpoint_keeps_its_own_wording(self):
        # It already answered this way for a missing event. One phrasing across
        # the JSON endpoints, so the wording cannot be used to tell which kind of
        # id space was probed.
        response = self.client.get('/api/audit/0/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'error': 'That audit event does not exist.'})

    def test_a_record_in_another_organization_answers_like_a_record_that_is_not_there(self):
        # The status code and the body have to be identical for both, or a
        # secretary walking the id space learns which ids exist elsewhere.
        cases = [
            ('get', f'/api/members/{self.member_b.pk}/'),
            ('post', f'/api/members/{self.member_b.pk}/', {'name': 'renamed'}),
            ('post', '/api/payments/', self.record(self.member_b)),
            ('post', '/api/payments/preview/', {'member_id': self.member_b.pk,
                                                 'amount': '50',
                                                 'start_month': '2026-09'}),
        ]
        for method, url, *body in cases:
            with self.subTest(url=url):
                response = getattr(self.client, method)(
                    url, *([json.dumps(body[0])] if body else []),
                    content_type='application/json')
                self.assertEqual(response.status_code, 404, f'{method.upper()} {url}')
                self.assertEqual(response.json(), {'error': 'That record does not exist.'})
        self.member_b.refresh_from_db()
        self.assertEqual(self.member_b.full_name, 'Bob')

    def test_no_orm_or_python_message_is_handed_to_the_user(self):
        responses = [
            self.post('/api/payments/', self.record(self.member_a, member_id=0)),
            self.post('/api/payments/preview/', {'member_id': 0, 'amount': '50',
                                                 'start_month': '2026-09'}),
            self.post('/api/payments/preview/', {'member_id': 'abc', 'amount': '50',
                                                 'start_month': '2026-09'}),
            self.post('/api/payments/0/void/'),
            self.post('/api/payments/', self.record(self.member_a, member_id='abc')),
            self.post('/api/payments/', self.record(self.member_a, amount='abc')),
            self.post('/api/payments/', self.record(self.member_a, payment_date='nope')),
        ]
        for response in responses:
            message = response.json()['error']
            for leak in ('matching query does not exist', 'invalid literal for int()',
                         'Traceback', 'DoesNotExist', 'sqlite3.', 'OperationalError'):
                self.assertNotIn(leak, message, f'{response.status_code}: {message}')

    def test_an_unparsable_page_number_is_not_an_error(self):
        # Same rule the audit history already used: a value we cannot parse is
        # ignored rather than refused, and a value we can parse is bounded. The
        # old `max(1, int(...))` handed both the Python message and an unbounded
        # offset to the database, where a huge page became an IntegrityError and
        # a 409 about a conflicting record.
        for query in ('?page=abc', '?page=', '?page=-3', '?page=0', '?page=1e9',
                      '?page=1.5', '?page=0x10', '?page=%20'):
            with self.subTest(query=query):
                response = self.client.get('/api/payments/' + query)
                self.assertEqual(response.status_code, 200, query)
                self.assertEqual(response.json()['page'], 1, query)
        huge = self.client.get('/api/payments/?page=99999999999999999999')
        self.assertEqual(huge.status_code, 200)
        self.assertEqual(huge.json()['payments'], [])
        self.assertFalse(huge.json()['has_more'])

    def test_pagination_still_returns_whole_pages(self):
        for _ in range(7):
            record_payment(self.record(self.member_a), self.sec_a)
        body = self.client.get('/api/payments/').json()
        self.assertEqual(len(body['payments']), 7)
        self.assertEqual(body['page'], 1)
        self.assertFalse(body['has_more'], '7 payments is not more than one page')
        self.assertEqual(self.client.get('/api/payments/?page=2').json()['payments'], [])
        # The cap short-circuits to an empty page rather than making the database
        # skip past the whole table to discard the rows, and the page number is
        # echoed back so a pager does not jump.
        capped = self.client.get(f'/api/payments/?page={PAYMENT_MAX_PAGE + 1}').json()
        self.assertEqual(capped['payments'], [])
        self.assertFalse(capped['has_more'])
        self.assertEqual(capped['page'], PAYMENT_MAX_PAGE + 1)
        self.assertEqual(self.client.get(f'/api/payments/?page={PAYMENT_MAX_PAGE}')
                         .json()['page'], PAYMENT_MAX_PAGE)

    def test_the_financial_invariant_holds_after_a_refused_void(self):
        # A payment that exists, but in the other organization.
        theirs = record_payment(self.record(self.member_b), self.sec_b)
        before = Allocation.objects.filter(payment=theirs).aggregate(total=Sum('amount'))
        self.assertEqual(self.post(f'/api/payments/{theirs.pk}/void/',
                                   {'reason': 'Written off in error'}).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.voided_at, 'a refused void must not void anything')
        self.assertEqual(Allocation.objects.filter(payment=theirs).aggregate(total=Sum('amount')),
                         before)
        self.assertEqual(AuditEvent.objects.filter(action='payment.voided').count(), 0,
                         'and it must not claim one was voided')


class DoubleClickMakesNoHistory(RefusalBase):
    """DD-07: a repeated action is answered, not recorded a second time."""

    def test_revoking_an_invitation_twice_records_one_event(self):
        from .models import SecretaryInvite
        created = self.post('/api/invites/', {'email': 'new@example.test'})
        self.assertEqual(created.status_code, 201, created.content)
        invite = SecretaryInvite.objects.get(pk=created.json()['id'])
        first = self.post(f'/api/invites/{invite.pk}/revoke/')
        self.assertEqual(first.status_code, 200, first.content)
        first_revoked = invite.__class__.objects.get(pk=invite.pk).revoked_at
        second = self.post(f'/api/invites/{invite.pk}/revoke/')
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json().get('already_revoked'))
        self.assertEqual(AuditEvent.objects.filter(action='invite.revoked').count(), 1,
                         'two clicks are one decision')
        self.assertEqual(invite.__class__.objects.get(pk=invite.pk).revoked_at, first_revoked,
                         'the second call must not move revoked_at either')

    def test_disabling_an_account_twice_records_one_event(self):
        # The convention revoke_invite was missing, asserted here so the two
        # endpoints stay aligned.
        from .models import UserAccess as Access
        member = User.objects.create_user('auditor-a', password=PASSWORD)
        Access.objects.create(organization=self.a, user=member, role='auditor')
        first = self.post(f'/api/accounts/{member.pk}/disable/')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(self.post(f'/api/accounts/{member.pk}/disable/').status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action='account.disabled').count(), 1)