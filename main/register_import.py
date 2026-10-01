"""Read the Recharge Register spreadsheet into Invoice rows.

Shared by the "Import CSV" screen on the register page and the
``import_register`` management command. The register is a hand-kept workbook,
so parsing is forgiving: the header row is found anywhere near the top,
columns are matched by name rather than position, blank and summary rows are
skipped, and money and dates are read in the forms the sheet actually uses
("$1,840.50", "2,766.00 tala", "29-Aug-26", "02-Sep-2026").

Importing is repeatable. Each row is matched to an existing entry, by invoice
number when it has one and otherwise by date, client, job and amount, so
importing an updated copy of the sheet updates the rows it already has (a
payment received since, say) instead of adding them twice.

The sheet's own Balance and Payment Status columns are not copied: the
register works both out from the amounts. Where the sheet disagrees with that,
the row carries a warning so the difference is visible before importing.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q

from .models import Customer, Invoice

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
    'email': ['email', 'e-mail'],
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

DATE_FORMATS = (
    '%d-%b-%y', '%d-%b-%Y', '%d-%B-%Y', '%d %b %Y', '%d %B %Y', '%d %b %y',
    '%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d-%m-%Y', '%d.%m.%Y',
)

#: Largest CSV the web importer accepts. The whole 2026 sheet is about 3 KB.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


class RegisterImportError(Exception):
    """The file cannot be read as a register at all."""


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def parse_decimal(value):
    """Read a money cell: ``$1,840.50``, ``2,766.00 tala``, ``WS$90``, ``(123)``."""
    if value is None or value == '':
        return Decimal('0')
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = norm(value)
    negative = text.startswith('(') and text.endswith(')') or text.startswith('-')
    digits = re.sub(r'[^0-9.]', '', text)
    if not digits or digits == '.':
        return Decimal('0')
    try:
        amount = Decimal(digits)
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
    for fmt in DATE_FORMATS:
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


def find_header(rows):
    """Return ``(row_index, {field: column_index})`` for the best header row."""
    best = (None, {})
    for index, row in enumerate(rows[:25]):
        columns = {}
        for col_index, cell in enumerate(row):
            label = norm(cell).lower()
            if not label:
                continue
            for name, patterns in COLUMN_PATTERNS.items():
                if name in columns:
                    continue
                if any(p in label for p in patterns):
                    columns[name] = col_index
                    break
        # A real header names the client and at least one money column.
        if len(columns) > len(best[1]) and 'client' in columns:
            best = (index, columns)
    return best if len(best[1]) >= 3 else (None, {})


def read_csv(data):
    """Decode an uploaded CSV. Excel writes UTF-8 with a BOM, or Windows-1252."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise RegisterImportError('That file is over 2 MB — is it the right one?')
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise RegisterImportError('The file could not be read as text.')
    return list(csv.reader(io.StringIO(text)))


def match_customer(client, email=''):
    """The customer account behind a register client, if one exists.

    Matched on the email first, then on the client name against the company,
    the customer's name or the account holder's full name. Only an exact,
    unambiguous match counts: linking a debt to the wrong person is worse
    than not linking it.
    """
    if email:
        by_email = Customer.objects.filter(
            Q(email__iexact=email) | Q(user__email__iexact=email)
        ).distinct()
        if by_email.count() == 1:
            return by_email.first()
    if not client:
        return None
    candidates = [c for c in Customer.objects.select_related('user') if _names(c) & {client.lower()}]
    return candidates[0] if len(candidates) == 1 else None


def _names(customer):
    names = {customer.company_name, customer.name}
    if customer.user:
        names.add(customer.user.get_full_name())
    return {norm(n).lower() for n in names if norm(n)}


@dataclass
class ParsedRow:
    line: int
    fields: dict
    email: str = ''
    sheet_balance: Decimal | None = None
    sheet_status: str | None = None
    warnings: list = field(default_factory=list)
    action: str = 'create'  # create | update | unchanged
    existing: Invoice | None = None
    customer: Customer | None = None

    @property
    def balance(self):
        return self.fields['invoice_amount'] - self.fields['amount_received']

    @property
    def status(self):
        if self.fields.get('status_is_manual'):
            return self.fields['payment_status']
        probe = Invoice(
            invoice_amount=self.fields['invoice_amount'],
            amount_received=self.fields['amount_received'],
        )
        return probe.derive_payment_status()

    def get_status_display(self):
        return dict(Invoice.PAYMENT_STATUS_CHOICES)[self.status]


@dataclass
class ParseResult:
    rows: list
    skipped: int
    header_line: int

    def count(self, action):
        return sum(1 for row in self.rows if row.action == action)

    @property
    def warnings(self):
        return [row for row in self.rows if row.warnings]


