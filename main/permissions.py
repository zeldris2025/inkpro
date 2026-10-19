"""Group definitions and access helpers.

Four groups mirror the audiences: ``Customer`` (own quotes only), ``Staff``
(write quotes for review, register, dashboard), ``Manager`` (everything Staff
can do, plus approving quotes and downloading their PDFs) and ``Owner``
(everything, including pricing and user management). ``is_staff`` on the user
model is the single gate the staff views check, so a superuser always gets
through. Owners and superusers count as managers.
"""

from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.db.models import Q

CUSTOMER_GROUP = 'Customer'
STAFF_GROUP = 'Staff'
MANAGER_GROUP = 'Manager'
OWNER_GROUP = 'Owner'

_STAFF_PERMISSIONS = [
    'view_quote',
    'change_quote',
    'add_quoteitem',
    'change_quoteitem',
    'delete_quoteitem',
    'view_quoteitem',
    'add_invoice',
    'change_invoice',
    'view_invoice',
    'view_customer',
    'change_customer',
    'view_servicecategory',
    'view_pricingrule',
]

#: Model permissions granted to each group by ``bootstrap_groups``.
GROUP_PERMISSIONS = {
    CUSTOMER_GROUP: [],
    STAFF_GROUP: _STAFF_PERMISSIONS,
    # Approval is a portal action, not a model permission, so a manager needs
    # no more model access than staff.
    MANAGER_GROUP: _STAFF_PERMISSIONS,
    OWNER_GROUP: '__all__',
}


def in_group(user, name):
    return user.is_authenticated and user.groups.filter(name=name).exists()


def is_inkpro_staff(user):
    """True for Django staff/superusers and anyone in a staff-side group."""
    return user.is_authenticated and (
        user.is_staff
        or user.is_superuser
        or user.groups.filter(name__in=[STAFF_GROUP, MANAGER_GROUP, OWNER_GROUP]).exists()
    )


def is_owner(user):
    return user.is_authenticated and (user.is_superuser or in_group(user, OWNER_GROUP))


def is_manager(user):
    """True for anyone who may approve quotes: managers, owners, superusers."""
    return user.is_authenticated and (
        user.is_superuser or user.groups.filter(name__in=[MANAGER_GROUP, OWNER_GROUP]).exists()
    )


def manager_emails(exclude=None):
    """Addresses of everyone who can approve a quote, minus *exclude* (a user)."""
    users = get_user_model().objects.filter(
        Q(is_superuser=True) | Q(groups__name__in=[MANAGER_GROUP, OWNER_GROUP]),
        is_active=True,
    ).exclude(email='')
    if exclude is not None and exclude.pk:
        users = users.exclude(pk=exclude.pk)
    return sorted(set(users.values_list('email', flat=True)))


staff_required = user_passes_test(is_inkpro_staff)


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_owner(request.user):
            raise PermissionDenied('Owner access required.')
        return view(request, *args, **kwargs)

    return wrapper
