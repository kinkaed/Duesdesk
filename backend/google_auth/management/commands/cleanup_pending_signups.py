"""Delete expired pending signups.

A pending signup is a stored password hash waiting to be used, so rows that can
no longer be completed should not be left lying around. This is a plain DELETE
and is safe to run repeatedly; see the Render cron job in GOOGLE-AUTH.md.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from google_auth.models import PendingSignup


class Command(BaseCommand):
    help = 'Delete pending signups that have passed their 24-hour expiry.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted without deleting it.')

    def handle(self, *args, **options):
        stale = PendingSignup.objects.filter(expires_at__lte=timezone.now())
        count = stale.count()
        if options['dry_run']:
            self.stdout.write(f'{count} expired pending signup(s) would be deleted.')
            return
        deleted, _ = stale.delete()
        self.stdout.write(f'Deleted {deleted} row(s) from {count} expired pending signup(s).')