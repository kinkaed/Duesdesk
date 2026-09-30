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
    serve(application, **options)


if __name__ == '__main__':
    main()