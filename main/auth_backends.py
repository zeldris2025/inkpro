"""Authentication that recognises an existing account however it is typed.

Django's default backend matches the username exactly, so somebody who
registered as ``Ada`` cannot sign in as ``ada`` — they conclude the account
does not exist and register again, which is precisely the duplication we want
to avoid. Signup already rejects case-variant usernames and emails, so a
case-insensitive lookup here cannot introduce ambiguity for new accounts.

The backend also accepts the email address, because customers reliably
remember the address they receive their quotes at and often do not remember
which username they chose.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

logger = logging.getLogger(__name__)


class CaseInsensitiveUsernameOrEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if username is None or password is None:
            return None

        identifier = username.strip()
        matches = list(
            UserModel._default_manager.filter(
                Q(username__iexact=identifier) | Q(email__iexact=identifier)
            )[:2]
        )

        if len(matches) > 1:
            # Only reachable for accounts that predate the case-insensitive
            # signup checks. Fall back to an exact match rather than guessing
            # which of two lookalike accounts was meant.
            logger.warning('Ambiguous login identifier %r matched multiple users.', identifier)
            matches = list(UserModel._default_manager.filter(username=identifier)[:1])

        if not matches:
            # Run the hasher anyway so a missing account cannot be detected by
            # how quickly the request comes back.
            UserModel().set_password(password)
            return None

        user = matches[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
