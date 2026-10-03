import hashlib
import logging
import secrets
from datetime import timedelta
from django.contrib.auth import logout
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.core.validators import validate_email
from .models import Organisation, UserAccess, SecretaryInvite
from .access import organization_for, role_for
from google_auth import create as signup_create
from google_auth.create import rate_limited, record_attempt

logger = logging.getLogger(__name__)
from .branding import DEFAULTS, branding_json, decode_logo, logo_token
from .forms import SignupForm
from .observability import client_ip
from .services import audit
from .views import api, body, READ_ONLY, apply_organization_settings


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def valid_invite(raw):
    return SecretaryInvite.objects.select_related('organization').filter(token_hash=token_hash(raw), used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(), organization__disabled_at__isnull=True).first()


def preview_owner(request):
    if request.user.is_authenticated:
        org = organization_for(request.user)
        return f'org:{org.pk}' if org else 'no-access'
    if not request.session.session_key:request.session.create()
    return f'session:{request.session.session_key}'


@require_POST
def logo_preview(request):
    if request.user.is_authenticated and role_for(request.user) != 'secretary':
        return JsonResponse({'error': READ_ONLY}, status=403)
    try:
        raw, palette = decode_logo(request.FILES.get('logo'))
        return JsonResponse({'logo_token':logo_token(raw,preview_owner(request)), **palette})
    except ValueError as error:
        return JsonResponse({'error':str(error)},status=400)


@require_GET
def logo(request, public_id):
    org = get_object_or_404(Organisation,public_id=public_id)
    # A disabled organization is soft-hidden everywhere, and its logo is part of
    # it: serving the image would keep the workspace visible after it was closed.
    if org.disabled_at or not org.logo:return HttpResponse(status=404)
    response = HttpResponse(bytes(org.logo),content_type='image/png')
    response['X-Content-Type-Options']='nosniff'
    return response


# The theme preview on the sign-up page needs rows to draw. They are invented here
# rather than queried, which is the point: a preview must never read a real
# member, and a preview that did would leak one to somebody signing up. The badge
# classes match the statuses the application renders, so the preview shows what
# the real table will look like.
PREVIEW_MEMBERS=(
    {'name':'Ama Mensah','code':'MBR-0001','status':'paid','status_label':'Paid','arrears':'—'},
    {'name':'Kwame Boateng','code':'MBR-0002','status':'partial','status_label':'Partial','arrears':'GH₵ 50'},
    {'name':'Akosua Owusu','code':'MBR-0003','status':'unpaid','status_label':'Unpaid','arrears':'GH₵ 175'},
    {'name':'Kofi Asante','code':'MBR-0004','status':'not-due','status_label':'Not due','arrears':'GH₵ 25'},
)


@require_http_methods(['GET','POST'])
def signup(request):
    """Step one: create the account, the organization and the membership.

    The account is usable the moment this returns. Branding is a separate,
    skippable step, so this page asks only for what the account needs. The
    invitation, when there is one, travels in the query string and is posted back
    with the form; nothing waits in the session for a later page.
    """
    if request.user.is_authenticated:return redirect('/')
    token=request.GET.get('invite','')
    invite=valid_invite(token) if token else None
    if token and not invite:return render(request,'registration/invite_invalid.html',status=400)
    form=SignupForm(request.POST or None)
    org_name=request.POST.get('organization_name','').strip()
    if request.method=='POST' and form.is_valid():
        email=form.cleaned_data['email']
        ip=client_ip(request)
        if rate_limited(email,ip):
            # A throughput refusal, stated plainly and without hinting whether the
            # address exists. The attempt is not recorded, so a blocked visitor is
            # not kept blocked by their own retries.
            return render(request,'registration/signup.html',
                          {'form':form,'invite':invite,'invite_token':token,
                           'organization_name':org_name,'limited':True},status=429)
        record_attempt(email,ip)
        try:
            signup_create.create(request,username=form.cleaned_data['username'],email=email,
                                 password=form.cleaned_data['password1'],
                                 organization_name=org_name,invite_token=token)
        except signup_create.Invalid as error:
            form.add_error(None,str(error))
        else:
            # A new organization still needs its branding; a join does not.
            return redirect('/overview/' if token else '/signup/branding/')
    context={'form':form,'invite':invite,'invite_token':token,'organization_name':org_name}
    # An invitation brings its organization with it, so the card is shown in that
    # organization's colors and logo.
    if invite:context.update(organisation=invite.organization,branding=branding_json(invite.organization))
    return render(request,'registration/signup.html',context)


