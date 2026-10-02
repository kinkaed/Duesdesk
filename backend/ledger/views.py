import csv
import io
import json
import hashlib
import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps
from uuid import uuid5, NAMESPACE_URL
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import PasswordResetView
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError, connection
from django.db.models import Sum, Q, Count
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from .forms import SignupForm
from .models import Member, Payment, Allocation, DuesMonth, UserAccess, AuditEvent, Organisation, RecoveryAttempt, ImportBatch, SecretaryInvite
from .access import role_for, visible_members, visible_payments, organization_for, membership, last_access_organization, revoke_sessions
from .services import RATE, next_month, parse_month, parse_amount, parse_report_month, plan_payment, record_payment, record_payment_result, void_payment, audit, defer_audit, write_deferred_audit, PaymentRejected

from . import audit_taxonomy
from .branding import branding_json, color, read_logo_token
from .observability import proxy_trusted

logger = logging.getLogger(__name__)

# Members are records, not accounts, so this is never shown to a member: there is
# no member login to reach it. It covers accounts with no active membership, such
# as a createsuperuser awaiting assignment or access that has been withdrawn.
NO_ACCESS = 'Your account has no active organization access. Contact your organization secretary or an administrator.'
# Shared by api() and the per-view role checks below so every JSON 403 for an
# insufficient role reads the same, instead of a zero-byte body.
READ_ONLY = 'Your account is read-only.'

# One wording for every record the caller cannot see. The audit history already
# used "That audit event does not exist." for the same situation, so this is
# that phrasing widened to the other record types. Deliberately does not name
# the type: saying "member" where the id was actually an invite would tell a
# caller probing another tenant which table the id space belongs to.
NOT_FOUND = 'That record does not exist.'

# One page of the history. Kept as a named constant because the endpoint also
# reads one row beyond it to decide has_more, and that arithmetic has to stay in
# step with the page it is slicing.
AUDIT_PAGE_SIZE = 100

# Deepest page the endpoint will actually run. Past this the offset would be so
# large that the database walks the whole history to discard it, and no reader
# is paging through hundreds of screens of a small association's history.
AUDIT_MAX_PAGE = 1000

# How many matching records the free-text search will look behind. Search is a
# scan, so the cap is what keeps a single-character-ish query from turning into
# an unbounded join on a large tenant.
AUDIT_SEARCH_MATCH_LIMIT = 200

# Member.code is a property, not a column, so a typed code is matched by parsing
# the number back out rather than by querying for it.
MEMBER_CODE_PATTERN = re.compile(r'^MBR-0*(\d{1,9})$', re.IGNORECASE)

# One page of the payments list, and the deepest page it will actually run. Same
# reasoning as the audit history below: past the cap the offset is large enough
# that the database walks past the whole table in order to discard the rows, and
# no secretary is paging through a thousand screens of payments.
PAYMENT_PAGE_SIZE = 100
PAYMENT_MAX_PAGE = 1000

def _db_diagnostics(error):
    """Pull the failing statement, table and constraint out of a database error.

    The generic 409 gave no way to find the cause, so record everything the
    driver knows. Returns a dict that is empty when the driver says nothing.
    """
    for attribute in ('__cause__', '__context__'):
        diag = getattr(getattr(error, attribute, None), 'diag', None)
        if diag is None:
            continue
        fields = ('constraint_name', 'table_name', 'column_name', 'message_primary', 'statement_position')
        found = {name: getattr(diag, name, None) for name in fields if getattr(diag, name, None)}
        if found:
            return found
    return {}

def read_only_post(view):
    """Mark a POST view that only reads, so api() skips the organization write lock.

    The lock in api() exists to serialize writes. Marking a genuinely read-only
    POST keeps it from blocking every other writer in the organization, without
    changing the authentication, CSRF or role checks that run before the lock.
    """
    view.read_only = True
    return view

def api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Your session ended. Please sign in again.'}, status=401)
        role = role_for(request.user)
        if not role:
            # An account with no active membership reaching a data endpoint is
            # worth an event: it is either a stale session after a disable, or
            # someone using a live credential against data they cannot see.
            # Without the organization it could only ever be a technical log line,
            # because audit() refuses a tenant event that no tenant owns. The
            # account usually still has a UserAccess row naming the organization
            # it lost, so the event lands with the tenant it actually happened in.
            audit(request.user,'access.denied',outcome='denied',reason='no_active_membership',
                  details={'view':view.__name__},
                  organization=last_access_organization(request.user))
            return JsonResponse({'error': NO_ACCESS},status=403)
        if request.method not in ('GET','HEAD') and role != 'secretary':
            audit(request.user,'access.denied',outcome='denied',reason='read_only_role',
                  details={'view':view.__name__})
            return JsonResponse({'error': READ_ONLY}, status=403)

        try:
            if request.method not in ('GET', 'HEAD') and not getattr(view, 'read_only', False):
                with transaction.atomic():
                    org = organization_for(request.user)
                    if not org:return JsonResponse({'error':'Your organization access has ended.'},status=403)
                    Organisation.objects.select_for_update().get(pk=org.pk)
                    if organization_for(request.user) != org or role_for(request.user) != 'secretary':
                        return JsonResponse({'error':'Your organization access has ended.'}, status=403)
                    return view(request, *args, **kwargs)
            return view(request, *args, **kwargs)
        except (Http404, Member.DoesNotExist, Payment.DoesNotExist) as missing:
            # A member, account, invite or payment that is not in the caller's
            # organization. Django's own handler answers this with a rendered
            # HTML page, which is the wrong shape entirely for a JSON endpoint:
            # api.ts has no status-specific handling for it and shows the
            # response as an infrastructure failure rather than "not found".
            # The same 404 wording for every record keeps a missing record and
            # someone else's record indistinguishable, which is what stops a
            # secretary walking the id space to enumerate other tenants' data.
            write_deferred_audit(request.user, missing)
            return JsonResponse({'error': NOT_FOUND}, status=404)
        except (ValueError, TypeError, ValidationError) as error:
            message = '; '.join(error.messages) if isinstance(error, ValidationError) else str(error)
            write_deferred_audit(request.user, error)
            return JsonResponse({'error': message or 'Check the entered values.'}, status=400)
        except IntegrityError as error:
            # Never swallow this silently. The generic 409 on its own left no way
            # to find the cause, so log the traceback plus everything the driver
            # reports about the failing statement.
            write_deferred_audit(request.user, error)
            details=_db_diagnostics(error)
            logger.exception('IntegrityError in %s db=%s %s', view.__name__, connection.vendor, details or 'no driver detail')
            constraint=details.get('constraint_name')
            return JsonResponse({'error': 'A conflicting record already exists. Refresh and try again.', 'detail': f'{view.__name__} violates {constraint}.' if constraint else f'{view.__name__} raised a database conflict.', 'constraint':constraint,'table':details.get('table_name')}, status=409)
        except Exception as error:
            # Anything the handlers above do not recognise still has to leave its
            # event behind. The refusals api() knows how to word are the ones a
            # view anticipated; this is the half-written import batch, the payment
            # that died between saving the row and writing its allocations. Those
            # are exactly the events that were being lost, because nothing was
            # there to catch them. The error still propagates: this is a real fault
            # and the caller keeps getting a 500 rather than a tidy 400.
            write_deferred_audit(request.user, error)
            raise
    return wrapped

