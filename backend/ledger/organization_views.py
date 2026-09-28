import hashlib
import secrets
from datetime import timedelta
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.core.validators import validate_email
from .models import Organisation, UserAccess, SecretaryInvite
from .access import organization_for, role_for
from .branding import DEFAULTS, branding_json, decode_logo, logo_token, read_logo_token, color
from .forms import SignupForm
from .services import audit
from .views import api, body


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
        return HttpResponse(status=403)
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
    token=request.GET.get('invite','')
    invite=valid_invite(token) if token else None
    if token and not invite:return render(request,'registration/invite_invalid.html',status=400)
    form=SignupForm(request.POST or None)
    org_name=request.POST.get('organization_name','').strip()
    owner=preview_owner(request)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                if token:
                    # Serialize invitations and membership changes on the organization row.
                    org=Organisation.objects.select_for_update().get(pk=invite.organization_id)
                    locked=SecretaryInvite.objects.select_for_update().get(pk=invite.pk)
                    if locked.used_at or locked.revoked_at or locked.expires_at<=timezone.now():
                        raise ValueError('This invitation is no longer valid.')
                    if form.cleaned_data['email'].casefold()!=locked.email.casefold():
                        raise ValueError('Use the email address named in your invitation.')
                else:
                    if not org_name:raise ValueError('Enter an organization name.')
                    org=Organisation(name=org_name)
                    for field,default in DEFAULTS.items():setattr(org,field,color(request.POST.get(field,default)))
                    if request.POST.get('logo_token'):org.logo=read_logo_token(request.POST['logo_token'],owner)
                    org.full_clean();org.save()
                user=form.save()
                UserAccess.objects.create(user=user,organization=org,role='secretary')
                if token:
                    locked.used_at=timezone.now();locked.used_by=user;locked.save(update_fields=['used_at','used_by'])
                audit(user,'organization.joined' if token else 'organization.created',org)
            login(request,user,backend='django.contrib.auth.backends.ModelBackend')
            return redirect('/')
        except (ValueError,ValidationError) as error:form.add_error(None, str(error))
        except IntegrityError:form.add_error(None,'This account or invitation has already been used. Please sign in or request a new invitation.')
    context={'form':form,'invite':invite,'organization_name':org_name,
             'palette':{k:request.POST.get(k,v) for k,v in DEFAULTS.items()},'logo_token':request.POST.get('logo_token','')}
    if invite:context.update(organisation=invite.organization,branding=branding_json(invite.organization))
    return render(request,'registration/signup.html',context)


@api
@require_http_methods(['GET','POST'])
def invites(request):
    if role_for(request.user)!='secretary':return HttpResponse(status=403)
    org=organization_for(request.user)
    if request.method=='GET':
        rows=SecretaryInvite.objects.filter(organization=org).order_by('-id')[:100]
        return JsonResponse({'invites':[{'id':x.pk,'email':x.email,'expires_at':x.expires_at.isoformat(),
            'status':'Used' if x.used_at else 'Revoked' if x.revoked_at else 'Expired' if x.expires_at<=timezone.now() else 'Pending'} for x in rows]})
    email=str(body(request).get('email','')).strip().lower();validate_email(email)
    raw=secrets.token_urlsafe(32)
    item=SecretaryInvite.objects.create(organization=org,email=email,token_hash=token_hash(raw),created_by=request.user,expires_at=timezone.now()+timedelta(days=7))
    audit(request.user,'invite.created',item,{'email':email})
    return JsonResponse({'id':item.pk,'url':request.build_absolute_uri('/signup/?invite='+raw),'expires_at':item.expires_at.isoformat()},status=201)


@api
@require_POST
def revoke_invite(request,pk):
    item=get_object_or_404(SecretaryInvite,pk=pk,organization=organization_for(request.user))
    if item.used_at:raise ValueError('This invitation has already been used.')
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
