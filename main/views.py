"""Public marketing site, the quote wizard and the customer account area."""

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Prefetch, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST

from . import pricing
from .emails import post_to_slack
from .forms import (
    ContactForm,
    StyledPasswordResetForm,
    CustomerProfileForm,
    QuoteItemForm,
    QuotePriceProbeForm,
    QuoteSubmitForm,
    SignUpForm,
)
from .models import (
    CategoryImage,
    Customer,
    Invoice,
    InvalidTransition,
    PricingRule,
    Quote,
    QuoteItem,
    ServiceCategory,
    UrgentFee,
)
from .permissions import CUSTOMER_GROUP
from .tasks import (
    notify_staff_new_quote_task,
    run_task,
    send_quote_received_task,
)

DRAFT_SESSION_KEY = 'draft_quote_id'


def active_categories():
    return ServiceCategory.objects.filter(is_active=True).prefetch_related(
        Prefetch('pricing_rules', queryset=PricingRule.objects.filter(is_active=True)),
        'images',
    )


# --- marketing --------------------------------------------------------------
def home(request):
    stats = Invoice.objects.aggregate(invoiced=Sum('invoice_amount'), jobs=Count('id'))
    return render(
        request,
        'main/home.html',
        {
            'categories': active_categories()[:6],
            'stats': {
                # Floors keep the marketing counters from reading as "0 jobs"
                # on a site that has only just gone live.
                'jobs': max(stats['jobs'] or 0, 1200),
                'clients': max(Customer.objects.count(), 180),
                'categories': ServiceCategory.objects.filter(is_active=True).count(),
                'years': 12,
            },
        },
    )


def services(request):
    return render(request, 'main/services.html', {'categories': active_categories()})


def service_detail(request, slug):
    category = get_object_or_404(ServiceCategory, slug=slug, is_active=True)
    return render(
        request,
        'main/service_detail.html',
        {
            'category': category,
            'rules': category.pricing_rules.filter(is_active=True),
            'images': category.images.all(),
            'related': active_categories().exclude(pk=category.pk)[:3],
        },
    )


def about(request):
    return render(request, 'main/about.html', {'categories': active_categories()})


def gallery(request):
    """Portfolio grid, built from the service photography."""
    images = CategoryImage.objects.select_related('category').filter(
        category__is_active=True
    )
    slug = request.GET.get('category', '')
    if slug:
        images = images.filter(category__slug=slug)
    return render(
        request,
        'main/gallery.html',
        {
            'images': images,
            'categories': active_categories(),
            'active_slug': slug,
        },
    )


def contact(request):
    form = ContactForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        post_to_slack(
            f'*Contact form* {data["name"]} <{data["email"]}>\n{data["message"]}'
        )
        messages.success(request, 'Thanks — we’ll be in touch shortly.')
        return redirect('contact')
    return render(request, 'main/contact.html', {'form': form})


# --- quote wizard -----------------------------------------------------------
def get_draft_quote(request, create=True):
    """Return the in-progress draft quote for this visitor.

    The wizard writes straight to a ``DRAFT`` Quote rather than a session cart,
    so artwork uploads land in real storage and the live totals come from the
    same code path that prices the final quote.
    """
    quote_id = request.session.get(DRAFT_SESSION_KEY)
    if quote_id:
        quote = Quote.objects.filter(pk=quote_id, status=Quote.DRAFT).first()
        if quote:
            return quote
        request.session.pop(DRAFT_SESSION_KEY, None)
    if not create:
        return None
    quote = Quote.objects.create(status=Quote.DRAFT)
    if request.user.is_authenticated:
        quote.customer = getattr(request.user, 'customer', None)
        quote.save(update_fields=['customer'])
    request.session[DRAFT_SESSION_KEY] = quote.pk
    return quote


def quote_context(quote):
    return {
        'quote': quote,
        'items': list(quote.items.select_related('category', 'pricing_rule')) if quote else [],
        'urgent_fee_band': UrgentFee.current(),
    }


