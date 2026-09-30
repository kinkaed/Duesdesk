"""Google's claims are checked before allauth is allowed to write anything.

allauth resolves and refreshes the stored SocialAccount inside
``SocialLogin.lookup()``, which runs before ``pre_social_login`` and writes to
the database. Hooking ``complete_login`` instead puts our checks in front of
that write, so a claim we would refuse can never modify a linked identity.
"""
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter as Base


class GoogleOAuth2Adapter(Base):
    def complete_login(self, request, app, token, **kwargs):
        login = super().complete_login(request, app, token, **kwargs)
        get_adapter(request).validate_claims(request, login.account)
        return login
