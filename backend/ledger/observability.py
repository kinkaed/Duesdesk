"""Request correlation, audit recording and structured technical logging.

Two streams are kept deliberately apart:

* Audit events answer "what changed, and who caused it". They are rows in the
  AuditEvent table, are tenant scoped, and are treated as evidence.
* Technical logs answer "what happened inside the software". They go through
  the standard library logging module and are not part of the audit record.

Nothing here dumps request.POST, request.body or headers. Callers pass explicit
fields they have already decided are safe, and redact() is a second line of
defence so a careless caller still cannot write a password or token to the
database or a log line.
"""
import ipaddress
import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import date, datetime, time as dtime
from decimal import Decimal

from django.conf import settings
from django.db import transaction

logger = logging.getLogger('ledger.observability')

# Render's edge sets tracerid for request tracing; some ingress in front of it
# sets X-Request-Id. Both are only ever accepted after validation.
REQUEST_ID_HEADERS = ('HTTP_X_REQUEST_ID', 'HTTP_TRACERID')
INBOUND_REQUEST_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

MAX_USER_AGENT = 300
MAX_DETAILS_CHARS = 8000
MAX_STRING_CHARS = 1000

# A field name containing any of these never has its value recorded. Matching is
# on the name, case insensitively, and deliberately does not include a bare
# "key" so that idempotency identifiers such as request_key stay useful.
SENSITIVE_KEY_PARTS = (
    'password', 'passwd', 'passphrase', 'secret', 'token', 'csrf', 'cookie',
    'authorization', 'credential', 'api_key', 'apikey', 'access_key',
    'private_key', 'otp', 'pin', 'session_key', 'signature', 'salt',
)

REDACTED = '[redacted]'

# logging refuses an extra whose key collides with an existing LogRecord
# attribute, raising rather than logging. Derive the set from a throwaway record
# so it cannot drift from the standard library; 'message' and 'asctime' are added
# by hand because logging rejects them by name even though a fresh record does
# not carry them. 'event' is added because this module sets it itself and a
# caller-supplied one must not silently replace the name the line is searched by.
# The correlation field names (request_id, method, path, status, duration_ms, ip,
# actor_id) are deliberately NOT here: they are not LogRecord attributes, they
# are exactly what callers need to pass, and dropping them would leave every line
# uncorrelated.
_RESERVED_LOG_ATTRS = frozenset(
    set(vars(logging.LogRecord('', logging.INFO, '', 0, '', (), None)))
    | {'message', 'asctime', 'event'}
)


OUTCOMES = frozenset({'success', 'failure', 'denied', 'rejected', 'replayed'})

# Events that can happen before anyone is authenticated, and so have no
# organization. Restricted to an explicit allowlist so a bug cannot quietly
# write a tenant event with no organization, which no tenant query would ever
# return. Never invent a user or an organization to satisfy the schema.
#
# access.denied is deliberately NOT here. A row with no organization is visible
# to no tenant at all, so allowlisting it would only trade a useful refusal for
# an invisible row. The denial is attributed to the organization the account
# actually belongs to instead, at the call site.
ANONYMOUS_ACTIONS = frozenset({
    'auth.login.success',
    'auth.login.failure',
    'auth.logout',
    'auth.recovery.requested',
    'security.account_locked',
    'security.csrf.failure',
    'security.suspicious_request',
    'system.startup',
})


class RequestContext:
    """Immutable per-request data that audit events and logs are built from."""

    __slots__ = ('request_id', 'method', 'path', 'actor_id', 'organization_id', 'ip_address', 'user_agent', 'proxy_trusted')

    def __init__(self, request_id, method='', path='', actor_id=None, organization_id=None,
                 ip_address=None, user_agent='', proxy_trusted=False):
        self.request_id = request_id
        self.method = method
        self.path = path
        self.actor_id = actor_id
        self.organization_id = organization_id
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.proxy_trusted = proxy_trusted

    def as_log_fields(self):
        fields = {'request_id': self.request_id}
        if self.method:
            fields['method'] = self.method
        if self.path:
            fields['path'] = self.path
        if self.ip_address:
            fields['ip'] = self.ip_address
        return fields


# A ContextVar rather than a module global: waitress runs 8 threads and reuses
# them, so a plain module attribute would leak one request's context into an
# unrelated later request served by the same thread. The middleware always
# resets this in a finally block.
_current: ContextVar = ContextVar('duedesk_request_context', default=None)


def current_context():
    return _current.get()


def bind_context(context):
    """Install context for this request and return a token for reset_context."""
    return _current.set(context)


def reset_context(token):
    try:
        _current.reset(token)
    except ValueError:
        # The token belongs to a different context (only possible if a worker
        # resets out of order). Clearing outright is safer than leaking.
        _current.set(None)


