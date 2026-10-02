import io
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4
from urllib.parse import urlparse, parse_qs
from django.contrib.auth.models import User
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from PIL import Image
from openpyxl import load_workbook
from .models import Organisation, UserAccess, Member, Payment, Allocation, DuesMonth, SecretaryInvite, ImportBatch
from .services import record_payment
from .branding import decode_logo, text_color, luminance, branding_json, DEFAULTS
from google_auth.models import PendingSignup
from google_auth.testsupport import SignupTestMixin

STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}}

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],STORAGES=STORAGES)
class OrganizationTests(SignupTestMixin, TestCase):
    def setUp(self):
        self.a=Organisation.objects.create(name='Organization A',primary='#123456')
        self.b=Organisation.objects.create(name='Organization B',primary='#eecc22')
        self.ua=User.objects.create_user('secretary-a',email='a@example.com')
        self.ub=User.objects.create_user('secretary-b',email='b@example.com',is_staff=True,is_superuser=True)
        for user,org in [(self.ua,self.a),(self.ub,self.b)]:UserAccess.objects.create(user=user,organization=org,role='secretary')
        self.ma=Member.objects.create(organization=self.a,full_name='Alice A',joined=date(2026,1,1))
        self.mb=Member.objects.create(organization=self.b,full_name='Bob B',joined=date(2026,1,1))
        self.payload={'member_id':self.ma.pk,'amount':'100','start_month':'2026-09','payment_date':'2026-09-01','method':'Cash','request_key':str(uuid4())}
        self.pa=record_payment(self.payload,self.ua)
        self.pb=record_payment({**self.payload,'member_id':self.mb.pk,'request_key':str(uuid4())},self.ub)
        self.client.force_login(self.ua)

    def post(self,url,data=None):return self.client.post(url,data or {},content_type='application/json')

    def signup_data(self,email='new@example.com'):
        return {'username':'new-secretary','email':email,'password1':'A-Long-New-Secret-Phrase!','password2':'A-Long-New-Secret-Phrase!','organization_name':'New independent organization'}

    def code_signup(self,client,data,token=''):
        """Sign up the way a real secretary must now: verify, then submit."""
        return self.signup_with_code(data,invite=token,client=client)

    def invitation(self):
        response=self.post('/api/invites/',{'email':'new@example.com','organization_id':self.b.pk})
        self.assertEqual(response.status_code,201,response.content)
        return response.json()

    def test_read_endpoints_and_exports_are_isolated(self):
        for user,own,other,payment,other_payment in [(self.ua,self.ma,self.mb,self.pa,self.pb),(self.ub,self.mb,self.ma,self.pb,self.pa)]:
            self.client.force_login(user)
            self.assertEqual([x['id'] for x in self.client.get('/api/members/').json()['members']],[own.pk])
            self.assertEqual([x['id'] for x in self.client.get('/api/overview/?month=2026-09').json()['members']],[own.pk])
            self.assertEqual([x['id'] for x in self.client.get('/api/payments/').json()['payments']],[payment.pk])
            self.assertEqual(self.client.get(f'/api/members/{other.pk}/').status_code,404)
            self.assertEqual(self.client.get(f'/api/members/{other.pk}/report/').status_code,404)
            self.assertEqual(self.client.get(f'/receipts/{other_payment.pk}/').status_code,404)
            self.assertEqual(self.client.get(f'/receipts/{payment.pk}/').status_code,200)
            for kind in ['balances','payments']:
                csv=self.client.get('/export/?month=2026-09&kind='+kind).content.decode()
                self.assertIn(own.full_name,csv);self.assertNotIn(other.full_name,csv)
                excel=self.client.get('/export/excel/?month=2026-09&kind='+kind)
                rows=str(list(load_workbook(io.BytesIO(excel.content)).active.values))
                self.assertIn(own.full_name,rows);self.assertNotIn(other.full_name,rows)
            report=self.client.get(f'/api/members/{own.pk}/report/')
            book=load_workbook(io.BytesIO(report.content))
            self.assertNotIn(other.full_name,str(list(book['Summary'].values)))
            accounts=self.client.get('/api/accounts/').json()['users']
            self.assertEqual([x['id'] for x in accounts],[user.pk])
            self.assertTrue(all(x['actor']==user.username for x in self.client.get('/api/audit/').json()['events']))

    def test_cross_org_mutations_are_rejected_and_spoofed_org_is_ignored(self):
        self.assertEqual(self.post(f'/api/members/{self.mb.pk}/',{'name':'hacked'}).status_code,404)
        data={**self.payload,'member_id':self.mb.pk,'request_key':str(uuid4()),'organization_id':self.b.pk}
        # 404, not 400, for a record that belongs to another organization. It used
        # to be a 400 carrying "Member matching query does not exist." from the
        # ORM, which both leaked the wording and answered a different status to the
        # member and account endpoints above for exactly the same attempt. The
        # answer now has to be identical whether the id is absent or someone
        # else's, or the status code alone confirms it exists.
        self.assertEqual(self.post('/api/payments/',data).status_code,404)
        self.assertEqual(self.post('/api/payments/preview/',data).status_code,404)
        self.assertEqual(self.post(f'/api/payments/{self.pb.pk}/void/',{'reason':'Wrong organization'}).status_code,404)
        self.assertEqual(self.post(f'/api/accounts/{self.ub.pk}/disable/').status_code,404)
        response=self.post('/api/members/',{'name':'Own new member','joined':'2026-09-01','organization_id':self.b.pk})
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(Member.objects.get(pk=response.json()['id']).organization,self.a)
        self.mb.refresh_from_db();self.pb.refresh_from_db()
        self.assertEqual(self.mb.full_name,'Bob B');self.assertIsNone(self.pb.voided_at)

    def test_invitation_is_email_bound_single_use_and_tenant_bound(self):
        result=self.invitation();invite=SecretaryInvite.objects.get(pk=result['id'])
        self.assertEqual(invite.organization,self.a)
        token=parse_qs(urlparse(result['url']).query)['invite'][0]
        self.assertNotEqual(invite.token_hash,token)
        visitor=Client()
        # The invitation is bound to an address, so a different one is refused at
        # submission and never reaches verification.
        wrong=visitor.post('/signup/?invite='+token,self.signup_data('wrong@example.com'))
        self.assertContains(wrong,'Use the email address')
        self.assertFalse(PendingSignup.objects.exists())
        self.assertFalse(User.objects.filter(username='new-secretary').exists())
        self.assertEqual(self.code_signup(visitor,self.signup_data(),token).status_code,302)
        user=User.objects.get(username='new-secretary')
        self.assertEqual(user.access.organization,self.a);self.assertEqual(user.access.role,'secretary')
        invite.refresh_from_db();self.assertEqual(invite.used_by,user)
        replay=Client().post('/signup/?invite='+token,{**self.signup_data(),'username':'replay'})
        self.assertEqual(replay.status_code,400);self.assertFalse(User.objects.filter(username='replay').exists())

    def test_cross_org_invite_visibility_and_revocation(self):
        result=self.invitation();self.client.force_login(self.ub)
        self.assertEqual(self.client.get('/api/invites/').json()['invites'],[])
        self.assertEqual(self.post(f"/api/invites/{result['id']}/revoke/").status_code,404)
        self.client.force_login(self.ua)
        self.assertEqual(self.post(f"/api/invites/{result['id']}/revoke/").status_code,200)
        self.assertEqual(Client().get(result['url']).status_code,400)

    def test_expired_invitation_is_rejected(self):
        result=self.invitation();SecretaryInvite.objects.filter(pk=result['id']).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(Client().get(result['url']).status_code,400)

    def test_signup_without_invite_creates_separate_org(self):
        visitor=Client();response=self.code_signup(visitor,self.signup_data())
        self.assertEqual(response.status_code,302,response.content)
        user=User.objects.get(username='new-secretary')
        self.assertNotIn(user.access.organization_id,[self.a.pk,self.b.pk])
        self.assertEqual(visitor.get('/api/members/').json()['members'],[])
        self.assertEqual(user.access.organization.name,'New independent organization')

    def test_leave_requires_active_successor_and_revokes_sessions(self):
        self.assertEqual(self.post('/api/organization/leave/').status_code,400)
        second=User.objects.create_user('second',is_active=False)
        access=UserAccess.objects.create(user=second,organization=self.a,role='secretary')
        self.assertEqual(self.post('/api/organization/leave/').status_code,400)
        second.is_active=True;second.save()
        access.active=False;access.save()
        self.assertEqual(self.post('/api/organization/leave/').status_code,400)
        access.active=True;access.save()
        invite=self.invitation();other_session=Client();other_session.force_login(self.ua)
        self.assertEqual(self.post('/api/organization/leave/').status_code,200)
        self.assertEqual(other_session.get('/api/members/').status_code,403)
        self.assertEqual(Client().get(invite['url']).status_code,400)
        self.assertTrue(Member.objects.filter(pk=self.ma.pk).exists());self.assertTrue(Payment.objects.filter(pk=self.pa.pk).exists())
        self.client.force_login(second)
        self.assertEqual(self.post('/api/organization/leave/').status_code,400)

    def test_disabling_secretary_revokes_invites(self):
        result=self.invitation()
        colleague=User.objects.create_user('colleague')
        UserAccess.objects.create(user=colleague,organization=self.a,role='secretary')
        self.client.force_login(colleague)
        self.assertEqual(self.post(f'/api/accounts/{self.ua.pk}/disable/').status_code,200)
        self.assertEqual(Client().get(result['url']).status_code,400)

    def test_staff_without_membership_never_bypasses_isolation(self):
        user=User.objects.create_superuser('operator','op@example.com','Some-Private-Password!')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/api/members/').status_code,403)
        self.assertEqual(self.client.get(f'/receipts/{self.pa.pk}/').status_code,404)

    def test_scoped_relations_reject_wrong_tenant(self):
        with self.assertRaises(ValidationError):
            Allocation.objects.create(organization=self.a,payment=self.pa,dues_month=DuesMonth.objects.filter(organization=self.b).first(),amount=1)
        with self.assertRaises(ValidationError):
            UserAccess.objects.create(organization=self.a,user=User.objects.create_user('bad-link'),role='auditor',member=self.mb)
        self.ma.organization=self.b
        with self.assertRaises(ValidationError):self.ma.save()

    def test_import_is_bound_to_user_and_organization_and_duplicates_are_local(self):
        raw=b'name,joined\nImported same file,2026-09-01\n'
        token=self.client.post('/api/import/preview/',{'kind':'members','file':SimpleUploadedFile('m.csv',raw)}).json()['token']
        self.client.force_login(self.ub)
        self.assertEqual(self.post('/api/import/commit/',{'token':token}).status_code,400)
        token_b=self.client.post('/api/import/preview/',{'kind':'members','file':SimpleUploadedFile('m.csv',raw)}).json()['token']
        self.assertEqual(self.post('/api/import/commit/',{'token':token_b}).status_code,200)
        self.client.force_login(self.ua)
        self.assertEqual(self.post('/api/import/commit/',{'token':token}).status_code,200)
        self.assertEqual(ImportBatch.objects.count(),2)

    def test_monthly_rates_are_organization_specific(self):
        DuesMonth.objects.filter(organization=self.b,month=date(2026,9,1)).update(amount_due=50)
        self.assertEqual(Decimal(self.client.get('/api/overview/?month=2026-09').json()['members'][0]['balance']),0)
        self.client.force_login(self.ub)
        self.assertEqual(Decimal(self.client.get('/api/overview/?month=2026-09').json()['members'][0]['balance']),25)

    def image(self,color='#cc2244'):
        out=io.BytesIO();Image.new('RGB',(40,40),color).save(out,format='PNG')
        return SimpleUploadedFile('logo.png',out.getvalue(),content_type='image/png')

    def test_branding_upload_persistence_and_two_org_theme_switch(self):
        uploaded=self.client.post('/api/branding/preview/',{'logo':self.image()})
        self.assertEqual(uploaded.status_code,200,uploaded.content)
        result=uploaded.json();self.assertEqual(result['primary'],'#cc2244')
        saved=self.post('/api/settings/',{'name':self.a.name,'logo_token':result['logo_token'],'primary':'#112233','secondary':'#fefefe','accent':'#aaccee'})
        self.assertEqual(saved.status_code,200,saved.content)
        a=self.client.get('/api/session/').json()['branding']
        self.assertEqual(a['primary'],'#112233');self.assertTrue(a['logo_url'])
        self.assertEqual(Client().get(a['logo_url'])['Content-Type'],'image/png')
        self.assertContains(Client().get('/login/?org='+a['public_id']),'#112233')
        self.client.force_login(self.ub)
        b=self.client.get('/api/session/').json()['branding']
        self.assertEqual(b['primary'],'#eecc22');self.assertNotEqual(a['primary_text'],b['primary_text'])
        self.assertEqual(self.post('/api/settings/',{'name':self.b.name,'logo_token':result['logo_token']}).status_code,400)
        self.assertContains(Client().get('/login/?org='+b['public_id']),'#eecc22')
        self.assertNotContains(Client().get('/login/?org='+b['public_id']),'Organization A')

    def test_invalid_logo_color_and_expired_preview(self):
        for file in [SimpleUploadedFile('bad.svg',b'<svg/>'),SimpleUploadedFile('large.png',b'x'*(1024*1024+1))]:
            self.assertEqual(self.client.post('/api/branding/preview/',{'logo':file}).status_code,400)
        self.assertEqual(self.post('/api/settings/',{'name':'A','primary':'red;display:none'}).status_code,400)
        bad=signing.dumps({'png':'AAAA','owner':'org:'+str(self.a.pk)},salt='wrong-salt')
        self.assertEqual(self.post('/api/settings/',{'name':'A','logo_token':bad}).status_code,400)
        self.a.refresh_from_db();self.assertEqual(self.a.primary,'#123456')

    def test_fallback_and_contrast_for_extreme_colors(self):
        org=Organisation(name='Default')
        self.assertEqual(branding_json(org)['primary'],DEFAULTS['primary'])
        org.primary='';self.assertEqual(branding_json(org)['primary'],DEFAULTS['primary'])
        for color in ['#ffffff','#000000','#777777','#ffff00','#0000ff','#ff0000']:
            foreground=text_color(color);light,dark=sorted([luminance(color),luminance(foreground)],reverse=True)
            self.assertGreaterEqual((light+.05)/(dark+.05),4.5)
        for color in ['#ffffff','#000000']:
            raw,palette=decode_logo(self.image(color));self.assertTrue(raw.startswith(b'\x89PNG'))
            self.assertEqual(palette['primary'],color)

    def test_anonymous_logo_preview_binds_to_signup_session(self):
        visitor=Client();data=visitor.post('/api/branding/preview/',{'logo':self.image()}).json()
        response=self.code_signup(visitor,{**self.signup_data(),**data})
        self.assertEqual(response.status_code,302,response.content)
        user=User.objects.get(username='new-secretary');self.assertTrue(user.access.organization.logo)
        self.assertEqual(user.access.organization.primary,'#cc2244')

    def test_expired_logo_token_is_rejected(self):
        import time
        from unittest.mock import patch
        from .branding import logo_token
        with patch('django.core.signing.time.time',return_value=time.time()-1801):
            token=logo_token(b'old-preview',f'org:{self.a.pk}')
        response=self.post('/api/settings/',{'name':self.a.name,'logo_token':token})
        self.assertEqual(response.status_code,400)
        self.assertIn('expired',response.json()['error'])

    def test_image_pixel_limit_and_transparency(self):
        output=io.BytesIO();Image.new('RGB',(2001,2000),'white').save(output,format='PNG')
        self.assertEqual(self.client.post('/api/branding/preview/',{'logo':SimpleUploadedFile('large.png',output.getvalue())}).status_code,400)
        output=io.BytesIO();Image.new('RGBA',(30,30),(200,0,0,0)).save(output,format='PNG')
        raw,palette=decode_logo(SimpleUploadedFile('clear.png',output.getvalue()))
        self.assertEqual(palette['primary'],'#ffffff')
        self.assertEqual(Image.open(io.BytesIO(raw)).size,(30,30))

    def test_new_write_routes_enforce_csrf_and_readonly_roles(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.ua)
        for path in ['/api/invites/','/api/organization/leave/','/api/branding/preview/','/api/accounts/',f'/api/accounts/{self.ua.pk}/disable/',f'/api/accounts/{self.ua.pk}/enable/']:
            self.assertEqual(client.post(path,{}).status_code,403)
        reader=User.objects.create_user('readonly-a')
        UserAccess.objects.create(user=reader,organization=self.a,role='auditor')
        self.client.force_login(reader)
        self.assertEqual(self.client.get('/api/invites/').status_code,403)
        for path in ['/api/invites/','/api/organization/leave/','/api/branding/preview/','/api/accounts/',f'/api/accounts/{self.ua.pk}/disable/',f'/api/accounts/{self.ua.pk}/enable/']:
            self.assertEqual(self.post(path).status_code,403)
        # A read-only role must not be able to mint an account either.
        self.assertEqual(self.post('/api/accounts/',{'username':'sneaky','email':'sneaky@example.com','password':'An-Excellent-Private-Phrase!','role':'auditor'}).status_code,403)
        self.assertFalse(User.objects.filter(username='sneaky').exists())
