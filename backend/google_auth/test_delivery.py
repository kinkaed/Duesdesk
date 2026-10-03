from unittest.mock import patch, Mock
from django.core.mail import EmailMessage
from django.test import SimpleTestCase, override_settings
from .email_backend import ResendEmailBackend, EmailDeliveryError


@override_settings(RESEND_API_KEY='fake-test-key', EMAIL_TIMEOUT=5)
class EmailBackendTests(SimpleTestCase):
    @patch('google_auth.email_backend.requests.post')
    def test_sends_over_https(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {'id': 'fake-message-id'}
        email = EmailMessage('Verification', 'Test message', 'sender@example.com', ['recipient@example.com'])
        self.assertEqual(ResendEmailBackend().send_messages([email]), 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.resend.com/emails')
        self.assertEqual(kwargs['json']['to'], ['recipient@example.com'])
        self.assertFalse(kwargs['allow_redirects'])

    @patch('google_auth.email_backend.requests.post')
    def test_provider_rejection_never_exposes_content(self, post):
        post.return_value = Mock(status_code=403)
        with self.assertRaisesMessage(EmailDeliveryError, 'Email could not be delivered'):
            ResendEmailBackend().send_messages([EmailMessage('Secret', 'private-code', to=['a@example.com'])])

    @patch('google_auth.email_backend.requests.post', side_effect=RuntimeError('private-code'))
    def test_network_failure_is_sanitized(self, post):
        with self.assertRaises(EmailDeliveryError) as caught:
            ResendEmailBackend().send_messages([EmailMessage('Secret', 'private-code', to=['a@example.com'])])
        self.assertNotIn('private-code', str(caught.exception))
        self.assertEqual(ResendEmailBackend(fail_silently=True).send_messages([EmailMessage('S', 'B', to=['a@example.com'])]), 0)

    @override_settings(RESEND_API_KEY='')
    @patch('google_auth.email_backend.requests.post')
    def test_missing_key_fails_without_network(self, post):
        with self.assertRaises(EmailDeliveryError):
            ResendEmailBackend().send_messages([EmailMessage('S', 'B', to=['a@example.com'])])
        post.assert_not_called()
