"""Turning a checked application into a working customer login.

Approval is the only way a customer account comes into being. It creates the
user, links a verified Customer record, and emails the sign-in details. The
password it sends is temporary: the customer is made to replace it on first
sign-in, so the emailed copy stops working as soon as they have used it.
"""

import re
import secrets
import unicodedata

from django.contrib.auth.models import Group, User
from django.db import transaction
from django.utils import timezone

from .models import Customer, CustomerApplication
from .permissions import CUSTOMER_GROUP

#: No 0/O, 1/l/I: the password is read from an email and typed by hand.
PASSWORD_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'


class ApplicationError(Exception):
    """The application cannot be actioned in its current state."""


def generate_temporary_password(groups=3, size=4):
    """e.g. ``Kp7m-Qx3r-Tz9w``: about 70 bits, and easy to copy correctly."""
    return '-'.join(
        ''.join(secrets.choice(PASSWORD_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def generate_username(first_name, last_name):
    """``firstname.lastname``, ASCII-folded, with a number added if taken."""

    def fold(value):
        ascii_only = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
        return re.sub(r'[^a-z0-9]+', '', ascii_only.lower())

    base = '.'.join(part for part in (fold(first_name), fold(last_name)) if part) or 'customer'
    base = base[:140]
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        candidate = f'{base}{n}'
    return candidate


def approve_application(application, reviewer):
    """Create the login, verify the customer, and email the sign-in details.

    Runs in one transaction with the email send: if the message cannot be
    delivered, nothing is created, so an approved customer can never be left
    with an account they were not told about.
    """
    from .emails import send_account_approved

    with transaction.atomic():
        application = CustomerApplication.objects.select_for_update().get(pk=application.pk)
        if application.status != CustomerApplication.PENDING:
            raise ApplicationError(f'This application is already {application.get_status_display().lower()}.')
        if User.objects.filter(email__iexact=application.email).exists():
            raise ApplicationError('An account with this email address already exists.')

        password = generate_temporary_password()
        user = User.objects.create_user(
            username=generate_username(application.first_name, application.last_name),
            email=application.email.lower(),
            password=password,
            first_name=application.first_name,
            last_name=application.last_name,
        )
        group, _ = Group.objects.get_or_create(name=CUSTOMER_GROUP)
        user.groups.add(group)
        customer = Customer.objects.create(
            user=user,
            name=application.full_name,
            email=user.email,
            phone=application.phone,
            is_verified=True,
            must_change_password=True,
        )
        application.status = CustomerApplication.APPROVED
        application.customer = customer
        application.reviewed_by = reviewer
        application.reviewed_at = timezone.now()
        application.save(update_fields=['status', 'customer', 'reviewed_by', 'reviewed_at'])

        # Sent inside the transaction and never queued: a task broker would
        # otherwise hold the plain-text password.
        send_account_approved(application, username=user.username, password=password)
    return user


def reject_application(application, reviewer, reason=''):
    from .emails import send_application_rejected

    with transaction.atomic():
        application = CustomerApplication.objects.select_for_update().get(pk=application.pk)
        if application.status != CustomerApplication.PENDING:
            raise ApplicationError(f'This application is already {application.get_status_display().lower()}.')
        application.status = CustomerApplication.REJECTED
        application.rejection_reason = reason.strip()
        application.reviewed_by = reviewer
        application.reviewed_at = timezone.now()
        application.save(update_fields=['status', 'rejection_reason', 'reviewed_by', 'reviewed_at'])
        send_application_rejected(application)
    return application
