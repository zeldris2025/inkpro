"""Template context shared across every page."""

from datetime import date, timedelta

from django.conf import settings
from django.templatetags.static import static
from django.utils import timezone

from .models import ServiceCategory

#: Logo variants produced by ``manage.py build_logo_assets``. The master
#: artwork is drawn for light backgrounds (black splat, black tagline), so the
#: dark site chrome needs the recoloured variant or the mark disappears.
LOGO_ON_LIGHT = 'img/inkpro-logo.png'
LOGO_ON_DARK = 'img/inkpro-logo-on-dark.png'
#: Browser-tab icons. The 512px PNG is kept for manifests and link previews,
#: but a tab that scales it down to 16px looks muddy, so a purpose-rendered
#: 32px PNG and an .ico pack are offered ahead of it.
FAVICON = 'img/favicon.png'
FAVICON_SMALL = 'img/favicon-32.png'
FAVICON_ICO = 'img/favicon.ico'
APPLE_TOUCH_ICON = 'img/apple-touch-icon.png'

TAGLINE = 'Think Ink, Think Pro'


def _static_if_present(path):
    """Static URL for *path*, or None when the asset has not been built yet."""
    for directory in settings.STATICFILES_DIRS:
        if (directory / path).exists():
            return static(path)
    return None


def favicon_ico_url():
    """URL of the .ico pack, or the 512px PNG when it has not been built.

    Resolved lazily by the URLconf, so that a missing icon cannot stop the
    site booting.
    """
    return _static_if_present(FAVICON_ICO) or static(FAVICON)


def absolute_logo_url(path=LOGO_ON_LIGHT):
    """Fully-qualified logo URL, for contexts with no browser to resolve a
    relative path against: transactional email and the rendered PDF."""
    url = _static_if_present(path)
    if not url:
        return None
    return url if url.startswith('http') else f'{settings.SITE_URL}{url}'


#: How long before White Sunday the announcement bar starts showing.
WHITE_SUNDAY_LEAD = timedelta(weeks=6)


def white_sunday(year):
    """Lotu a Tamaiti falls on the second Sunday of October."""
    first = date(year, 10, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=1)


def upcoming_white_sunday(today):
    """This year's White Sunday while the bar should show, else None.

    Shows for the six weeks before, and stays up through the Monday public
    holiday that follows it.
    """
    day = white_sunday(today.year)
    if day - WHITE_SUNDAY_LEAD <= today <= day + timedelta(days=1):
        return day
    return None


def white_sunday_banner():
    today = timezone.localdate()
    day = upcoming_white_sunday(today)
    if day is None:
        return None
    # From the day itself it becomes a greeting: "order early" would be too late.
    return {'date': day, 'arrived': today >= day}


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
        'FAVICON_SMALL_URL': _static_if_present(FAVICON_SMALL),
        'FAVICON_ICO_URL': _static_if_present(FAVICON_ICO),
        'APPLE_TOUCH_ICON_URL': _static_if_present(APPLE_TOUCH_ICON),
        'TAGLINE': TAGLINE,
        'ENABLE_3D_HERO': settings.ENABLE_3D_HERO,
        'LAUNCH_PROMO_ENABLED': settings.LAUNCH_PROMO_ENABLED,
        'LAUNCH_PROMO_TEXT': settings.LAUNCH_PROMO_TEXT,
        'WHITE_SUNDAY': white_sunday_banner() if settings.WHITE_SUNDAY_BANNER else None,
        'CURRENCY_CODE': settings.CURRENCY_CODE,
        'CURRENCY_NAME': settings.CURRENCY_NAME,
        'CURRENCY_SYMBOL': settings.CURRENCY_SYMBOL,
    }
