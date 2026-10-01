import hashlib
import logging
import secrets
from datetime import timedelta
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.core.validators import validate_email
from .models import Organisation, UserAccess, SecretaryInvite
from .access import organization_for, role_for
from google_auth import flows as google_flows, signup as google_signup

logger = logging.getLogger(__name__)
from .branding import DEFAULTS, branding_json, color, decode_logo, logo_token
from .forms import SignupForm
from .services import audit
from .views import api, body, READ_ONLY


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def valid_invite(raw):
    return SecretaryInvite.objects.select_related('organization').filter(token_hash=token_hash(raw), used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now()).first()


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
    if not org.logo:return HttpResponse(status=404)
    response = HttpResponse(bytes(org.logo),content_type='image/png')
    response['X-Content-Type-Options']='nosniff'
    return response


@require_http_methods(['GET','POST'])
def signup(request):
    if request.user.is_authenticated:return redirect('/')
    # "Wrong email, go back" on the verification page keeps the typed values,
    # except the password, which is never held anywhere.
    kept=request.session.pop(google_flows.RETURN,None)
    # The invitation is taken from the link, and otherwise from the session,
    # which is how it survives a trip via the verification page. It is never
    # trusted from a posted field.
    token=request.GET.get('invite','') or (kept or {}).get('invite') or request.session.get(google_flows.INVITE,'') or ''
    invite=valid_invite(token) if token else None
    if token and not invite:return render(request,'registration/invite_invalid.html',status=400)
    initial={}
    if kept:
        initial={k:v for k,v in kept.items() if k not in ('invite','logo_token','palette') and v}
    form=SignupForm(request.POST or None,initial=initial or None)
    org_name=request.POST.get('organization_name','').strip() or (kept or {}).get('organization_name','')
    logo_token=(request.POST.get('logo_token')
               or request.session.get(google_flows.LOGO,'') or (kept or {}).get('logo_token',''))
    if request.method=='POST' and form.is_valid():
        # Validate exactly as before, then create nothing. The user, the
        # organization and the membership are created only once the address has
        # been proven; see google_auth.signup.complete.
        try:
            if not token and not org_name:raise ValueError('Enter an organization name.')
            if token:
                # Nothing is created here, so this is only a friendly check to
                # stop somebody typing a wrong address. The invitation is locked
                # and rechecked for real in google_auth.signup.complete.
                locked=valid_invite(token)
                if locked is None:
                    raise ValueError('This invitation is no longer valid.')
                if form.cleaned_data['email'].casefold()!=locked.email.casefold():
                    raise ValueError('Use the email address named in your invitation.')
            palette=dict((kept or {}).get('palette') or {})
            for field,default in DEFAULTS.items():
                palette[field]=color(request.POST.get(field) or palette.get(field,default))
        except (ValueError,ValidationError) as error:
            form.add_error(None,str(error))
        else:
            pending,problem=google_signup.create(request,form,org_name,invite_token=token,
                palette=palette,logo_token=logo_token)
            if problem:form.add_error(None,problem)
            else:
                # Nothing exists but a pending signup and the email we are about
                # to send. Sign in happens here only after verification.
                google_flows.begin_signup(request,pending.token)
                # The pending signup keeps only the invitation's hash and the
                # decoded logo, so both are held here to survive "wrong email,
                # go back".
                request.session[google_flows.INVITE]=token
                request.session[google_flows.LOGO]=logo_token
                problem=google_signup.send_code(request,pending)
                if problem:request.session[google_flows.SIGNUP_ERROR]=problem
                else:request.session['verify_code_sent']=True
                return redirect('/signup/verify/')
    context={
        'form':form,'invite':invite,'invite_token':token,'organization_name':org_name,
        'palette':{k:request.POST.get(k,(kept or {}).get('palette',{}).get(k,v)) for k,v in DEFAULTS.items()},
        'logo_token':logo_token}
    # An invitation brings its organization with it, so the card is shown in that
    # organization's colors and logo.
    if invite:context.update(organisation=invite.organization,branding=branding_json(invite.organization))
    return render(request,'registration/signup.html',context)


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
