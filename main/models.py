"""InkPro domain models: service catalogue, quoting, and the finance register."""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from . import pricing


def _token():
    """URL-safe token backing the no-login quote view link emailed to customers."""
    return secrets.token_urlsafe(32)


class ServiceCategory(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    icon = models.CharField(
        max_length=100, blank=True, help_text='Short emoji or icon key used on the service cards.'
    )
    hero_image = models.ImageField(upload_to='categories/', blank=True, null=True)
    display_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'service categories'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('service_detail', args=[self.slug])

    @property
    def card_image(self):
        """Image for the service card: the explicit hero_image upload if one
        was set, otherwise whichever gallery image is flagged as hero, else the
        first gallery image. Returns None so templates can fall back to the icon."""
        if self.hero_image:
            return self.hero_image
        gallery = list(self.images.all())
        if not gallery:
            return None
        hero = next((image for image in gallery if image.is_hero), gallery[0])
        return hero.image

    @property
    def price_from(self):
        """Cheapest GST-exclusive starting price across this category's rules."""
        prices = [r.display_price_ex_gst for r in self.pricing_rules.all() if r.is_active]
        return min(prices) if prices else None


class CategoryImage(models.Model):
    """A product photograph belonging to a service category.

    Populated from InkPro's rate card PDF by ``manage.py import_ratecard_images``,
    but equally editable by hand in the admin. One image per category may be
    flagged ``is_hero`` to front the service cards; the rest feed the gallery.
    """

    category = models.ForeignKey(
        ServiceCategory, related_name='images', on_delete=models.CASCADE
    )
    image = models.ImageField(upload_to='services/')
    caption = models.CharField(max_length=200, blank=True)
    alt_text = models.CharField(
        max_length=200,
        blank=True,
        help_text='Describes the photo for screen readers. Falls back to the caption.',
    )
    is_hero = models.BooleanField(
        default=False, help_text='Front this image on the service card.'
    )
    display_order = models.IntegerField(default=0)
    source_hash = models.CharField(
        max_length=32,
        blank=True,
        db_index=True,
        help_text='Content hash of the source image, so re-importing updates rather than duplicates.',
    )

    class Meta:
        ordering = ['display_order', 'pk']

    def __str__(self):
        return self.caption or f'{self.category.name} image #{self.pk}'

    @property
    def description(self):
        return self.alt_text or self.caption or self.category.name


class PricingRule(models.Model):
    FLAT_RATE = 'FLAT_RATE'
    PER_METER = 'PER_METER'
    PER_UNIT_RANGE = 'PER_UNIT_RANGE'
    CUSTOM_SIZE_FORMULA = 'CUSTOM_SIZE_FORMULA'
    PRICING_TYPE_CHOICES = [
        (FLAT_RATE, 'Flat rate'),
        (PER_METER, 'Per square metre'),
        (PER_UNIT_RANGE, 'Ranged (from/to)'),
        (CUSTOM_SIZE_FORMULA, 'Custom size (rate x size)'),
    ]

    category = models.ForeignKey(
        ServiceCategory, related_name='pricing_rules', on_delete=models.CASCADE
    )
    name = models.CharField(max_length=200, help_text='e.g. "A4", "2m x 1m banner", "Door decal"')
    pricing_type = models.CharField(max_length=30, choices=PRICING_TYPE_CHOICES, default=FLAT_RATE)
    base_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text='Flat price, or the per-square-metre rate for size-based rules.',
    )
    min_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit_label = models.CharField(max_length=50, default='each', blank=True)
    min_qty = models.PositiveIntegerField(default=1)
    gst_inclusive = models.BooleanField(
        default=False, help_text='Tick if base_price already includes GST.'
    )
    requires_dimensions = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    display_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['category__display_order', 'display_order', 'name']

    def __str__(self):
        return f'{self.category.name} — {self.name}'

    def save(self, *args, **kwargs):
        self.requires_dimensions = self.pricing_type in (self.PER_METER, self.CUSTOM_SIZE_FORMULA)
        super().save(*args, **kwargs)

    def unit_price(self, width_m=None, height_m=None) -> Decimal:
        """GST-exclusive price for one unit under this rule."""
        return pricing.unit_price_for(
            self.pricing_type,
            self.base_price,
            width_m=width_m,
            height_m=height_m,
            min_price=self.min_price,
            max_price=self.max_price,
            gst_inclusive=self.gst_inclusive,
        )

    @property
    def display_price_ex_gst(self) -> Decimal:
        """Indicative GST-exclusive price shown on the public rate tables.

        Size-based rules quote their per-square-metre rate, since there is no
        meaningful single price until the customer names a size.
        """
        if self.requires_dimensions:
            base = Decimal(str(self.base_price))
            return pricing.strip_gst(base) if self.gst_inclusive else pricing.money(base)
        return self.unit_price()

    @property
    def display_price_inc_gst(self) -> Decimal:
        ex = self.display_price_ex_gst
        return pricing.money(ex + pricing.add_gst(ex))

    @property
    def price_range_label(self) -> str:
        """Human-readable price for the marketing tables ("$20 – $60")."""
        if self.pricing_type == self.PER_UNIT_RANGE and self.min_price and self.max_price:
            return f'${self.min_price:,.0f} – ${self.max_price:,.0f}'
        if self.requires_dimensions:
            return f'${self.display_price_ex_gst:,.0f} per m²'
        return f'${self.display_price_ex_gst:,.2f}'


