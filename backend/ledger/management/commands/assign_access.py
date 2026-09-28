from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ledger.models import UserAccess, Organisation


class Command(BaseCommand):
    help = 'Assign or update application access for a user.'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--organization-id', type=int, required=True)
        parser.add_argument('--role', choices=['secretary', 'auditor'], default='secretary')
        parser.add_argument('--member-id', type=int)

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options['username']).first()
        if not user:
            raise CommandError(f'User {options["username"]!r} was not found.')

        org=Organisation.objects.filter(pk=options['organization_id']).first()
        if not org:raise CommandError('Organization not found.')
        existing=UserAccess.objects.filter(user=user).first()
        if existing and existing.organization_id!=org.pk:raise CommandError('Cannot transfer an existing membership to another organization.')
        role = options['role']
        # Members are records, not accounts, so a member is never linked here.
        # --member-id is rejected outright rather than silently ignored.
        if options['member_id']:
            raise CommandError('Members are records, not accounts, and cannot be linked to a login.')

        access, created = UserAccess.objects.update_or_create(
            user=user,
            defaults={'role': role, 'member': None, 'organization':org, 'active':True},
        )
        # Disabling an account also clears the auth flag, so re-enabling has to put
        # it back or the operator repairs access the user still cannot sign in with.
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=['is_active'])
        action = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{action} {role} access for {user.username} (access #{access.pk}).'))
