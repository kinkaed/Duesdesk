"""Optional HTTPS email delivery for hosts that block SMTP."""
import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class EmailDeliveryError(Exception):
    pass


class ResendEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            if not message.recipients():
                continue
            try:
                if not settings.RESEND_API_KEY:
                    raise EmailDeliveryError('Email delivery is not configured.')
                if message.attachments:
                    raise EmailDeliveryError('Attachments are not supported by this backend.')
                payload = {'from': message.from_email, 'to': message.to,
                           'subject': message.subject, 'text': message.body}
                if message.cc:
                    payload['cc'] = message.cc
                if message.bcc:
                    payload['bcc'] = message.bcc
                if message.reply_to:
                    payload['reply_to'] = message.reply_to
                for content, mimetype in getattr(message, 'alternatives', []):
                    if mimetype == 'text/html':
                        payload['html'] = content
                if message.content_subtype == 'html':
                    payload['html'] = payload.pop('text')
                # Fixed HTTPS endpoint; never expose message bodies or keys in errors.
                response = requests.post('https://api.resend.com/emails',
                    headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}',
                             'Content-Type': 'application/json'},
                    json=payload, timeout=settings.EMAIL_TIMEOUT, allow_redirects=False)
                if not 200 <= response.status_code < 300 or not response.json().get('id'):
                    raise EmailDeliveryError('Email provider did not accept the message.')
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise EmailDeliveryError('Email could not be delivered. Please try again.') from None
        return sent
