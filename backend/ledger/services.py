from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID
import hashlib
import json
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from .models import Member, Payment, DuesMonth, Allocation, AuditEvent, Organisation

from .access import organization_for, role_for
from .observability import ANONYMOUS_ACTIONS, OUTCOMES, current_context, safe_details
from .observability import logger as obs_logger

RATE = Decimal('25.00')

_UNSET = object()

def audit(user, action, obj=None, details=None, *, outcome='success', reason='', organization=_UNSET, resource='', resource_id='', record_http=True):
    """Record one audit event, or record why it could not be recorded.

    `organization` defaults to the actor's active organization. If the actor has
    no active membership, the affected object's own organization is used, so an
    event caused by a just-disabled account still lands with the tenant it
    happened in rather than raising. Only the pre-auth allowlist in
    observability may have no organization at all, and no user or organization
    is ever invented to fill the column.

    The insert runs inside a savepoint when the caller is already in a
    transaction. A failed audit write is then rolled back on its own and the
    surrounding business transaction stays usable, so a logging fault can never
    turn a successful payment or member edit into a 500.

    Nothing in here is allowed to raise. Resolving the organization, serializing
    the details and the insert are all inside the guard, because the one call
    site that must never fail is a login rejection: an exception from the audit
    helper would replace a 400 with a 500 and hand an attacker a stack trace.
    """
    if outcome not in OUTCOMES:
        # A wrong outcome is a programming error. Fail visibly in the technical
        # log rather than recording a success that did not happen.
        obs_logger.error('Unknown audit outcome %r for action %r', outcome, action)
        outcome = 'failure'
    actor = user if getattr(user, 'is_authenticated', False) else None
    try:
        if organization is _UNSET:
            # organization_for() needs an authenticated user; a pre-auth event
            # passes actor=None, so the lookup is skipped rather than crashed on.
            organization = organization_for(actor) if actor is not None else None
        if organization is None:
            organization = getattr(obj, 'organization', None)
        if organization is None and action not in ANONYMOUS_ACTIONS:
            # Preserving "a null organization means pre-auth" is what keeps tenant
            # queries from ever mixing in an unrelated event. Send it to the
            # technical log, where a developer will see it, instead of writing a
            # row that no tenant could read.
            obs_logger.error('Refusing tenant audit event %r with no organization (actor=%s)', action, getattr(actor, 'username', None))
            return None
        entity = resource or (obj.__class__.__name__ if obj is not None else '')
        identity = resource_id or (str(obj.pk) if obj is not None and getattr(obj, 'pk', None) else '')
        context = current_context()
        fields = {
            'organization': organization,
            'actor': actor,
            'action': action,
            'outcome': outcome,
            'reason': str(reason)[:60],
            'entity': entity[:30],
            'entity_id': identity[:40],
            'request_id': ((context.request_id if context else '') or '')[:64],
            'details': safe_details(details),
        }
        if context:
            fields['ip_address'] = context.ip_address
            fields['user_agent'] = (context.user_agent or '')[:300]
            if record_http:
                fields['http_method'] = (context.method or '')[:10]
                fields['path'] = (context.path or '')[:200]
        with transaction.atomic():
            return AuditEvent.objects.create(**fields)
    except Exception:
        # Deliberately swallowed: the audit trail must not be able to fail an
        # otherwise valid request. The exception is kept in the technical log
        # with the same request id, so it is still investigable.
        obs_logger.exception('Failed to record audit event %s', action)
        return None

def next_month(value):
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)

def parse_month(value):
    try:
        result = date.fromisoformat(str(value) + '-01')
    except (TypeError, ValueError):
        raise ValueError('Choose a valid starting month.')
    if not 2000 <= result.year <= 2100:
        raise ValueError('Choose a month between 2000 and 2100.')
    return result

def _month_start(value):
    return value.replace(day=1) if value else None

