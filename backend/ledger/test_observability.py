"""Request correlation, redaction, and the audit trail's own guarantees.

The audit history is a business record: it answers what changed, who changed it
and whether it was allowed. These tests protect the properties that make it
worth reading at all, and the two boundaries the design deliberately keeps:

* business audit (AuditEvent rows) and technical logging are separate streams, and
  a technical log line must never be mistaken for a business event;
* an audit write must never be able to fail the request that triggered it.
"""
import json
import logging
import re
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import observability
from .audit_taxonomy import (
    ACTIONS,
    CATEGORY_IDS,
    OUTCOME_LABELS,
    REASON_LABELS,
    SEVERITIES,
    UNKNOWN_REASON_LABEL,
    changes,
    describe,
    public_taxonomy,
    reason_label,
)
from .auth_views import username_fingerprint
from .models import AuditEvent, Member, Organisation, UserAccess
from .observability import (
    ANONYMOUS_ACTIONS,
    RequestContext,
    bind_context,
    client_ip,
    new_request_id,
    redact,
    reset_context,
    safe_details,
    technical,
)
from .services import audit
from .views import AUDIT_MAX_PAGE

STORAGE = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
           'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


class _FakeRequest:
    """The two attributes client_ip() is allowed to look at."""

    def __init__(self, **meta):
        self.META = meta