class UrgentFee(models.Model):
    """Same-day / rush surcharge band added on top of the normal price."""

    min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('50'))
    max_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('100'))
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['min_amount']

    def __str__(self):
        return f'Urgent fee ${self.min_amount:,.0f}–${self.max_amount:,.0f}'

    @classmethod
    def current(cls):
        return cls.objects.filter(is_active=True).first()

    @classmethod
    def default_amount(cls) -> Decimal:
        """Amount applied automatically when a customer ticks "urgent".

        Quotes start at the bottom of the band; staff nudge it up during review
        if the job warrants it.
        """
        fee = cls.current()
        return pricing.money(fee.min_amount) if fee else Decimal('0.00')


class Customer(models.Model):
    INDIVIDUAL = 'INDIVIDUAL'
    BUSINESS = 'BUSINESS'
    CUSTOMER_TYPE_CHOICES = [(INDIVIDUAL, 'Individual'), (BUSINESS, 'Business')]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='customer',
    )
    name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    customer_type = models.CharField(
        max_length=20, choices=CUSTOMER_TYPE_CHOICES, default=INDIVIDUAL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['company_name', 'name']

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        if self.company_name:
            return self.company_name
        if self.name:
            return self.name
        if self.user:
            return self.user.get_full_name() or self.user.get_username()
        return f'Customer #{self.pk}'

    @property
    def contact_email(self):
        return self.email or (self.user.email if self.user else '')


class QuoteQuerySet(models.QuerySet):
    def active(self):
        return self.exclude(status__in=[Quote.DRAFT, Quote.EXPIRED, Quote.DECLINED])

    def awaiting_staff(self):
        return self.filter(status__in=[Quote.SUBMITTED, Quote.IN_REVIEW, Quote.REVISED])


class Quote(models.Model):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'
    IN_REVIEW = 'IN_REVIEW'
    REVISED = 'REVISED'
    APPROVED = 'APPROVED'
    SENT = 'SENT'
    ACCEPTED = 'ACCEPTED'
    DECLINED = 'DECLINED'
    EXPIRED = 'EXPIRED'
    STATUS_CHOICES = [
        (DRAFT, 'Draft'),
        (SUBMITTED, 'Submitted'),
        (IN_REVIEW, 'In review'),
        (REVISED, 'Revised'),
        (APPROVED, 'Approved'),
        (SENT, 'Sent'),
        (ACCEPTED, 'Accepted'),
        (DECLINED, 'Declined'),
        (EXPIRED, 'Expired'),
    ]

    #: Which statuses a quote may legally move to. Enforced by
    #: ``transition_to`` so the workflow cannot skip steps or resurrect a
    #: finished quote.
    ALLOWED_TRANSITIONS = {
        DRAFT: {SUBMITTED, EXPIRED},
        SUBMITTED: {IN_REVIEW, APPROVED, DECLINED, EXPIRED},
        IN_REVIEW: {REVISED, APPROVED, DECLINED, EXPIRED},
        REVISED: {IN_REVIEW, APPROVED, DECLINED, EXPIRED},
        APPROVED: {SENT, IN_REVIEW, EXPIRED},
        SENT: {ACCEPTED, DECLINED, IN_REVIEW, EXPIRED},
        ACCEPTED: set(),
        DECLINED: {IN_REVIEW},
        EXPIRED: {IN_REVIEW},
    }

    #: Statuses the customer sees on the public token link.
    CUSTOMER_VISIBLE = {SENT, ACCEPTED, DECLINED, EXPIRED}

    quote_number = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    access_token = models.CharField(max_length=64, unique=True, default=_token, editable=False)
    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name='quotes'
    )
    guest_name = models.CharField(max_length=200, blank=True)
    guest_email = models.EmailField(blank=True)
    guest_phone = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='reviewed_quotes',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    urgent_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text='GST-exclusive discount.'
    )
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_override = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Set to bypass the calculated total entirely.',
    )

    is_urgent = models.BooleanField(default=False)
    customer_notes = models.TextField(blank=True)
    staff_notes = models.TextField(blank=True, help_text='Internal only — never shown to customers.')
    decline_reason = models.TextField(blank=True)
    pdf_file = models.FileField(upload_to='quotes/', blank=True, null=True)

    objects = QuoteQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.quote_number or f'Quote #{self.pk}'

    def save(self, *args, **kwargs):
        if not self.quote_number:
            self.quote_number = self._next_quote_number()
        super().save(*args, **kwargs)

    @staticmethod
    def _next_quote_number():
        """Allocate the next ``INK-Q-<year>-<seq>`` reference for this year."""
        year = timezone.localdate().year
        prefix = f'INK-Q-{year}-'
        last = (
            Quote.objects.filter(quote_number__startswith=prefix)
            .order_by('-quote_number')
            .values_list('quote_number', flat=True)
            .first()
        )
        seq = 1
        if last:
            try:
                seq = int(last.rsplit('-', 1)[1]) + 1
            except (IndexError, ValueError):
                seq = Quote.objects.filter(quote_number__startswith=prefix).count() + 1
        return f'{prefix}{seq:04d}'

    # -- contact details -----------------------------------------------------
    @property
    def contact_name(self):
        return self.customer.display_name if self.customer else self.guest_name

    @property
    def contact_email(self):
        if self.customer and self.customer.contact_email:
            return self.customer.contact_email
        return self.guest_email

    @property
    def contact_phone(self):
        if self.customer and self.customer.phone:
            return self.customer.phone
        return self.guest_phone

    # -- money ---------------------------------------------------------------
    def recalculate(self, commit=True):
        """Recompute the money fields from the current line items.

        The discount reduces the GST-exclusive subtotal, so GST is charged on
        the discounted figure. ``total_override`` wins over everything, which
        is how staff force a negotiated number onto a quote.
        """
        totals = pricing.quote_totals(
            [item.line_total for item in self.items.all()],
            urgent_fee=self.urgent_fee,
        )
        discount = pricing.money(self.discount or 0)
        subtotal = totals['subtotal']
        taxable = subtotal - discount + totals['urgent_fee']
        if taxable < 0:
            taxable = Decimal('0.00')
        gst = pricing.add_gst(taxable)

        self.subtotal = subtotal
        self.gst_amount = gst
        self.total = (
            pricing.money(self.total_override)
            if self.total_override is not None
            else pricing.money(taxable + gst)
        )
        if commit:
            self.save(
                update_fields=['subtotal', 'gst_amount', 'total', 'updated_at']
            )
        return self

    # -- workflow ------------------------------------------------------------
    def can_transition_to(self, new_status) -> bool:
        if new_status == self.status:
            return True
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    @transaction.atomic
    def transition_to(self, new_status, *, user=None, commit=True):
        """Move the quote to *new_status*, stamping the relevant timestamps.

        Raises ``InvalidTransition`` rather than silently ignoring an illegal
        move, so a bad call site surfaces during review instead of leaving a
        quote in an impossible state.
        """
        if not self.can_transition_to(new_status):
            raise InvalidTransition(
                f'Cannot move {self} from {self.status} to {new_status}.'
            )
        now = timezone.now()
        fields = ['status', 'updated_at']
        self.status = new_status

        if new_status == self.SUBMITTED and not self.submitted_at:
            self.submitted_at = now
            fields.append('submitted_at')
        if new_status in (self.IN_REVIEW, self.REVISED, self.APPROVED):
            self.reviewed_at = now
            fields.append('reviewed_at')
            if user is not None and user.is_authenticated:
                self.reviewed_by = user
                fields.append('reviewed_by')
        if new_status == self.SENT:
            self.sent_at = now
            fields.append('sent_at')
            if not self.valid_until:
                self.valid_until = (
                    now + timedelta(days=settings.QUOTE_VALID_DAYS)
                ).date()
                fields.append('valid_until')
        if new_status in (self.ACCEPTED, self.DECLINED):
            self.responded_at = now
            fields.append('responded_at')

        if commit:
            self.save(update_fields=fields)
        return self

    @property
    def is_expired(self):
        return bool(self.valid_until and self.valid_until < timezone.localdate())

    @property
    def awaiting_customer(self):
        return self.status == self.SENT and not self.is_expired

    @property
    def is_editable_by_staff(self):
        return self.status not in (self.ACCEPTED, self.DECLINED)

    def public_url(self):
        return f"{settings.SITE_URL}{reverse('public_quote', args=[self.access_token])}"


