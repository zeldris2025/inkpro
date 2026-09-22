"""Django settings for InkPro.

Environment driven. Copy ``.env.example`` to ``.env`` and adjust per
environment (dev / staging / prod). SQLite is the zero-config default so the
project runs straight after a clone; set ``DATABASE_URL`` to a ``postgres://``
DSN to switch.
"""

from pathlib import Path

import environ

from .database import resolve_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    ALLOWED_HOSTS=(list, ['*']),
    CSRF_TRUSTED_ORIGINS=(list, []),
    SECRET_KEY=(str, 'django-insecure-inkpro-dev-key-change-me'),
    DATABASE_URL=(str, f'sqlite:///{BASE_DIR / "db.sqlite3"}'),
    CELERY_BROKER_URL=(str, ''),
    EMAIL_BACKEND=(str, 'django.core.mail.backends.console.EmailBackend'),
    EMAIL_HOST=(str, ''),
    EMAIL_PORT=(int, 587),
    EMAIL_HOST_USER=(str, ''),
    EMAIL_HOST_PASSWORD=(str, ''),
    EMAIL_USE_TLS=(bool, True),
    DEFAULT_FROM_EMAIL=(str, 'InkPro <quotes@inkpro.example>'),
    STAFF_NOTIFY_EMAILS=(list, ['quotes@inkpro.example']),
    SLACK_WEBHOOK_URL=(str, ''),
    SITE_URL=(str, 'http://localhost:8000'),
    GST_RATE=(str, '0.15'),
    QUOTE_VALID_DAYS=(int, 30),
    ENABLE_3D_HERO=(bool, False),
    MEDIA_ROOT=(str, ''),
    CONN_MAX_AGE=(int, 0),
    LAUNCH_PROMO_ENABLED=(bool, True),
    LAUNCH_PROMO_TEXT=(str, 'We just launched — our first 20 sales will receive a massive 50% discount'),
    CURRENCY_CODE=(str, 'WST'),
    CURRENCY_NAME=(str, 'Samoan Tala'),
)

environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env('ALLOWED_HOSTS')
# Django 4+ rejects any POST whose Origin header is not listed here, so an
# unset CSRF_TRUSTED_ORIGINS breaks every form behind HTTPS. Derive the origins
# from the hosts we already trust rather than relying on a second env var.
CSRF_TRUSTED_ORIGINS = env('CSRF_TRUSTED_ORIGINS')


def _trust_origin(origin):
    if origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(origin)


for _host in (h.strip().lstrip('.') for h in ALLOWED_HOSTS):
    if not _host or _host == '*':
        continue
    _scheme = 'http' if _host in ('localhost', '127.0.0.1', '[::1]') else 'https'
    _trust_origin(f'{_scheme}://{_host}')
    if '.' in _host and not _host.startswith(('www.', '*.')):
        _trust_origin(f'{_scheme}://www.{_host}')

_site_url = env('SITE_URL').rstrip('/')
if _site_url.startswith(('http://', 'https://')):
    _trust_origin(_site_url)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'rest_framework',
    'main',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # Uploaded media. Django only routes MEDIA_URL when DEBUG is on, so without
    # this every photo and artwork file 404s in production.
    'main.middleware.MediaFilesMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# WhiteNoise is only needed when serving static files without a CDN/Nginx in
# front. Drop the middleware rather than hard-failing if it is not installed.
try:  # pragma: no cover - import guard
    import whitenoise  # noqa: F401
except ImportError:  # pragma: no cover
    MIDDLEWARE.remove('whitenoise.middleware.WhiteNoiseMiddleware')
    MIDDLEWARE.remove('main.middleware.MediaFilesMiddleware')

ROOT_URLCONF = 'inkpro.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'main.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'inkpro.wsgi.application'

# Azure's Service Connector injects AZURE_POSTGRESQL_* rather than
# DATABASE_URL, so reading only DATABASE_URL would fall back to SQLite on a
# correctly-provisioned App Service — and then fail with "no such table".
DATABASES = {
    'default': env.db_url_config(
        resolve_database_url(default=f'sqlite:///{BASE_DIR / "db.sqlite3"}')
    )
}
DATABASES['default'].setdefault('ATOMIC_REQUESTS', False)

