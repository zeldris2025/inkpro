"""Group definitions and access helpers.

Three groups mirror the three audiences: ``Customer`` (own quotes only),
``Staff`` (quote review, register, dashboard) and ``Owner`` (everything,
including pricing and user management). ``is_staff`` on the user model is the
single gate the staff views check, so a superuser always gets through.
"""

from functools import wraps

from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied

CUSTOMER_GROUP = 'Customer'
STAFF_GROUP = 'Staff'
OWNER_GROUP = 'Owner'

#: Model permissions granted to each group by ``bootstrap_groups``.
GROUP_PERMISSIONS = {
    CUSTOMER_GROUP: [],
    STAFF_GROUP: [
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
    ],
    OWNER_GROUP: '__all__',
}


def in_group(user, name):
    return user.is_authenticated and user.groups.filter(name=name).exists()


def is_inkpro_staff(user):
    """True for Django staff/superusers and anyone in the Staff or Owner group."""
    return user.is_authenticated and (
        user.is_staff or user.is_superuser or in_group(user, STAFF_GROUP) or in_group(user, OWNER_GROUP)
    )


def is_owner(user):
    return user.is_authenticated and (user.is_superuser or in_group(user, OWNER_GROUP))


staff_required = user_passes_test(is_inkpro_staff)


def owner_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_owner(request.user):
            raise PermissionDenied('Owner access required.')
        return view(request, *args, **kwargs)

    return wrapper