class InvalidTransition(Exception):
    """Raised when a quote is pushed into a status it cannot legally reach."""


class QuoteItem(models.Model):
    quote = models.ForeignKey(Quote, related_name='items', on_delete=models.CASCADE)
    category = models.ForeignKey(ServiceCategory, on_delete=models.PROTECT)
    pricing_rule = models.ForeignKey(
        PricingRule, null=True, blank=True, on_delete=models.SET_NULL
    )
    description = models.CharField(max_length=255)
    size = models.CharField(max_length=200, blank=True, help_text='Free-text size label.')
    width_m = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    height_m = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    artwork = models.FileField(upload_to='artwork/%Y/%m/', blank=True, null=True)
    is_urgent = models.BooleanField(default=False)
    added_by_staff = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'pk']

    def __str__(self):
        return f'{self.description} x{self.quantity}'

    def price_from_rule(self):
        """Unit price implied by the attached rule and this item's dimensions."""
        if not self.pricing_rule:
            return pricing.money(self.unit_price)
        return self.pricing_rule.unit_price(self.width_m, self.height_m)

    def save(self, *args, **kwargs):
        if self.pricing_rule and not self.added_by_staff and not self.unit_price:
            self.unit_price = self.price_from_rule()
        self.line_total = pricing.line_total(self.unit_price, self.quantity)
        if not self.size and self.width_m and self.height_m:
            self.size = f'{self.width_m:g}m x {self.height_m:g}m'
        super().save(*args, **kwargs)

    @property
    def area_m2(self):
        return pricing.area(self.width_m, self.height_m)