class _RecordCollector(logging.Handler):
    """Collects whole log records.

    assertLogs only renders the message, so it cannot see the structured extras
    that redaction, correlation and the event name actually live in. The
    properties under test here are all extras, so the records themselves are
    collected instead.
    """

    def __init__(self, level=logging.DEBUG):
        super().__init__(level=level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def events(self):
        return [getattr(record, 'event', None) for record in self.records]

    def find(self, event):
        for record in self.records:
            if getattr(record, 'event', None) == event:
                return record
        return None

    def as_dicts(self):
        return [dict(record.__dict__) for record in self.records]


@contextmanager
def capture_logs(name='ledger.observability', level=logging.DEBUG):
    """Capture every record on a logger, including ones that log nothing."""
    logger = logging.getLogger(name)
    collector = _RecordCollector(level)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(level)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class RequestIdTests(TestCase):
    def test_valid_inbound_request_id_is_reused(self):
        # A correlation id supplied by Render or an upstream proxy is what makes
        # one request traceable across systems; it is honoured when it is safe.
        self.assertEqual(new_request_id({'HTTP_X_REQUEST_ID': 'abc-123_XYZ.9'}), 'abc-123_XYZ.9')

    def test_tracerid_is_accepted_as_a_second_source(self):
        self.assertEqual(new_request_id({'HTTP_TRACERID': 'render-trace-1'}), 'render-trace-1')

    def test_hostile_inbound_request_id_is_replaced(self):
        # The id is echoed into a response header and written to a log line, so a
        # value carrying a newline or a field separator is never reused.
        for hostile in ['a' * 65, 'has space', 'inject\nX-Admin: 1', '', 'semi;colon', 'tab\there']:
            generated = new_request_id({'HTTP_X_REQUEST_ID': hostile})
            self.assertNotEqual(generated, hostile.strip())
            self.assertTrue(generated.isalnum())
            self.assertLessEqual(len(generated), 64)

    def test_generated_id_is_unique_per_request(self):
        self.assertNotEqual(new_request_id({}), new_request_id({}))


@override_settings(TRUST_PROXY=True, STORAGES=STORAGE)
class ClientIpTests(TestCase):
    def test_forwarded_header_is_never_trusted_directly(self):
        # Reading X-Forwarded-For here would hand an untrusted client control of
        # an audited field. serve.py is what resolves the proxy chain, into
        # REMOTE_ADDR; this asserts nothing here changes that decision.
        request = _FakeRequest(REMOTE_ADDR='10.0.0.1', HTTP_X_FORWARDED_FOR='203.0.113.9')
        self.assertEqual(client_ip(request), '10.0.0.1')

    def test_ipv6_is_kept(self):
        self.assertEqual(client_ip(_FakeRequest(REMOTE_ADDR='2001:db8::1')), '2001:db8::1')

    def test_unparseable_remote_addr_is_discarded_rather_than_stored(self):
        # A non-address would fail GenericIPAddressField validation on insert and
        # take the surrounding business transaction down with it.
        for junk in ['', '   ', 'not-an-ip', '999.999.999.999', 'localhost']:
            self.assertIsNone(client_ip(_FakeRequest(REMOTE_ADDR=junk)))

    def test_missing_request_is_handled(self):
        self.assertIsNone(client_ip(None))


class RedactionTests(TestCase):
    def test_sensitive_keys_are_replaced_by_name(self):
        result = redact({'username': 'ada', 'password': 'hunter2', 'api_key': 'k', 'note': 'fine'})
        self.assertEqual(result['username'], 'ada')
        self.assertEqual(result['note'], 'fine')
        for key in ('password', 'api_key'):
            self.assertEqual(result[key], '[redacted]')
            self.assertNotIn('hunter2', json.dumps(result))

    def test_redaction_reaches_into_nested_structures(self):
        # Details arrive from several call sites and from decoded request
        # bodies; a nested secret is still a secret.
        result = redact({'rows': [{'name': 'Ada', 'csrfmiddlewaretoken': 'x'}], 'meta': {'token': 'y'}})
        self.assertEqual(result['rows'][0]['csrfmiddlewaretoken'], '[redacted]')
        self.assertEqual(result['rows'][0]['name'], 'Ada')
        self.assertEqual(result['meta']['token'], '[redacted]')

    def test_idempotency_and_reference_keys_are_not_caught_by_the_word_token(self):
        # A bare "key" is deliberately absent from the denylist: request_key and
        # reference identify a payment without disclosing anything about it, and
        # dropping them would make a replay unreadable.
        result = redact({'request_key': 'abc', 'access_key_id': 'k', 'key': 'k'})
        self.assertEqual(result['request_key'], 'abc')
        self.assertEqual(result['key'], 'k')

    def test_long_strings_and_deep_nesting_are_capped(self):
        self.assertLessEqual(len(redact({'note': 'x' * 5000})['note']), 1001)
        deep = current = {}
        for _ in range(20):
            current['child'] = {}
            current = current['child']
        self.assertIn('[truncated]', json.dumps(redact(deep)))

    def test_details_are_serialized_safely_and_bounded(self):
        stored = safe_details({'password': 'hunter2', 'count': 3})
        self.assertNotIn('hunter2', stored)
        self.assertEqual(json.loads(stored)['count'], 3)
        self.assertLessEqual(len(stored), observability.MAX_DETAILS_CHARS + 100)

    def test_details_survive_an_unserializable_value(self):
        self.assertIsInstance(json.loads(safe_details({'when': object()})), dict)


@override_settings(TRUST_PROXY=False, STORAGES=STORAGE)
class AuditRecordTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')
        self.rival_org = Organisation.objects.create(name='Rival Organization')
        self.rival = User.objects.create_user('rival', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.rival_org, user=self.rival, role='secretary')

    def test_tenant_event_carries_request_context(self):
        context = RequestContext(request_id='req-42', method='POST', path='/api/members/',
                                 ip_address='203.0.113.5', user_agent='pytest')
        token = bind_context(context)
        try:
            member = Member.objects.create(organization=self.org, full_name='Ada', joined=date(2026, 9, 1))
            event = audit(self.secretary, 'member.created', member, {'name': 'Ada'})
        finally:
            reset_context(token)
        self.assertEqual(event.request_id, 'req-42')
        self.assertEqual(event.http_method, 'POST')
        self.assertEqual(event.path, '/api/members/')
        self.assertEqual(event.ip_address, '203.0.113.5')
        self.assertEqual(event.outcome, 'success')

    def test_context_is_not_left_behind_for_the_next_request(self):
        # waitress reuses threads. A context that survived the request would
        # stamp the next, unrelated request with the previous one's id.
        token = bind_context(RequestContext(request_id='req-first', method='GET', path='/api/audit/'))
        reset_context(token)
        self.assertIsNone(observability.current_context())
        member = Member.objects.create(organization=self.org, full_name='Bo', joined=date(2026, 9, 1))
        self.assertEqual(audit(self.secretary, 'member.created', member).request_id, '')

    def test_details_are_redacted_before_they_reach_the_database(self):
        member = Member.objects.create(organization=self.org, full_name='Cy', joined=date(2026, 9, 1))
        event = audit(self.secretary, 'member.created', member, {'password': 'hunter2', 'name': 'Cy'})
        self.assertNotIn('hunter2', event.details)
        self.assertIn('name', event.details)

    def test_unknown_outcome_is_not_recorded_as_success(self):
        # Defaulting to success would put an event in the history asserting
        # something the code never established.
        member = Member.objects.create(organization=self.org, full_name='Di', joined=date(2026, 9, 1))
        with capture_logs(level=logging.ERROR) as captured:
            event = audit(self.secretary, 'member.created', member, outcome='maybe')
        self.assertEqual(event.outcome, 'failure')
        self.assertIn('maybe', captured.records[0].getMessage())

    def test_tenant_event_without_an_organization_is_refused_not_stored(self):
        # A null organization is the marker for a pre-auth event. Storing a
        # business event under that marker would make it invisible to the tenant
        # it belongs to, and visible to nobody.
        orphan = User.objects.create_user('orphan', password='A-Fresh-Strong-Password!')
        with capture_logs(level=logging.ERROR) as captured:
            self.assertIsNone(audit(orphan, 'member.created', None))
        self.assertIsNotNone(captured.find('Refusing tenant audit event')
                             or captured.records[0])
        self.assertFalse(AuditEvent.objects.filter(action='member.created').exists())

    def test_allowlist_cannot_be_widened_by_a_caller(self):
        # Every other action is refused without an organization, including ones
        # that look plausible, so the marker keeps its meaning.
        orphan = User.objects.create_user('orphan2', password='A-Fresh-Strong-Password!')
        for action in ['payment.recorded', 'export.generated', 'access.denied',
                       'member.created', 'auth.password.changed']:
            with capture_logs(level=logging.ERROR):
                self.assertIsNone(audit(orphan, action))
            self.assertFalse(AuditEvent.objects.filter(action=action).exists())
        # auth.logout is deliberately allowed with no organization: a member can
        # leave after their access was withdrawn, and the row must still exist.
        self.assertIn('auth.logout', ANONYMOUS_ACTIONS)

    def test_pre_auth_allowlist_may_have_no_organization(self):
        # A failed login has no user and no tenant, and inventing either one
        # would be a fabrication. The allowlist is what makes the row legitimate.
        event = audit(None, 'auth.login.failure', outcome='failure', reason='invalid_credentials',
                      details={'username_fingerprint': 'abc123'})
        self.assertIsNotNone(event)
        self.assertIsNone(event.organization)
        self.assertIsNone(event.actor)
        self.assertIn('auth.login.failure', ANONYMOUS_ACTIONS)

    def test_allowlist_cannot_be_widened_by_a_caller(self):
        # Every other action is refused without an organization, including ones
        # that look plausible, so the marker keeps its meaning.
        orphan = User.objects.create_user('orphan2', password='A-Fresh-Strong-Password!')
        for action in ['payment.recorded', 'export.generated', 'access.denied',
                       'member.created', 'auth.password.changed']:
            with capture_logs(level=logging.ERROR):
                self.assertIsNone(audit(orphan, action))
            self.assertFalse(AuditEvent.objects.filter(action=action).exists())
        # auth.logout is deliberately allowed with no organization: a member can
        # leave after their access was withdrawn, and the row must still exist.
        self.assertIn('auth.logout', ANONYMOUS_ACTIONS)

    def test_organization_falls_back_to_the_affected_object(self):
        # A secretary who was just disabled still caused this, and the event must
        # land with the tenant where it happened rather than being lost.
        UserAccess.objects.filter(user=self.secretary).update(active=False)
        member = Member.objects.create(organization=self.org, full_name='Eve', joined=date(2026, 9, 1))
        event = audit(self.secretary, 'member.updated', member)
        self.assertEqual(event.organization, self.org)

    def test_history_is_append_only(self):
        member = Member.objects.create(organization=self.org, full_name='Fay', joined=date(2026, 9, 1))
        event = audit(self.secretary, 'member.created', member)
        event.outcome = 'failure'
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_queryset_cannot_bypass_the_instance_guard(self):
        # A queryset update or delete is the easy way to rewrite history, so it
        # is refused at the same level as the instance methods.
        member = Member.objects.create(organization=self.org, full_name='Gil', joined=date(2026, 9, 1))
        audit(self.secretary, 'member.created', member)
        with self.assertRaises(ValidationError):
            AuditEvent.objects.update(outcome='failure')
        with self.assertRaises(ValidationError):
            AuditEvent.objects.all().delete()
        self.assertEqual(AuditEvent.objects.filter(action='member.created').count(), 1)

    def test_audit_failure_cannot_break_the_business_transaction(self):
        # The rule the whole helper exists for: a payment must not become a 500
        # because the audit insert failed.
        with patch.object(AuditEvent.objects, 'create', side_effect=IntegrityError('audit down')):
            with capture_logs(level=logging.ERROR) as captured:
                with transaction.atomic():
                    member = Member.objects.create(organization=self.org, full_name='Hal', joined=date(2026, 9, 1))
                    self.assertIsNone(audit(self.secretary, 'member.created', member))
                    # The savepoint released cleanly, so the surrounding
                    # transaction is still usable afterwards.
                    self.assertIsNotNone(Member.objects.filter(full_name='Hal').first())
        self.assertTrue(any('audit down' in str(record.exc_info) for record in captured.records))

    def test_audit_failure_outside_a_transaction_is_also_swallowed(self):
        with patch.object(AuditEvent.objects, 'create', side_effect=IntegrityError('audit down')):
            with capture_logs(level=logging.ERROR) as captured:
                self.assertIsNone(audit(self.secretary, 'member.created', None))
        self.assertTrue(captured.records)

    def test_events_are_ordered_newest_first(self):
        member = Member.objects.create(organization=self.org, full_name='Ivy', joined=date(2026, 9, 1))
        first = audit(self.secretary, 'member.created', member)
        second = audit(self.secretary, 'member.updated', member)
        ids = list(AuditEvent.objects.values_list('id', flat=True))
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(AuditEvent.objects.first().pk, second.pk)
        self.assertLess(first.pk, second.pk)


@override_settings(TRUST_PROXY=False, STORAGES=STORAGE)
class TechnicalLogSeparationTests(TestCase):
    """The technical stream must not be able to masquerade as the business one."""

    def test_technical_logging_writes_no_audit_row(self):
        with capture_logs():
            technical('request.completed', '%s %s', 'GET', '/api/audit/', status=200, duration_ms=3)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_technical_extras_are_redacted_too(self):
        # A technical log is the one that leaves the machine, so a secret passed
        # to it by mistake is the worse leak of the two.
        with capture_logs() as captured:
            technical('auth.probe', 'probe', password='hunter2', ok=True)
        record = captured.find('auth.probe')
        self.assertIsNotNone(record)
        self.assertEqual(record.password, '[redacted]')
        self.assertTrue(record.ok)
        self.assertNotIn('hunter2', json.dumps(captured.as_dicts(), default=str))

    def test_reserved_record_attributes_do_not_break_the_log_call(self):
        # logging raises on an extra that collides with a LogRecord attribute.
        # Correlation fields use exactly these words, so a caller passing them
        # must not turn a log line into a TypeError mid-request.
        with capture_logs() as captured:
            technical('request.completed', 'ok', level=logging.INFO,
                      name='x', message='y', args=(), event='not-the-event')
        record = captured.find('request.completed')
        self.assertIsNotNone(record)
        self.assertEqual(record.getMessage(), 'ok')

    def test_json_formatter_emits_one_object_per_line(self):
        with capture_logs() as captured:
            token = bind_context(RequestContext(request_id='req-json', method='GET', path='/health/'))
            try:
                technical('request.completed', '%s %s', 'GET', '/health/', status=200, duration_ms=7)
            finally:
                reset_context(token)
        payload = json.loads(observability.JsonFormatter().format(captured.find('request.completed')))
        self.assertEqual(payload['message'], 'GET /health/')
        self.assertEqual(payload['request_id'], 'req-json')
        self.assertEqual(payload['path'], '/health/')
        self.assertEqual(payload['status'], 200)
        self.assertEqual(payload['duration_ms'], 7)
        self.assertEqual(payload['level'], 'INFO')
        self.assertEqual(payload['logger'], 'ledger.observability')

    def test_request_context_filter_fills_placeholders_outside_a_request(self):
        # The human-readable format string references these attributes on every
        # line, including loggers that ran with no request at all. A missing
        # attribute raises inside logging and prints a logging error instead of
        # the message, which is the opposite of useful.
        self.assertIsNone(observability.current_context())
        record = logging.getLogger('ledger.observability').makeRecord(
            'ledger.observability', logging.INFO, '', 0, 'startup', (), None)
        observability.RequestContextFilter().filter(record)
        payload = json.loads(observability.JsonFormatter().format(record))
        self.assertEqual(payload['message'], 'startup')
        self.assertNotIn('request_id', payload)
        self.assertEqual(record.method, '-')
        self.assertEqual(record.status, '-')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class CorrelationMiddlewareTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')

    def test_every_response_carries_a_request_id(self):
        response = self.client.get('/login/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['X-Request-Id'])
        self.assertEqual(len(response['X-Request-Id']), 16)

    def test_a_valid_inbound_id_is_echoed_back(self):
        response = self.client.get('/login/', headers={'x-request-id': 'trace-abc-1'})
        self.assertEqual(response['X-Request-Id'], 'trace-abc-1')

    def test_a_hostile_inbound_id_is_replaced_not_echoed(self):
        response = self.client.get('/login/', headers={'x-request-id': 'bad id with spaces'})
        self.assertNotEqual(response['X-Request-Id'], 'bad id with spaces')

    def test_context_is_bound_during_the_view_and_cleared_after(self):
        # Driven through the middleware directly: a URLconf has already resolved
        # views.health to the function object, so patching the module attribute
        # would not change what the route calls.
        from django.http import JsonResponse
        from django.test import RequestFactory
        from .middleware import RequestCorrelationMiddleware
        seen = {}

        def view(request):
            context = observability.current_context()
            seen['request_id'] = context.request_id
            seen['ip'] = context.ip_address
            seen['method'] = context.method
            seen['path'] = context.path
            return JsonResponse({})

        middleware = RequestCorrelationMiddleware(view)
        request = RequestFactory().get('/api/members/', HTTP_X_REQUEST_ID='bound-1',
                                       REMOTE_ADDR='203.0.113.5')
        response = middleware(request)
        self.assertEqual(seen['request_id'], 'bound-1')
        self.assertEqual(response['X-Request-Id'], 'bound-1')
        self.assertEqual(seen['ip'], '203.0.113.5')
        self.assertEqual(seen['method'], 'GET')
        self.assertEqual(seen['path'], '/api/members/')
        self.assertEqual(request.request_id, 'bound-1')
        # Nothing may be left bound for the next request on this thread.
        self.assertIsNone(observability.current_context())

    def test_context_is_cleared_even_when_the_view_raises(self):
        # waitress reuses threads, so a context surviving an exception would be
        # inherited by whatever that thread serves next.
        from django.test import RequestFactory
        from .middleware import RequestCorrelationMiddleware

        def boom(request):
            raise RuntimeError('view exploded')

        middleware = RequestCorrelationMiddleware(boom)
        with capture_logs(level=logging.ERROR) as captured:
            with self.assertRaises(RuntimeError):
                middleware(RequestFactory().get('/api/members/'))
        self.assertIsNone(observability.current_context())
        record = captured.find('request.failed')
        self.assertIsNotNone(record)
        self.assertIsNotNone(record.exc_info)

    def test_static_and_health_requests_are_not_logged(self):
        # One access line per request is the point; a page load pulls dozens of
        # static assets and health probes arrive continuously. They would bury it.
        with capture_logs() as captured:
            self.assertEqual(self.client.get('/health/').status_code, 200)
            self.assertEqual(self.client.get('/static/app.css').status_code, 200)
        self.assertNotIn('request.completed', captured.events)
        self.assertNotIn('request.failed', captured.events)

    def test_an_ordinary_request_is_logged_with_its_correlation_id(self):
        with capture_logs() as captured:
            response = self.client.get('/login/', headers={'x-request-id': 'trace-99'})
        record = captured.find('request.completed')
        self.assertIsNotNone(record)
        self.assertEqual(record.request_id, 'trace-99')
        self.assertEqual(record.method, 'GET')
        self.assertEqual(record.path, '/login/')
        self.assertEqual(record.status, 200)
        self.assertEqual(record.getMessage(), 'GET /login/ -> 200 in %dms' % record.duration_ms)

    def test_a_server_error_is_logged_above_info_with_its_status(self):
        # Driven through the middleware directly so a genuine 500 is produced
        # without depending on a view failing for some unrelated reason.
        from django.http import JsonResponse
        from django.test import RequestFactory
        from .middleware import RequestCorrelationMiddleware
        with capture_logs() as captured:
            response = RequestCorrelationMiddleware(
                lambda request: JsonResponse({}, status=500))(RequestFactory().get('/api/members/'))
        self.assertEqual(response.status_code, 500)
        record = captured.find('request.completed')
        self.assertIsNotNone(record)
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(record.status, 500)
        self.assertEqual(record.method, 'GET')
        self.assertEqual(record.path, '/api/members/')

    def test_health_still_answers_when_the_database_is_down(self):
        # Reading request.user would open a session and turn this 503 into a 500.
        with patch('ledger.views.connection.cursor', side_effect=OperationalError('database down')):
            self.assertEqual(self.client.get('/health/').status_code, 503)


    def test_no_legacy_action_name_is_written_by_the_application(self):
        # Migration 0008 renamed three actions. A call site left behind would put
        # two spellings of the same idea in one history, and the filters would
        # silently miss half of it, so the old names are asserted to be gone from
        # the source rather than trusted to stay gone.
        import pathlib
        source = pathlib.Path(__file__).parent
        legacy = ['organisation.updated', 'report.exported', 'member.report_exported']
        found = []
        for path in source.rglob('*.py'):
            if path.name in ('0008_auditevent_http_method_auditevent_ip_address_and_more.py', 'test_audit_migration.py'):
                # The migration names them on purpose, and its test asserts on them.
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if 'audit(' in line and any(name in line for name in legacy):
                    found.append(f'{path.name}:{number}')
        self.assertEqual(found, [], f'legacy audit action names still written at {found}')

    def test_the_current_action_names_are_the_documented_ones(self):
        import pathlib
        source = pathlib.Path(__file__).parent
        for path in source.rglob('*.py'):
            if 'migrations' in str(path):
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                if "audit(" in line and "'organization.updated'" in line:
                    self.assertNotIn('organisation.updated', line)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class AuthAuditTests(TestCase):
    """The events that happen before any ordinary view can be watched."""

    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('secretary', password='A-Fresh-Strong-Password!', email='s@example.com')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')

    def test_successful_login_is_recorded_for_the_tenant(self):
        response = self.client.post('/login/', {'username': 'secretary', 'password': 'A-Fresh-Strong-Password!'})
        self.assertEqual(response.status_code, 302)
        event = AuditEvent.objects.filter(action='auth.login.success').get()
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.actor, self.secretary)
        self.assertEqual(event.outcome, 'success')
        self.assertEqual(event.entity, 'User')
        # The request context belongs in columns, not repeated inside details.
        self.assertEqual(event.details, '{}')

    def test_failed_login_is_recorded_without_disclosing_the_username(self):
        self.client.post('/login/', {'username': 'secretary', 'password': 'wrong-password'})
        event = AuditEvent.objects.filter(action='auth.login.failure').get()
        self.assertEqual(event.outcome, 'failure')
        self.assertEqual(event.reason, 'invalid_credentials')
        # The history is readable by auditors. Writing the attempted username
        # would confirm which accounts exist and list who is being targeted.
        self.assertNotIn('secretary', event.details)
        self.assertIn(username_fingerprint('secretary'), event.details)

    def test_failed_login_never_records_the_submitted_password(self):
        self.client.post('/login/', {'username': 'secretary', 'password': 'My-Secret-Attempt-99'})
        for event in AuditEvent.objects.all():
            self.assertNotIn('My-Secret-Attempt-99', event.details)
        with patch('ledger.observability.logger') as logger:
            self.client.post('/login/', {'username': 'secretary', 'password': 'My-Secret-Attempt-99'})
        logged = ' '.join(str(call) for call in logger.log.call_args_list)
        self.assertNotIn('My-Secret-Attempt-99', logged)

    def test_an_unknown_username_and_a_wrong_password_are_recorded_identically(self):
        # Any difference here would tell an attacker which accounts are real.
        self.client.post('/login/', {'username': 'secretary', 'password': 'wrong-password'})
        first = AuditEvent.objects.get(action='auth.login.failure')
        self.client.post('/login/', {'username': 'no-such-account', 'password': 'wrong-password'})
        second = AuditEvent.objects.filter(action='auth.login.failure').order_by('-id').first()
        self.assertEqual(first.reason, second.reason)
        self.assertNotIn('secretary', second.details)
        self.assertNotIn('no-such-account', second.details)

    def test_logout_is_recorded(self):
        self.client.force_login(self.secretary)
        self.client.post('/logout/')
        event = AuditEvent.objects.filter(action='auth.logout').get()
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.actor, self.secretary)

    def test_password_change_is_recorded(self):
        self.client.force_login(self.secretary)
        self.client.post('/account/password/', {'old_password': 'A-Fresh-Strong-Password!',
                                                'new_password1': 'Another-Strong-Password!1',
                                                'new_password2': 'Another-Strong-Password!1'})
        event = AuditEvent.objects.filter(action='auth.password.changed').get()
        self.assertEqual(event.organization, self.org)

    def test_rejected_password_change_is_recorded_without_the_submitted_values(self):
        self.client.force_login(self.secretary)
        self.client.post('/account/password/', {'old_password': 'wrong',
                                                'new_password1': 'short',
                                                'new_password2': 'mismatch'})
        event = AuditEvent.objects.filter(action='auth.password.change_failed').get()
        self.assertEqual(event.outcome, 'rejected')
        self.assertNotIn('mismatch', event.details)

    def test_recovery_request_is_recorded_without_the_address(self):
        self.client.post('/account/reset/', {'email': 's@example.com'})
        event = AuditEvent.objects.filter(action='auth.recovery.requested').get()
        self.assertIsNone(event.organization)
        self.assertNotIn('s@example.com', event.details)
        self.assertIn(username_fingerprint('s@example.com'), event.details)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class AuditApiTests(TestCase):
    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')
        self.member = Member.objects.create(organization=self.org, full_name='Ada', joined=date(2026, 9, 1))
        self.rival_org = Organisation.objects.create(name='Rival Organization')
        self.rival = User.objects.create_user('rival', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.rival_org, user=self.rival, role='secretary')
        # A pre-auth event: no organization, so it belongs to no tenant and must
        # never surface in a tenant's history.
        AuditEvent.objects.create(organization=None, actor=None, action='auth.login.failure',
                                  outcome='failure', reason='invalid_credentials', entity='', entity_id='')
        AuditEvent.objects.create(organization=self.rival_org, actor=self.rival, action='payment.recorded',
                                  entity='Payment', entity_id='1')
        # One event this tenant legitimately owns, so the default listing and the
        # tenant-isolation assertions are both made against real data.
        self.own = AuditEvent.objects.create(organization=self.org, actor=self.secretary, action='member.created',
                                             entity='Member', entity_id=str(self.member.pk))

    def events(self, query=''):
        self.client.force_login(self.secretary)
        return self.client.get(f'/api/audit/{query}').json()['events']

    def test_default_response_keeps_the_existing_shape(self):
        events = self.events()
        self.assertTrue(events)
        for key in ('id', 'date', 'actor', 'action', 'entity', 'entity_id'):
            self.assertIn(key, events[0])
        # `details` is the one intentional change to this contract. It can hold
        # 8 KB of JSON per row and a page is a hundred rows, so it moved to the
        # detail endpoint rather than being shipped for every row in the list.
        # The opt-in below is what keeps an existing client working.
        self.assertNotIn('details', events[0])
        self.assertIn('details', self.events('?details=1')[0])

    def test_the_row_carries_what_a_reader_needs_to_identify_it(self):
        row = self.events()[0]
        self.assertEqual(row['label'], f'{self.member.full_name} ({self.member.code}) was added')
        self.assertEqual(row['category'], 'members')
        self.assertEqual(row['severity'], 'routine')
        self.assertEqual(row['outcome_label'], 'Successful')
        # The member is now named, not just numbered.
        self.assertEqual(row['resource'], f'Ada ({self.member.code})')
        self.assertEqual(row['actor'], 'sec')
        self.assertEqual(row['actor_role'], 'secretary')
        self.assertEqual(row['related_count'], 0)

    def test_another_tenants_events_are_never_returned(self):
        actions = {event['action'] for event in self.events()}
        self.assertNotIn('payment.recorded', actions)

    def test_pre_auth_events_are_never_returned_to_a_tenant(self):
        actions = {event['action'] for event in self.events()}
        self.assertNotIn('auth.login.failure', actions)

    def test_filter_by_action(self):
        member = audit(self.secretary, 'member.created', self.member)
        AuditEvent.objects.create(organization=self.org, actor=self.secretary, action='payment.voided',
                                  entity='Payment', entity_id='9')
        # The setUp event is also a member.created, so the filter must return
        # every match rather than silently dropping one.
        self.assertEqual([event['id'] for event in self.events('?action=member.created')],
                         [member.pk, self.own.pk])
        self.assertEqual(self.events('?action=payment.voided')[0]['action'], 'payment.voided')
        self.assertEqual(self.events('?action=does.not.exist'), [])

    def test_filter_by_outcome_and_resource(self):
        voided = AuditEvent.objects.create(organization=self.org, actor=self.secretary, action='payment.voided',
                                           entity='Payment', entity_id='9', outcome='success')
        replayed = AuditEvent.objects.create(organization=self.org, actor=self.secretary, action='payment.replayed',
                                             entity='Payment', entity_id='9', outcome='replayed',
                                             reason='request_key_conflict')
        # The pre-existing member.created event also has outcome success, so an
        # outcome filter must return every match, not just the newest.
        self.assertEqual({row['id'] for row in self.events('?outcome=success')},
                         {voided.pk, self.own.pk})
        self.assertEqual([row['id'] for row in self.events('?outcome=replayed')], [replayed.pk])
        self.assertEqual([row['id'] for row in self.events('?resource=Payment')], [replayed.pk, voided.pk])
        self.assertEqual([row['id'] for row in self.events(f'?resource=Payment&resource_id=9')],
                         [replayed.pk, voided.pk])
        self.assertEqual(self.events('?resource=Payment&resource_id=nothing'), [])

    def test_filter_by_request_id_and_actor(self):
        context = RequestContext(request_id='req-lookup', method='POST', path='/api/members/')
        token = bind_context(context)
        try:
            event = audit(self.secretary, 'member.created', self.member)
        finally:
            reset_context(token)
        # Only the event written inside the context carries the request id.
        self.assertEqual([row['id'] for row in self.events('?request_id=req-lookup')], [event.pk])
        self.assertEqual([row['id'] for row in self.events('?request_id=nothing')], [])
        self.assertEqual({row['id'] for row in self.events('?actor=sec')}, {event.pk, self.own.pk})
        self.assertEqual(self.events('?actor=nobody'), [])
        self.assertEqual(self.events('?actor=rival'), [])

    def test_filter_by_date_range(self):
        # Only the setUp event is dated today; there is no event from any other
        # day, so an outside range must come back empty rather than everything.
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self.assertEqual([row['id'] for row in self.events(f'?since={today}&until={today}')], [self.own.pk])
        self.assertEqual(self.events(f'?since={yesterday}&until={yesterday}'), [])
        self.assertEqual(self.events('?since=2099-01-01'), [])
        self.assertEqual(len(self.events('?since=2000-01-01')), 1)

    def test_a_nonsense_filter_is_ignored_rather_than_rejected(self):
        # The existing client sends no filters at all. An unparseable one must
        # not turn the whole history into an error, and must not widen the
        # result set either: ignoring it returns exactly the unfiltered history.
        self.assertEqual([row['id'] for row in self.events('?since=not-a-date')], [self.own.pk])
        self.assertEqual([row['id'] for row in self.events('?until=2026-13-45')], [self.own.pk])
        self.assertEqual([row['action'] for row in self.events('?action=member.created&action=&outcome=')],
                         ['member.created'])
        self.assertEqual(len(self.events('')), 1)

    def test_response_exposes_the_new_fields_when_present(self):
        event = audit(self.secretary, 'member.created', self.member, outcome='rejected', reason='duplicate')
        row = self.events('?action=member.created')[0]
        self.assertEqual(row['id'], event.pk)
        self.assertEqual(row['outcome'], 'rejected')
        self.assertEqual(row['reason'], 'duplicate')
        self.assertEqual(row['outcome_label'], 'Rejected')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class AuditReadDesignTests(TestCase):
    """The reader-facing half: what a row says, what can be found, and what is hidden."""

    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=self.secretary, role='secretary')
        self.auditor = User.objects.create_user('aud', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=self.auditor, role='auditor')
        self.member = Member.objects.create(organization=self.org, full_name='Ada Lovelace',
                                            joined=date(2026, 9, 1))
        self.other = Member.objects.create(organization=self.org, full_name='Grace Hopper',
                                           joined=date(2026, 9, 1))
        self.rival_org = Organisation.objects.create(name='Rival Organization')
        self.rival = User.objects.create_user('rival', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.rival_org, user=self.rival, role='secretary')
        self.rival_member = Member.objects.create(organization=self.rival_org, full_name='Rival Person',
                                                  joined=date(2026, 9, 1))
        self.edit = audit(self.secretary, 'member.updated', self.member,
                          {'changed': {'full_name': {'from': 'Ada', 'to': 'Ada Lovelace'}}})
        self.rival_event = audit(self.rival, 'member.updated', self.rival_member,
                                 {'changed': {'full_name': {'from': 'R', 'to': 'RR'}}})

    def get(self, path=''):
        self.client.force_login(self.secretary)
        return self.client.get(f'/api/audit/{path}')

    def rows(self, query=''):
        return self.get(query).json()['events']

    def test_the_api_carries_a_readable_reason_next_to_the_stored_value(self):
        # import.failed records the raised exception's class name. Both are
        # returned: the label is what the summary shows, and the raw value stays
        # reachable for somebody matching a log line.
        row = AuditEvent.objects.create(
            organization=self.org, actor=self.secretary, action='import.failed',
            entity='import', entity_id='7', outcome='failure',
            reason='UnexpectedNullCharacter', details=json.dumps({'error': 'x'}))
        body = self.get(f'{row.pk}/').json()
        self.assertEqual(body['reason'], 'UnexpectedNullCharacter')
        self.assertEqual(body['reason_label'], UNKNOWN_REASON_LABEL)
        # Nothing a collapsed row renders may carry the class name. The raw value
        # is still there, but only where the technical section reads it from.
        summary = {key: body[key] for key in
                   ('label', 'reason_label', 'category', 'severity', 'outcome_label', 'actor')}
        self.assertNotIn('UnexpectedNull', json.dumps(summary))
        # A known reason reads as a sentence an administrator can act on.
        known = audit(self.secretary, 'import.rejected', None, {},
                      outcome='rejected', reason='duplicate_file')
        self.assertEqual(self.get(f'{known.pk}/').json()['reason_label'],
                         REASON_LABELS['duplicate_file'])

    # --- Naming and severity --------------------------------------------------

    def test_a_row_is_a_sentence_and_never_a_bare_action_name(self):
        row = self.rows()[0]
        # The code is read off the member rather than written out: Member.code is
        # derived from the primary key, which SQLite reuses across tests but
        # Postgres keeps advancing, so a literal MBR-0001 only holds on one of them.
        self.assertEqual(row['label'],
                         f'Details updated for {self.member.full_name} ({self.member.code})')
        self.assertEqual(row['severity'], 'routine')
        self.assertEqual(row['category'], 'members')

    def test_a_denied_event_is_raised_to_critical_and_says_why(self):
        audit(self.secretary, 'access.denied', outcome='denied', reason='read_only_role',
              organization=self.org, details={'view': 'accounts'})
        row = self.rows('?action=access.denied')[0]
        self.assertEqual(row['severity'], 'critical')
        self.assertEqual(row['outcome_label'], 'Denied')
        self.assertEqual(row['reason'], 'read_only_role')
        self.assertEqual(row['label'], 'Access was denied')

    def test_an_event_whose_record_is_gone_still_reads_as_a_sentence(self):
        # No placeholder, no dangling "#999", and no missing name in the headline.
        AuditEvent.objects.create(organization=self.org, actor=self.secretary,
                                  action='member.created', entity='Member', entity_id='999')
        row = self.rows('?action=member.created')[0]
        self.assertEqual(row['label'], 'A member was added')
        self.assertEqual(row['resource'], '')

    def test_an_unrecognised_action_from_an_older_release_still_renders(self):
        # The history outlives the code that wrote it, so a label must never be
        # the only thing standing between a reader and a row.
        AuditEvent.objects.create(organization=self.org, actor=self.secretary,
                                  action='legacy.thing_happened', entity='', entity_id='')
        row = self.rows('?action=legacy.thing_happened')[0]
        self.assertEqual(row['label'], 'Legacy thing happened')
        self.assertEqual(row['category'], 'other')
        self.assertTrue(row['severity'])

    # --- Searching ------------------------------------------------------------

    def test_search_finds_an_event_by_the_members_name(self):
        self.assertEqual([row['id'] for row in self.rows('?q=Lovelace')], [self.edit.pk])
        self.assertEqual([row['id'] for row in self.rows('?q=Hopper')], [])

    def test_search_finds_an_event_by_the_member_code_shown_on_the_row(self):
        self.assertEqual([row['id'] for row in self.rows(f'?q={self.member.code}')], [self.edit.pk])

    def test_search_finds_an_event_by_the_actor_and_by_the_action(self):
        self.assertEqual([row['id'] for row in self.rows('?q=sec')], [self.edit.pk])
        self.assertEqual([row['id'] for row in self.rows('?q=updated')], [self.edit.pk])

    def test_a_one_character_search_is_ignored_rather_than_matching_everything(self):
        # A scan bounded only by length would be the slowest thing in the app.
        self.assertEqual([row['id'] for row in self.rows('?q=a')], [self.edit.pk])
        self.assertEqual(self.rows('?q=a'), self.rows(''))

    def test_search_cannot_reach_another_tenants_members(self):
        # The name lookup is the one place a name from outside could leak, so it
        # is filtered by organization rather than trusted.
        self.assertEqual(self.rows('?q=Rival'), [])

    # --- Categories -----------------------------------------------------------

    def test_category_narrows_the_history(self):
        self.assertEqual([row['id'] for row in self.rows('?category=members')], [self.edit.pk])
        self.assertEqual(self.rows('?category=payments'), [])
        self.assertEqual([row['id'] for row in self.rows('?category=security')], [])

    def test_an_unknown_category_is_ignored_rather_than_widening_or_emptying(self):
        self.assertEqual([row['id'] for row in self.rows('?category=made-up')], [self.edit.pk])

    # --- Pagination -----------------------------------------------------------

    def test_a_malformed_page_is_not_an_error(self):
        for query in ('?page=abc', '?page=-3', '?page=0', '?page='):
            response = self.get(query)
            self.assertEqual(response.status_code, 200, query)
            self.assertEqual(response.json()['page'], 1, query)

    def test_has_more_is_answered_without_counting_the_whole_history(self):
        # Sign in outside the capture: force_login writes the session, and its
        # queries would be counted against the endpoint being measured.
        self.client.force_login(self.secretary)
        with CaptureQueriesContext(connection) as captured:
            body = self.client.get('/api/audit/').json()
        self.assertEqual(body['has_more'], False)
        # A full-table count is the thing this replaced, and it is the only query
        # that would grow without bound as the history does.
        counted = [q for q in captured.captured_queries
                   if 'COUNT(*)' in q['sql'].upper() and 'GROUP BY' not in q['sql'].upper()]
        self.assertEqual(counted, [], 'has_more must not scan the tenant history')
        self.assertNotIn('total', body)

    def test_the_page_after_the_last_one_is_empty_rather_than_wrong(self):
        self.client.force_login(self.secretary)
        for number in range(AUDIT_MAX_PAGE + 5, AUDIT_MAX_PAGE + 8):
            body = self.client.get(f'/api/audit/?page={number}').json()
            self.assertEqual(body['events'], [])
            self.assertEqual(body['has_more'], False)
            self.assertEqual(body['page'], number)

    def test_the_list_does_not_cost_a_query_per_row(self):
        for index in range(30):
            audit(self.secretary, 'member.updated', self.other, {'changed': {}})
        self.client.force_login(self.secretary)
        with CaptureQueriesContext(connection) as captured:
            self.client.get('/api/audit/')
        # Bounded: session, user, the membership lookups, the page itself, the
        # label lookups, the actor list and the related counts. Growing with the
        # page size would mean an N+1 crept back in.
        self.assertLessEqual(len(captured.captured_queries), 12)

    # --- Detail ---------------------------------------------------------------

    def test_the_detail_endpoint_returns_what_the_list_withholds(self):
        self.client.force_login(self.secretary)
        response = self.client.get(f'/api/audit/{self.edit.pk}/')
        self.assertEqual(response.status_code, 200)
        row = response.json()
        self.assertEqual(row['id'], self.edit.pk)
        self.assertEqual(row['details']['changed']['full_name'],
                         {'from': 'Ada', 'to': 'Ada Lovelace'})
        # The before/after pair the reader actually wants, already structured.
        self.assertEqual(row['changes'], [{'field': 'full_name', 'from': 'Ada', 'to': 'Ada Lovelace'}])
        for key in ('http_method', 'path', 'user_agent', 'ip_address'):
            self.assertIn(key, row)

    def test_the_detail_endpoint_cannot_read_another_tenants_event(self):
        # A 404, not a 403: saying "forbidden" would confirm the row exists.
        self.client.force_login(self.secretary)
        response = self.client.get(f'/api/audit/{self.rival_event.pk}/')
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('Rival', response.content.decode())

    def test_the_detail_endpoint_is_refused_to_an_account_without_access(self):
        orphan = User.objects.create_user('orphan', password='A-Fresh-Strong-Password!')
        self.client.force_login(orphan)
        self.assertEqual(self.client.get(f'/api/audit/{self.edit.pk}/').status_code, 403)

    def test_events_from_one_request_are_grouped(self):
        context = RequestContext(request_id='req-one', method='POST', path='/api/members/')
        token = bind_context(context)
        try:
            first = audit(self.secretary, 'member.created', self.member)
            second = audit(self.secretary, 'member.updated', self.other, {'changed': {}})
        finally:
            reset_context(token)
        rows = {row['id']: row for row in self.rows()}
        # Both share the request id, so both know there is a sibling to look at.
        self.assertEqual(rows[first.pk]['related_count'], 2)
        self.assertEqual(rows[second.pk]['related_count'], 2)
        self.client.force_login(self.secretary)
        row = self.client.get(f'/api/audit/{second.pk}/').json()
        # Only the sibling from the same request, never an unrelated event.
        self.assertEqual([item['id'] for item in row['related']], [first.pk])

    def test_grouping_never_reaches_into_another_tenants_request(self):
        context = RequestContext(request_id='req-shared', method='POST', path='/api/members/')
        token = bind_context(context)
        try:
            mine = audit(self.secretary, 'member.created', self.member)
            theirs = audit(self.rival, 'member.created', self.rival_member)
        finally:
            reset_context(token)
        self.client.force_login(self.secretary)
        row = self.client.get(f'/api/audit/{mine.pk}/').json()
        self.assertEqual([item['id'] for item in row['related']], [])
        self.assertEqual(row['related_count'], 1)
        self.assertNotEqual(mine.pk, theirs.pk)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class DeniedAccessIsRecordedTests(TestCase):
    """A denied request is the event an auditor most needs, so it has to survive.

    It used to be silently dropped: the account has no active membership, so
    there was no organization to file it under, and audit() refuses a tenant event
    that no tenant owns. Only a technical log line survived, which no secretary or
    auditor ever reads.
    """

    def setUp(self):
        self.org = Organisation.objects.create(name='Test Organization')
        self.auditor = User.objects.create_user('aud', password='A-Fresh-Strong-Password!')
        self.access = UserAccess.objects.create(organization=self.org, user=self.auditor, role='auditor')

    def test_a_disabled_account_reaching_a_data_endpoint_is_recorded_for_its_organization(self):
        UserAccess.objects.filter(user=self.auditor).update(active=False)
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get('/api/overview/').status_code, 403)
        event = AuditEvent.objects.get(action='access.denied')
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.actor, self.auditor)
        self.assertEqual(event.outcome, 'denied')
        self.assertEqual(event.reason, 'no_active_membership')
        # And it is visible to somebody, which is the whole point.
        secretary = User.objects.create_user('sec', password='A-Fresh-Strong-Password!')
        UserAccess.objects.create(organization=self.org, user=secretary, role='secretary')
        self.client.force_login(secretary)
        self.assertEqual([row['id'] for row in
                          self.client.get('/api/audit/').json()['events']], [event.pk])

    def test_the_denial_names_the_view_that_refused(self):
        UserAccess.objects.filter(user=self.auditor).update(active=False)
        self.client.force_login(self.auditor)
        self.client.get('/api/members/')
        self.assertEqual(json.loads(AuditEvent.objects.get(action='access.denied').details)['view'],
                         'members')

    def test_an_account_that_has_no_organization_at_all_is_not_invented_one(self):
        # A createsuperuser awaiting assignment belongs to nobody, so there is
        # genuinely nowhere to file this and the refusal still stands. Inventing
        # an organization would put the row in a history it has no business in.
        orphan = User.objects.create_user('orphan', password='A-Fresh-Strong-Password!')
        self.client.force_login(orphan)
        self.assertEqual(self.client.get('/api/overview/').status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action='access.denied').exists())

    def test_a_role_refusal_is_still_recorded_against_the_organization(self):
        # The read-only case always had an organization; this is the regression
        # guard for it, since the two denials now take different paths.
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.post('/api/accounts/', data='{}', content_type='application/json').status_code, 403)
        event = AuditEvent.objects.get(action='access.denied')
        self.assertEqual(event.organization, self.org)
        self.assertEqual(event.reason, 'read_only_role')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   STORAGES=STORAGE)