def secretary_only(request):
    return role_for(request.user) == 'secretary'

def body(request):
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict): raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Send a valid form.')

def member_rows(month, user):
    people = list(visible_members(user).order_by('full_name'))
    amounts = Allocation.objects.filter(organization=organization_for(user), payment__voided_at__isnull=True, payment__member__in=people, dues_month__month__lte=month).values('payment__member_id','dues_month__month').annotate(total=Sum('amount'))
    paid = {(r['payment__member_id'], r['dues_month__month']): r['total'] for r in amounts}
    rates = {(d.organization_id,d.month): d.amount_due for d in DuesMonth.objects.filter(organization_id__in={p.organization_id for p in people},month__lte=month)}
    rows = []
    for person in people:
        start = person.joined.replace(day=1)
        applicable = start <= month and (not person.billing_end or month <= person.billing_end)
        amount = paid.get((person.pk, month), Decimal('0'))
        balance = max(Decimal('0'), rates.get((person.organization_id,month),RATE) - amount) if applicable else Decimal('0')
        status = ('Paid' if balance == 0 else 'Partial' if amount else 'Unpaid') if applicable else 'Not due'
        arrears = Decimal('0')
        cursor = start
        last = min(month, person.billing_end) if person.billing_end else month
        while cursor <= last:
            arrears += max(Decimal('0'), rates.get((person.organization_id,cursor),RATE) - paid.get((person.pk,cursor),Decimal('0')))
            cursor = next_month(cursor)
        rows.append({'id':person.pk,'code':person.code,'name':person.full_name,'phone':person.phone,'email':person.email,'joined':person.joined.isoformat(),'billing_end':person.billing_end.strftime('%Y-%m') if person.billing_end else '', 'member_status':person.status,'paid':str(amount),'balance':str(balance),'arrears':str(arrears),'status':status})
    return rows

def payment_json(payment):
    return {'id':payment.pk,'receipt':payment.receipt_number,'member_id':payment.member_id,'member':payment.member_name_snapshot or payment.member.full_name,'amount':str(payment.amount_received),'date':payment.payment_date.isoformat(),'method':payment.method,'reference':payment.reference,'notes':payment.notes,'voided':bool(payment.voided_at),'void_reason':payment.void_reason,'allocations':[{'month':a.dues_month.month.strftime('%B %Y'),'amount':str(a.amount)} for a in payment.allocations.all()]}

@login_required
def home(request):
    if not role_for(request.user): return HttpResponse(NO_ACCESS,status=403)
    return render(request,'react.html')

@api
@require_GET
@ensure_csrf_cookie
def session_info(request):
    org=organization_for(request.user)
    return JsonResponse({'username':request.user.get_full_name() or request.user.username,'role':role_for(request.user),'user_id':request.user.pk,'today':timezone.localdate().isoformat(),'demo':settings.DEMO_MODE,'organisation':org.name, 'branding':branding_json(org)})

@api
@require_GET
def overview(request):
    month = parse_report_month(request.GET.get('month',timezone.localdate().strftime('%Y-%m')),organization_for(request.user))
    rows = member_rows(month,request.user)
    due = [r for r in rows if r['status']!='Not due']
    records = visible_payments(request.user)
    transactions = records.filter(voided_at__isnull=True,payment_date__gte=month,payment_date__lt=next_month(month))
    collections = transactions.aggregate(total=Sum('amount_received'))['total'] or 0
    assigned = Allocation.objects.filter(organization=organization_for(request.user), payment__in=records,payment__voided_at__isnull=True,dues_month__month=month).aggregate(total=Sum('amount'))['total'] or 0
    recent = records.select_related('member').prefetch_related('allocations__dues_month').order_by('-created_at')[:8]
    return JsonResponse({'members':rows,'role':role_for(request.user),'metrics':{'collections':str(collections),'assigned':str(assigned),'outstanding':str(sum(Decimal(r['balance']) for r in due)),'arrears':str(sum(Decimal(r['arrears']) for r in rows)),'paid':sum(r['status']=='Paid' for r in due),'active':len(due)},'recent':[payment_json(p) for p in recent]})

# The member fields an edit can change, and serialize() renders them for the audit
# history. Dates become ISO strings and None stays null so the stored details are
# JSON without a custom encoder.
MEMBER_AUDITED_FIELDS=('full_name','phone','email','status','joined','billing_end')

def serialize(value):
    return value.isoformat() if isinstance(value,date) else value

def fill_member(person,data):
    try: joined=date.fromisoformat(data.get('joined',''))
    except (TypeError,ValueError): raise ValueError('Enter a joining date.')
    if not date(2000,1,1)<=joined<=timezone.localdate(): raise ValueError('Joining date must be between 2000 and today.')
    end = parse_month(data['billing_end']) if data.get('billing_end') else None
    if end and end<joined.replace(day=1): raise ValueError('Last billable month cannot be before the joining month.')
    if person.pk and person.payments.exists():
        if person.joined!=joined: raise ValueError('Joining date is locked after the first payment. Contact your administrator to correct historical billing.')
        if end and Allocation.objects.filter(organization=person.organization, payment__member=person,payment__voided_at__isnull=True,dues_month__month__gt=end).exists():
            raise ValueError('There are payments beyond the chosen last billable month. Correct those payments first.')
    person.full_name=str(data.get('name','')).strip()
    person.phone=str(data.get('phone','')).strip()
    person.email=str(data.get('email','')).strip()
    person.joined=joined
    person.status=data.get('status','Active')
    person.billing_end=end
    person.full_clean()
    return person