def report_horizon(org):
    """The latest month an organization can meaningfully report on.

    Anchored to the organization's own data rather than a fixed far-future
    ceiling, so a stray reporting month cannot walk every member's arrears from
    the month they joined all the way to an arbitrary date.
    """
    today = timezone.localdate().replace(day=1)
    if not org:
        return today
    latest_dues = DuesMonth.objects.filter(organization=org).order_by('-month').values_list('month', flat=True).first()
    bounds = Member.objects.filter(organization=org).aggregate(joined=Max('joined'), billing_end=Max('billing_end'))
    bounds = [_month_start(latest_dues), _month_start(bounds['joined']), _month_start(bounds['billing_end'])]
    return max([today, *[b for b in bounds if b]])

def parse_report_month(value, org=None):
    """Validate a reporting month against the organization's data horizon.

    Deliberately separate from parse_month, which validates payment and member
    dates the user types and must keep its wider accepted range.
    """
    month = parse_month(value)
    horizon = report_horizon(org)
    if month > horizon:
        raise ValueError(f'Reports are available up to {horizon:%B %Y}.')
    return month

def parse_amount(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 3000 or amount != amount.quantize(Decimal('.01')):
            raise ValueError()
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter an amount from GH₵0.01 to GH₵3,000 with at most two decimal places.')
    return amount

def plan_payment(member, amount, month):
    if member.status != 'Active':
        raise ValueError('Only active members can receive a new payment.')
    if month < member.joined.replace(day=1):
        raise ValueError('The covered period cannot begin before the member joined.')
    paid = dict(Allocation.objects.filter(organization=member.organization, payment__member=member, payment__voided_at__isnull=True, dues_month__month__gte=month).values('dues_month__month').annotate(total=Sum('amount')).values_list('dues_month__month', 'total'))
    rates = dict(DuesMonth.objects.filter(organization=member.organization, month__gte=month).values_list('month', 'amount_due'))
    remaining = amount
    result = []
    for _ in range(240):
        if member.billing_end and month > member.billing_end:
            raise ValueError('This payment exceeds the member’s last billable month.')
        rate = rates.get(month, RATE)
        available = max(Decimal('0'), rate - paid.get(month, Decimal('0')))
        if available:
            value = min(remaining, available)
            result.append({'month': month, 'amount': value, 'dues': rate, 'status': 'Paid' if paid.get(month, 0) + value == rate else 'Partial'})
            remaining -= value
        if remaining == 0:
            return result
        month = next_month(month)
    raise ValueError('This payment spans too many months. Choose a later starting month.')

def defer_audit(error, action, obj=None, details=None, *, outcome='rejected', reason=''):
    """Attach an audit event to an exception so it survives the rollback.

    A refusal and its audit event cannot share one transaction. The refusal is
    raised to undo the work it is refusing, and the undo takes the event with it:
    the payment replay, the expired import preview and the half-written import
    batch all produced no row at all, which is exactly when an event matters
    most. The comment above api()'s write lock used to argue the event was safe
    because "the transaction has already rolled back by the time this runs". It
    had not: the caller is still inside that transaction, and the re-raise that
    produces the error response is what ends it.

    So the event travels out with the exception instead. api() is the only place
    that knows the transaction is over, and it writes the event there, after the
    rollback and before the response.
    """
    error.deferred_audit = {'action': action, 'obj': obj, 'details': details,
                            'outcome': outcome, 'reason': reason}
    return error


def write_deferred_audit(user, error):
    """Record an event deferred by defer_audit(), if the error carries one."""
    pending = getattr(error, 'deferred_audit', None)
    if not pending:
        return False
    audit(user, pending['action'], pending['obj'], pending['details'],
          outcome=pending['outcome'], reason=pending['reason'])
    return True


class PaymentRejected(ValueError):
    """A request refused on the grounds that it duplicates an earlier one.

    Carries what the audit event needs, but does not write it here: this is
    raised from inside the payment transaction, so an event written now would be
    rolled back along with the refusal and never exist. The caller records it
    after the transaction has ended, then re-raises this to produce the 400.
    """

    def __init__(self, message, *, action, reason, details, obj=None):
        super().__init__(message)
        self.action = action
        self.reason = reason
        self.details = details
        self.obj = obj


@transaction.atomic
def record_payment_result(data, user):
    """Record a payment, reporting whether this save created it or replayed it."""
    try:
        key = UUID(str(data.get('request_key', '')))
        member_id = int(data.get('member_id', 0))
        payment_date = date.fromisoformat(data.get('payment_date', ''))
    except (ValueError, TypeError):
        raise ValueError('Choose a member and payment date, then try again.')
    if payment_date > timezone.localdate():
        raise ValueError('The payment date cannot be in the future.')
    if payment_date.year < 2000:
        raise ValueError('Choose a payment date from 2000 onwards.')
    org = organization_for(user)
    if not org or role_for(user) != 'secretary':
        raise ValueError('Secretary access is required.')
    Organisation.objects.select_for_update().get(pk=org.pk)
    if organization_for(user) != org or role_for(user) != 'secretary':
        raise ValueError('Your organization access has ended.')
    member = Member.objects.select_for_update().get(pk=member_id, organization=org)
    amount = parse_amount(data.get('amount'))
    month = parse_month(data.get('start_month'))
    normalized = {'member_id': member_id, 'amount': str(amount.quantize(Decimal('.01'))), 'start_month': month.isoformat(), 'payment_date': payment_date.isoformat(), 'method': data.get('method'), 'reference': str(data.get('reference', '')).strip(), 'notes': str(data.get('notes', '')).strip()}
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    existing = Payment.objects.filter(organization=org, request_key=key).first()
    if existing:
        if existing.member_id != member.pk or existing.amount_received != amount or existing.created_by_id != user.pk or existing.request_fingerprint != fingerprint:
            # The same idempotency key carrying a different body: a client retry
            # that changed its payload, or two callers colliding on one key.
            # Worth an event even though the caller still gets the original
            # payment's error, because a pattern of these is a real signal.
            raise PaymentRejected(
                'This save request has already been used. Reload and try again.',
                action='payment.replayed', reason='request_key_conflict',
                details={'request_key': str(key), 'member_id': member.pk, 'amount': str(amount)},
                obj=existing,
            )
        # An identical retry. Not an event in its own right: a double click must
        # not manufacture a second payment record in the history.
        return existing, False
    if data.get('method') not in dict(Payment._meta.get_field('method').choices):
        raise ValueError('Select a payment method.')
    plan = plan_payment(member, amount, month)
    payment = Payment(organization=org, member=member, amount_received=amount, payment_date=payment_date, method=data['method'], reference=normalized['reference'], notes=normalized['notes'], request_key=key, created_by=user, member_name_snapshot=member.full_name, request_fingerprint=fingerprint)
    payment.full_clean()
    payment.save()
    for item in plan:
        dues, _ = DuesMonth.objects.get_or_create(organization=org, month=item['month'], defaults={'amount_due': item['dues']})
        Allocation.objects.create(organization=org, payment=payment, dues_month=dues, amount=item['amount'])
    audit(user, 'payment.recorded', payment, {'amount': str(amount), 'member_id': member.pk, 'months': [p['month'] for p in plan]})
    return payment, True

def record_payment(data, user):
    return record_payment_result(data, user)[0]

@transaction.atomic
def void_payment(pk, user, reason):
    reason = str(reason).strip()
    if not 5 <= len(reason) <= 500:
        raise ValueError('Enter a correction reason between 5 and 500 characters.')
    org = organization_for(user)
    if not org or role_for(user) != 'secretary':
        raise ValueError('Secretary access is required.')
    Organisation.objects.select_for_update().get(pk=org.pk)
    if organization_for(user) != org or role_for(user) != 'secretary':
        raise ValueError('Your organization access has ended.')
    item = Payment.objects.get(pk=pk, organization=org)
    Member.objects.select_for_update().get(pk=item.member_id,organization=org)
    item = Payment.objects.select_for_update().get(pk=pk, organization=org)
    if item.voided_at:
        raise ValueError('This payment has already been voided.')
    item.voided_at = timezone.now()
    item.voided_by = user
    item.void_reason = reason
    item.save(update_fields=['voided_at','voided_by','void_reason'])
    audit(user, 'payment.voided', item, {'reason': reason, 'amount': str(item.amount_received)})
    return item