# Reusing connections matters on a managed Postgres: opening one per request
# adds latency and burns the instance's connection allowance. Health checks
# stop a reused-but-dead connection from failing the request after a failover.
if DATABASES['default']['ENGINE'].endswith('postgresql'):
    DATABASES['default']['CONN_MAX_AGE'] = env('CONN_MAX_AGE') or 60
    DATABASES['default']['CONN_HEALTH_CHECKS'] = True
    # Azure Database for PostgreSQL refuses connections without TLS.
    DATABASES['default'].setdefault('OPTIONS', {}).setdefault('sslmode', 'require')

# Sign-in accepts the username or the email address, in any case. See
# main/auth_backends.py for why.
AUTHENTICATION_BACKENDS = [
    'main.auth_backends.CaseInsensitiveUsernameOrEmailBackend',
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# en-nz is used only for its date and number formatting (d/m/Y); Django has no
# en-ws locale. The business, its currency and its clock are Samoan.
LANGUAGE_CODE = 'en-nz'
TIME_ZONE = 'Pacific/Apia'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# The manifest storage requires `collectstatic` to have run, so it is only
# used in production; dev and the test suite serve files straight from disk.
_use_manifest = (
    not DEBUG and 'whitenoise.middleware.WhiteNoiseMiddleware' in MIDDLEWARE
)
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        if _use_manifest
        else 'django.contrib.staticfiles.storage.StaticFilesStorage'
    },
}

MEDIA_URL = '/media/'
# Overridable because a deploy target's persistent disk is rarely inside the
# app directory. On Azure App Service only /home survives a restart, and a
# deployment replaces /home/site/wwwroot — so uploads belong somewhere like
# /home/site/media, outside the deployed tree.
MEDIA_ROOT = Path(env('MEDIA_ROOT')) if env('MEDIA_ROOT') else BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'my_quotes'
LOGOUT_REDIRECT_URL = 'home'

# --- Email -----------------------------------------------------------------
EMAIL_BACKEND = env('EMAIL_BACKEND')
EMAIL_HOST = env('EMAIL_HOST')
EMAIL_PORT = env('EMAIL_PORT')
EMAIL_HOST_USER = env('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD')
EMAIL_USE_TLS = env('EMAIL_USE_TLS')
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL')
STAFF_NOTIFY_EMAILS = env('STAFF_NOTIFY_EMAILS')
SLACK_WEBHOOK_URL = env('SLACK_WEBHOOK_URL')

# --- Celery ----------------------------------------------------------------
# With no broker configured, ``main.tasks`` runs work inline so that a fresh
# checkout still sends email and builds PDFs without extra infrastructure.
CELERY_BROKER_URL = env('CELERY_BROKER_URL')
CELERY_TASK_ALWAYS_EAGER = not CELERY_BROKER_URL
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_RESULT_BACKEND = CELERY_BROKER_URL or None
CELERY_TIMEZONE = TIME_ZONE

# --- Business rules --------------------------------------------------------
SITE_URL = env('SITE_URL').rstrip('/')
GST_RATE = env('GST_RATE')
QUOTE_VALID_DAYS = env('QUOTE_VALID_DAYS')

# Prices are quoted in Samoan Tala. WST shares the "$" symbol, so the code and
# name are surfaced wherever the amount could otherwise be read as some other
# dollar — the quote PDF, the emailed quote and the public rate card.
CURRENCY_CODE = env('CURRENCY_CODE')
CURRENCY_NAME = env('CURRENCY_NAME')
# Not env-driven: django-environ treats a leading "$" as a variable reference
# and recurses trying to expand it. The symbol is a property of the currency,
# not of the deployment, so it is fixed here.
CURRENCY_SYMBOL = '$'

# Launch promotion marquee on the homepage hero. A launch offer is temporary by
# definition, so both the copy and the on/off switch live here rather than in
# the template — retiring it after the 20th sale needs no code change.
LAUNCH_PROMO_ENABLED = env('LAUNCH_PROMO_ENABLED')
LAUNCH_PROMO_TEXT = env('LAUNCH_PROMO_TEXT')

# Scroll-driven WebGL layer on the homepage. Off by default; set
# ENABLE_3D_HERO=True to bring it back. The scene, its loader and the static
# fallback illustration all remain in the codebase either way.
ENABLE_3D_HERO = env('ENABLE_3D_HERO')

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

if not DEBUG:  # pragma: no cover - production hardening
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