class Invoice(models.Model):
    """One row of the InkPro Recharge Register."""

    PAID = 'PAID'
    NOT_PAID = 'NOT_PAID'
    PARTIAL = 'PARTIAL'
    TBC = 'TBC'
    PAYMENT_STATUS_CHOICES = [
        (PAID, 'Paid'),
        (NOT_PAID, 'Not paid'),
        (PARTIAL, 'Partial'),
        (TBC, 'TBC'),
    ]

    CASH_CHQ = 'CASH_CHQ'
    PO_ON_ACCOUNT = 'PO_ON_ACCOUNT'
    BANK_TRANSFER = 'BANK_TRANSFER'
    CARD = 'CARD'
    OTHER = 'OTHER'
    PAYMENT_METHOD_CHOICES = [
        (CASH_CHQ, 'Cash / Cheque'),
        (PO_ON_ACCOUNT, 'P.O. on account'),
        (BANK_TRANSFER, 'Bank transfer'),
        (CARD, 'Card'),
        (OTHER, 'Other'),
    ]

    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name='invoices'
    )
    client_name = models.CharField(
        max_length=255,
        blank=True,
        help_text='Free-text client, used for imported and manual rows with no customer record.',
    )
    job_details = models.TextField(blank=True)
    quote = models.ForeignKey(
        Quote, null=True, blank=True, on_delete=models.SET_NULL, related_name='invoices'
    )
    date = models.DateField(null=True, blank=True, db_index=True)
    qty = models.IntegerField(default=0)
    invoice_no = models.CharField(max_length=100, blank=True, db_index=True)
    invoice_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    receipt_no = models.CharField(max_length=100, blank=True)
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default=NOT_PAID, db_index=True
    )
    payment_method = models.CharField(
        max_length=30, choices=PAYMENT_METHOD_CHOICES, default=CASH_CHQ
    )
    status_is_manual = models.BooleanField(
        default=False,
        help_text='Keeps a hand-set status (e.g. TBC) from being overwritten by the balance rule.',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-pk']

    def __str__(self):
        return f'{self.invoice_no or "(no number)"} — {self.client}'

    @property
    def client(self):
        return self.customer.display_name if self.customer else (self.client_name or 'Unknown')

    def derive_payment_status(self):
        """Status implied by the balance, ignoring any manual override."""
        amount = Decimal(str(self.invoice_amount or 0))
        received = Decimal(str(self.amount_received or 0))
        if amount <= 0 and received <= 0:
            return self.TBC
        if received <= 0:
            return self.NOT_PAID
        if received >= amount:
            return self.PAID
        return self.PARTIAL

    def save(self, *args, **kwargs):
        self.balance = pricing.money(
            Decimal(str(self.invoice_amount or 0)) - Decimal(str(self.amount_received or 0))
        )
        if not self.status_is_manual:
            self.payment_status = self.derive_payment_status()
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        """Unpaid for more than 30 days from the invoice date."""
        if self.payment_status in (self.PAID, self.TBC) or not self.date:
            return False
        return (timezone.localdate() - self.date).days > 30


class EmailTemplate(models.Model):
    """Owner-editable copy for the transactional emails."""

    QUOTE_RECEIVED = 'QUOTE_RECEIVED'
    QUOTE_SENT = 'QUOTE_SENT'
    STAFF_NEW_QUOTE = 'STAFF_NEW_QUOTE'
    PAYMENT_REMINDER = 'PAYMENT_REMINDER'
    KEY_CHOICES = [
        (QUOTE_RECEIVED, 'Customer — quote request received'),
        (QUOTE_SENT, 'Customer — your quote is ready'),
        (STAFF_NEW_QUOTE, 'Staff — new quote request'),
        (PAYMENT_REMINDER, 'Customer — payment reminder'),
    ]

    key = models.CharField(max_length=40, choices=KEY_CHOICES, unique=True)
    subject = models.CharField(max_length=255)
    intro = models.TextField(
        blank=True, help_text='Opening paragraph. Supports {name} and {quote_number}.'
    )
    outro = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.get_key_display()

    def render(self, **context):
        def fmt(value):
            try:
                return value.format(**context)
            except (KeyError, IndexError):
                return value

        return {'subject': fmt(self.subject), 'intro': fmt(self.intro), 'outro': fmt(self.outro)}
