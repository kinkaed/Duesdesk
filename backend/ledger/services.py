from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID
import hashlib
import json
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .models import Member, Payment, DuesMonth, Allocation, AuditEvent

RATE = Decimal('25.00')

def audit(user, action, obj, details=None):
    return AuditEvent.objects.create(actor=user, action=action, entity=obj.__class__.__name__, entity_id=str(obj.pk), details=json.dumps(details or {}, default=str))

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
    paid = dict(Allocation.objects.filter(payment__member=member, payment__voided_at__isnull=True, dues_month__month__gte=month).values('dues_month__month').annotate(total=Sum('amount')).values_list('dues_month__month', 'total'))
    rates = dict(DuesMonth.objects.filter(month__gte=month).values_list('month', 'amount_due'))
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

@transaction.atomic
def record_payment(data, user):
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
    member = Member.objects.select_for_update().get(pk=member_id)
    amount = parse_amount(data.get('amount'))
    month = parse_month(data.get('start_month'))
    normalized = {'member_id': member_id, 'amount': str(amount.quantize(Decimal('.01'))), 'start_month': month.isoformat(), 'payment_date': payment_date.isoformat(), 'method': data.get('method'), 'reference': str(data.get('reference', '')).strip(), 'notes': str(data.get('notes', '')).strip()}
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    existing = Payment.objects.filter(request_key=key).first()
    if existing:
        if existing.member_id != member.pk or existing.amount_received != amount or existing.created_by_id != user.pk or existing.request_fingerprint != fingerprint:
            raise ValueError('This save request has already been used. Reload and try again.')
        return existing
    if data.get('method') not in dict(Payment._meta.get_field('method').choices):
        raise ValueError('Select a payment method.')
    plan = plan_payment(member, amount, month)
    payment = Payment(member=member, amount_received=amount, payment_date=payment_date, method=data['method'], reference=normalized['reference'], notes=normalized['notes'], request_key=key, created_by=user, member_name_snapshot=member.full_name, request_fingerprint=fingerprint)
    payment.full_clean()
    payment.save()
    for item in plan:
        dues, _ = DuesMonth.objects.get_or_create(month=item['month'], defaults={'amount_due': item['dues']})
        Allocation.objects.create(payment=payment, dues_month=dues, amount=item['amount'])
    audit(user, 'payment.recorded', payment, {'amount': str(amount), 'member_id': member.pk, 'months': [p['month'] for p in plan]})
    return payment

@transaction.atomic
def void_payment(pk, user, reason):
    reason = str(reason).strip()
    if not 5 <= len(reason) <= 500:
        raise ValueError('Enter a correction reason between 5 and 500 characters.')
    item = Payment.objects.get(pk=pk)
    Member.objects.select_for_update().get(pk=item.member_id)
    item = Payment.objects.select_for_update().get(pk=pk)
    if item.voided_at:
        raise ValueError('This payment has already been voided.')
    item.voided_at = timezone.now()
    item.voided_by = user
    item.void_reason = reason
    item.save(update_fields=['voided_at','voided_by','void_reason'])
    audit(user, 'payment.voided', item, {'reason': reason, 'amount': str(item.amount_received)})
    return item