@api
@require_http_methods(['GET','POST'])
def members(request):
    if request.method=='GET':return JsonResponse({'members':member_rows(timezone.localdate().replace(day=1),request.user)})
    with transaction.atomic():
        person=fill_member(Member(organization=organization_for(request.user)),body(request));person.save()
        audit(request.user,'member.created',person,{'name':person.full_name,'joined':person.joined})
    return JsonResponse({'id':person.pk,'name':person.full_name,'code':person.code},status=201)

@api
@require_http_methods(['GET','POST'])
def member_detail(request,pk):
    if request.method=='POST':
        with transaction.atomic():
            member=get_object_or_404(visible_members(request.user).select_for_update(),pk=pk)
            before={field:serialize(getattr(member,field)) for field in MEMBER_AUDITED_FIELDS}
            fill_member(member,body(request)).save()
            after={field:serialize(getattr(member,field)) for field in MEMBER_AUDITED_FIELDS}
            # Only the fields that actually changed. Recording a full before/after
            # of an unchanged phone number on every edit makes the history harder
            # to read, and stores personal data that was never in question.
            changed={field:{'from':before[field],'to':after[field]} for field in MEMBER_AUDITED_FIELDS if before[field]!=after[field]}
            audit(request.user,'member.updated',member,{'changed':changed} if changed else {'changed':{}})
        return JsonResponse({'id':member.pk,'name':member.full_name})
    member=get_object_or_404(visible_members(request.user),pk=pk)
    records=visible_payments(request.user).filter(member=member).select_related('member').prefetch_related('allocations__dues_month').order_by('-payment_date','-id')
    total=records.filter(voided_at__isnull=True).aggregate(total=Sum('amount_received'))['total'] or 0
    row=next(r for r in member_rows(timezone.localdate().replace(day=1),request.user) if r['id']==member.pk)
    return JsonResponse({**row,'total':str(total),'payments':[payment_json(p) for p in records]})

@api
@require_http_methods(['POST'])
@read_only_post
def payment_preview(request):
    data=body(request)
    try:member_id=int(data.get('member_id',0))
    except (TypeError,ValueError):raise ValueError('Choose a member.')
    # get_object_or_404 rather than .get(): a member that is not in the caller's
    # organization is a 404, which is what every other record lookup already
    # answers, instead of a 400 carrying "Member matching query does not exist."
    member=get_object_or_404(visible_members(request.user),pk=member_id)
    plan=plan_payment(member,parse_amount(data.get('amount')),parse_month(data.get('start_month')))
    return JsonResponse({'allocations':[{'month':p['month'].strftime('%B %Y'),'amount':str(p['amount']),'status':p['status']} for p in plan]})

@api
@require_http_methods(['GET','POST'])
def payments(request):
    if request.method=='GET':
        # Same bounded read as the audit history. A hand-typed or huge page
        # value used to reach int() uncaught, so api() answered 400 with the
        # verbatim Python message "invalid literal for int() with base 10",
        # and a page large enough to overflow the offset arithmetic became an
        # IntegrityError and a misleading 409 about a conflicting record.
        page=_audit_page(request.GET.get('page')) or 1
        if page > PAYMENT_MAX_PAGE:
            return JsonResponse({'payments':[],'page':page,'has_more':False})
        items=visible_payments(request.user).select_related('member').prefetch_related('allocations__dues_month').order_by('-payment_date','-id')
        return JsonResponse({'payments':[payment_json(p) for p in items[(page-1)*PAYMENT_PAGE_SIZE:page*PAYMENT_PAGE_SIZE]],'page':page,'has_more':items.count()>page*PAYMENT_PAGE_SIZE})
    try:
        item,created=record_payment_result(body(request),request.user)
    except PaymentRejected as rejection:
        # Deferred rather than recorded. This except block is still inside the
        # write lock api() holds, so an event written here is rolled back by the
        # re-raise on the next line and the replay leaves no trace at all. The
        # event rides out with the exception instead and api() writes it once
        # the transaction is over.
        defer_audit(rejection,rejection.action,rejection.obj,rejection.details,outcome='replayed',reason=rejection.reason)
        raise
    return JsonResponse({'id':item.pk,'receipt':item.receipt_number,'amount':str(item.amount_received)},status=201 if created else 200)

@api
@require_http_methods(['POST'])
def void(request,pk):
    item=void_payment(pk,request.user,body(request).get('reason',''))
    return JsonResponse({'id':item.pk,'receipt':item.receipt_number})

@login_required
def receipt(request,pk):
    item=get_object_or_404(visible_payments(request.user).select_related('member').prefetch_related('allocations__dues_month'),pk=pk)
    return render(request,'receipt.html',{'payment':item})

def safe_cell(value):
    value=str(value)
    return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value

def report_data(request):
    month=parse_report_month(request.GET.get('month',timezone.localdate().strftime('%Y-%m')),organization_for(request.user))
    kind=request.GET.get('kind','balances')
    if kind not in ('balances','payments'):raise ValueError('Choose a valid report type.')
    if kind=='payments':
        header=['Receipt','Member ID','Member','Received','Method','Amount GHS','Reference','Status','Void reason']
        payments=visible_payments(request.user).filter(payment_date__gte=month,payment_date__lt=next_month(month)).select_related('member').order_by('id')
        rows=[[p.receipt_number,p.member.code,p.member_name_snapshot or p.member.full_name,p.payment_date.isoformat(),p.method,float(p.amount_received),p.reference,'VOID' if p.voided_at else 'Valid',p.void_reason] for p in payments]
    else:
        header=['Member ID','Name','Month','Paid GHS','Outstanding GHS','Arrears through month GHS','Payment status','Member status']
        rows=[[r['code'],r['name'],month.strftime('%Y-%m'),float(r['paid']),float(r['balance']),float(r['arrears']),r['status'],r['member_status']] for r in member_rows(month,request.user)]
    return month,header,[[safe_cell(v) if isinstance(v,str) else v for v in row] for row in rows]