def parse(rows):
    """Turn spreadsheet rows into ParsedRows, matched against the register."""
    header_index, columns = find_header(rows)
    if header_index is None:
        raise RegisterImportError(
            'Couldn’t find the header row. The file needs columns such as Date, '
            'Client, Invoice Amount and Amount Received.'
        )
    if 'invoice_amount' not in columns:
        raise RegisterImportError('Couldn’t find an “Invoice Amount” column.')

    parsed, skipped = [], 0
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        def cell(name):
            index = columns.get(name)
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
            skipped += 1
            continue
        if SUMMARY_MARKERS.match(client) or SUMMARY_MARKERS.match(job_details):
            skipped += 1
            continue

        sheet_status = map_choice(cell('payment_status'), STATUS_MAP, None)
        fields = {
            'client_name': client,
            'job_details': job_details,
            'date': parse_date(cell('date')),
            'qty': parse_int(cell('qty')),
            'invoice_no': invoice_no,
            'invoice_amount': invoice_amount,
            'receipt_no': norm(cell('receipt_no')),
            'amount_received': amount_received,
            'payment_method': map_choice(cell('payment_method'), METHOD_MAP, ''),
            'notes': norm(cell('notes')),
        }
        # A TBC status carries information the amounts cannot reproduce, so it
        # is kept; every other status is re-derived from the amounts on save.
        if sheet_status == Invoice.TBC:
            fields['payment_status'] = Invoice.TBC
            fields['status_is_manual'] = True
        else:
            fields['status_is_manual'] = False

        result = ParsedRow(
            line=offset,
            fields=fields,
            email=norm(cell('email')).lower(),
            sheet_status=sheet_status,
        )
        raw_balance = cell('balance')
        if norm(raw_balance):
            result.sheet_balance = parse_decimal(raw_balance)
        _check(result, cell('date'))
        _match(result)
        parsed.append(result)
    return ParseResult(rows=parsed, skipped=skipped, header_line=header_index + 1)


def _check(row, raw_date):
    if norm(raw_date) and row.fields['date'] is None:
        row.warnings.append(f'Date “{norm(raw_date)}” wasn’t recognised, so it was left blank.')
    if row.fields['amount_received'] > row.fields['invoice_amount'] > 0:
        row.warnings.append('More was received than invoiced.')
    if row.sheet_balance is not None and row.sheet_balance != row.balance:
        row.warnings.append(
            f'The sheet says the balance is ${row.sheet_balance:,.2f}; from the amounts it is '
            f'${row.balance:,.2f}. The register will show ${row.balance:,.2f}.'
        )
    if row.sheet_status and row.sheet_status != Invoice.TBC and row.sheet_status != row.status:
        sheet = dict(Invoice.PAYMENT_STATUS_CHOICES)[row.sheet_status]
        row.warnings.append(
            f'The sheet says “{sheet}”, but the amounts make it “{row.get_status_display()}”.'
        )


def _match(row):
    """Find the register entry this row describes, and whether it changed."""
    fields = row.fields
    existing = None
    if fields['invoice_no']:
        existing = Invoice.objects.filter(invoice_no__iexact=fields['invoice_no']).first()
    if existing is None:
        existing = Invoice.objects.filter(
            date=fields['date'],
            client_name__iexact=fields['client_name'],
            job_details__iexact=fields['job_details'],
            invoice_amount=fields['invoice_amount'],
        ).first()
    row.customer = match_customer(fields['client_name'], row.email)
    if existing is None:
        row.action = 'create'
        return
    row.existing = existing
    changed = any(
        getattr(existing, name) != value
        for name, value in fields.items()
        # A method staff filled in by hand is not wiped by a blank cell.
        if not (name == 'payment_method' and not value)
    )
    if row.email and row.email != existing.client_email:
        changed = True
    row.action = 'update' if changed else 'unchanged'


@transaction.atomic
def apply(result):
    """Write the parsed rows. Returns ``{'created': n, 'updated': n, 'unchanged': n}``."""
    counts = {'created': 0, 'updated': 0, 'unchanged': 0}
    for row in result.rows:
        if row.action == 'unchanged':
            counts['unchanged'] += 1
            continue
        fields = dict(row.fields)
        if row.email:
            fields['client_email'] = row.email
        if row.action == 'update':
            invoice = row.existing
            if not fields['payment_method']:
                fields.pop('payment_method')
            for name, value in fields.items():
                setattr(invoice, name, value)
            if invoice.customer is None and row.customer is not None:
                invoice.customer = row.customer
            invoice.save()
            counts['updated'] += 1
        else:
            Invoice.objects.create(customer=row.customer, **fields)
            counts['created'] += 1
    return counts
