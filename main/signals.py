"""Staff alerts that must fire however a quote comes into being.

A quote can be created through the website's quote builder, in Django admin,
by a script or by a management command. Hanging the alert on the model rather
than on one view means staff hear about every new request, whichever way it
arrived.
"""

import threading
from contextlib import contextmanager

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Quote

#: Statuses that are not a request staff need to act on: a basket still being
#: filled in, or an abandoned one being tidied away.
NOT_A_REQUEST = {Quote.DRAFT, Quote.EXPIRED}

_state = threading.local()


@contextmanager
def new_quote_alerts_suppressed():
    """For bulk jobs such as demo data, which would otherwise email per quote."""
    previous = getattr(_state, 'suppressed', False)
    _state.suppressed = True
    try:
        yield
    finally:
        _state.suppressed = previous


@receiver(post_save, sender=Quote)
def alert_staff_to_new_quote(sender, instance, created, raw=False, **kwargs):
    previous = instance._saved_status
    instance._saved_status = instance.status

    if raw or getattr(_state, 'suppressed', False):
        return
    if instance.status in NOT_A_REQUEST:
        return
    # Brand new and already a request (admin, scripts), or a draft that has
    # just been submitted (the quote builder). Anything else has alerted before.
    if not (created or previous in (None, Quote.DRAFT)):
        return

    from .tasks import notify_staff_new_quote_task, run_task

    pk = instance.pk
    # After commit: in admin the line items are saved after the quote itself,
    # in the same transaction, and the alert should show them and the total.
    transaction.on_commit(lambda: run_task(notify_staff_new_quote_task, pk))
