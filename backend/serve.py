"""Run the Waitress WSGI server on Render (Linux) and locally.

The start command is `python serve.py`; it must be run from the `backend`
directory so that `config` is importable. Contains no Windows-specific paths,
no reliance on a .venv, and listens on 0.0.0.0 so Render can detect the port.
"""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()


def main():
    from waitress import serve

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
    if os.environ.get('TRUST_PROXY') == '1':
        options.update(
            trusted_proxy=os.environ.get('TRUSTED_PROXY_IP', '127.0.0.1'),
            trusted_proxy_headers={'x-forwarded-proto'},
            clear_untrusted_proxy_headers=True,
        )
    serve(application, **options)


if __name__ == '__main__':
    main()