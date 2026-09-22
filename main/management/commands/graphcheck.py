"""Verify the Microsoft 365 / Graph email credentials.

Run this the moment the Azure app registration details land in ``.env`` — it
fails loudly here rather than silently swallowing a customer's quote:

    python manage.py graphcheck
    python manage.py graphcheck --to you@inkpro.ws    # also sends a real email
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from main.graph_mail import GraphEmailBackend, GraphError, address_only

BACKEND_PATH = 'main.graph_mail.GraphEmailBackend'


class Command(BaseCommand):
    help = 'Check the Microsoft Graph email credentials, and optionally send a test message.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            help='Send a test message to this address once the token check passes.',
        )

    def handle(self, *args, **options):
        backend = GraphEmailBackend()

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
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'  PASS  Test message sent to {recipient}.'))


class _Blank:
    """Stand-in message so mailbox_for() can resolve without a real email."""

    from_email = ''
