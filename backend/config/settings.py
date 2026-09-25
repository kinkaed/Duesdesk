import os
import secrets
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env', override=False)
PRODUCTION = os.environ.get('APP_ENV', 'local') == 'production'
DEMO_MODE = not PRODUCTION
DEBUG = os.environ.get('DJANGO_DEBUG', 'false').lower() in ('1', 'true', 'yes', 'on')
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()


def required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise ImproperlyConfigured(f'{name} is required in production.')
    return value


def csv_values(value):
    return [item.strip() for item in (value or '').split(',') if item.strip()]


if PRODUCTION:
    SECRET_KEY = required('DJANGO_SECRET_KEY')
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured('Use a random DJANGO_SECRET_KEY of at least 50 characters.')
    explicit_hosts = csv_values(os.environ.get('ALLOWED_HOSTS', ''))
    if '*' in explicit_hosts:
        raise ImproperlyConfigured('Explicit ALLOWED_HOSTS are required.')
    hosts = list(explicit_hosts)
    if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in hosts:
        hosts.append(RENDER_EXTERNAL_HOSTNAME)
    ALLOWED_HOSTS = hosts
else:
    secret_file = BASE_DIR / '.local-secret'
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(64), encoding='utf-8')
    SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or secret_file.read_text().strip()
    hosts = csv_values(os.environ.get('ALLOWED_HOSTS', '127.0.0.1,localhost,testserver'))
    if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in hosts:
        hosts.append(RENDER_EXTERNAL_HOSTNAME)
    ALLOWED_HOSTS = hosts

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'axes',
    'ledger',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ledger.middleware.SecurityHeadersMiddleware',
    'axes.middleware.AxesMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'ledger.context.site_context']}}]
WSGI_APPLICATION = 'config.wsgi.application'
if os.environ.get('TEST_SQLITE') == '1':
    if PRODUCTION:
        raise ImproperlyConfigured('SQLite testing is disabled in production.')
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'test.sqlite3'}}
else:
    default_url = required('DATABASE_URL') if PRODUCTION else os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/duesdesk')
    DATABASES = {'default': dj_database_url.config(default=default_url, conn_max_age=600, conn_health_checks=True)}
AUTHENTICATION_BACKENDS = ['axes.backends.AxesStandaloneBackend', 'django.contrib.auth.backends.ModelBackend']
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ['username', ['username', 'ip_address']]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = 'registration/locked.html'
AXES_SENSITIVE_PARAMETERS = ['password', 'new_password1', 'new_password2']
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Africa/Accra'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
WHITENOISE_USE_FINDERS = not PRODUCTION
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
origins = csv_values(os.environ.get('CSRF_TRUSTED_ORIGINS', ''))
if RENDER_EXTERNAL_HOSTNAME:
    https_origin = f'https://{RENDER_EXTERNAL_HOSTNAME}'
    if https_origin not in origins:
        origins.append(https_origin)
CSRF_TRUSTED_ORIGINS = origins
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
SECURE_REFERRER_POLICY = 'same-origin'
if os.environ.get('TRUST_PROXY', '0') == '1':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 2097152
FILE_UPLOAD_MAX_MEMORY_SIZE = 2097152
PASSWORD_RESET_TIMEOUT = 3600
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend' if PRODUCTION else 'django.core.mail.backends.console.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', '1') == '1'
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Duesdesk <noreply@localhost>')
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'handlers': {'console': {'class': 'logging.StreamHandler'}}, 'root': {'handlers': ['console'], 'level': 'WARNING'}, 'loggers': {'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False}}}