@api
@require_GET
def export_csv(request):
    month,header,rows=report_data(request)
    response=HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition']=f'attachment; filename="dues-{month:%Y-%m}.csv"'
    response.write('\ufeff')
    writer=csv.writer(response);writer.writerow(header);writer.writerows(rows)
    audit(request.user,'export.generated',request.user,{'format':'csv','month':month,'rows':len(rows)})
    return response

@api
@require_GET
def export_excel(request):
    month,header,rows=report_data(request)
    book=Workbook();sheet=book.active;sheet.title='Monthly report'
    sheet.append(header)
    for row in rows:sheet.append(row)
    for cell in sheet[1]:cell.font=Font(color='FFFFFF',bold=True);cell.fill=PatternFill('solid',fgColor='214F43')
    sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width=min(45,max(18,max(len(str(c.value or '')) for c in column)+2))
    output=io.BytesIO();book.save(output)
    response=HttpResponse(output.getvalue(),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition']=f'attachment; filename="dues-{month:%Y-%m}.xlsx"'
    audit(request.user,'export.generated',request.user,{'format':'xlsx','month':month,'rows':len(rows)})
    return response

@api
@require_http_methods(['GET','POST'])
def organisation_settings(request):
    if not secretary_only(request):return JsonResponse({'error': READ_ONLY},status=403)
    item=organization_for(request.user)
    if request.method=='POST':
        with transaction.atomic():
            data=body(request)
            item.name=str(data.get('name',item.name)).strip();item.contact=str(data.get('contact',item.contact)).strip();item.receipt_footer=str(data.get('receipt_footer',item.receipt_footer)).strip()
            for field in ('primary','secondary','accent'):
                if field in data:setattr(item,field,color(data[field]))
            if data.get('logo_token'):item.logo=read_logo_token(data['logo_token'],f'org:{item.pk}')
            if data.get('remove_logo'):item.logo=b''
            item.full_clean();item.save();audit(request.user,'organization.updated',item,{'name':item.name})
    return JsonResponse({**branding_json(item),'name':item.name,'contact':item.contact,'receipt_footer':item.receipt_footer})

def _audit_page(value):
    """Read the page number, or return None when the request is not asking for a page.

    int() on a hand-typed query string raises, and that exception was being caught
    by api() and rendered as a verbatim Python message. A malformed page is
    treated as no page at all, which is the same "ignore what you cannot parse"
    rule the date filters already use.
    """
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _audit_search_term(value):
    """The free-text query, bounded before it reaches the database.

    Two characters is enough to cut a large history down to something readable
    and short enough that a scan of one organization's rows stays cheap. The
    length is truncated rather than refused so a pasted sentence still filters.
    """
    term = (value or '').strip()
    return term[:64] if len(term) >= 2 else ''


def _audit_named_records(organization, term):
    """Ids of members and payments whose name matches the search term.

    An audit row stores an id, never a name, so "what happened to this member"
    cannot be answered from the row alone. The names live in the organization's
    own records, so the term is resolved there first and the ids are matched
    against the events. Both lookups are filtered to the caller's organization,
    so a name belonging to another tenant cannot widen the result.

    Payments are matched on both the member's current name and the name frozen
    onto the payment when it was taken, so a member who has since been renamed
    is still findable under the name they had at the time.
    """
    member_ids = [str(pk) for pk in Member.objects.filter(organization=organization,
                                                          full_name__icontains=term)
                  .order_by('pk').values_list('pk', flat=True)[:AUDIT_SEARCH_MATCH_LIMIT]]
    # A member's code is shown on every row but is derived, not stored, so the one
    # identifier a reader can see is otherwise the one they cannot search for.
    if code:=MEMBER_CODE_PATTERN.match(term):
        member_ids.append(code.group(1))
    payment_ids = [str(pk) for pk in Payment.objects.filter(organization=organization)
                   .filter(Q(member__full_name__icontains=term) |
                           Q(member_name_snapshot__icontains=term))
                   .order_by('pk').values_list('pk', flat=True)[:AUDIT_SEARCH_MATCH_LIMIT]]
    return member_ids, payment_ids


def _audit_actors(organization):
    """The organization's accounts, for the role column and the actor filter.

    One query for both uses. Keyed by user id because that is what an event row
    holds, so a disabled account is still described correctly, and a username
    list because that is what the filter control needs.
    """
    roles = {}
    options = []
    for user_id, username, role in UserAccess.objects.filter(organization=organization)\
            .values_list('user_id', 'user__username', 'role'):
        roles[user_id] = role
        options.append({'username': username, 'role': role})
    return roles, sorted(options, key=lambda row: row['username'])


def _audit_resource_labels(organization, requests):
    """Turn stored entity ids into names, in one query per resource type.

    `requests` is a set of (entity, entity_id) pairs drawn from the page being
    returned. Resolving them individually would be one query per row, so they are
    collected first and fetched in bulk.

    Every lookup is filtered on the caller's organization, not merely on the ids.
    The ids come from rows the caller is already entitled to, so this cannot widen
    anything today, but a name is the one piece of a row that could disclose
    another tenant's data, and it is cheap to make that impossible to get wrong.
    An id that no longer resolves is left out rather than invented: members have
    no delete endpoint today, but a record removed by a future migration or by a
    database administrator must not render as a blank or a wrong name.
    """
    labels = {}
    member_ids = {value for entity, value in requests if entity == 'Member' and value.isdigit()}
    payment_ids = {value for entity, value in requests if entity == 'Payment' and value.isdigit()}
    account_ids = {value for entity, value in requests if entity == 'User' and value.isdigit()}
    invite_ids = {value for entity, value in requests if entity == 'SecretaryInvite' and value.isdigit()}
    batch_ids = {value for entity, value in requests if entity == 'ImportBatch' and value.isdigit()}
    numeric = lambda values: {int(value) for value in values}

    if member_ids:
        for member in Member.objects.filter(organization=organization, pk__in=numeric(member_ids)):
            labels[('Member', str(member.pk))] = f'{member.full_name} ({member.code})'
    if payment_ids:
        for payment in Payment.objects.filter(organization=organization, pk__in=numeric(payment_ids)).select_related('member'):
            name = payment.member_name_snapshot or payment.member.full_name
            labels[('Payment', str(payment.pk))] = f'{payment.receipt_number} ({name})'
    if account_ids:
        for account in User.objects.filter(access__organization=organization, pk__in=numeric(account_ids)):
            labels[('User', str(account.pk))] = account.username
    if invite_ids:
        for invite in SecretaryInvite.objects.filter(organization=organization, pk__in=numeric(invite_ids)):
            labels[('SecretaryInvite', str(invite.pk))] = invite.email
    if batch_ids:
        for batch in ImportBatch.objects.filter(organization=organization, pk__in=numeric(batch_ids)):
            noun = 'payment' if batch.kind.startswith('payment') else 'member'
            labels[('ImportBatch', str(batch.pk))] = f'{batch.row_count} {noun} rows'
    if any(entity == 'Organisation' for entity, _ in requests):
        labels[('Organisation', str(organization.pk))] = organization.name
    return labels


def _audit_row(event, labels, roles, related_counts):
    """Serialize one event for the list.

    `details` is intentionally absent. It can hold 8 KB of JSON, the history page
    asks for a hundred rows, and almost all of it is only wanted by the one
    reader who has opened that row. The detail endpoint serves it on demand.
    """
    resource = labels.get((event.entity, event.entity_id), '')
    described = audit_taxonomy.describe(event.action, event.outcome, resource)
    return {
        'id': event.pk,
        'date': event.created_at.isoformat(),
        'actor': event.actor.username if event.actor else 'System',
        'actor_role': roles.get(event.actor_id, ''),
        'action': event.action,
        'label': described['label'],
        'category': described['category'],
        'severity': described['severity'],
        'outcome': event.outcome,
        'outcome_label': audit_taxonomy.OUTCOME_LABELS.get(event.outcome, event.outcome),
        'reason': event.reason,
        'reason_label': audit_taxonomy.reason_label(event.reason, event.outcome),
        'request_id': event.request_id,
        'entity': event.entity,
        'entity_id': event.entity_id,
        'resource': resource,
        'related_count': related_counts.get(event.request_id, 0) if event.request_id else 0,
    }


def _audit_related_counts(organization, events):
    """How many events share a request id, so grouping does not need a round trip.

    One extra query for the whole page instead of one per row. Only the ids on
    the page are counted, and the organization filter is applied to the count
    itself rather than to a list of ids taken on trust.
    """
    request_ids = {event.request_id for event in events if event.request_id}
    if not request_ids:
        return {}
    counted = AuditEvent.objects.filter(organization=organization, request_id__in=request_ids)\
        .values('request_id').annotate(total=Count('id'))
    return {row['request_id']: row['total'] for row in counted}


@api
@require_GET
def audit_log(request):
    access = membership(request.user)
    if not access or access.role not in ('secretary','auditor'):return JsonResponse({'error': NO_ACCESS},status=403)
    organization = access.organization
    # Scoped to the caller's organization before anything else, so a filter can
    # never widen the result set. Rows with no organization are pre-auth events
    # and are never shown to a tenant: they belong to no organization to own them.
    # select_related keeps the actor in the same query, because every row renders
    # an actor name and without it a page of a hundred costs a hundred more.
    records=AuditEvent.objects.filter(organization=organization).select_related('actor')
    # Filters are opt-in and additive. An unrecognised or empty value is ignored
    # rather than rejected, so the existing client keeps working unchanged and a
    # hand-typed query returns the whole history instead of an error.
    if action:=request.GET.get('action'):records=records.filter(action=action)
    if outcome:=request.GET.get('outcome'):records=records.filter(outcome=outcome)
    if request_id:=request.GET.get('request_id'):records=records.filter(request_id=request_id)
    if resource:=request.GET.get('resource'):
        records=records.filter(entity=resource)
        if resource_id:=request.GET.get('resource_id'):records=records.filter(entity_id=resource_id)
    if actor:=request.GET.get('actor'):
        # Matched on the username an auditor can actually see in the column, not
        # on a user id they would have to look up first.
        records=records.filter(actor__username=actor)
    if since:=request.GET.get('since'):
        if parsed:=_audit_date(since):records=records.filter(created_at__date__gte=parsed)
    if until:=request.GET.get('until'):
        if parsed:=_audit_date(until):records=records.filter(created_at__date__lte=parsed)
    if category:=request.GET.get('category'):
        # Only a known category filters. An unknown one is ignored rather than
        # widening the result, matching every other filter here.
        if category in audit_taxonomy.CATEGORY_IDS:
            records=records.filter(action__in=audit_taxonomy.category_actions(category))
    if term:=_audit_search_term(request.GET.get('q')):
        match=(Q(action__icontains=term)|Q(entity__icontains=term)|Q(entity_id__icontains=term)|
               Q(actor__username__icontains=term)|Q(reason__icontains=term))
        member_ids,payment_ids=_audit_named_records(organization,term)
        if member_ids:match|=Q(entity='Member',entity_id__in=member_ids)
        if payment_ids:match|=Q(entity='Payment',entity_id__in=payment_ids)
        records=records.filter(match)
    if request.GET.get('security'):
        records=records.filter(action__in=audit_taxonomy.CATEGORY_ACTIONS['security'])
    # Ordered by the timestamp the composite organization index is built on, then
    # by id so that events written in the same instant keep a stable order across
    # pages. Ordering by id alone left the index unused and made the database sort
    # the whole history on every request.
    records=records.order_by('-created_at','-id')
    page = _audit_page(request.GET.get('page')) or 1
    # One row beyond the page is enough to answer has_more. COUNT(*) over the
    # organization's whole history was O(total events) on every single request,
    # and grows without bound as the history does.
    #
    # A page beyond the cap short-circuits to an empty result rather than asking
    # the database to skip a hundred thousand rows in order to discard them. The
    # page number is echoed back unchanged so the pager does not jump.
    offset=(page-1)*AUDIT_PAGE_SIZE
    if offset // AUDIT_PAGE_SIZE >= AUDIT_MAX_PAGE:
        window=[]
    else:
        window=list(records[offset:offset+AUDIT_PAGE_SIZE+1])
    has_more=len(window)>AUDIT_PAGE_SIZE
    events=window[:AUDIT_PAGE_SIZE]
    labels=_audit_resource_labels(organization,{(e.entity,e.entity_id) for e in events})
    roles,actors=_audit_actors(organization)
    related=_audit_related_counts(organization,events)
    payload = {
        'events':[_audit_row(event,labels,roles,related) for event in events],
        'page':page,
        'has_more':has_more,
        'page_size':AUDIT_PAGE_SIZE,
        'actors':actors,
        'taxonomy':audit_taxonomy.public_taxonomy(),
    }
    if request.GET.get('total') and offset // AUDIT_PAGE_SIZE < AUDIT_MAX_PAGE:
        # Off by default on purpose. Answering it means counting every matching
        # row, which is the cost this endpoint was just restructured to remove.
        payload['total']=records.count()
    if request.GET.get('details'):
        # Opt-in escape hatch for any client that still wants the raw payload in
        # the list, so removing it from the default response breaks nobody.
        for row,event in zip(payload['events'],events):
            row['details']=json.loads(event.details or '{}')
    return JsonResponse(payload)


@api
@require_GET
def audit_event(request, pk):
    """One event in full, for the reader who has opened it.

    Everything the list deliberately withholds lives here: the raw record, and the
    request context that says which request, from where, using which browser. The
    organization filter is repeated rather than inherited, because this is a
    direct lookup by primary key and that is exactly the shape of request a
    cross-tenant leak takes.
    """
    access = membership(request.user)
    if not access or access.role not in ('secretary','auditor'):return JsonResponse({'error': NO_ACCESS},status=403)
    organization = access.organization
    event=AuditEvent.objects.filter(organization=organization, pk=pk).select_related('actor').first()
    if event is None:
        # 404 rather than 403: the row either does not exist or belongs to another
        # organization, and saying which would let an auditor probe for the
        # existence of another tenant's history.
        return JsonResponse({'error': 'That audit event does not exist.'}, status=404)
    labels=_audit_resource_labels(organization,{(event.entity,event.entity_id)})
    roles,_=_audit_actors(organization)
    related_counts=_audit_related_counts(organization,[event])
    row=_audit_row(event,labels,roles,related_counts)
    details=json.loads(event.details or '{}')
    row.update({
        'details':details,
        'changes':audit_taxonomy.changes(details),
        'http_method':event.http_method,
        'path':event.path,
        'ip_address':event.ip_address,
        # When a proxy is trusted, the recorded address is the peer's socket
        # address, which on Render is the proxy's, not the member's. The page
        # says so rather than presenting it as the member's IP.
        'ip_is_peer':proxy_trusted(),
        'user_agent':event.user_agent,
    })
    # The events that happened during the same request, which is the only
    # grouping the backend can actually justify. Same organization, same filter
    # as the list, and the event itself is excluded.
    related=[]
    if event.request_id:
        siblings=AuditEvent.objects.filter(organization=organization, request_id=event.request_id)\
            .exclude(pk=event.pk).order_by('created_at','id')[:20]
        sibling_labels=_audit_resource_labels(organization,{(e.entity,e.entity_id) for e in siblings})
        related=[_audit_row(item,sibling_labels,roles,related_counts) for item in siblings]
    row['related']=related
    return JsonResponse(row)


def _audit_date(value):
    """Parse a YYYY-MM-DD filter, or return None to mean "do not filter".

    Returning the widest possible range instead would be the wrong direction: a
    typo would then widen the result set instead of being ignored, and a filter
    that fails open is worse than one that does nothing.
    """
    try:return date.fromisoformat(value)
    except (TypeError,ValueError):return None

@api
@require_http_methods(['GET','POST'])
def accounts(request):
    if not secretary_only(request):return JsonResponse({'error': READ_ONLY},status=403)
    if request.method=='GET':
        # Role comes from the joined UserAccess row, not role_for(): that helper
        # resolves active membership only, so a disabled account reported no role
        # at all. Reading the row keeps the real role visible while active is False,
        # and avoids one membership() query per account.
        return JsonResponse({'users':[{'id':u.pk,'username':u.username,'email':u.email,'role':u.access.role,'active':u.is_active and u.access.active} for u in User.objects.filter(access__organization=organization_for(request.user)).select_related('access').order_by('username')]})
    data=body(request)
    with transaction.atomic():
        user=User(username=str(data.get('username','')).strip(),email=str(data.get('email','')).strip())
        if not user.email:raise ValueError('Email is required for account recovery.')
        if User.objects.filter(email__iexact=user.email).exists():raise ValueError('An account already uses this email.')
        role=data.get('role')
        if role == 'secretary':raise ValueError('Use Invite Secretary to add a secretary.')
        if role != 'auditor':raise ValueError('Choose a valid role.')
        password=data.get('password','');validate_password(password,user)
        user.set_password(password);user.full_clean();user.save()
        access=UserAccess(organization=organization_for(request.user),user=user,role=role);access.full_clean();access.save()
        audit(request.user,'account.created',user,{'role':role})
    return JsonResponse({'id':user.pk,'username':user.username},status=201)

@api
@require_http_methods(['POST'])
def disable_account(request,pk):
    if not secretary_only(request):return JsonResponse({'error': READ_ONLY},status=403)
    # Checked before any database work so a self-disable can never half-apply.
    if pk==request.user.pk:raise ValueError("You can't disable your own account. Ask another secretary to do it.")
    logger.info('account.disable requested by=%s target=%s',request.user.username,pk)
    with transaction.atomic():
        user=get_object_or_404(User.objects.filter(access__organization=organization_for(request.user)).select_for_update(),pk=pk)
        if user.is_superuser and not request.user.is_superuser:raise ValueError('Only a superuser can disable a superuser.')
        access=user.access
        # Writes: ledger_useraccess (active), auth_user (is_active),
        # ledger_secretaryinvite (revoked_at), ledger_auditevent (insert).
        if not access.active:
            logger.info('account.disable no-op: %s is already disabled',user.username)
            return JsonResponse({'ok':True,'already_disabled':True})
        # Never leave an organization with nobody who can run it.
        if access.role=='secretary' and not UserAccess.objects.filter(organization=access.organization,role='secretary',active=True).exclude(pk=access.pk).exists():
            raise ValueError('This is the last active secretary. Invite another secretary before disabling this one.')
        access.active=False;access.save(update_fields=['active'])
        # Clear the auth flag too. Leaving is_active True meant a disabled account
        # still passed authenticate() and could hold a live Django session.
        if user.is_active:
            user.is_active=False;user.save(update_fields=['is_active'])
        # ...and drop the sessions themselves. is_active only stops a *new*
        # sign-in; rows in django_session outlive it, so without this an already
        # signed-in device keeps working and re-enabling the account would restore
        # it. A user has one session per browser, so this is not a single row.
        revoked=revoke_sessions(user)
        logger.info('account.disable revoked %d session(s) for %s',revoked,user.username)
        SecretaryInvite.objects.filter(organization=access.organization,created_by=user,used_at__isnull=True,revoked_at__isnull=True).update(revoked_at=timezone.now())
        audit(request.user,'account.disabled',user,{'sessions_revoked':revoked})
    return JsonResponse({'ok':True,'already_disabled':False})

@api
@require_http_methods(['POST'])
def enable_account(request,pk):
    if not secretary_only(request):return JsonResponse({'error': READ_ONLY},status=403)
    # Restores exactly what disable_account took away: the UserAccess row and the
    # auth flag. The role is never changed here, so a secretary cannot use this to
    # grant access the account did not already hold.
    with transaction.atomic():
        user=get_object_or_404(User.objects.filter(access__organization=organization_for(request.user)).select_for_update(),pk=pk)
        if user.is_superuser and not request.user.is_superuser:raise ValueError('Only a superuser can enable a superuser.')
        access=user.access
        if access.active and user.is_active:
            # A double click must not spam the audit history with duplicate events.
            return JsonResponse({'ok':True,'already_enabled':True})
        access.active=True;access.save(update_fields=['active'])
        if not user.is_active:
            user.is_active=True;user.save(update_fields=['is_active'])
        audit(request.user,'account.enabled',user,{'role':access.role})
    return JsonResponse({'ok':True,'already_enabled':False})

@api
@require_GET
def import_template(request):
    if not secretary_only(request):return JsonResponse({'error': READ_ONLY},status=403)
    kind=request.GET.get('kind','members')
    text='name,phone,email,joined,status,billing_end\nExample Member,0240000000,,2026-09-01,Active,\n' if kind=='members' else 'member_id,amount,start_month,payment_date,method,reference,notes\n1,100,2026-09,2026-09-22,Cash,,Example import\n'
    response=HttpResponse(text,content_type='text/csv');response['Content-Disposition']=f'attachment; filename="{kind}-template.csv"';return response

@api
@require_http_methods(['POST'])
def import_preview(request):
    file=request.FILES.get('file');kind=request.POST.get('kind')
    if kind not in ('members','payments'):raise ValueError('Choose members or payments.')
    if not file or file.size>1048576:raise ValueError('Choose a CSV file smaller than 1 MB.')
    try:
        reader=csv.DictReader(io.StringIO(file.read().decode('utf-8-sig')))
        required={'name','joined'} if kind=='members' else {'member_id','amount','start_month','payment_date','method'}
        if not required.issubset(reader.fieldnames or []):raise ValueError('Missing required column headers. Use the template.')
        rows=list(reader)
    except (UnicodeDecodeError,csv.Error):raise ValueError('Use a valid UTF-8 CSV file.')
    if not 1<=len(rows)<=500:raise ValueError('Import between 1 and 500 rows at a time.')
    for i,row in enumerate(rows,2):
        if None in row or any(v is None for v in row.values()):raise ValueError(f'Row {i}: column count does not match the header.')
        try:
            if kind=='members':fill_member(Member(organization=organization_for(request.user)),row)
            else:
                try:member_id=int(row['member_id'])
                except (TypeError,ValueError):raise ValueError('member_id must be a number.')
                member=visible_members(request.user).filter(pk=member_id).first()
                if not member:raise ValueError('that member ID is not in this organization.')
                plan_payment(member,parse_amount(row['amount']),parse_month(row['start_month']))
                payment_date=date.fromisoformat(row['payment_date'])
                if not date(2000,1,1)<=payment_date<=timezone.localdate():raise ValueError('Invalid payment date.')
                if row['method'] not in dict(Payment._meta.get_field('method').choices):raise ValueError('Invalid method.')
                for field in ('reference', 'notes'):
                    limit = Payment._meta.get_field(field).max_length
                    if len(str(row.get(field, '')).strip()) > limit:
                        raise ValueError(f'{field.capitalize()} must be {limit} characters or fewer.')
        except (ValueError,ValidationError) as e:raise ValueError(f'Row {i}: {e}')
    token=signing.dumps({'rows':rows,'kind':kind,'user':request.user.pk,'organization':organization_for(request.user).pk},salt='csv-import',compress=True)
    return JsonResponse({'token':token,'count':len(rows),'preview':rows[:8],'kind':kind})

@api
@require_http_methods(['POST'])
def import_commit(request):
    # Recorded whatever happens, so a bulk write that fails halfway leaves a
    # trace. outcome/reason say which; the row count in the details is zero
    # because the batch is rolled back with everything it created. Every one of
    # these is deferred rather than written here: the preview expiry and the
    # duplicate file are refused before the batch exists but still inside the
    # write lock api() holds, and the failure below is raised out of it, so an
    # event written at any of these three points is rolled back before it can be
    # read. api() writes them once the transaction is over.
    try:
        payload=signing.loads(body(request).get('token',''),salt='csv-import',max_age=600)
    except signing.BadSignature as expired:
        raise defer_audit(ValueError('The preview expired. Upload the file again.'),
                          'import.rejected',outcome='rejected',reason='preview_expired') from expired
    if payload.get('organization') != organization_for(request.user).pk:raise ValueError('This preview belongs to another organization.')
    if payload['user']!=request.user.pk:raise ValueError('This preview belongs to another user.')
    digest=hashlib.sha256(json.dumps({'kind':payload['kind'],'rows':payload['rows']},sort_keys=True).encode()).hexdigest()
    if ImportBatch.objects.filter(organization=organization_for(request.user),digest=digest).exists():
        # Refused before the batch exists, so there is nothing to attach it to.
        raise defer_audit(ValueError('This exact file has already been imported.'),
                          'import.rejected',
                          details={'kind':payload['kind'],'rows':len(payload['rows'])},
                          outcome='replayed',reason='duplicate_file')
    try:
        with transaction.atomic():
            batch=ImportBatch.objects.create(organization=organization_for(request.user),digest=digest,kind=payload['kind'],row_count=len(payload['rows']),created_by=request.user)
            for row_index,row in enumerate(payload['rows'],1):
                if payload['kind']=='members':
                    member=fill_member(Member(organization=organization_for(request.user)),row);member.save();audit(request.user,'member.imported',member,{'batch':batch.pk})
                else:
                    row['request_key']=str(uuid5(NAMESPACE_URL,f'duesdesk:{organization_for(request.user).pk}:{digest}:{row_index}'))
                    record_payment(row,request.user)
            audit(request.user,'import.completed',batch,{'kind':batch.kind,'rows':batch.row_count})
    except Exception as error:
        # Deferred for the same reason as the payment replay. api() also keeps
        # the response message: the exception text is a validation message, or a
        # database error api() has already scrubbed before it is shown.
        defer_audit(error,'import.failed',details={'kind':payload['kind'],'rows':len(payload['rows']),'message':str(error)[:200]},
                    outcome='failure',reason=type(error).__name__)
        raise
    return JsonResponse({'count':batch.row_count})

class RecoveryView(PasswordResetView):
    template_name='registration/account_form.html'
    email_template_name='registration/password_reset_email.txt'
    subject_template_name='registration/password_reset_subject.txt'
    extra_context={'heading':'Reset your password','description':'Enter the email address attached to your account.','button':'Send reset link'}
    def form_valid(self,form):
        # Do not reveal whether an email is registered. Limit repeated emails to one per 5 minutes.
        email=form.cleaned_data['email'].strip().lower()
        key=hashlib.sha256(email.encode()).hexdigest()
        with transaction.atomic():
            item,_=RecoveryAttempt.objects.select_for_update().get_or_create(key=key,defaults={'requested_at':timezone.now()-timedelta(days=1)})
            if item.requested_at>timezone.now()-timedelta(minutes=5):return redirect('password_reset_done')
            item.requested_at=timezone.now();item.save()
        try:return super().form_valid(form)
        except Exception:
            logger.exception('Password recovery delivery failed')
            return redirect('password_reset_done')

@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:cursor.execute('SELECT 1');cursor.fetchone()
        return JsonResponse({'status':'ok'})
    except Exception:return JsonResponse({'status':'unavailable'},status=503)


@api
@require_GET
def member_report(request, pk):
    """Secretary-requested statement: one receipt row, separate monthly allocations."""
    if not secretary_only(request):
        return JsonResponse({'error': READ_ONLY},status=403)
    member = get_object_or_404(visible_members(request.user), pk=pk)
    today = timezone.localdate()
    # The report month drives outstanding dues and arrears; payment history and
    # the filename stay complete and unchanged.
    report_month = parse_report_month(request.GET.get('month', today.strftime('%Y-%m')), member.organization)
    payments = list(visible_payments(request.user).filter(member=member).prefetch_related('allocations__dues_month').order_by('payment_date', 'pk'))
    allocated = {}
    for payment in payments:
        if not payment.voided_at:
            for allocation in payment.allocations.all():
                month = allocation.dues_month.month
                allocated[month] = allocated.get(month, Decimal('0')) + allocation.amount
    rates = dict(DuesMonth.objects.filter(organization=member.organization).values_list('month', 'amount_due'))
    monthly_rows = []
    arrears = Decimal('0')
    cursor = member.joined.replace(day=1)
    last_due = min(report_month, member.billing_end) if member.billing_end else report_month
    last = max([last_due, *allocated.keys()])
    while cursor <= last:
        due = rates.get(cursor, RATE) if cursor <= last_due else Decimal('0')
        paid = allocated.get(cursor, Decimal('0'))
        outstanding = max(due - paid, Decimal('0'))
        arrears += outstanding
        monthly_rows.append([cursor.strftime('%B %Y'), due, paid, outstanding,
                             'Advance payment' if cursor > last_due else 'Paid' if not outstanding else 'Partial' if paid else 'Unpaid'])
        cursor = next_month(cursor)
    workbook = Workbook()
    summary = workbook.active
    summary.title = 'Summary'
    org = organization_for(request.user)
    for row in [
        ['Member payment report', 'Value'],
        ['Organisation', org.name if org else 'Membership Association'],
        ['Member ID', member.code], ['Member name', member.full_name],
        ['Generated on', today.isoformat()], ['Currency', 'GHS'],
        ['Total paid (excludes voids)', sum((p.amount_received for p in payments if not p.voided_at), Decimal('0'))],
        ['Outstanding through ' + report_month.strftime('%B %Y'), arrears],
        ['Report coverage', 'All recorded payments; outstanding dues through ' + report_month.strftime('%B %Y')],
        ['Note', 'Voided payments are retained for reference and excluded from totals.'],
    ]:
        summary.append([(safe_cell(value) if isinstance(value, str) else value) for value in row])
    ledger = workbook.create_sheet('Payments')
    ledger.append(['Receipt', 'Payment date', 'Amount (GHS)', 'Method', 'Reference', 'Status', 'Notes', 'Void reason'])
    allocations = workbook.create_sheet('Months covered')
    allocations.append(['Receipt', 'Covered month', 'Allocated amount (GHS)', 'Receipt status'])
    for payment in payments:
        status = 'VOID' if payment.voided_at else 'Valid'
        ledger.append([(safe_cell(value) if isinstance(value, str) else value) for value in [payment.receipt_number, payment.payment_date.isoformat(),
            payment.amount_received, payment.method, payment.reference, status, payment.notes, payment.void_reason]])
        for allocation in sorted(payment.allocations.all(), key=lambda a: a.dues_month.month):
            allocations.append([payment.receipt_number, allocation.dues_month.month.strftime('%B %Y'), allocation.amount, status])
    balances = workbook.create_sheet('Monthly balances')
    balances.append(['Month', 'Due to date (GHS)', 'Paid towards month (GHS)', 'Outstanding (GHS)', 'Status'])
    for row in monthly_rows:
        balances.append(row)
    for sheet in workbook:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='166534')
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(70, max(18, max(len(str(c.value or '')) for c in column) + 2))
            for cell in column[1:]:
                if isinstance(cell.value, Decimal):
                    cell.number_format = '#,##0.00'
    output = io.BytesIO()
    workbook.save(output)
    audit(request.user, 'member.export.generated', member, {'format': 'xlsx', 'payments': len(payments)})
    response = HttpResponse(output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{member.code}-payment-report-{today.isoformat()}.xlsx"'
    return response