class TaxonomyTests(TestCase):
    """The label vocabulary, and the promise that it cannot drift from reality."""

    def test_every_action_the_application_writes_has_a_label(self):
        # The application is the source of truth for which events exist, and this
        # is the only place that can notice a new one being added without a
        # corresponding entry. Scanning the source is the same technique the
        # legacy-rename test already uses.
        import pathlib
        source = pathlib.Path(__file__).parent
        written = set()
        for path in source.rglob('*.py'):
            if 'migrations' in str(path) or path.name.startswith('test'):
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                for match in re.finditer(r"""audit\([^,]+,\s*'([a-z][a-z0-9_.]*)'""", line):
                    written.add(match.group(1))
                # The replayed outcome is raised as a keyword argument rather than
                # a literal, so it is matched separately. A dot is required in the
                # pattern, which is what keeps an argparse action='store_true' in
                # a management command out of the results.
                for match in re.finditer(r"""action\s*=\s*'([a-z][a-z0-9_]*\.[a-z0-9_.]+)'""", line):
                    written.add(match.group(1))
        self.assertTrue(written, 'the scan found no audit call sites at all')
        self.assertEqual(sorted(written - set(ACTIONS)), [],
                         'an action is written without a human-readable label')

    def test_the_allowlist_of_organization_less_events_also_has_labels(self):
        self.assertEqual(sorted(ANONYMOUS_ACTIONS - set(ACTIONS)), [])

    def test_labels_never_leave_a_placeholder_in_the_output(self):
        for action, entry in ACTIONS.items():
            for outcome in ('success', 'failure', 'denied', 'rejected', 'replayed'):
                for subject in ('Ada Lovelace', ''):
                    described = describe(action, outcome, subject)
                    self.assertNotIn('{', described['label'], f'{action}/{outcome}')
                    self.assertTrue(described['label'].strip(), f'{action}/{outcome}')
                    self.assertIn(described['severity'], SEVERITIES)

    def test_severity_rises_with_a_bad_outcome_but_never_falls_below_routine(self):
        self.assertEqual(describe('member.created', 'success')['severity'], 'routine')
        self.assertEqual(describe('member.created', 'rejected')['severity'], 'notice')
        self.assertEqual(describe('member.created', 'failure')['severity'], 'warning')
        self.assertEqual(describe('payment.voided', 'failure')['severity'], 'critical')
        self.assertEqual(describe('access.denied', 'success')['severity'], 'critical')

    def test_a_missing_or_malformed_before_and_after_pair_does_not_raise(self):
        # Historical rows and other call sites write different shapes, and reading
        # the history must not be the thing that breaks.
        for details in ({}, {'changed': {}}, {'changed': 'nonsense'}, {'changed': {'a': 1}},
                        {'changed': {'a': {'to': 'b'}}}, None, 'text', []):
            self.assertIsInstance(changes(details), list)

    def test_the_public_taxonomy_covers_every_category_and_outcome(self):
        public = public_taxonomy()
        self.assertEqual({row['id'] for row in public['categories']}, set(CATEGORY_IDS))
        self.assertEqual({row['id'] for row in public['outcomes']}, set(OUTCOME_LABELS))
        for name, entry in public['actions'].items():
            self.assertIn(entry['category'], CATEGORY_IDS, name)
            self.assertIn(entry['severity'], SEVERITIES, name)

    def test_every_reason_the_application_writes_has_a_label(self):
        # The same drift the action scan above guards against, for reasons. An
        # unlabelled reason is not a crash: the row silently reads "it did not
        # complete", which is the one outcome message an administrator cannot act
        # on. This is why the vocabulary lives in one module.
        import pathlib
        source = pathlib.Path(__file__).parent
        written = set()
        for path in source.rglob('*.py'):
            if 'migrations' in str(path) or path.name.startswith('test'):
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                for match in re.finditer(r"""reason\s*=\s*'([a-z][a-z0-9_]*)'""", line):
                    written.add(match.group(1))
        self.assertTrue(written, 'the scan found no reason literals at all')
        # import.failed stores the raised exception's class name, which is
        # deliberately absent: see UNKNOWN_REASON_LABEL.
        self.assertNotIn('error', written)
        self.assertEqual(sorted(written - set(REASON_LABELS)), [],
                         'a reason is written without a plain-language label')

    def test_an_unrecognized_reason_never_echoes_the_stored_value(self):
        # The stored value may be an exception class name or a reason from a
        # newer release than this one. Neither belongs in a summary sentence.
        for reason in ('IntegrityError', 'ValueError', 'something_new', 'x'):
            self.assertEqual(reason_label(reason), UNKNOWN_REASON_LABEL)
        # A success has no reason to explain, so it says nothing at all rather
        # than something vague.
        self.assertEqual(reason_label(''), '')
        self.assertEqual(reason_label(None), '')
        for reason in REASON_LABELS:
            label = reason_label(reason, 'rejected')
            self.assertTrue(label, reason)
            self.assertNotIn('_', label, f'{reason}: a machine value leaked into a sentence')
            self.assertNotIn(reason, label, f'{reason}: the label is just the value back')

