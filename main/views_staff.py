"""Staff portal: quote review inbox, the approve-and-send flow and the register."""

import csv
import io
from datetime import timedelta

from django.contrib import messages
from django.db.models import Case, Count, IntegerField, Q, Sum, Value, When
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    InvoiceForm,
    StaffQuoteForm,
    StaffQuoteItemAddForm,
    StaffQuoteItemFormSet,
)
from .models import Invoice, InvalidTransition, Quote
from .pdf import attach_pdf_to_quote
from .permissions import staff_required
from .tasks import run_task, send_quote_to_customer_task
from .views import create_invoice_for_quote

#: Columns of the register, in the spreadsheet's own order.
REGISTER_COLUMNS = [
    ('date', 'Date'),
    ('client', 'Client'),
    ('job_details', 'Recharge / Job Details'),
    ('qty', 'Qty'),
    ('invoice_no', 'Invoice No.'),
    ('invoice_amount', 'Invoice Amount'),
    ('receipt_no', 'Receipt No.'),
    ('amount_received', 'Amount Received'),
    ('balance', 'Balance'),
    ('payment_status', 'Payment Status'),
    ('payment_method', 'Payment Method'),
    ('notes', 'Notes'),
]


@staff_required
def dashboard(request):
    """Staff home — KPI cards plus the Chart.js canvases fed by the API."""
    money = Invoice.objects.aggregate(
        invoiced=Sum('invoice_amount'), received=Sum('amount_received'), outstanding=Sum('balance')
    )
    status_counts = {
        row['payment_status']: row['n']
        for row in Invoice.objects.values('payment_status').annotate(n=Count('id'))
    }
    return render(
        request,
        'main/staff/dashboard.html',
        {
            'kpi': {
                'invoiced': money['invoiced'] or 0,
                'received': money['received'] or 0,
                'outstanding': money['outstanding'] or 0,
                'paid_count': status_counts.get(Invoice.PAID, 0),
                'unpaid_count': status_counts.get(Invoice.NOT_PAID, 0)
                + status_counts.get(Invoice.TBC, 0)
                + status_counts.get(Invoice.PARTIAL, 0),
            },
            'awaiting': Quote.objects.awaiting_staff().count(),
            'recent_quotes': Quote.objects.exclude(status=Quote.DRAFT)[:8],
            'recent_payments': Invoice.objects.filter(amount_received__gt=0).order_by('-pk')[:6],
            'overdue': [i for i in Invoice.objects.exclude(payment_status=Invoice.PAID)[:50] if i.is_overdue][:6],
        },
    )


#: Kanban columns, left to right. Accepted and declined sit at the end so the
#: team can see answers come in and reopen a quote for a revised send.
BOARD_COLUMNS = [
    Quote.SUBMITTED, Quote.IN_REVIEW, Quote.APPROVED, Quote.SENT, Quote.ACCEPTED, Quote.DECLINED,
]


@staff_required
def quote_inbox(request):
    """Filterable list / kanban of everything in the pipeline."""
    status = request.GET.get('status', '')
    search = request.GET.get('q', '').strip()
    quotes = Quote.objects.exclude(status=Quote.DRAFT).select_related('customer', 'reviewed_by')
    if status:
        quotes = quotes.filter(status=status)
    if search:
        quotes = quotes.filter(
            Q(quote_number__icontains=search)
            | Q(guest_name__icontains=search)
            | Q(guest_email__icontains=search)
            | Q(customer__company_name__icontains=search)
        )

    board = {
        key: list(
            Quote.objects.filter(status=key).select_related('customer')[:25]
        )
        for key in BOARD_COLUMNS
    }
    return render(
        request,
        'main/staff/quote_inbox.html',
        {
            'quotes': quotes[:200],
            'board': [
                (key, dict(Quote.STATUS_CHOICES)[key], board[key]) for key in BOARD_COLUMNS
            ],
            'status': status,
            'search': search,
            'statuses': Quote.STATUS_CHOICES,
            'view_mode': request.GET.get('view', 'board'),
        },
    )


