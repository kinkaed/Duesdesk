"""Run the same WSGI server on Windows or Linux behind the hosting platform's HTTPS proxy."""
import os
from waitress import serve
from config.wsgi import application

options = {'host':os.environ.get('BIND_HOST','127.0.0.1'),'port':int(os.environ.get('PORT','8765')),'threads':8,'channel_timeout':60,'max_request_body_size':2097152}
if os.environ.get('TRUST_PROXY') == '1':
    options.update(trusted_proxy=os.environ.get('TRUSTED_PROXY_IP','127.0.0.1'),trusted_proxy_headers={'x-forwarded-proto'},clear_untrusted_proxy_headers=True)
serve(application, **options)
