"""Transactional email for the quote and invoice flows.

Every message renders the same branded black/yellow HTML shell with a plain
text alternative. Copy for the opening and closing paragraphs comes from the
``EmailTemplate`` rows so the owner can reword them from the admin without a
deploy; hard-coded defaults are used when a template row is missing.
"""

import json
import logging
import urllib.request

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import strip_tags

from .models import EmailTemplate

logger = logging.getLogger(__name__)

DEFAULT_COPY = {
    EmailTemplate.QUOTE_RECEIVED: {
        'subject': 'We’ve received your quote request ({quote_number})',
        'intro': 'Thanks {name} — your request has landed with our team.',
        'outro': 'Think Ink. Think Pro.',
    },
    EmailTemplate.QUOTE_SENT: {
        'subject': 'Your InkPro quote {quote_number} is ready',
        'intro': 'Hi {name}, your quote is ready to review.',
        'outro': 'Questions? Just reply to this email.',
    },
    EmailTemplate.QUOTE_ACCEPTED: {
        'subject': 'Confirmed: your final InkPro quote {quote_number}',
        'intro': 'Thanks {name} — you’ve accepted quote {quote_number}. '
                 'Your final quote is attached for your records.',
        'outro': 'We’ll be in touch with timing. Questions? Just reply to this email.',
    },
    EmailTemplate.STAFF_QUOTE_RESPONSE: {
        'subject': 'Quote {quote_number} {decision} by {name}',
        'intro': '{name} has {decision} quote {quote_number}.',
        'outro': '',
    },
    EmailTemplate.STAFF_NEW_QUOTE: {
        'subject': 'New quote request: {quote_number}',
        'intro': 'A new quote request came in from {name}.',
        'outro': '',
    },
    EmailTemplate.PAYMENT_REMINDER: {
        'subject': 'Friendly reminder: invoice {invoice_no}',
        'intro': 'Hi {name}, invoice {invoice_no} is still showing as outstanding.',
        'outro': 'If you’ve already paid, please ignore this note.',
    },
}


def _copy(key, **context):
    """Resolve subject/intro/outro for *key*, preferring the database row."""
    template = EmailTemplate.objects.filter(key=key, is_active=True).first()
    if template:
        return template.render(**context)
    defaults = DEFAULT_COPY.get(key, {'subject': 'InkPro', 'intro': '', 'outro': ''})
    out = {}
    for field, value in defaults.items():
        try:
            out[field] = value.format(**context)
        except (KeyError, IndexError):
            out[field] = value
    return out


