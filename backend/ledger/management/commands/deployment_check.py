import time

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Exists, OuterRef, Q
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from ledger.models import Allocation, Member, Organisation, Payment, UserAccess

# The only account seed_demo creates is this username, and it is the only
# account the application ever marks as staff. seed_demo refuses to run under
# APP_ENV=production, so the demo password can only reach a live database
# through an import or a hand-rolled shell command, and it lands on an account
# that is one of these two. The default gate therefore checks those, which is
# one PBKDF2 hash, and --all-users keeps the exhaustive sweep available.
DEMO_USERNAME = 'secretary'
DEMO_PASSWORD = 'TryDues25!'
SECRETARY = Q(active=True, role='secretary', user__is_active=True)


class Command(BaseCommand):
    help = 'Fail deployment when known unsafe defaults or required live configuration remain.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all-users', action='store_true',
            help='Check every active account for the demo password instead of only the accounts '
                 'seed_demo can create. Costs one PBKDF2 hash per active user, which is over a '
                 'second each and dominates the whole command.')

    def handle(self, *args, **options):
        if not settings.PRODUCTION:
            raise CommandError('Set APP_ENV=production to check the live environment.')
        started = time.perf_counter()
        self._report_size()
        timings = []
        # The timings are written from a finally block because a failed deploy is
        # exactly when someone reads this log: without them a phase that blew the
        # budget, or one that never ran, is invisible.
        try:
            self._phase(timings, 'django check --deploy', call_command, 'check', deploy=True, fail_level='WARNING')
            self._phase(timings, 'check_finances', call_command, 'check_finances')
            self._phase(timings, 'proxy configuration', self._check_proxy)
            self._phase(timings, 'smtp configuration', self._check_smtp)
            self._phase(timings, 'demo password', self._check_demo_password, options['all_users'])
            self._phase(timings, 'active secretaries', self._check_active_secretaries)
        finally:
            self._write_timings(timings, started)
        self.stdout.write(self.style.SUCCESS('Application checks passed. Verify DNS, TLS, SMTP delivery, backups and restore at the host before go-live.'))

    def _phase(self, timings, label, function, *args, **kwargs):
        start = time.perf_counter()
        try:
            function(*args, **kwargs)
        except BaseException:
            timings.append((label, time.perf_counter() - start, False))
            raise
        timings.append((label, time.perf_counter() - start, True))

    def _write_timings(self, timings, started):
        self.stdout.write('Phase timings:')
        for label, elapsed, passed in timings:
            self.stdout.write(f'  {label}: {elapsed:.2f}s' + ('' if passed else '  FAILED'))
        self.stdout.write(f'Total: {time.perf_counter() - started:.2f}s')

    def _check_smtp(self):
        if not settings.EMAIL_HOST or 'localhost' in settings.DEFAULT_FROM_EMAIL:
            raise CommandError('Configure a real SMTP server and DEFAULT_FROM_EMAIL for recovery emails.')

    def _check_proxy(self):
        """Refuse the one proxy configuration that makes client IPs forgeable.

        Render appends to X-Forwarded-For rather than replacing it, so the first
        value in that header is whatever the client sent. Honouring it while
        TRUSTED_PROXY_IP is a wildcard -- waitress's only option for Render,
        which publishes no proxy CIDR -- would let any request claim any
        address, and that address is what the audit trail would record. The safe
        default, x-forwarded-proto only, is also the enforcing one: this phase
        exists so the unsafe combination fails the deploy rather than shipping.
        """
        if not settings.TRUST_PROXY:
            return
        if settings.TRUSTED_PROXY_IP == '*' and 'x-forwarded-for' in settings.TRUSTED_PROXY_HEADERS:
            raise CommandError(
                'TRUSTED_PROXY_IP is "*" and x-forwarded-for is trusted. On Render, '
                'which appends to X-Forwarded-For rather than replacing it, that lets '
                'any client choose the address the audit trail records. Trust only '
                'x-forwarded-proto, or pin TRUSTED_PROXY_IP to a known proxy.')

    def _report_size(self):
        """Print the row counts these checks are reasoning about.

        The check's cost and its usefulness both depend on how much data the
        live organization holds, and the repository does not record that. Naming
        the numbers on every deploy turns an assumption into a measurement.
        """
        self.stdout.write('Live data:')
        for label, model in (('organizations', Organisation), ('members', Member),
                             ('payments', Payment), ('allocations', Allocation),
                             ('accounts', User)):
            self.stdout.write(f'  {label}: {model.objects.count()}')

    def _check_demo_password(self, all_users):
        if all_users:
            accounts = User.objects.filter(is_active=True)
        else:
            accounts = User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(username=DEMO_USERNAME))
        for user in accounts:
            if user.check_password(DEMO_PASSWORD):
                raise CommandError('Disable or change the local demo account before deployment.')

    def _check_active_secretaries(self):
        """Every organization holding records needs an active secretary.

        Asking for the organizations through three joined relations and then
        deduplicating made Postgres build members x payments rows per
        organization before collapsing them, so the query grew with the square
        of the data in the largest tenant. Four correlated EXISTS subqueries ask
        the same question per organization and each stops at the first matching
        index entry, so the cost follows the number of organizations instead.
        """
        organizations = Organisation.objects.annotate(
            has_access=Exists(UserAccess.objects.filter(organization_id=OuterRef('pk'))),
            has_member=Exists(Member.objects.filter(organization_id=OuterRef('pk'))),
            has_payment=Exists(Payment.objects.filter(organization_id=OuterRef('pk'))),
            has_secretary=Exists(UserAccess.objects.filter(organization_id=OuterRef('pk')).filter(SECRETARY)),
        ).filter(Q(has_access=True) | Q(has_member=True) | Q(has_payment=True))
        without = organizations.exclude(has_secretary=True).values_list('pk', 'name')[:10]
        missing = list(without)
        if not missing:
            return
        listed = ', '.join(f'{pk} ({name})' for pk, name in missing)
        more = organizations.exclude(has_secretary=True).count() - len(missing)
        suffix = f' and {more} more' if more > 0 else ''
        raise CommandError(
            f'Organization{suffix}: {listed}. '
            'Each needs an active secretary. Use assign_access with --organization-id.')
