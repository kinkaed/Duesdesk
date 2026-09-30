from django.contrib.auth import views as auth
from django.urls import path
from google_auth import views as google_views
from google_auth.forms import GuardedAuthenticationForm, GooglePasswordResetForm
from ledger import views, organization_views as org_views

urlpatterns = [
    path('accounts/google/login/', google_views.google_login, name='google_login'),
    path('accounts/google/login/callback/', google_views.google_callback, name='google_callback'),
    path('', views.home),
    path('api/session/', views.session_info),
    path('login/', auth.LoginView.as_view(authentication_form=GuardedAuthenticationForm), name='login'),
    path('logout/', auth.LogoutView.as_view()),
    path('signup/', org_views.signup, name='signup'),
    path('account/password/', auth.PasswordChangeView.as_view(template_name='registration/account_form.html', success_url='/account/password/done/', extra_context={'heading':'Change your password','button':'Save new password'}), name='password_change'),
    path('account/password/done/', auth.PasswordChangeDoneView.as_view(template_name='registration/account_done.html', extra_context={'heading':'Password changed','description':'Your new password is ready to use.'}), name='password_change_done'),
    path('account/reset/', views.RecoveryView.as_view(form_class=GooglePasswordResetForm), name='password_reset'),
    path('account/reset/sent/', auth.PasswordResetDoneView.as_view(template_name='registration/account_done.html', extra_context={'heading':'Check your email','description':'If this email belongs to an active account, a reset link will be sent. Check spam too. Contact your secretary if it does not arrive.'}), name='password_reset_done'),
    path('account/reset/<uidb64>/<token>/', auth.PasswordResetConfirmView.as_view(template_name='registration/account_form.html', extra_context={'heading':'Choose a new password','button':'Save password'}), name='password_reset_confirm'),
    path('account/reset/complete/', auth.PasswordResetCompleteView.as_view(template_name='registration/account_done.html', extra_context={'heading':'Password reset','description':'You can now sign in with your new password.'}), name='password_reset_complete'),
    path('api/overview/', views.overview),
    path('api/members/', views.members),
    path('api/members/<int:pk>/', views.member_detail),
    path('api/members/<int:pk>/report/', views.member_report),
    path('api/payments/preview/', views.payment_preview),
    path('api/payments/', views.payments),
    path('api/payments/<int:pk>/void/', views.void),
    path('receipts/<int:pk>/', views.receipt),
    path('export/', views.export_csv),
    path('export/excel/', views.export_excel),
    path('api/settings/', views.organisation_settings),
    path('api/audit/', views.audit_log),
    path('api/accounts/', views.accounts),
    path('api/accounts/<int:pk>/disable/', views.disable_account),
    path('api/import/template/', views.import_template),
    path('api/import/preview/', views.import_preview),
    path('api/import/commit/', views.import_commit),
    path('api/branding/preview/', org_views.logo_preview),
    path('api/invites/', org_views.invites),
    path('api/invites/<int:pk>/revoke/', org_views.revoke_invite),
    path('api/organization/leave/', org_views.leave),
    path('organizations/<uuid:public_id>/logo/', org_views.logo),
    path('health/', views.health),
]
