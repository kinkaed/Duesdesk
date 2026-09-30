"""Production configuration must fail closed, proven by booting the settings.

settings.py is imported once per process, and its whole job on a live service is
to refuse to start when the environment is incomplete. That behaviour cannot be
tested with override_settings or by importing config.settings directly, because
the import has already happened by the time a test body runs. So each case here
boots a fresh interpreter with only the variables under test set, and judges it
on the exit code and stdout alone.

No secret value appears here. The DJANGO_SECRET_KEY below is a fixed run of one
character chosen to be structurally valid, so the cases that are not about the
key still reach the check being exercised; the DATABASE_URL is a loopback
placeholder that is never connected to.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parent.parent

# A complete, structurally valid production environment. Each test removes or
# breaks exactly one entry to prove that entry is genuinely load-bearing.
PRODUCTION_ENV = {
    'APP_ENV': 'production',
    'DJANGO_SECRET_KEY': 'A' * 60,
    'DATABASE_URL': 'postgresql://placeholder:placeholder@localhost:5432/placeholder',
    'ALLOWED_HOSTS': 'duesdesk.onrender.com',
    'CSRF_TRUSTED_ORIGINS': 'https://duesdesk.onrender.com',
}

# Every variable that decides the security posture, so one left over from the
# ambient environment cannot quietly satisfy a case that means to remove it.
CLEARED = ('APP_ENV', 'DJANGO_SECRET_KEY', 'DJANGO_DEBUG', 'DATABASE_URL',
           'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS', 'DEV_TRUSTED_ORIGINS',
           'RENDER_EXTERNAL_HOSTNAME', 'TEST_SQLITE', 'TRUST_PROXY',
           'TRUSTED_PROXY_IP', 'TRUSTED_PROXY_HEADERS')

# Reports the posture on success. Nothing secret is read back: only lengths and
# booleans, so this string is safe to assert on and safe to print on failure.
PROBE = (
    'import config.settings as s;'
    'print("POSTURE %d %d %s" % ('
    '  len(s.ALLOWED_HOSTS), len(s.CSRF_TRUSTED_ORIGINS), s.SECURE_SSL_REDIRECT))'
)


class ProductionConfigFailsClosedTests(SimpleTestCase):
    """A live service must refuse to start rather than run half-configured."""

    def boot(self, **overrides):
        """Start a fresh interpreter with PRODUCTION_ENV, adjusted by overrides.

        An override of None removes that variable, which is how a test
        expresses a missing setting without the ambient environment supplying
        one by accident. Any other value is set, including a variable the base
        environment does not mention.
        """
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        configured = dict(PRODUCTION_ENV)
        for key, value in overrides.items():
            if value is None:
                configured.pop(key, None)
            else:
                configured[key] = value
        env.update(configured)
        return subprocess.run([sys.executable, '-c', PROBE], cwd=BACKEND,
                              capture_output=True, text=True, env=env, timeout=120)

    def assert_refused(self, result, expected):
        self.assertNotEqual(result.returncode, 0,
                            'settings imported when it should have refused: %r' % result.stdout)
        self.assertIn(expected, result.stderr)

    def test_a_complete_production_environment_starts(self):
        result = self.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        # One host, one origin, SSL redirect on. The counts, not the values, so
        # no configuration string ends up in a failure message.
        self.assertIn('POSTURE 1 1 True', result.stdout)

    def test_allowed_hosts_is_required(self):
        self.assert_refused(self.boot(ALLOWED_HOSTS=None), 'ALLOWED_HOSTS is required in production')

    def test_a_blank_allowed_hosts_is_not_treated_as_configured(self):
        # An env var set to "" is a misconfiguration, not permission to serve
        # nobody: the service would start and reject every request.
        self.assert_refused(self.boot(ALLOWED_HOSTS=''), 'ALLOWED_HOSTS is required in production')

    def test_a_wildcard_allowed_hosts_is_refused(self):
        self.assert_refused(self.boot(ALLOWED_HOSTS='*'), 'Explicit ALLOWED_HOSTS are required')

    def test_csrf_trusted_origins_is_required(self):
        # Without an origin every form post is rejected, including signing in,
        # which presents as an unexplained outage rather than a config error.
        self.assert_refused(self.boot(CSRF_TRUSTED_ORIGINS=None),
                            'CSRF_TRUSTED_ORIGINS is required in production')

    def test_a_blank_csrf_origin_list_is_refused(self):
        self.assert_refused(self.boot(CSRF_TRUSTED_ORIGINS=''),
                            'CSRF_TRUSTED_ORIGINS is required in production')

    def test_the_render_hostname_satisfies_the_csrf_requirement(self):
        # Render always sets RENDER_EXTERNAL_HOSTNAME for a web service, so the
        # platform-native deployment path must not be blocked by the check above.
        result = self.boot(CSRF_TRUSTED_ORIGINS=None,
                           RENDER_EXTERNAL_HOSTNAME='duesdesk.onrender.com')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('POSTURE 1 1 True', result.stdout)

    def test_a_short_secret_key_is_refused(self):
        self.assert_refused(self.boot(DJANGO_SECRET_KEY='tooshort'),
                            'at least 50 characters')

    def test_database_url_is_required(self):
        # The failure this prevents is the serious one: with a missing
        # DATABASE_URL, local mode quietly falls back to a localhost database
        # and the service comes up empty instead of down.
        self.assert_refused(self.boot(DATABASE_URL=None), 'DATABASE_URL is required in production')

    def test_app_env_must_be_categorisable(self):
        # serve.py normalises and reports this before Django loads. settings
        # covers the entry points that bypass serve.py, so it must not accept a
        # value it cannot place. 'test' is accepted here but not by serve.py,
        # because the test runner legitimately needs a non-production posture.
        for bad in ('Production', 'PRODUCTION', 'production ', 'staging', ''):
            with self.subTest(app_env=bad):
                self.assert_refused(self.boot(APP_ENV=bad), 'APP_ENV must be exactly one of')

    def test_the_render_blueprint_states_every_value_settings_demands(self):
        # Settings requires ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS. A blueprint
        # that omits one produces a service that will not boot, so the two lists
        # are compared here instead of discovered during a release.
        spec = (BACKEND.parent / 'render.yaml').read_text(encoding='utf-8')
        for name in ('ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS'):
            with self.subTest(variable=name):
                self.assertIn(f'- key: {name}', spec)


class ProductionBuildEntryPointTests(SimpleTestCase):
    """A build that boots production config must declare everything it needs.

    Demanding ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS in production is only
    correct while the builds that run production mode supply them. A build has no
    real secrets, so it cannot simply adopt the service environment, and the
    placeholders it does carry have to be kept in step with settings by hand.

    These cases do not restate the required list, because a restated list is
    exactly what drifts: the moment settings demands a new variable, a copied
    list still looks complete. Instead each case parses the placeholders out of
    the build definition and boots the settings with precisely those, so a
    requirement settings adds is enforced here whether or not this file was
    updated alongside it.
    """

    def boot_with(self, declared):
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update(declared)
        return subprocess.run([sys.executable, '-c', PROBE], cwd=BACKEND,
                              capture_output=True, text=True, env=env, timeout=120)

    def logical_statements(self, path):
        """Yield Dockerfile statements with backslash continuations rejoined."""
        buffer = ''
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if stripped.endswith('\\'):
                buffer += stripped[:-1].strip() + ' '
            else:
                yield buffer + stripped
                buffer = ''

    def test_the_image_build_declares_a_completable_production_environment(self):
        # The image build collects static files under production settings, because
        # only that storage backend writes the manifest verify_static reads back.
        path = BACKEND.parent / 'Dockerfile'
        build = [s for s in self.logical_statements(path)
                 if 'sh -c' in s and 'collectstatic' in s]
        self.assertEqual(len(build), 1, 'Dockerfile has no single build step that runs collectstatic')
        declared = dict(re.findall(r'([A-Z_][A-Z0-9_]*)=(\S+)', build[0]))
        self.assertEqual(declared.get('APP_ENV'), 'production',
                         'the build step must select production to prove the manifest pipeline')
        result = self.boot_with(declared)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_ci_step_in_production_declares_a_completable_environment(self):
        # CI mirrors the image build, so a requirement added to settings breaks
        # the release pipeline as surely as it breaks the image.
        path = BACKEND.parent / '.github' / 'workflows' / 'verify.yml'
        blocks = re.findall(r'env:\n((?:\s+[A-Z_][A-Z0-9_]*:.*\n)+)', path.read_text(encoding='utf-8'))
        production = [b for b in blocks if re.search(r'^\s+APP_ENV:\s*production\s*$', b, re.M)]
        self.assertTrue(production, 'workflow has no step running under APP_ENV: production')
        for block in production:
            declared = dict(re.findall(r'^\s+([A-Z_][A-Z0-9_]*):\s*(.+?)\s*$', block, re.M))
            label = ' '.join(f'{k}={v}' for k, v in sorted(declared.items())
                             if k not in ('DJANGO_SECRET_KEY', 'DATABASE_URL'))
            with self.subTest(step=label):
                result = self.boot_with(declared)
                self.assertEqual(result.returncode, 0, result.stderr)