def quote_builder(request):
    """Step 1 — choose a service category."""
    quote = get_draft_quote(request, create=False)
    return render(
        request,
        'main/quote/step_category.html',
        {'categories': active_categories(), 'step': 1, **quote_context(quote)},
    )


def quote_configure(request, slug):
    """Steps 2-3 — choose an option, size it, set quantity, attach artwork."""
    category = get_object_or_404(ServiceCategory, slug=slug, is_active=True)
    quote = get_draft_quote(request)
    form = QuoteItemForm(
        request.POST or None, request.FILES or None, category=category
    )

    if request.method == 'POST' and form.is_valid():
        form.save(quote)
        quote.recalculate()
        messages.success(request, 'Added to your quote.')
        if 'add_another' in request.POST:
            return redirect('quote_builder')
        return redirect('quote_review')

    return render(
        request,
        'main/quote/step_configure.html',
        {
            'category': category,
            'form': form,
            'rules': category.pricing_rules.filter(is_active=True),
            'step': 2,
            **quote_context(quote),
        },
    )


@require_POST
def quote_remove_item(request, item_id):
    quote = get_draft_quote(request, create=False)
    if quote:
        quote.items.filter(pk=item_id).delete()
        quote.recalculate()
    if request.headers.get('HX-Request'):
        return render(request, 'main/quote/_summary.html', quote_context(quote))
    return redirect('quote_review')


def quote_review(request):
    """Final step — contact details, urgency and submission."""
    quote = get_draft_quote(request, create=False)
    if not quote or not quote.items.exists():
        messages.info(request, 'Add at least one item to start your quote.')
        return redirect('quote_builder')

    form = QuoteSubmitForm(request.POST or None, instance=quote, user=request.user)
    if request.method == 'POST' and form.is_valid():
        quote = form.save(commit=False)
        quote.urgent_fee = UrgentFee.default_amount() if quote.is_urgent else 0
        if request.user.is_authenticated:
            quote.customer = getattr(request.user, 'customer', None)
        quote.save()
        quote.recalculate()
        try:
            quote.transition_to(Quote.SUBMITTED)
        except InvalidTransition:
            messages.error(request, 'That quote has already been submitted.')
            return redirect('quote_builder')

        run_task(send_quote_received_task, quote.pk)
        run_task(notify_staff_new_quote_task, quote.pk)
        request.session.pop(DRAFT_SESSION_KEY, None)
        request.session['submitted_quote_token'] = quote.access_token
        return redirect('quote_submitted', token=quote.access_token)

    return render(
        request, 'main/quote/step_review.html', {'form': form, 'step': 3, **quote_context(quote)}
    )


def quote_submitted(request, token):
    quote = get_object_or_404(Quote, access_token=token)
    return render(request, 'main/quote/submitted.html', {'quote': quote})


@require_POST
def quote_toggle_urgent(request):
    """HTMX endpoint behind the "need it urgently?" switch."""
    quote = get_draft_quote(request, create=False)
    if not quote:
        return HttpResponseBadRequest('No draft quote in progress.')
    quote.is_urgent = request.POST.get('is_urgent') in ('on', 'true', '1')
    quote.urgent_fee = UrgentFee.default_amount() if quote.is_urgent else 0
    quote.save(update_fields=['is_urgent', 'urgent_fee', 'updated_at'])
    quote.recalculate()
    return render(request, 'main/quote/_summary.html', quote_context(quote))


def quote_summary_partial(request):
    """HTMX polling target for the live price sidebar."""
    return render(
        request, 'main/quote/_summary.html', quote_context(get_draft_quote(request, create=False))
    )


def price_probe(request):
    """Live per-line price for the wizard, called as the fields change.

    Returns a small HTML fragment for HTMX rather than JSON so the markup and
    the rounding rules stay in one place.
    """
    form = QuotePriceProbeForm(request.GET or None)
    context = {'valid': False}
    if form.is_valid():
        rule = form.cleaned_data['pricing_rule']
        unit = rule.unit_price(form.cleaned_data['width_m'], form.cleaned_data['height_m'])
        line = pricing.line_total(unit, form.cleaned_data['quantity'])
        context = {
            'valid': True,
            'rule': rule,
            'unit_price': unit,
            'line_total': line,
            'gst': pricing.add_gst(line),
            'inc_gst': pricing.money(line + pricing.add_gst(line)),
            'quantity': form.cleaned_data['quantity'],
            'below_min_qty': form.cleaned_data['quantity'] < rule.min_qty,
        }
    return render(request, 'main/quote/_line_price.html', context)


