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
def build_quote_pdf_task(quote_id):
    from .models import Quote
    from .pdf import attach_pdf_to_quote

    return bool(attach_pdf_to_quote(Quote.objects.get(pk=quote_id)))


@_task
def send_payment_reminders_task():
    """Periodic task: nudge every overdue invoice that has a contactable customer."""
    from .emails import send_payment_reminder
    from .models import Invoice

    overdue = (
        Invoice.objects.exclude(payment_status__in=[Invoice.PAID, Invoice.TBC])
        .filter(customer__isnull=False)
        .select_related('customer')
    )
    sent = 0
    for invoice in overdue:
        if invoice.is_overdue and send_payment_reminder(invoice):
            sent += 1
    logger.info('Sent %s payment reminders.', sent)
    return sent
