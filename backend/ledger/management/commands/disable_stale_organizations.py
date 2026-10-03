"""Disable organizations that signup created but nobody ever set up.

Immediate account creation means every signup leaves a real organization behind:
there is no pending row that can expire, so an abandoned signup would otherwise
stay live forever, holding a name, a subdomain-like public id and a login that
resolves to a workspace with nothing in it.

This command is the manual counterpart to that, and it is deliberately
conservative. It only disables an organization that is still awaiting onboarding,
has never been onboarded, was created before the cutoff, has exactly one
membership (an active secretary), and has no member, payment or accepted
invitation. A single member or payment means somebody is using it, and "zero
members" alone is not evidence of abandonment: an organization can be real before
its first member is entered.

The stop is soft. ``disabled_at`` makes the organization resolve to no membership
everywhere, while its audit history and records remain, and an operator can
reverse it. Nothing is deleted. Run with ``--dry-run`` first to see what would
change.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ledger.models import Member, Organisation, Payment, SecretaryInvite, UserAccess
from ledger.services import audit


class Command(BaseCommand):
    help = ('Disable organizations that were never onboarded and hold no members, '
            'payments or accepted invitations.')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Only consider organizations older than this many days (default 30).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be disabled without writing anything.')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options['days'])
        candidates = Organisation.objects.filter(
            onboarding_required=True, onboarded_at__isnull=True,
            disabled_at__isnull=True, created_at__lt=cutoff).order_by('pk')
        disabled = 0
        for org in candidates:
            if not self.is_stale(org):
                continue
            if options['dry_run']:
                self.stdout.write(f'Would disable {org.pk} {org.name!r} (created {org.created_at.isoformat()}).')
                continue
            self.disable(org)
            disabled += 1
            self.stdout.write(self.style.WARNING(f'Disabled {org.pk} {org.name!r}.'))
        if options['dry_run']:
            self.stdout.write(self.style.SUCCESS('Dry run: nothing was changed.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Disabled {disabled} organization(s).'))

    def is_stale(self, org):
        """Whether this organization shows no sign of ever being used."""
        accesses = list(UserAccess.objects.filter(organization=org))
        if len(accesses) != 1:
            return False
        only = accesses[0]
        if only.role != 'secretary' or not only.active:
            return False
        if Member.objects.filter(organization=org).exists():
            return False
        if Payment.objects.filter(organization=org).exists():
            return False
        # An accepted invitation is somebody joining a workspace that exists.
        if SecretaryInvite.objects.filter(organization=org, used_at__isnull=False).exists():
            return False
        return True

    def disable(self, org):
        """Soft-stop the organization and record the reason on the timeline."""
        with transaction.atomic():
            org = Organisation.objects.select_for_update().get(pk=org.pk)
            if org.disabled_at or not self.is_stale(org):
                return
            org.disabled_at = timezone.now()
            org.save(update_fields=['disabled_at'])
            # No actor: a command has no signed-in user. The organization is passed
            # explicitly because Organisation has no .organization for the helper
            # to fall back on, and record_http=False because there is no request.
            audit(None, 'organization.disabled', org, {'reason': 'stale_onboarding'},
                  reason='stale_onboarding', organization=org, record_http=False)
