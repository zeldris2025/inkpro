"""Branded quote PDF generation.

WeasyPrint is the renderer, but it depends on native cairo/pango libraries that
are not always present on a given host. When it cannot be imported the quote
still goes out — the customer gets the same branded document as an HTML
attachment, and the emailed "view online" link is unaffected.
"""

import logging

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

            pdf = HTML(string=html, base_url=_base_url(request)).write_pdf()
            return f'{stem}.pdf', pdf, 'application/pdf'
        except Exception:  # pragma: no cover - renderer failure
            logger.exception('WeasyPrint failed for %s; falling back to HTML.', stem)

    return f'{stem}.html', html.encode('utf-8'), 'text/html'


def _base_url(request):
    from django.conf import settings

    return request.build_absolute_uri('/') if request else settings.SITE_URL


def attach_pdf_to_quote(quote, request=None):
    """Render and store the quote document on ``quote.pdf_file``.

    Re-sending a quote replaces its document rather than adding another. Django
    storage appends a suffix instead of overwriting, so without clearing the
    old file first every send leaves an orphaned PDF behind in media/quotes.
    """
    from django.core.files.base import ContentFile

    filename, content, _ = render_quote_pdf(quote, request=request)
    if quote.pdf_file:
        quote.pdf_file.delete(save=False)
    storage = quote.pdf_file.storage
    target = quote.pdf_file.field.upload_to + filename
    if storage.exists(target):
        storage.delete(target)
    quote.pdf_file.save(filename, ContentFile(content), save=True)
    return quote.pdf_file