def send_branded_email(*, to, key, template, context, attachments=None, **copy_context):
    """Render and send one branded message. Returns True when it went out."""
    recipients = [address for address in (to if isinstance(to, (list, tuple)) else [to]) if address]
    if not recipients:
        logger.warning('Skipping %s email: no recipient address.', key)
        return False

    from .context_processors import LOGO_ON_DARK, absolute_logo_url

    copy = _copy(key, **copy_context)
    html = render_to_string(
        template,
        {
            **context,
            'copy': copy,
            'SITE_URL': settings.SITE_URL,
            # Mail clients need an absolute URL; a relative one silently breaks.
            # The email header is black, so it needs the on-dark variant.
            'LOGO_URL': absolute_logo_url(LOGO_ON_DARK),
        },
    )
    message = EmailMultiAlternatives(
        subject=copy['subject'],
        body=strip_tags(html.replace('</p>', '</p>\n')),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    message.attach_alternative(html, 'text/html')
    for filename, content, mimetype in attachments or []:
        message.attach(filename, content, mimetype)
    message.send(fail_silently=False)
    return True


# -- quote flow --------------------------------------------------------------
def send_quote_received(quote):
    """Acknowledge a freshly submitted request to the customer."""
    return send_branded_email(
        to=quote.contact_email,
        key=EmailTemplate.QUOTE_RECEIVED,
        template='main/email/quote_received.html',
        context={'quote': quote},
        name=quote.contact_name or 'there',
        quote_number=quote.quote_number,
    )


def notify_staff_new_quote(quote):
    """Tell the team a request is waiting, by email and optionally Slack."""
    staff_url = f"{settings.SITE_URL}{reverse('staff_quote_detail', args=[quote.pk])}"
    sent = send_branded_email(
        to=list(settings.STAFF_NOTIFY_EMAILS),
        key=EmailTemplate.STAFF_NEW_QUOTE,
        template='main/email/staff_new_quote.html',
        context={'quote': quote, 'staff_url': staff_url},
        name=quote.contact_name or 'a guest',
        quote_number=quote.quote_number,
    )
    post_to_slack(
        f'*New quote request* {quote.quote_number} from '
        f'{quote.contact_name or "a guest"} — ${quote.total:,.2f}\n{staff_url}'
    )
    return sent


def send_quote_to_customer(quote, request=None):
    """Email the approved quote with its PDF and one-click accept/decline links."""
    from .pdf import render_quote_pdf

    filename, content, mimetype = render_quote_pdf(quote, request=request)
    public_url = quote.public_url()
    return send_branded_email(
        to=quote.contact_email,
        key=EmailTemplate.QUOTE_SENT,
        template='main/email/quote_sent.html',
        context={
            'quote': quote,
            'items': quote.items.all(),
            'public_url': public_url,
            'accept_url': f'{public_url}?action=accept',
            'decline_url': f'{public_url}?action=decline',
        },
        attachments=[(filename, content, mimetype)],
        name=quote.contact_name or 'there',
        quote_number=quote.quote_number,
    )


def send_quote_accepted(quote):
    """Confirm an acceptance to the customer with the final quote attached.

    The PDF is rendered afresh, so it carries the Accepted stamp, and replaces
    the stored copy so the "Download a copy" link serves the final version too.
    """
    from .pdf import attach_pdf_to_quote

    filename, content, mimetype = attach_pdf_to_quote(quote)
    return send_branded_email(
        to=quote.contact_email,
        key=EmailTemplate.QUOTE_ACCEPTED,
        template='main/email/quote_accepted.html',
        context={'quote': quote, 'items': quote.items.all(), 'public_url': quote.public_url()},
        attachments=[(filename, content, mimetype)],
        name=quote.contact_name or 'there',
        quote_number=quote.quote_number,
    )


def notify_staff_quote_response(quote):
    """Tell the team the customer accepted or declined, by email and Slack."""
    accepted = quote.status == quote.ACCEPTED
    decision = 'accepted' if accepted else 'declined'
    staff_url = f"{settings.SITE_URL}{reverse('staff_quote_detail', args=[quote.pk])}"
    sent = send_branded_email(
        to=list(settings.STAFF_NOTIFY_EMAILS),
        key=EmailTemplate.STAFF_QUOTE_RESPONSE,
        template='main/email/staff_quote_response.html',
        context={'quote': quote, 'accepted': accepted, 'staff_url': staff_url},
        name=quote.contact_name or 'The customer',
        quote_number=quote.quote_number,
        decision=decision,
    )
    post_to_slack(
        f'✅ Quote {quote.quote_number} accepted — ${quote.total:,.2f}\n{staff_url}'
        if accepted
        else f'Quote {quote.quote_number} was declined.\n{staff_url}'
    )
    return sent


def send_payment_reminder(invoice):
    email = invoice.customer.contact_email if invoice.customer else ''
    return send_branded_email(
        to=email,
        key=EmailTemplate.PAYMENT_REMINDER,
        template='main/email/payment_reminder.html',
        context={'invoice': invoice},
        name=invoice.client,
        invoice_no=invoice.invoice_no or '(no number)',
    )


def post_to_slack(text):
    """Best-effort Slack ping; never lets a webhook failure break a request."""
    url = settings.SLACK_WEBHOOK_URL
    if not url:
        return False
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps({'text': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        urllib.request.urlopen(request, timeout=5).read()
        return True
    except Exception:
        logger.exception('Slack notification failed.')
        return False
