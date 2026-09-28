from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from ledger.models import Organisation, UserAccess
from ledger.access import role_for

class Command(BaseCommand):
    help='Fail deployment when known unsafe defaults or required live configuration remain.'
    def handle(self,*args,**kwargs):
        if not settings.PRODUCTION:raise CommandError('Set APP_ENV=production to check the live environment.')
        call_command('check',deploy=True,fail_level='WARNING')
        call_command('check_finances')
        if not settings.EMAIL_HOST or 'localhost' in settings.DEFAULT_FROM_EMAIL:raise CommandError('Configure a real SMTP server and DEFAULT_FROM_EMAIL for recovery emails.')
        for user in User.objects.filter(is_active=True):
            if user.check_password('TryDues25!'):raise CommandError('Disable or change the local demo account before deployment.')
        for org in Organisation.objects.filter(Q(useraccess__isnull=False)|Q(member__isnull=False)|Q(payment__isnull=False)).distinct():
            if not UserAccess.objects.filter(organization=org,active=True,role='secretary',user__is_active=True).exists():
                raise CommandError(f'Organization {org.pk} needs an active secretary. Use assign_access with --organization-id.')
        self.stdout.write(self.style.SUCCESS('Application checks passed. Verify DNS, TLS, SMTP delivery, backups and restore at the host before go-live.'))