@staff_required
def quote_detail(request, pk):
    """Review screen — edit lines and money, then approve and send."""
    quote = get_object_or_404(Quote.objects.select_related('customer'), pk=pk)
    item_queryset = quote.items.all()
    form = StaffQuoteForm(instance=quote)
    formset = StaffQuoteItemFormSet(queryset=item_queryset)
    add_form = StaffQuoteItemAddForm()

    if request.method == 'POST' and 'save_quote' in request.POST:
        if not quote.is_editable_by_staff:
            messages.error(request, 'A draft quote belongs to the customer until they submit it.')
            return redirect('staff_quote_detail', pk=pk)
        reopening = quote.edit_reopens

        form = StaffQuoteForm(request.POST, instance=quote)
        formset = StaffQuoteItemFormSet(request.POST, queryset=item_queryset)
        if form.is_valid() and formset.is_valid():
            form.save()
            for item in formset.save(commit=False):
                item.quote = quote
                item.save()
            for item in formset.deleted_objects:
                item.delete()
            quote.refresh_from_db()
            quote.recalculate()
            if quote.status == Quote.SUBMITTED or reopening:
                quote.transition_to(Quote.IN_REVIEW, user=request.user)
            if reopening:
                messages.success(
                    request,
                    f'Saved as revision {quote.revision}. The customer’s link is paused '
                    'until you send the revised quote.',
                )
            else:
                messages.success(request, 'Quote updated.')
            return redirect('staff_quote_detail', pk=pk)
        messages.error(request, 'Please fix the highlighted fields.')

    return render(
        request,
        'main/staff/quote_detail.html',
        {
            'quote': quote,
            'form': form,
            'formset': formset,
            'add_form': add_form,
            'items': item_queryset,
            # Templates cannot call can_transition_to() with an argument, so the
            # legal next steps are resolved here.
            'allowed_transitions': [
                (value, label)
                for value, label in Quote.STATUS_CHOICES
                if value in Quote.ALLOWED_TRANSITIONS.get(quote.status, set())
            ],
        },
    )


@staff_required
@require_POST
def quote_add_item(request, pk):
    quote = get_object_or_404(Quote, pk=pk)
    form = StaffQuoteItemAddForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.quote = quote
        item.added_by_staff = True
        item.display_order = quote.items.count()
        item.save()
        quote.recalculate()
        if quote.edit_reopens:
            quote.transition_to(Quote.IN_REVIEW, user=request.user)
            messages.success(
                request, f'Line added — saved as revision {quote.revision}. Send it when ready.'
            )
        else:
            messages.success(request, 'Line added.')
    else:
        messages.error(request, 'Could not add that line.')
    return redirect('staff_quote_detail', pk=pk)


@staff_required
@require_POST
def quote_transition(request, pk):
    """Move a quote along the workflow, or approve-and-send in one click."""
    quote = get_object_or_404(Quote, pk=pk)
    target = request.POST.get('status')

    if target == 'APPROVE_AND_SEND':
        return _approve_and_send(request, quote)

    try:
        quote.transition_to(target, user=request.user)
    except InvalidTransition as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Moved to {quote.get_status_display()}.')
        if target == Quote.ACCEPTED:
            create_invoice_for_quote(quote)
    return redirect('staff_quote_detail', pk=pk)


def _approve_and_send(request, quote):
    """Approve, render the branded PDF and email it with accept/decline links."""
    if not quote.contact_email:
        messages.error(request, 'This quote has no customer email address to send to.')
        return redirect('staff_quote_detail', pk=quote.pk)

    quote.recalculate()
    try:
        if quote.status != Quote.APPROVED:
            quote.transition_to(Quote.APPROVED, user=request.user)
        quote.transition_to(Quote.SENT, user=request.user)
    except InvalidTransition as exc:
        messages.error(request, str(exc))
        return redirect('staff_quote_detail', pk=quote.pk)

    try:
        attach_pdf_to_quote(quote, request=request)
    except Exception:
        messages.warning(request, 'Quote sent, but the PDF could not be stored on the record.')

    run_task(send_quote_to_customer_task, quote.pk)
    label = f'Quote {quote.quote_number}'
    if quote.revision:
        label = f'Revision {quote.revision} of {quote.quote_number}'
    messages.success(request, f'{label} sent to {quote.contact_email}.')
    return redirect('staff_quote_detail', pk=quote.pk)