def new_request_id(headers=None):
    """Return a validated inbound request id, or a fresh generated one.

    A client-supplied id is only trusted if it is short and matches a strict
    character class; anything else is discarded and replaced, so a caller cannot
    inject newlines or fake fields into somebody else's log line.
    """
    for name in REQUEST_ID_HEADERS:
        candidate = (headers or {}).get(name)
        if candidate:
            candidate = candidate.strip()
            if INBOUND_REQUEST_ID.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex[:16]


def client_ip(request):
    """The peer address Django resolved, or None.

    Deliberately does not read X-Forwarded-For. When a trusted proxy is
    configured (serve.py passes waitress trusted_proxy and
    clear_untrusted_proxy_headers), waitress has already rewritten REMOTE_ADDR
    from the forwarded header and stripped spoofed ones. Reading XFF here would
    hand an untrusted client control of an audited field. When no proxy is
    trusted, this value is the proxy's address and must not be treated as the
    client's.

    Anything that is not a valid IP is discarded rather than stored: a
    REMOTE_ADDR that is not an address would fail GenericIPAddressField
    validation on insert and take the surrounding business transaction with it.
    """
    if request is None:
        return None
    candidate = (request.META.get('REMOTE_ADDR') or '').strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def proxy_trusted():
    return bool(getattr(settings, 'TRUST_PROXY', False))


def _is_sensitive(key):
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact(value, _depth=0):
    """Recursively replace sensitive values. Returns a structure safe to store."""
    if _depth > 6:
        return '[truncated]'
    if isinstance(value, dict):
        return {k: (REDACTED if _is_sensitive(k) else redact(v, _depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
        return value[:MAX_STRING_CHARS] + '…'
    return value


def safe_details(details):
    """Serialize audit details: redact first, then cap the stored size."""
    payload = redact(details if details is not None else {})
    try:
        text = json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = json.dumps({'unserializable': str(payload)[:MAX_DETAILS_CHARS]})
    if len(text) > MAX_DETAILS_CHARS:
        text = json.dumps({'truncated': True, 'preview': text[:MAX_DETAILS_CHARS // 2]})
    return text


def actor_username(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'username', None) or None


def _timestamp(value):
    if isinstance(value, (datetime, dtime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


class RequestContextFilter(logging.Filter):
    """Attach the current request to every technical log record.

    Reads from the ContextVar so a logger call anywhere in the request gets
    correlation without being handed the request object. A field the caller
    already supplied always wins; the rest are filled from the current context,
    or from '-' when there is no request at all, because the human-readable
    format string references these attributes on every line and a %-style format
    referencing an attribute the record does not have raises and prints a logging
    error to the terminal, which is the opposite of the point.
    """

    FORMAT_FIELDS = ('request_id', 'method', 'path', 'status', 'duration_ms', 'actor_id')
    # Placeholder used when a value is genuinely unknown, rather than absent.
    BLANK = '-'

    def filter(self, record):
        context = _current.get()
        for field in self.FORMAT_FIELDS:
            if getattr(record, field, None) not in (None, ''):
                continue
            value = getattr(context, field, None) if context is not None else None
            setattr(record, field, self.BLANK if value in (None, '') else value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log aggregation in production.

    Built on the standard library so production logging needs no extra
    dependency. Human-readable output is used outside production.
    """

    def format(self, record):
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        for field in ('request_id', 'method', 'path', 'status', 'duration_ms',
                      'ip', 'actor_id', 'organization_id', 'event', 'outcome',
                      'reason', 'resource', 'resource_id', 'entity', 'entity_id'):
            value = getattr(record, field, None)
            # The '-' placeholder exists so the human-readable format string has
            # something to print. In JSON it would be noise: a field that is
            # genuinely unknown is better absent than present and meaningless,
            # because a log query for status=500 should not match "-" too.
            if value in (None, '', RequestContextFilter.BLANK):
                continue
            payload[field] = value
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def technical(event, message='', /, *args, level=logging.INFO, exc_info=None, **fields):
    """Emit a technical log line with a stable event name and safe extras.

    `event` and `message` are positional-only so that a caller can still pass
    extras named `event` or `message`: correlation fields use exactly those
    words, and a collision would either be a TypeError in the middle of handling
    a request or, worse, quietly replace the log line's own message.

    The message is a %-style format string, so user data belongs in `args` or in
    the redacted extras, never interpolated into the format string itself. Extras
    go through redact() for the same reason audit details do: a technical log is
    still a place a password can leak, and it is the one that gets shipped
    off-box to a log aggregator, where it is much harder to take back.

    exc_info is a keyword of logging.log rather than an extra, so it is passed
    straight through; everything else in **fields becomes structured data.
    """
    safe = {key: value for key, value in redact(fields).items() if key not in _RESERVED_LOG_ATTRS}
    logger.log(level, message or event, *args, exc_info=exc_info, extra={'event': event, **safe})

