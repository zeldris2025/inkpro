"""Staff portal: quote review inbox, the approve-and-send flow and the register."""

import csv

from django.contrib import messages
from django.db.models import Count, Q, Sum
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
@staff_required
def register(request):
    """The Recharge Register: search, filter, inline edit and export."""
    invoices = Invoice.objects.select_related('customer', 'quote')
    search = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    method = request.GET.get('method', '')

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
        },
    )


def _export_csv(invoices):
    response = HttpResponse(content_type='text/csv')
    stamp = timezone.localdate().isoformat()
    response['Content-Disposition'] = f'attachment; filename="inkpro-register-{stamp}.csv"'
    writer = csv.writer(response)
    writer.writerow([label for _, label in REGISTER_COLUMNS])
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