# --- public quote link (token authenticated, no login) ----------------------
def public_quote(request, token):
    """The page the emailed "View & accept" button lands on."""
    quote = get_object_or_404(
        Quote.objects.select_related('customer'), access_token=token
    )
    if quote.status not in Quote.CUSTOMER_VISIBLE:
        return render(request, 'main/quote/not_ready.html', {'quote': quote}, status=403)
    return render(
        request,
        'main/quote/public.html',
        {'quote': quote, 'items': quote.items.all(), 'action': request.GET.get('action', '')},
    )


@require_POST
def public_quote_respond(request, token):
    """Accept or decline from the emailed link; acceptance opens an invoice."""
    quote = get_object_or_404(Quote, access_token=token)
    decision = request.POST.get('decision')
    target = {'accept': Quote.ACCEPTED, 'decline': Quote.DECLINED}.get(decision)
    if target is None:
        return HttpResponseBadRequest('Unknown decision.')

    try:
        quote.transition_to(target)
    except InvalidTransition:
        messages.error(
            request, f'This quote is already {quote.get_status_display().lower()}.'
        )
        return redirect('public_quote', token=token)

    if target == Quote.DECLINED:
        quote.decline_reason = request.POST.get('reason', '')
        quote.save(update_fields=['decline_reason', 'updated_at'])
        post_to_slack(f'Quote {quote.quote_number} was declined.')
    else:
        create_invoice_for_quote(quote)
        post_to_slack(f'✅ Quote {quote.quote_number} accepted — ${quote.total:,.2f}')

    messages.success(
        request,
        'Thanks — we’ll get started right away.'
        if target == Quote.ACCEPTED
        else 'Thanks for letting us know.',
    )
    return redirect('public_quote', token=token)


def create_invoice_for_quote(quote):
    """Open an unpaid register entry for an accepted quote (idempotent)."""
    existing = Invoice.objects.filter(quote=quote).first()
    if existing:
        return existing
    return Invoice.objects.create(
        quote=quote,
        customer=quote.customer,
        client_name='' if quote.customer else (quote.contact_name or ''),
        job_details='; '.join(quote.items.values_list('description', flat=True)),
        date=quote.responded_at.date() if quote.responded_at else None,
        qty=sum(quote.items.values_list('quantity', flat=True)) or 0,
        invoice_amount=quote.total,
        amount_received=0,
        notes=f'Auto-created from accepted quote {quote.quote_number}.',
    )


# --- accounts ---------------------------------------------------------------
def signup(request):
    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        from django.contrib.auth.models import Group

        group, _ = Group.objects.get_or_create(name=CUSTOMER_GROUP)
        user.groups.add(group)
        # The user was just created rather than authenticated, so it carries no
        # `backend` attribute. With more than one backend configured Django
        # cannot guess which to record on the session, so name it explicitly.
        login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
        # Carry any in-progress draft over to the new account.
        draft = get_draft_quote(request, create=False)
        if draft and not draft.customer:
            draft.customer = user.customer
            draft.save(update_fields=['customer'])
        messages.success(request, f'Welcome to InkPro, {user.username}.')
        return redirect('my_quotes')
    return render(request, 'main/account/signup.html', {'form': form})


def quotes_for_user(user):
    """Quotes this user may see: their own customer record, plus guest quotes
    they raised under the same email before signing up.

    Built as an explicit OR of only the clauses that apply. A naive
    ``Q(customer=customer)`` with ``customer=None`` would match every guest
    quote in the database, so each clause is added only when it has a value.
    """
    customer = getattr(user, 'customer', None)
    criteria = Q(pk__in=[])
    if customer is not None:
        criteria |= Q(customer=customer)
    if user.email:
        criteria |= Q(guest_email__iexact=user.email)
    return Quote.objects.filter(criteria)


