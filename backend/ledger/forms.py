from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError

User = get_user_model()


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, max_length=User._meta.get_field('email').max_length)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        # EmailClaim is the database authority, but an account created outside this
        # form (admin, an operator command) may have an address without a claim, so
        # the User table is checked too. The claim's unique constraint is what
        # resolves a race; this is only the friendly, earlier refusal.
        from google_auth.models import EmailClaim
        if EmailClaim.objects.filter(email=email).exists() or User.objects.filter(email__iexact=email).exists():
            raise ValidationError('An account with this email already exists, please log in.')
        return email
