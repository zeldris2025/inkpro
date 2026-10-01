"""Celery tasks for the slow parts of the quote flow.

With no ``CELERY_BROKER_URL`` configured the app still works: ``run_task``
falls back to calling the function inline, so a fresh checkout sends email and
builds PDFs without Redis or a worker running. Point the setting at a broker
and the same call sites start dispatching asynchronously.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on celery being installed
    from inkpro.celery import app as celery_app

    CELERY_READY = True
except Exception:  # pragma: no cover
    celery_app = None
    CELERY_READY = False


def _task(func):
    """Register *func* with Celery when available, leaving it callable directly."""
    if CELERY_READY:
        return celery_app.task(name=f'main.{func.__name__}', bind=False)(func)
    return func


def run_task(task, *args, **kwargs):
    """Dispatch *task* to a worker, or run it inline when there is no broker.

    Email and PDF work must never take a customer-facing request down with it,
    so failures are logged and swallowed in the inline path.
    """
    if CELERY_READY and settings.CELERY_BROKER_URL:
        return task.delay(*args, **kwargs)
    try:
        return task(*args, **kwargs)
    except Exception:
        logger.exception('Inline task %s failed.', getattr(task, '__name__', task))
        return None


@_task
def send_quote_received_task(quote_id):
    from .emails import send_quote_received
    from .models import Quote

    return send_quote_received(Quote.objects.get(pk=quote_id))


@_task
def notify_staff_new_quote_task(quote_id):
    from .emails import notify_staff_new_quote
    from .models import Quote

    return notify_staff_new_quote(Quote.objects.get(pk=quote_id))


@_task
def send_quote_to_customer_task(quote_id):
    from .emails import send_quote_to_customer
    from .models import Quote

    return send_quote_to_customer(Quote.objects.get(pk=quote_id))


@_task
def send_quote_accepted_task(quote_id):
    from .emails import send_quote_accepted
    from .models import Quote

    return send_quote_accepted(Quote.objects.get(pk=quote_id))


@_task
def notify_staff_quote_response_task(quote_id):
    from .emails import notify_staff_quote_response
    from .models import Quote

    return notify_staff_quote_response(Quote.objects.get(pk=quote_id))


@_task
def build_quote_pdf_task(quote_id):
    from .models import Quote
    from .pdf import attach_pdf_to_quote

    return bool(attach_pdf_to_quote(Quote.objects.get(pk=quote_id)))


@_task
def send_payment_reminders_task(dry_run=False):
    """Daily: remind every overdue client we can reach, then brief the office.

    A client is reminded at most once a week per invoice (Invoice.reminder_due).
    The office summary lists everything overdue, including invoices with no
    email on file, which no automatic reminder can reach.
    """
    from .emails import notify_staff_overdue, send_payment_reminder
    from .models import Invoice

    overdue = [
        invoice
        for invoice in Invoice.objects.exclude(payment_status__in=[Invoice.PAID, Invoice.TBC])
        .filter(balance__gt=0)
        .select_related('customer__user')
        if invoice.is_overdue
    ]
    reminded = []
    for invoice in overdue:
        if not invoice.reminder_due:
            continue
        if dry_run:
            reminded.append(invoice)
            continue
        try:
            if send_payment_reminder(invoice):
                reminded.append(invoice)
        except Exception:
            logger.exception('Payment reminder for invoice %s failed.', invoice.pk)
    if not dry_run:
        try:
            notify_staff_overdue(overdue, reminded)
        except Exception:
            logger.exception('Overdue summary to staff failed.')
    logger.info('Sent %s payment reminders; %s invoices overdue.', len(reminded), len(overdue))
    return {'overdue': overdue, 'reminded': reminded}
