"""Template context shared across every page."""

from django.conf import settings
from django.templatetags.static import static

from .models import ServiceCategory

#: Logo variants produced by ``manage.py build_logo_assets``. The master
#: artwork is drawn for light backgrounds (black splat, black tagline), so the
#: dark site chrome needs the recoloured variant or the mark disappears.
LOGO_ON_LIGHT = 'img/inkpro-logo.png'
LOGO_ON_DARK = 'img/inkpro-logo-on-dark.png'
FAVICON = 'img/favicon.png'

TAGLINE = 'Think Ink, Think Pro'


def _static_if_present(path):
    """Static URL for *path*, or None when the asset has not been built yet."""
    for directory in settings.STATICFILES_DIRS:
        if (directory / path).exists():
            return static(path)
    return None


def absolute_logo_url(path=LOGO_ON_LIGHT):
    """Fully-qualified logo URL, for contexts with no browser to resolve a
    relative path against: transactional email and the rendered PDF."""
    url = _static_if_present(path)
    if not url:
        return None
    return url if url.startswith('http') else f'{settings.SITE_URL}{url}'


def site_settings(request):
    user = getattr(request, 'user', None)
    return {
        'SITE_URL': settings.SITE_URL,
        'GST_RATE_PCT': int(float(settings.GST_RATE) * 100),
        'nav_categories': ServiceCategory.objects.filter(is_active=True)[:8],
        # getattr, because this also renders for requests built outside the
        # middleware stack (management commands, PDF rendering, tests).
        'is_inkpro_staff': bool(
            user and user.is_authenticated and user.is_staff
        ),
        # The site chrome is near-black, so pages use the on-dark variant.
        'LOGO_URL': _static_if_present(LOGO_ON_DARK),
        'LOGO_ON_LIGHT_URL': _static_if_present(LOGO_ON_LIGHT),
        'FAVICON_URL': _static_if_present(FAVICON),
        'TAGLINE': TAGLINE,
        'ENABLE_3D_HERO': settings.ENABLE_3D_HERO,
        'LAUNCH_PROMO_ENABLED': settings.LAUNCH_PROMO_ENABLED,
        'LAUNCH_PROMO_TEXT': settings.LAUNCH_PROMO_TEXT,
        'CURRENCY_CODE': settings.CURRENCY_CODE,
        'CURRENCY_NAME': settings.CURRENCY_NAME,
        'CURRENCY_SYMBOL': settings.CURRENCY_SYMBOL,
    }