def finish_onboarding(org):
    """Mark the guided first run complete, once, without re-creating anything."""
    org.onboarded_at=timezone.now()
    org.onboarding_required=False
    org.save(update_fields=['onboarded_at','onboarding_required'])


@require_http_methods(['GET','POST'])
def signup_branding(request):
    """Step two: name and colours, which are entirely skippable.

    Only the organization that still requires onboarding reaches this page, and
    only a secretary can change branding. Saving goes through the same helper the
    settings endpoint uses, so the two write paths cannot disagree; the browser
    enhancer posts the same payload to /api/settings/ and then asks here to
    finish, and this page's own POST is the no-JavaScript fallback.
    """
    if not request.user.is_authenticated:return redirect('/login/')
    org=organization_for(request.user)
    if org is None or role_for(request.user)!='secretary' or not org.onboarding_required or org.onboarded_at:
        return redirect('/overview/')
    error=''
    if request.method=='POST':
        if request.POST.get('action') in ('skip','finish'):
            finish_onboarding(org)
            return redirect('/overview/')
        try:
            with transaction.atomic():
                apply_organization_settings(org,request.POST,f'org:{org.pk}')
                audit(request.user,'organization.updated',org,{'name':org.name})
        except (ValueError,ValidationError) as exc:
            error=str(exc)
        else:
            finish_onboarding(org)
            return redirect('/overview/')
    return render(request,'registration/branding.html',{
        'organisation':org,'branding':branding_json(org),
        'palette':{k:getattr(org,k) for k in DEFAULTS},
        'preview_members':PREVIEW_MEMBERS,'error':error})


@api
@require_http_methods(['GET','POST'])
def invites(request):
    if role_for(request.user)!='secretary':return JsonResponse({'error': READ_ONLY},status=403)
    org=organization_for(request.user)
    if request.method=='GET':
        rows=SecretaryInvite.objects.filter(organization=org).order_by('-id')[:100]
        return JsonResponse({'invites':[{'id':x.pk,'email':x.email,'expires_at':x.expires_at.isoformat(),
            'status':'Used' if x.used_at else 'Revoked' if x.revoked_at else 'Expired' if x.expires_at<=timezone.now() else 'Pending'} for x in rows]})
    email=str(body(request).get('email','')).strip().lower();validate_email(email)
    pending=SecretaryInvite.objects.filter(organization=org,email__iexact=email,used_at__isnull=True,revoked_at__isnull=True,expires_at__gt=timezone.now()).exists()
    if pending:raise ValueError('A pending invitation already exists for this email. Revoke it to send a new one.')
    raw=secrets.token_urlsafe(32)
    item=SecretaryInvite.objects.create(organization=org,email=email,token_hash=token_hash(raw),created_by=request.user,expires_at=timezone.now()+timedelta(days=7))
    audit(request.user,'invite.created',item,{'email':email})
    return JsonResponse({'id':item.pk,'url':request.build_absolute_uri('/signup/?invite='+raw),'expires_at':item.expires_at.isoformat()},status=201)


@api
@require_POST
def revoke_invite(request,pk):
    item=get_object_or_404(SecretaryInvite,pk=pk,organization=organization_for(request.user))
    if item.used_at:raise ValueError('This invitation has already been used.')
    # Idempotent, like disable_account and enable_account next door. Revoking
    # twice used to write a second invite.revoked row and stamp a second
    # revoked_at on the same invite, so a double click on Revoke manufactured
    # duplicate history: an auditor reading the trail would see the invitation
    # cancelled by two separate decisions when only one was ever made.
    if item.revoked_at:return JsonResponse({'ok':True,'already_revoked':True})
    item.revoked_at=timezone.now();item.save(update_fields=['revoked_at'])
    audit(request.user,'invite.revoked',item)
    return JsonResponse({'ok':True})


@api
@require_POST
def leave(request):
    org=organization_for(request.user)
    # api() holds the organization lock, including for concurrent disable requests.
    other=UserAccess.objects.filter(organization=org,active=True,role='secretary',user__is_active=True).exclude(user=request.user)
    if not other.exists():raise ValueError('Invite another secretary and wait for them to join before leaving.')
    access=UserAccess.objects.get(user=request.user,organization=org,active=True)
    audit(request.user,'organization.left',org)
    access.active=False;access.save(update_fields=['active'])
    SecretaryInvite.objects.filter(organization=org,created_by=request.user,used_at__isnull=True,revoked_at__isnull=True).update(revoked_at=timezone.now())
    logout(request)
    return JsonResponse({'ok':True})
