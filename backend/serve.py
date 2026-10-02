"""Run the Waitress WSGI server on Render (Linux) and locally.

The start command is `python serve.py`; it must be run from the `backend`
directory so that `config` is importable. Contains no Windows-specific paths,
no reliance on a .venv, and listens on 0.0.0.0 so Render can detect the port.
"""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

ALLOWED_APP_ENVS = ('local', 'production')

def validate_app_env(value):
    """Normalize the environment name, or exit before Django is imported.

    Settings compare APP_ENV against 'production' exactly, so a misspelled or
    wrongly cased value would quietly start a public service with local
    security settings. Catch that here, where the message is actionable.
    """
    name = str(value if value is not None else '').strip().lower()
    if name not in ALLOWED_APP_ENVS:
        raise SystemExit(f'APP_ENV must be one of {", ".join(ALLOWED_APP_ENVS)}; got {value!r}.')
    return name

os.environ['APP_ENV'] = validate_app_env(os.environ.get('APP_ENV', 'local'))

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()


def main():
    from waitress import serve

    from django.conf import settings

    from ledger.observability import technical

    # Once per process, at the single canonical place the service actually
    # starts. It goes to the technical log rather than the audit trail on
    # purpose: a startup is not a tenant event, and an AuditEvent with no
    # organization is returned by no tenant query, so a row here would be
    # invisible to every auditor while still growing a table forever. The log is
    # where an operator looking at a deploy actually looks.
    #
    # serve.py is the only production entry point (render.yaml startCommand), so
    # this cannot double up the way an AppConfig.ready() hook would across
    # migrate, collectstatic, tests and every management command.
    technical('system.startup', 'Duesdesk starting',
              environment=os.environ.get('APP_ENV'),
              debug=settings.DEBUG)

    host = '0.0.0.0'
    port = int(os.environ.get('PORT', '10000'))
    print(f'Serving on http://{host}:{port} (waitress)')
    options = {
        'host': host,
        'port': port,
        'threads': 8,
        'channel_timeout': 60,
        'max_request_body_size': 2097152,
    }
    if settings.TRUST_PROXY:
        options.update(
            trusted_proxy=settings.TRUSTED_PROXY_IP,
            # The header set is configuration, not a literal, so
            # deployment_check can refuse the combination of a wildcard proxied
            # peer and x-forwarded-for, which Render makes trivially spoofable.
            trusted_proxy_headers=set(settings.TRUSTED_PROXY_HEADERS),
            clear_untrusted_proxy_headers=True,
        )
    from google_auth.maintenance import start_cleanup
    cleanup_stop = start_cleanup()
    try:
        serve(application, **options)
    finally:
        cleanup_stop.set()


if __name__ == '__main__':
    main()