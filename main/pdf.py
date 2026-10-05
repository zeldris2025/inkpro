"""Branded quote PDF generation.

WeasyPrint is the preferred renderer, but it depends on native cairo/pango
libraries that are not always present on a given host (Windows development
machines, or an App Service where the startup install failed). When it cannot
run, the same document is drawn with ReportLab, which is pure Python, so a real
PDF still goes out. Only if both fail does the customer get the branded
document as an HTML attachment.
"""

import base64
import logging
import pathlib
from decimal import Decimal
from email.utils import parseaddr
from urllib.parse import urlparse

from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:  # pragma: no cover - depends on host libraries
        return False
    return True


def logo_path():
    """Filesystem path of the light-background logo, or None if not built."""
    from django.conf import settings
    from django.contrib.staticfiles import finders

    from .context_processors import LOGO_ON_LIGHT

    found = finders.find(LOGO_ON_LIGHT) or pathlib.Path(settings.STATIC_ROOT or '', LOGO_ON_LIGHT)
    found = pathlib.Path(found)
    return found if found.is_file() else None


def logo_data_uri():
    """The logo inlined as a data: URI.

    Embedding it means the document never fetches its own logo: no reliance on
    ``SITE_URL`` matching the address the site is actually served from, no
    round trip for WeasyPrint, and the HTML fallback shows it offline too.
    """
    path = logo_path()
    if not path:
        return None
    return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def quote_document_context(quote):
    """Everything either renderer needs to lay out the quote document."""
    from django.conf import settings

    from .models import plain_number

    items = list(quote.items.all())
    return {
        'quote': quote,
        'items': items,
        # Area/rate headings when every line is sized; otherwise the columns
        # also carry quantities and unit prices.
        'all_sized': bool(items) and all(item.area_m2 > 0 for item in items),
        'special': quote.special_rate_summary(),
        'gst_pct': plain_number(Decimal(str(settings.GST_RATE)) * 100),
        'valid_days': _valid_days(quote),
        'SITE_URL': settings.SITE_URL,
        'CONTACT_EMAIL': parseaddr(settings.DEFAULT_FROM_EMAIL)[1],
    }


def render_quote_html(quote, request=None) -> str:
    context = quote_document_context(quote)
    context['LOGO_URL'] = logo_data_uri()
    return render_to_string('main/pdf/quote.html', context, request=request)


def _valid_days(quote):
    """How long the quote stands, counted from its issue date."""
    from django.conf import settings
    from django.utils import timezone

    issued = quote.sent_at or quote.created_at
    if quote.valid_until and issued:
        days = (quote.valid_until - timezone.localtime(issued).date()).days
        if days > 0:
            return days
    return settings.QUOTE_VALID_DAYS


def render_quote_pdf(quote, request=None):
    """Render *quote* to a PDF.

    Returns ``(filename, bytes, content_type)``. Tries WeasyPrint, then
    ReportLab, and only falls back to HTML if neither can produce a PDF, so
    callers never have to branch.
    """
    stem = quote.quote_number or f'quote-{quote.pk}'

    if weasyprint_available():
        try:
            from weasyprint import HTML

            pdf = HTML(
                string=render_quote_html(quote, request=request),
                base_url=_base_url(request),
                url_fetcher=_local_static_fetcher(),
            ).write_pdf()
            return f'{stem}.pdf', pdf, 'application/pdf'
        except Exception:  # pragma: no cover - renderer failure
            logger.exception('WeasyPrint failed for %s; trying ReportLab.', stem)

    try:
        from .pdf_reportlab import render as render_with_reportlab

        return f'{stem}.pdf', render_with_reportlab(quote_document_context(quote), logo_path()), 'application/pdf'
    except Exception:  # pragma: no cover - renderer failure
        logger.exception('ReportLab failed for %s; falling back to HTML.', stem)

    html = render_quote_html(quote, request=request)
    return f'{stem}.html', html.encode('utf-8'), 'text/html'


def _base_url(request):
    from django.conf import settings

    return request.build_absolute_uri('/') if request else settings.SITE_URL


def _local_static_fetcher():
    """A WeasyPrint fetcher that serves our own static files from disk.

    The template carries absolute URLs so the HTML fallback works in a mail
    client, but WeasyPrint fetching them over HTTP makes the server download its
    own files — which fails outright while ``SITE_URL`` is localhost (the logo
    silently drops out), and otherwise costs a round trip through the public
    internet on every render.
    """
    from django.conf import settings
    from django.contrib.staticfiles import finders
    from weasyprint.urls import URLFetcher

    prefix = urlparse(settings.STATIC_URL).path

    class LocalStaticFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            path = urlparse(url).path
            if url.startswith(('http://', 'https://')) and path.startswith(prefix):
                relative = path[len(prefix):]
                local = finders.find(relative) or pathlib.Path(settings.STATIC_ROOT, relative)
                if pathlib.Path(local).is_file():
                    url = pathlib.Path(local).resolve().as_uri()
            return super().fetch(url, headers)

    return LocalStaticFetcher()


def attach_pdf_to_quote(quote, request=None):
    """Render and store the quote document on ``quote.pdf_file``.

    Re-sending a quote replaces its document rather than adding another. Django
    storage appends a suffix instead of overwriting, so without clearing the
    old file first every send leaves an orphaned PDF behind in media/quotes.

    Returns the rendered ``(filename, bytes, content_type)``, so a caller that
    also emails the document does not render it twice.
    """
    from django.core.files.base import ContentFile

    filename, content, mimetype = render_quote_pdf(quote, request=request)
    if quote.pdf_file:
        quote.pdf_file.delete(save=False)
    storage = quote.pdf_file.storage
    target = quote.pdf_file.field.upload_to + filename
    if storage.exists(target):
        storage.delete(target)
    quote.pdf_file.save(filename, ContentFile(content), save=True)
    return filename, content, mimetype
