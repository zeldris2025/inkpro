"""Email payment reminders for overdue invoices, and the office a summary.

Celery beat runs this every weekday morning when a task broker is configured.
Without one (the usual Azure setup), schedule this command instead — an Azure
WebJob, or any cron — or use "Send reminders" on the register page::

    python manage.py send_payment_reminders
    python manage.py send_payment_reminders --dry-run
"""

from django.core.management.base import BaseCommand

from main.tasks import send_payment_reminders_task


class Command(BaseCommand):
    help = 'Remind clients with overdue invoices and email staff the overdue summary.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', help='List what would be sent without sending.'
        )

    def handle(self, *args, **options):
        result = send_payment_reminders_task(dry_run=options['dry_run'])
        verb = 'Would remind' if options['dry_run'] else 'Reminded'
        for invoice in result['overdue']:
            mark = '✓' if invoice in result['reminded'] else ' '
            reach = invoice.contact_email or 'NO EMAIL ON FILE'
            self.stdout.write(
                f' {mark} {invoice.client} — ${invoice.balance:,.2f}, '
                f'{invoice.days_overdue} days overdue — {reach}'
            )
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {len(result["reminded"])} of {len(result["overdue"])} overdue invoices.'
            )
        )