@staff_required
def quote_pdf(request, pk):
    """Stream the quote document for a staff preview before sending."""
    from .pdf import render_quote_pdf

    quote = get_object_or_404(Quote, pk=pk)
    filename, content, mimetype = render_quote_pdf(quote, request=request)
    response = HttpResponse(content, content_type=mimetype)
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


# --- finance register -------------------------------------------------------
#: Register order, top to bottom: what still needs chasing comes first.
PAYMENT_ORDER = [Invoice.NOT_PAID, Invoice.PARTIAL, Invoice.TBC, Invoice.PAID]


@staff_required
def register(request):
    """The Recharge Register: search, filter, inline edit and export."""
    # Money still owed first, settled last; newest first within each group.
    invoices = Invoice.objects.select_related('customer', 'quote').annotate(
        payment_rank=Case(
            *[When(payment_status=status, then=Value(rank)) for rank, status in enumerate(PAYMENT_ORDER)],
            default=Value(len(PAYMENT_ORDER)),
            output_field=IntegerField(),
        )
    ).order_by('payment_rank', '-date', '-pk')
    search = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    method = request.GET.get('method', '')
    overdue_only = request.GET.get('overdue') == '1'
    no_email_only = request.GET.get('no_email') == '1'

    if search:
        invoices = invoices.filter(
            Q(invoice_no__icontains=search)
            | Q(client_name__icontains=search)
            | Q(customer__company_name__icontains=search)
            | Q(job_details__icontains=search)
            | Q(receipt_no__icontains=search)
        )
    if status:
        invoices = invoices.filter(payment_status=status)
    if method:
        invoices = invoices.filter(payment_method=method)
    if overdue_only:
        invoices = invoices.filter(_overdue_q())
    if no_email_only:
        invoices = invoices.filter(_unpaid_q()).filter(_no_email_q())

    if request.GET.get('export') == 'csv':
        return _export_csv(invoices)

    totals = invoices.aggregate(
        invoiced=Sum('invoice_amount'), received=Sum('amount_received'), balance=Sum('balance')
    )
    return render(
        request,
        'main/staff/register.html',
        {
            'invoices': invoices[:500],
            'totals': totals,
            'search': search,
            'status': status,
            'method': method,
            'statuses': Invoice.PAYMENT_STATUS_CHOICES,
            'methods': Invoice.PAYMENT_METHOD_CHOICES,
            'columns': REGISTER_COLUMNS,
            'count': invoices.count(),
            'overdue_only': overdue_only,
            'no_email_only': no_email_only,
            'alerts': _register_alerts(),
        },
    )


def _unpaid_q():
    return ~Q(payment_status__in=[Invoice.PAID, Invoice.TBC]) & Q(balance__gt=0)


def _overdue_q():
    """Database form of Invoice.is_overdue, for filtering and counting."""
    cutoff = timezone.localdate() - timedelta(days=Invoice.PAYMENT_TERMS_DAYS)
    return _unpaid_q() & Q(date__lt=cutoff)


def _no_email_q():
    """Database form of "Invoice.contact_email is empty"."""
    customer_has_none = Q(customer__email='') & (
        Q(customer__user__isnull=True) | Q(customer__user__email='')
    )
    return Q(client_email='') & (Q(customer__isnull=True) | customer_has_none)


def _register_alerts():
    """The figures behind the alert strip at the top of the register."""
    overdue = Invoice.objects.filter(_overdue_q()).select_related('customer__user')
    overdue_list = list(overdue)
    return {
        'overdue_count': len(overdue_list),
        'overdue_total': sum((i.balance for i in overdue_list), 0),
        'reminders_due': sum(1 for i in overdue_list if i.reminder_due),
        'no_email_count': Invoice.objects.filter(_unpaid_q()).filter(_no_email_q()).count(),
    }


