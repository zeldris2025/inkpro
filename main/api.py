"""DRF endpoints backing the dashboard charts and the wizard's live pricing."""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response

from . import pricing
from .models import Invoice, PricingRule, Quote, QuoteItem
from .permissions import is_inkpro_staff

class IsInkProStaff(BasePermission):
    message = 'Staff access required.'

    def has_permission(self, request, view):
        return is_inkpro_staff(request.user)


def _money(value):
    return float(pricing.money(value or 0))


@api_view(['GET'])
@permission_classes([IsInkProStaff])
def dashboard_stats(request):
    """Every figure the dashboard charts need, in one round trip.

    ``months`` (default 12) controls the window for the time series; the KPI
    and status figures always cover the whole register so they match the
    spreadsheet's summary row.
    """
    try:
        months = max(1, min(int(request.GET.get('months', 12)), 60))
    except (TypeError, ValueError):
        months = 12
    since = (timezone.localdate().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)

    invoices = Invoice.objects.all()
    totals = invoices.aggregate(
        invoiced=Sum('invoice_amount'), received=Sum('amount_received'), outstanding=Sum('balance')
    )

    # Sales over time
    series = (
        invoices.filter(date__gte=since)
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(invoiced=Sum('invoice_amount'), received=Sum('amount_received'), jobs=Count('id'))
        .order_by('month')
    )
    sales_over_time = [
        {
            'month': row['month'].strftime('%b %Y') if row['month'] else 'Undated',
            'invoiced': _money(row['invoiced']),
            'received': _money(row['received']),
            'jobs': row['jobs'],
        }
        for row in series
        if row['month']
    ]

    # Revenue by service category, from the line items of quotes that converted.
    by_category = (
        QuoteItem.objects.filter(quote__status__in=[Quote.ACCEPTED, Quote.SENT])
        .values('category__name')
        .annotate(revenue=Sum('line_total'), lines=Count('id'))
        .order_by('-revenue')[:10]
    )

    # Payment status mix
    status_rows = invoices.values('payment_status').annotate(
        n=Count('id'), amount=Sum('invoice_amount')
    )
    status_labels = dict(Invoice.PAYMENT_STATUS_CHOICES)

    # Top clients, merging linked customers and free-text imported names.
    client_totals = {}
    for invoice in invoices.select_related('customer').only(
        'client_name', 'invoice_amount', 'customer'
    ):
        key = invoice.client
        client_totals[key] = client_totals.get(key, Decimal('0')) + Decimal(
            str(invoice.invoice_amount or 0)
        )
    top_clients = sorted(client_totals.items(), key=lambda kv: kv[1], reverse=True)[:8]

    # Conversion funnel. Each stage counts quotes that reached it or beyond, so
    # the funnel never widens as it descends.
    quotes = Quote.objects.exclude(status=Quote.DRAFT)
    reached_sent = [Quote.SENT, Quote.ACCEPTED, Quote.DECLINED, Quote.EXPIRED]
    submitted = quotes.count()
    sent = quotes.filter(status__in=reached_sent).count()
    accepted = quotes.filter(status=Quote.ACCEPTED).count()

    return Response(
        {
            'kpi': {
                'total_invoiced': _money(totals['invoiced']),
                'total_received': _money(totals['received']),
                'outstanding': _money(totals['outstanding']),
                'invoice_count': invoices.count(),
                'paid_count': invoices.filter(payment_status=Invoice.PAID).count(),
                'unpaid_count': invoices.exclude(payment_status=Invoice.PAID).count(),
                'quotes_awaiting': Quote.objects.awaiting_staff().count(),
            },
            'sales_over_time': sales_over_time,
            'revenue_by_category': [
                {'label': row['category__name'], 'value': _money(row['revenue']), 'lines': row['lines']}
                for row in by_category
            ],
            'payment_status': [
                {
                    'label': status_labels.get(row['payment_status'], row['payment_status']),
                    'value': row['n'],
                    'amount': _money(row['amount']),
                }
                for row in status_rows
            ],
            'top_clients': [{'label': name, 'value': _money(amount)} for name, amount in top_clients],
            'funnel': {
                'submitted': submitted,
                'sent': sent,
                'accepted': accepted,
                'conversion_rate': round(accepted / sent * 100, 1) if sent else 0.0,
            },
            'generated_at': timezone.now().isoformat(),
        }
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def rule_price(request):
    """Public live-pricing lookup used by the quote wizard's Alpine state."""
    try:
        rule = PricingRule.objects.select_related('category').get(
            pk=request.GET.get('rule'), is_active=True
        )
    except (PricingRule.DoesNotExist, ValueError, TypeError):
        return Response({'error': 'Unknown pricing rule.'}, status=404)

    def number(name):
        try:
            return Decimal(request.GET[name])
        except Exception:
            return None

    quantity = int(number('quantity') or 1)
    unit = rule.unit_price(number('width_m'), number('height_m'))
    line = pricing.line_total(unit, quantity)
    gst = pricing.add_gst(line)
    return Response(
        {
            'rule': rule.name,
            'category': rule.category.name,
            'unit_label': rule.unit_label,
            'requires_dimensions': rule.requires_dimensions,
            'min_qty': rule.min_qty,
            'unit_price': _money(unit),
            'line_total': _money(line),
            'gst': _money(gst),
            'inc_gst': _money(line + gst),
            'notes': rule.notes,
        }
    )
