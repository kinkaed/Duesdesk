from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ledger.models import Member, UserAccess


class Command(BaseCommand):
    help = 'Assign or update application access for a user.'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--role', choices=['secretary', 'auditor', 'member'], default='secretary')
        parser.add_argument('--member-id', type=int)

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options['username']).first()
        if not user:
            raise CommandError(f'User {options["username"]!r} was not found.')

        role = options['role']
        member = None
        if role == 'member':
            member_id = options['member_id']
            if not member_id:
                raise CommandError('A linked member is required for the member role.')
            member = Member.objects.filter(pk=member_id).first()
            if not member:
                raise CommandError(f'Member {member_id} was not found.')
        elif options['member_id']:
            raise CommandError('A linked member can only be set for the member role.')

        access, created = UserAccess.objects.update_or_create(
            user=user,
            defaults={'role': role, 'member': member},
        )
        action = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{action} {role} access for {user.username} (access #{access.pk}).'))