def _export_csv(invoices):
    response = HttpResponse(content_type='text/csv')
    stamp = timezone.localdate().isoformat()
    response['Content-Disposition'] = f'attachment; filename="inkpro-register-{stamp}.csv"'
    writer = csv.writer(response)
    # Email is last so the file round-trips through "Import CSV" losslessly.
    writer.writerow([label for _, label in REGISTER_COLUMNS] + ['Email'])
    for invoice in invoices:
        writer.writerow(
            [
                invoice.date.isoformat() if invoice.date else '',
                invoice.client,
                invoice.job_details,
                invoice.qty,
                invoice.invoice_no,
                invoice.invoice_amount,
                invoice.receipt_no,
                invoice.amount_received,
                invoice.balance,
                invoice.get_payment_status_display(),
                invoice.get_payment_method_display(),
                invoice.notes,
                invoice.contact_email,
            ]
        )
    return response


@staff_required
def invoice_edit(request, pk=None):
    invoice = get_object_or_404(Invoice, pk=pk) if pk else None
    form = InvoiceForm(request.POST or None, instance=invoice)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        messages.success(request, f'Saved invoice {saved.invoice_no or saved.pk}.')
        return redirect('staff_register')
    return render(
        request, 'main/staff/invoice_form.html', {'form': form, 'invoice': invoice}
    )


@staff_required
@require_POST
def invoice_inline_update(request, pk):
    """HTMX inline edit from the register table; returns the refreshed row."""
    invoice = get_object_or_404(Invoice, pk=pk)
    field = request.POST.get('field')
    value = request.POST.get('value', '')
    if field == 'client_email':
        return _update_client_email(request, invoice, value.strip())
    editable = {
        'amount_received',
        'invoice_amount',
        'receipt_no',
        'invoice_no',
        'qty',
        'notes',
        'payment_method',
        'job_details',
    }
    if field not in editable:
        return HttpResponse('Field is not editable.', status=400)

    try:
        if field in ('amount_received', 'invoice_amount'):
            setattr(invoice, field, value or 0)
        elif field == 'qty':
            setattr(invoice, field, int(value or 0))
        else:
            setattr(invoice, field, value)
        invoice.full_clean(exclude=['customer', 'quote'])
    except Exception:
        return HttpResponse('That value could not be saved.', status=400)

    invoice.save()
    return render(request, 'main/staff/_register_row.html', {'invoice': invoice})


def _update_client_email(request, invoice, email):
    """Set where reminders go for this client — on every unpaid row of theirs.

    Clients appear on many rows (one per job), so an address typed once is
    copied to their other entries that have none, rather than having to be
    typed into each.
    """
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    if email:
        try:
            validate_email(email)
        except ValidationError:
            return HttpResponse('That isn\u2019t a valid email address.', status=400)
    invoice.client_email = email.lower()
    invoice.save(update_fields=['client_email'])
    if email and invoice.client_name:
        Invoice.objects.filter(
            client_name__iexact=invoice.client_name, client_email=''
        ).update(client_email=email.lower())
    return render(request, 'main/staff/_register_row.html', {'invoice': invoice})


@staff_required
@require_POST
def invoice_send_reminder(request, pk):
    """Send one payment reminder now, from the register row."""
    from .emails import send_payment_reminder

    invoice = get_object_or_404(Invoice.objects.select_related('customer__user'), pk=pk)
    if invoice.balance <= 0 or invoice.payment_status in (Invoice.PAID, Invoice.TBC):
        messages.error(request, 'There is nothing outstanding on that entry.')
    elif not invoice.contact_email:
        messages.error(request, f'Add an email for {invoice.client} first.')
    else:
        try:
            send_payment_reminder(invoice)
        except Exception:
            messages.error(request, 'The reminder could not be sent. Check the email settings.')
        else:
            messages.success(
                request, f'Reminder for ${invoice.balance:,.2f} sent to {invoice.contact_email}.'
            )
    return redirect(request.POST.get('next') or 'staff_register')