@login_required
def my_quotes(request):
    quotes = quotes_for_user(request.user).exclude(status=Quote.DRAFT).prefetch_related('items')
    return render(
        request,
        'main/account/my_quotes.html',
        {'quotes': quotes, 'customer': getattr(request.user, 'customer', None)},
    )


@login_required
def my_quote_detail(request, pk):
    quote = get_object_or_404(quotes_for_user(request.user), pk=pk)
    return render(
        request, 'main/account/quote_detail.html', {'quote': quote, 'items': quote.items.all()}
    )


@login_required
@require_POST
def reorder_quote(request, pk):
    """Copy a past quote's lines into a fresh draft so it can be re-submitted."""
    source = get_object_or_404(quotes_for_user(request.user), pk=pk)
    draft = get_draft_quote(request)
    for item in source.items.all():
        QuoteItem.objects.create(
            quote=draft,
            category=item.category,
            pricing_rule=item.pricing_rule,
            description=item.description,
            size=item.size,
            width_m=item.width_m,
            height_m=item.height_m,
            quantity=item.quantity,
            unit_price=item.unit_price,
            display_order=draft.items.count(),
        )
    draft.recalculate()
    messages.success(request, f'Copied {source.quote_number} into a new quote.')
    return redirect('quote_review')


@login_required
def profile(request):
    customer, _ = Customer.objects.get_or_create(
        user=request.user, defaults={'email': request.user.email}
    )
    form = CustomerProfileForm(request.POST or None, instance=customer)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Profile updated.')
        return redirect('profile')
    return render(request, 'main/account/profile.html', {'form': form, 'customer': customer})


def health_check(request):
    """Readiness probe, safe to expose publicly.

    Reports whether the app can actually serve — not merely whether the process
    is up. A deploy whose startup command was never set runs gunicorn directly,
    skipping migrations, and then fails with "no such table" on the first page
    that touches the database. This surfaces that in one request.

    Deliberately names no hostnames, credentials or connection strings; it
    reports which component is unhealthy, not how it is configured.
    """
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.http import JsonResponse

    checks = {}
    healthy = True

    try:
        connection.ensure_connection()
        checks['database'] = 'ok'
    except Exception:
        checks['database'] = 'unreachable'
        healthy = False

    if checks['database'] == 'ok':
        try:
            executor = MigrationExecutor(connection)
            pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if pending:
                checks['migrations'] = f'{len(pending)} pending'
                healthy = False
            else:
                checks['migrations'] = 'applied'
        except Exception:
            checks['migrations'] = 'unknown'
            healthy = False

    if checks.get('migrations') == 'applied':
        try:
            checks['catalogue'] = (
                'ok' if ServiceCategory.objects.exists() else 'empty — run bootstrap'
            )
            if checks['catalogue'] != 'ok':
                healthy = False
        except Exception:
            checks['catalogue'] = 'unreadable'
            healthy = False

    checks['engine'] = connection.vendor
    return JsonResponse(
        {'status': 'ok' if healthy else 'degraded', 'checks': checks},
        status=200 if healthy else 503,
    )


# --- password reset ---------------------------------------------------------
class BrandedPasswordResetView(auth_views.PasswordResetView):
    """Send the reset link on InkPro letterhead.

    ``extra_email_context`` is a property rather than a class attribute so the
    logo URL is resolved per request; resolving it at import time would bake in
    whatever happened to be on disk when the process started.
    """

    template_name = 'main/account/password_reset.html'
    email_template_name = 'main/email/password_reset.txt'
    html_email_template_name = 'main/email/password_reset.html'
    subject_template_name = 'main/email/password_reset_subject.txt'
    form_class = StyledPasswordResetForm
    success_url = reverse_lazy('password_reset_done')

    @property
    def extra_email_context(self):
        from .context_processors import LOGO_ON_DARK, absolute_logo_url

        return {
            'LOGO_URL': absolute_logo_url(LOGO_ON_DARK),  # black email header
            'SITE_URL': settings.SITE_URL,
            'TAGLINE': 'Think Ink, Think Pro',
        }
