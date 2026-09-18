"""Bulk-import the historical "INKPRO Recharge Register" spreadsheet.

The register is a hand-maintained workbook, so this importer is deliberately
forgiving: it locates the header row anywhere in the first 20 rows, matches
columns by fuzzy name rather than fixed position, skips blank and summary rows,
and parses the money/date cells whether they arrive as real Excel values or as
text like "$1,234.50" or "12/03/2026".

Usage::

    python manage.py import_register "INKPRO Recharge Register 2026.xlsx"
    python manage.py import_register register.xlsx --sheet 2026 --dry-run
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main.models import Customer, Invoice

# Canonical field -> substrings that identify its column in the workbook.
COLUMN_PATTERNS = {
    'date': ['date'],
    'client': ['client', 'customer', 'company'],
    'job_details': ['recharge', 'job detail', 'job', 'details', 'description', 'particulars'],
    'qty': ['qty', 'quantity'],
    'invoice_no': ['invoice no', 'invoice #', 'inv no', 'invoice number'],
    'invoice_amount': ['invoice amount', 'invoice amt', 'amount invoiced', 'invoice value'],
    'receipt_no': ['receipt no', 'receipt #', 'receipt number'],
    'amount_received': ['amount received', 'received', 'amount paid', 'paid'],
    'balance': ['balance', 'outstanding'],
    'payment_status': ['payment status', 'status', 'paid?'],
    'payment_method': ['payment method', 'method', 'mode of payment', 'payment type'],
    'notes': ['notes', 'remarks', 'comment'],
}

STATUS_MAP = {
    'paid': Invoice.PAID,
    'fully paid': Invoice.PAID,
    'not paid': Invoice.NOT_PAID,
    'unpaid': Invoice.NOT_PAID,
    'no': Invoice.NOT_PAID,
    'partial': Invoice.PARTIAL,
    'part paid': Invoice.PARTIAL,
    'partially paid': Invoice.PARTIAL,
    'tbc': Invoice.TBC,
    'to be confirmed': Invoice.TBC,
    'pending': Invoice.TBC,
}

METHOD_MAP = {
    'cash': Invoice.CASH_CHQ,
    'cheque': Invoice.CASH_CHQ,
    'chq': Invoice.CASH_CHQ,
    'cash/chq': Invoice.CASH_CHQ,
    'cash / chq': Invoice.CASH_CHQ,
    'po': Invoice.PO_ON_ACCOUNT,
    'p.o': Invoice.PO_ON_ACCOUNT,
    'p.o.': Invoice.PO_ON_ACCOUNT,
    'on account': Invoice.PO_ON_ACCOUNT,
    'po on account': Invoice.PO_ON_ACCOUNT,
    'bank': Invoice.BANK_TRANSFER,
    'transfer': Invoice.BANK_TRANSFER,
    'bank transfer': Invoice.BANK_TRANSFER,
    'internet banking': Invoice.BANK_TRANSFER,
    'direct credit': Invoice.BANK_TRANSFER,
    'card': Invoice.CARD,
    'eftpos': Invoice.CARD,
    'credit card': Invoice.CARD,
    'other': Invoice.OTHER,
    'misc': Invoice.OTHER,
}

#: Rows whose client cell matches these are workbook summary lines, not data.
SUMMARY_MARKERS = re.compile(
    r'^\s*(total|totals|grand total|sub[- ]?total|summary|balance c/?f)\b', re.I
)


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def parse_decimal(value):
    """Read a money cell, tolerating ``$``, thousands separators and ``(123)``."""
    if value is None or value == '':
        return Decimal('0')
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = norm(value).replace('$', '').replace(',', '').replace(' ', '')
    if not text or text in {'-', '--'}:
        return Decimal('0')
    negative = text.startswith('(') and text.endswith(')')
    text = text.strip('()')
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal('0')
    return -amount if negative else amount


def parse_int(value):
    try:
        return int(parse_decimal(value))
    except (ValueError, TypeError):
        return 0


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = norm(value)
    if not text:
        return None
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d-%m-%Y', '%d %b %Y', '%d %B %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def map_choice(value, mapping, default):
    text = norm(value).lower().rstrip('.')
    if not text:
        return default
    if text in mapping:
        return mapping[text]
    for needle, mapped in mapping.items():
        if needle in text:
            return mapped
    return default


class Command(BaseCommand):
    help = 'Import the historical Recharge Register workbook into Invoice rows.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to the .xlsx workbook.')
        parser.add_argument('--sheet', help='Worksheet name (defaults to every sheet).')
        parser.add_argument(
            '--dry-run', action='store_true', help='Parse and report without writing.'
        )
        parser.add_argument(
            '--link-customers',
            action='store_true',
            help='Attach rows to an existing Customer when the client name matches exactly.',
        )

    def handle(self, *args, **options):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover
            raise CommandError('openpyxl is required: pip install openpyxl') from exc

        try:
            workbook = load_workbook(options['path'], data_only=True, read_only=True)
        except FileNotFoundError as exc:
            raise CommandError(f'Workbook not found: {options["path"]}') from exc

        sheets = [workbook[options['sheet']]] if options['sheet'] else list(workbook.worksheets)
        created = updated = skipped = 0

        with transaction.atomic():
            for sheet in sheets:
                rows = list(sheet.iter_rows(values_only=True))
                header_index, columns = self._find_header(rows)
                if header_index is None:
                    self.stdout.write(
                        self.style.WARNING(f'  {sheet.title}: no recognisable header, skipped.')
                    )
                    continue
                self.stdout.write(
                    f'  {sheet.title}: header on row {header_index + 1}, '
                    f'{len(columns)} columns matched.'
                )
                for row in rows[header_index + 1 :]:
                    result = self._import_row(row, columns, options)
                    if result == 'created':
                        created += 1
                    elif result == 'updated':
                        updated += 1
                    else:
                        skipped += 1
            if options['dry_run']:
                transaction.set_rollback(True)

        verb = 'Would import' if options['dry_run'] else 'Imported'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {created} new and {updated} updated invoice rows ({skipped} skipped).'
            )
        )

    def _find_header(self, rows):
        """Return ``(row_index, {field: column_index})`` for the best header row."""
        best = (None, {})
        for index, row in enumerate(rows[:20]):
            columns = {}
            for col_index, cell in enumerate(row):
                label = norm(cell).lower()
                if not label:
                    continue
                for field, patterns in COLUMN_PATTERNS.items():
                    if field in columns:
                        continue
                    if any(p in label for p in patterns):
                        columns[field] = col_index
                        break
            # A real header names the client and at least one money column.
            if len(columns) > len(best[1]) and 'client' in columns:
                best = (index, columns)
        return best if len(best[1]) >= 3 else (None, {})

    def _import_row(self, row, columns, options):
        def cell(field):
            index = columns.get(field)
            if index is None or index >= len(row):
                return None
            return row[index]

        client = norm(cell('client'))
        invoice_no = norm(cell('invoice_no'))
        job_details = norm(cell('job_details'))
        invoice_amount = parse_decimal(cell('invoice_amount'))
        amount_received = parse_decimal(cell('amount_received'))

        # Blank spacer rows and the workbook's own totals line are not data.
        if not any([client, invoice_no, job_details]) or (
            not invoice_amount and not amount_received and not job_details
        ):
            return 'skipped'
        if SUMMARY_MARKERS.match(client) or SUMMARY_MARKERS.match(job_details):
            return 'skipped'

        status_raw = cell('payment_status')
        derived_status = map_choice(status_raw, STATUS_MAP, None)

        fields = {
            'client_name': client,
            'job_details': job_details,
            'date': parse_date(cell('date')),
            'qty': parse_int(cell('qty')),
            'invoice_amount': invoice_amount,
            'receipt_no': norm(cell('receipt_no')),
            'amount_received': amount_received,
            'payment_method': map_choice(cell('payment_method'), METHOD_MAP, Invoice.CASH_CHQ),
            'notes': norm(cell('notes')),
        }

        # A spreadsheet status of TBC carries information the balance cannot
        # reproduce, so preserve it; everything else is re-derived on save.
        if derived_status == Invoice.TBC:
            fields['payment_status'] = Invoice.TBC
            fields['status_is_manual'] = True
        else:
            fields['status_is_manual'] = False

        if options['link_customers'] and client:
            fields['customer'] = Customer.objects.filter(company_name__iexact=client).first()

        if options['dry_run']:
            return 'created'

        # Invoice numbers are the register's natural key; rows without one are
        # always appended since there is nothing safe to match on.
        if invoice_no:
            invoice, created = Invoice.objects.update_or_create(
                invoice_no=invoice_no, defaults=fields
            )
            return 'created' if created else 'updated'
        Invoice.objects.create(invoice_no='', **fields)
        return 'created'
