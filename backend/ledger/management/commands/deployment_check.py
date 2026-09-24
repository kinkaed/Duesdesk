from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from ledger.models import Organisation
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
        if not any(role_for(u)=='secretary' for u in User.objects.filter(is_active=True)):raise CommandError('Create a secretary account with createsuperuser before starting the live app.')
        org=Organisation.objects.first()
        if not org or org.name=='Membership Association':raise CommandError('Set your organisation name before deployment.')
        self.stdout.write(self.style.SUCCESS('Application checks passed. Verify DNS, TLS, SMTP delivery, backups and restore at the host before go-live.'))
