from datetime import date
from uuid import uuid4
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.core.management.base import CommandError
from ledger.models import Member, UserAccess
from ledger.services import record_payment

class Command(BaseCommand):
    help = 'Create an explicitly labelled local test login and fictional example members.'

    @transaction.atomic
    def handle(self, *args, **kwargs):
        if settings.PRODUCTION:
            raise CommandError('Demo data is disabled in production.')
        user, created = User.objects.get_or_create(username='secretary', defaults={'is_staff':True, 'first_name':'Financial Secretary'})
        if created:
            user.set_password('TryDues25!')
            user.save()
        UserAccess.objects.get_or_create(user=user, defaults={'role': 'secretary'})
        if Member.objects.exists():
            self.stdout.write('Existing members retained; no sample data added.')
            return
        today = timezone.localdate()
        month = today.replace(day=1)
        samples = [('Ama Mensah', '024 000 0001', '100'), ('Kwame Boateng', '024 000 0002', '25'), ('Akosua Owusu', '024 000 0003', '10'), ('Kofi Asante', '024 000 0004', None), ('Abena Appiah', '024 000 0005', '25'), ('Yaw Osei', '024 000 0006', None)]
        for name, phone, amount in samples:
            member = Member.objects.create(full_name=name, phone=phone, email='', joined=month)
            if amount:
                record_payment({'member_id':member.pk, 'amount':amount, 'start_month':month.strftime('%Y-%m'), 'payment_date':today.isoformat(), 'method':'Mobile Money' if amount=='100' else 'Cash', 'notes':'Fictional sample transaction for testing.', 'request_key':str(uuid4())},user)
        self.stdout.write('Created six fictional members and four sample payments. Local login: secretary / TryDues25!')
