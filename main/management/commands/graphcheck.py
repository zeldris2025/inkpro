"""Verify the Microsoft 365 / Graph email credentials.

Run this the moment the Azure app registration details land in ``.env`` — it
fails loudly here rather than silently swallowing a customer's quote:

    python manage.py graphcheck
    python manage.py graphcheck --to you@example.com      # also sends a real email
    python manage.py graphcheck --from other@domain.com   # try another mailbox
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from main.graph_mail import GraphEmailBackend, GraphError, address_only, graph_get

BACKEND_PATH = 'main.graph_mail.GraphEmailBackend'


class Command(BaseCommand):
    help = 'Check the Microsoft Graph email credentials, and optionally send a test message.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            help='Send a test message to this address once the token check passes.',
        )
        parser.add_argument(
            '--from',
            dest='sender',
            help='Send as this mailbox instead of MS_GRAPH_SENDER, without editing the '
                 'environment. Useful for finding which address Graph accepts.',
        )

    def handle(self, *args, **options):
        backend = GraphEmailBackend(sender=options.get('sender'))

        missing = backend.missing_settings()
        if missing:
            raise CommandError(
                'Missing credentials: ' + ', '.join(missing) + '. Add them to .env '
                '(or the App Service configuration) and run this again.'
            )

        mailbox = backend.mailbox_for(_Blank())
        if not mailbox:
            raise CommandError('Set MS_GRAPH_SENDER to the mailbox that should send.')

        self.stdout.write(f'Tenant:  {backend.tenant_id}')
        self.stdout.write(f'Client:  {backend.client_id}')
        self.stdout.write(f'Mailbox: {mailbox}')

        try:
            backend.get_token(force_refresh=True)
        except GraphError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS('  PASS  Acquired an app-only access token.'))

        if settings.EMAIL_BACKEND != BACKEND_PATH:
            self.stdout.write(
                self.style.WARNING(
                    f'  WARN  EMAIL_BACKEND is {settings.EMAIL_BACKEND}, not {BACKEND_PATH} — '
                    'the site is not actually sending through Graph yet.'
                )
            )

        from_address = address_only(settings.DEFAULT_FROM_EMAIL)
        if from_address and mailbox and from_address.lower() != mailbox.lower():
            self.stdout.write(
                self.style.WARNING(
                    f'  WARN  DEFAULT_FROM_EMAIL is {from_address} but messages will be sent '
                    f'from {mailbox}; recipients will see the mailbox address.'
                )
            )

        recipient = options.get('to')
        if not recipient:
            self.stdout.write('')
            self.stdout.write('Credentials look good. Re-run with --to you@example.com to send a test.')
            return

        from django.core.mail import EmailMultiAlternatives

        message = EmailMultiAlternatives(
            subject='InkPro: Microsoft Graph test message',
            body='If you are reading this, InkPro can send email through Microsoft 365.',
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
            connection=backend,
        )
        message.attach_alternative(
            '<p>If you are reading this, InkPro can send email through '
            '<strong>Microsoft 365</strong>.</p>',
            'text/html',
        )
        try:
            message.send(fail_silently=False)
        except GraphError as exc:
            self.explain(str(exc), mailbox, backend)
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'  PASS  Test message sent to {recipient}.'))

    # -- diagnosis ----------------------------------------------------------
    def explain(self, error, mailbox, backend):
        """Turn Graph's terse refusal into the thing to go and check.

        A token proves the app registration is valid; it says nothing about
        whether *this mailbox* exists, is licensed, or is one the app may send
        as. Those are the failures worth naming, because Graph's own wording
        ("The requested user is invalid") points at none of them.
        """
        hints = []
        if 'ErrorInvalidUser' in error or 'ResourceNotFound' in error or 'Invalid user' in error:
            hints = [
                f'Graph cannot resolve {mailbox} to a mailbox in this tenant. In order of'
                ' likelihood:',
                '  1. It is an alias, not the account\'s primary address. Graph resolves'
                ' /users/ by User Principal Name or object ID only — a secondary proxy'
                ' address fails exactly like this. Microsoft 365 admin centre > Users >'
                ' Active users > the account > the *Username* field is the UPN to use.',
                '  2. The account has no Exchange Online licence, so it has no mailbox.'
                ' An unlicensed user object is "invalid" to sendMail.',
                '  3. It is a distribution list or a Microsoft 365 Group. Neither can'
                ' send; a shared mailbox can.',
                f'  4. The domain of {mailbox} belongs to a different tenant than'
                f' MS_GRAPH_TENANT_ID ({backend.tenant_id}).',
            ]
        elif 'MailboxNotEnabledForRESTAPI' in error:
            hints = [
                f'{mailbox} exists but is not reachable over Graph — usually an on-premises'
                ' or hybrid mailbox that has not been migrated to Exchange Online.',
            ]
        elif 'ErrorAccessDenied' in error or 'Access is denied' in error:
            hints = [
                'Authentication succeeded but sending was refused. Either the Mail.Send'
                ' *application* permission has not been granted admin consent, or an'
                ' Exchange ApplicationAccessPolicy excludes this mailbox from the app.',
            ]
        for line in hints:
            self.stdout.write(self.style.WARNING(line))

        # Naming the mailbox Graph does know about ends the guessing, but the
        # lookup needs a directory permission the app may not have been given.
        try:
            found = graph_get(f'users/{mailbox}', backend.get_token(), backend.timeout)
        except GraphError as lookup_error:
            if '403' in str(lookup_error):
                self.stdout.write(
                    '  (Add the User.Read.All application permission to have this command '
                    'look the mailbox up and report its real address.)'
                )
            return
        self.stdout.write('')
        self.stdout.write('Graph does know this account:')
        self.stdout.write(f"  userPrincipalName: {found.get('userPrincipalName')}")
        self.stdout.write(f"  mail:              {found.get('mail')}")
        self.stdout.write(
            '  Use the userPrincipalName as MS_GRAPH_SENDER; "mail" may be an alias.'
        )


class _Blank:
    """Stand-in message so mailbox_for() can resolve without a real email."""

    from_email = ''
