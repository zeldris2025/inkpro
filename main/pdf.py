"""Branded quote PDF generation.

WeasyPrint is the renderer, but it depends on native cairo/pango libraries that
are not always present on a given host. When it cannot be imported the quote
still goes out — the customer gets the same branded document as an HTML
attachment, and the emailed "view online" link is unaffected.
"""

import logging
import pathlib
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


def render_quote_html(quote, request=None) -> str:
    from django.conf import settings

    from .context_processors import absolute_logo_url

    return render_to_string(
        'main/pdf/quote.html',
        {
            'quote': quote,
            'items': quote.items.all(),
            # WeasyPrint has no browser to resolve a relative static path, and
            # the same markup doubles as the HTML fallback attachment.
            'LOGO_URL': absolute_logo_url(),
            'SITE_URL': settings.SITE_URL,
            'CONTACT_EMAIL': parseaddr(settings.DEFAULT_FROM_EMAIL)[1],
        },
        request=request,
    )


def render_quote_pdf(quote, request=None):
    """Render *quote* to a PDF.

    Returns ``(filename, bytes, content_type)``. Falls back to HTML when
    WeasyPrint is unavailable so callers never have to branch.
    """
    html = render_quote_html(quote, request=request)
    stem = quote.quote_number or f'quote-{quote.pk}'

    if weasyprint_available():
        try:
            from weasyprint import HTML

            pdf = HTML(
                string=html, base_url=_base_url(request), url_fetcher=_local_static_fetcher()
            ).write_pdf()
            return f'{stem}.pdf', pdf, 'application/pdf'
        except Exception:  # pragma: no cover - renderer failure
            logger.exception('WeasyPrint failed for %s; falling back to HTML.', stem)

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