@staff_required
@require_POST
def register_send_reminders(request):
    """Run the daily reminder pass now: every overdue client due a reminder."""
    from .tasks import send_payment_reminders_task

    result = send_payment_reminders_task()
    reminded, overdue = len(result['reminded']), len(result['overdue'])
    if reminded:
        messages.success(
            request,
            f'Sent {reminded} reminder{"s" if reminded != 1 else ""}. '
            'The office has been emailed the overdue summary.',
        )
    else:
        messages.info(
            request,
            f'No reminders were due — {overdue} overdue, but each was either reminded in the '
            'last week or has no email on file.' if overdue else 'Nothing is overdue.',
        )
    return redirect('staff_register')


#: Session key holding an uploaded CSV between the preview and the confirm step.
IMPORT_SESSION_KEY = 'register_import_csv'


@staff_required
def register_import(request):
    """Upload a CSV of the register, preview what will change, then confirm.

    The file is parsed twice: once to preview, and again on confirm, from the
    copy kept in the session, so nothing is written until staff have seen the
    preview and the result reflects the register as it is at that moment.
    """
    from . import register_import as importer

    if request.method == 'POST' and 'confirm' in request.POST:
        stored = request.session.get(IMPORT_SESSION_KEY)
        if not stored:
            messages.error(request, 'That import has expired — please upload the file again.')
            return redirect('staff_register_import')
        result = importer.parse(importer.read_csv(stored['data'].encode('utf-8')))
        counts = importer.apply(result)
        request.session.pop(IMPORT_SESSION_KEY, None)
        messages.success(
            request,
            f'Imported {stored["name"]}: {counts["created"]} new, {counts["updated"]} updated, '
            f'{counts["unchanged"]} already up to date.',
        )
        return redirect('staff_register')

    context = {'max_mb': importer.MAX_UPLOAD_BYTES // (1024 * 1024)}
    if request.method == 'POST':
        upload = request.FILES.get('file')
        if not upload:
            context['error'] = 'Choose a CSV file to import.'
        elif not upload.name.lower().endswith('.csv'):
            context['error'] = (
                'That isn\u2019t a CSV file. In Excel use File \u2192 Save As \u2192 '
                'CSV UTF-8, then upload that.'
            )
        else:
            try:
                rows = importer.read_csv(upload.read())
                result = importer.parse(rows)
            except importer.RegisterImportError as exc:
                context['error'] = str(exc)
            else:
                # Stored as text: the session serialiser only takes JSON types.
                request.session[IMPORT_SESSION_KEY] = {
                    'name': upload.name,
                    'data': '\n'.join(_csv_line(row) for row in rows),
                }
                context.update({
                    'result': result,
                    'filename': upload.name,
                    'new_count': result.count('create'),
                    'updated_count': result.count('update'),
                    'unchanged_count': result.count('unchanged'),
                    'totals': {
                        'invoiced': sum((r.fields['invoice_amount'] for r in result.rows), 0),
                        'received': sum((r.fields['amount_received'] for r in result.rows), 0),
                        'balance': sum((r.balance for r in result.rows), 0),
                    },
                })
    return render(request, 'main/staff/register_import.html', context)


def _csv_line(row):
    buffer = io.StringIO()
    csv.writer(buffer).writerow(['' if cell is None else cell for cell in row])
    return buffer.getvalue().rstrip('\r\n')


@staff_required
def register_import_template(request):
    """A blank CSV with the register's columns, for preparing future imports."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="inkpro-register-template.csv"'
    response.write('\ufeff')  # BOM, so Excel opens it as UTF-8
    writer = csv.writer(response)
    writer.writerow([label for _, label in REGISTER_COLUMNS[:8]] + ['Payment Method', 'Email', 'Notes'])
    writer.writerow([
        timezone.localdate().strftime('%d-%b-%Y'), 'Example Client Ltd', 'Pull-up banner 850mm x 2m',
        1, 'INV-1001', '450.00', '', '0.00', 'Bank transfer', 'accounts@example.com', '',
    ])
    return response
