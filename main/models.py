"""InkPro domain models: service catalogue, quoting, and the finance register."""

import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from . import pricing


def plain_number(value) -> str:
    """A Decimal without trailing zeros or exponent: 4.800 -> "4.8", 10 -> "10"."""
    if value is None:
        return ''
    return f'{Decimal(str(value)).normalize():f}'


def _token():
    """URL-safe token backing the no-login quote view link emailed to customers."""
    return secrets.token_urlsafe(32)


# -- kept for migration history ---------------------------------------------
# Migration 0004 (customer ID verification, since removed) refers to these by
# import path. Django loads every migration on each run, so they must stay
# importable even though no model uses them any more.
def private_storage():
    from pathlib import Path

    from django.core.files.storage import FileSystemStorage

    return FileSystemStorage(location=Path(settings.MEDIA_ROOT).parent / 'private', base_url=None)


def id_document_path(instance, filename):
    from pathlib import Path

    return f'id-documents/{secrets.token_hex(16)}{Path(filename).suffix.lower()}'


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
        """Cheapest starting price across this category's rules, as customers pay it."""
        prices = [r.display_price_inc_gst for r in self.pricing_rules.all() if r.is_active]
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
        default=True,
        help_text='Ticked when base_price already includes GST, as every rate card price '
        'does. Untick to have GST added on top.',
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

    def listed_price(self, width_m=None, height_m=None) -> Decimal:
        """Price for one unit exactly as the rate card lists it.

        GST-inclusive when ``gst_inclusive`` is set (as every rate card price
        is): this is what the customer pays, and what the quote builder
        prices lines from so totals match the card to the cent.
        """
        return pricing.unit_price_for(
            self.pricing_type,
            self.base_price,
            width_m=width_m,
            height_m=height_m,
            min_price=self.min_price,
            max_price=self.max_price,
        )

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
        if self.gst_inclusive:
            # The card's own figure, not ex-GST plus GST, which can drift a cent.
            if self.requires_dimensions:
                return pricing.money(self.base_price)
            return self.listed_price()
        ex = self.display_price_ex_gst
        return pricing.money(ex + pricing.add_gst(ex))

    @property
    def price_range_label(self) -> str:
        """Human-readable price as the customer pays it ("$20 – $60", "$140 per m²")."""
        suffix = '' if self.gst_inclusive else ' + GST'
        if self.pricing_type == self.PER_UNIT_RANGE and self.min_price and self.max_price:
            return f'${self.min_price:,.0f} – ${self.max_price:,.0f}{suffix}'
        if self.requires_dimensions:
            return f'${pricing.money(self.base_price):,.0f} per m²{suffix}'
        return f'${self.listed_price():,.2f}{suffix}'


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
    #: ``transition_to`` so the workflow cannot skip steps. A quote the customer
    #: has answered can only go back to review, which starts a new revision.
    ALLOWED_TRANSITIONS = {
        DRAFT: {SUBMITTED, EXPIRED},
        SUBMITTED: {IN_REVIEW, APPROVED, DECLINED, EXPIRED},
        IN_REVIEW: {REVISED, APPROVED, DECLINED, EXPIRED},
        REVISED: {IN_REVIEW, APPROVED, DECLINED, EXPIRED},
        APPROVED: {SENT, IN_REVIEW, EXPIRED},
        SENT: {ACCEPTED, DECLINED, IN_REVIEW, EXPIRED},
        ACCEPTED: {IN_REVIEW},
        DECLINED: {IN_REVIEW},
        EXPIRED: {IN_REVIEW},
    }

    #: Statuses the customer sees on the public token link.
    CUSTOMER_VISIBLE = {SENT, ACCEPTED, DECLINED, EXPIRED}
    #: Statuses a customer has already seen. Reopening one of these for review
    #: starts a new revision, so the next send is labelled as a revised quote.
    ISSUED = {SENT, ACCEPTED, DECLINED, EXPIRED}

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
    # Separate from reviewed_by, which follows whoever last edited the quote:
    # this records who signed off the version the customer was sent.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_quotes',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
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
    project_title = models.CharField(
        max_length=255,
        blank=True,
        help_text='One-line job summary printed under the client name, e.g. "17 shopfront signs across Upolu".',
    )
    normal_rate = models.DecimalField(
        'normal rate per m²',
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='GST-exclusive standard rate. When set, the quote is shown as a special rate '
        'with the saving against this rate.',
    )
    customer_notes = models.TextField(blank=True)
    staff_notes = models.TextField(blank=True, help_text='Internal only — never shown to customers.')
    decline_reason = models.TextField(blank=True)
    revision = models.PositiveIntegerField(
        default=0, help_text='Bumped each time an issued quote is reopened for changes.'
    )
    pdf_file = models.FileField(upload_to='quotes/', blank=True, null=True)

    objects = QuoteQuerySet.as_manager()

    #: Status as last loaded from or saved to the database; None for a quote
    #: not saved yet. Lets the new-quote alert (main.signals) tell "just became
    #: a real request" apart from any other save.
    _saved_status = None

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.quote_number or f'Quote #{self.pk}'

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if 'status' in field_names:
            instance._saved_status = instance.status
        return instance

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
        items = list(self.items.all())
        totals = pricing.quote_totals(
            [item.line_total for item in items],
            urgent_fee=self.urgent_fee,
        )
        discount = pricing.money(self.discount or 0)
        subtotal = totals['subtotal']
        taxable = subtotal - discount + totals['urgent_fee']
        if taxable < 0:
            taxable = Decimal('0.00')
        gst = pricing.add_gst(taxable)

        # Quick-quote lines were priced GST-inclusive. Their GST is whatever
        # takes them back up to the typed figure, so the quote total matches
        # what staff entered to the cent instead of drifting on rounding.
        inclusive = [item for item in items if item.total_inc_gst is not None]
        if inclusive:
            inclusive_ex = sum((item.line_total for item in inclusive), Decimal('0'))
            others = taxable - inclusive_ex
            if others >= 0:
                inclusive_gst = sum(
                    (pricing.money(item.total_inc_gst) for item in inclusive), Decimal('0')
                ) - inclusive_ex
                gst = pricing.money(inclusive_gst + pricing.add_gst(others))

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

    # -- GST-inclusive figures, for what the customer sees -------------------
    @property
    def urgent_fee_inc_gst(self):
        return pricing.money(self.urgent_fee + pricing.add_gst(self.urgent_fee))

    @property
    def discount_inc_gst(self):
        return pricing.money(self.discount + pricing.add_gst(self.discount))

    def special_rate_summary(self):
        """The "special rate applied" figures for the quote document, or None.

        Prices the same square metres at ``normal_rate`` (lines with no area
        keep their own price), runs that through the same discount, urgency
        fee and GST as the real quote, and reports the difference. Returns
        None when no normal rate is set or it would not be a saving.
        """
        if self.normal_rate is None:
            return None
        items = list(self.items.all())
        sized = [item for item in items if item.area_m2 > 0]
        if not sized:
            return None
        normal_rate = pricing.money(self.normal_rate)
        normal_subtotal = sum(
            (
                pricing.line_total(normal_rate * item.area_m2, item.quantity)
                if item.area_m2 > 0
                else item.line_total
                for item in items
            ),
            Decimal('0'),
        )
        taxable = max(
            normal_subtotal - pricing.money(self.discount or 0) + pricing.money(self.urgent_fee or 0),
            Decimal('0'),
        )
        normal_total = pricing.money(taxable + pricing.add_gst(taxable))
        saving = normal_total - pricing.money(self.total)
        if saving <= 0:
            return None
        rates = {item.rate for item in sized}
        rate = rates.pop() if len(rates) == 1 else None
        return {
            'rate': rate,
            'rate_inc_gst': pricing.money(rate + pricing.add_gst(rate)) if rate is not None else None,
            'normal_rate': normal_rate,
            'normal_total': normal_total,
            'saving': saving,
        }

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
        if new_status == self.IN_REVIEW and self.status in self.ISSUED:
            self.revision += 1
            fields.append('revision')
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
        if new_status == self.APPROVED:
            self.approved_at = now
            fields.append('approved_at')
            if user is not None and user.is_authenticated:
                self.approved_by = user
                fields.append('approved_by')
        if new_status == self.SENT:
            self.sent_at = now
            fields.append('sent_at')
            # A resent revision gets a fresh window unless staff set a later date.
            if not self.valid_until or self.valid_until < now.date():
                self.valid_until = (
                    now + timedelta(days=settings.QUOTE_VALID_DAYS)
                ).date()
                fields.append('valid_until')
            # The previous answer belonged to the previous revision.
            if self.responded_at or self.decline_reason:
                self.responded_at = None
                self.decline_reason = ''
                fields += ['responded_at', 'decline_reason']
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
        return self.status != self.DRAFT

    @property
    def edit_reopens(self):
        """Saving changes sends the quote back to review as a new revision."""
        return self.status in self.ISSUED

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
    rate_per_m2 = models.DecimalField(
        'rate per m²',
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='When set with a width and height, the unit price is rate × area.',
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_inc_gst = models.DecimalField(
        'line total incl. GST',
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Quick-quote price for the whole line, GST included. When set, the line '
        'is priced from it and GST is backed out automatically.',
    )
    artwork = models.FileField(upload_to='artwork/%Y/%m/', blank=True, null=True)
    customer_brief = models.TextField(
        blank=True, help_text='What the customer wants, in their own words.'
    )
    is_urgent = models.BooleanField(default=False)
    added_by_staff = models.BooleanField(default=False)
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'pk']

    def __str__(self):
        return f'{self.description} x{self.quantity}'

    @property
    def amount_inc_gst(self):
        """This line as the customer pays it, GST included."""
        if self.total_inc_gst is not None:
            return pricing.money(self.total_inc_gst)
        return pricing.money(self.line_total + pricing.add_gst(self.line_total))

    @property
    def unit_price_inc_gst(self):
        qty = Decimal(self.quantity or 1)
        return pricing.money(self.amount_inc_gst / qty)

    def price_from_rule(self):
        """Unit price implied by the attached rule and this item's dimensions."""
        if not self.pricing_rule:
            return pricing.money(self.unit_price)
        return self.pricing_rule.unit_price(self.width_m, self.height_m)

    def save(self, *args, **kwargs):
        if self.total_inc_gst is not None:
            # The typed figure covers the whole line, whatever the quantity.
            self.line_total = pricing.strip_gst(self.total_inc_gst)
            qty = Decimal(self.quantity or 1)
            self.unit_price = pricing.money(self.line_total / qty)
        else:
            if self.rate_per_m2 is not None and self.area_m2 > 0:
                self.unit_price = pricing.money(Decimal(str(self.rate_per_m2)) * self.area_m2)
            elif self.pricing_rule and not self.added_by_staff and not self.unit_price:
                self.unit_price = self.price_from_rule()
            self.line_total = pricing.line_total(self.unit_price, self.quantity)
        if not self.size and self.width_m and self.height_m:
            self.size = f'{plain_number(self.width_m)}m × {plain_number(self.height_m)}m'
        super().save(*args, **kwargs)

    @property
    def area_m2(self):
        return pricing.area(self.width_m, self.height_m)

    @property
    def area_label(self):
        """Square metres for the quote document, e.g. "4.8"; blank with no size."""
        area = self.area_m2
        return plain_number(area.quantize(Decimal('0.01'))) if area > 0 else ''

    @property
    def rate(self):
        """GST-exclusive price per square metre, or None for an unsized line."""
        if self.area_m2 <= 0:
            return None
        if self.rate_per_m2 is not None:
            return pricing.money(self.rate_per_m2)
        return pricing.money(Decimal(str(self.unit_price)) / self.area_m2)


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
    # Blank until the client actually pays: an unpaid invoice has no method,
    # and defaulting one would put a guess into the books.
    payment_method = models.CharField(
        max_length=30, choices=PAYMENT_METHOD_CHOICES, blank=True, default=''
    )
    client_email = models.EmailField(
        blank=True,
        help_text='Where payment reminders go when the client has no customer account.',
    )
    reminder_sent_at = models.DateTimeField(
        null=True, blank=True, help_text='When the last payment reminder was emailed.'
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

    #: Days after the invoice date before an unpaid balance counts as overdue.
    PAYMENT_TERMS_DAYS = 30
    #: Minimum gap between two reminders for the same invoice.
    REMINDER_INTERVAL_DAYS = 7

    @property
    def is_overdue(self):
        """Unpaid for more than 30 days from the invoice date."""
        if self.payment_status in (self.PAID, self.TBC) or not self.date:
            return False
        return (timezone.localdate() - self.date).days > self.PAYMENT_TERMS_DAYS

    @property
    def days_overdue(self):
        if not self.is_overdue:
            return 0
        return (timezone.localdate() - self.date).days - self.PAYMENT_TERMS_DAYS

    @property
    def contact_email(self):
        """Who a reminder goes to: the linked customer's address, else the one staff entered."""
        if self.customer and self.customer.contact_email:
            return self.customer.contact_email
        return self.client_email

    @property
    def reminder_due(self):
        """Overdue, reachable, and not reminded within the last week."""
        if not self.is_overdue or self.balance <= 0 or not self.contact_email:
            return False
        if self.reminder_sent_at is None:
            return True
        return timezone.now() - self.reminder_sent_at >= timedelta(days=self.REMINDER_INTERVAL_DAYS)


class EmailTemplate(models.Model):
    """Owner-editable copy for the transactional emails."""

    QUOTE_RECEIVED = 'QUOTE_RECEIVED'
    QUOTE_SENT = 'QUOTE_SENT'
    QUOTE_ACCEPTED = 'QUOTE_ACCEPTED'
    STAFF_NEW_QUOTE = 'STAFF_NEW_QUOTE'
    STAFF_QUOTE_RESPONSE = 'STAFF_QUOTE_RESPONSE'
    MANAGER_APPROVAL_REQUEST = 'MANAGER_APPROVAL_REQUEST'
    STAFF_QUOTE_APPROVED = 'STAFF_QUOTE_APPROVED'
    PAYMENT_REMINDER = 'PAYMENT_REMINDER'
    STAFF_OVERDUE_DIGEST = 'STAFF_OVERDUE_DIGEST'
    KEY_CHOICES = [
        (QUOTE_RECEIVED, 'Customer — quote request received'),
        (QUOTE_SENT, 'Customer — your quote is ready'),
        (QUOTE_ACCEPTED, 'Customer — final quote after accepting'),
        (STAFF_NEW_QUOTE, 'Staff — new quote request'),
        (STAFF_QUOTE_RESPONSE, 'Staff — customer accepted or declined'),
        (MANAGER_APPROVAL_REQUEST, 'Manager — quote waiting for approval'),
        (STAFF_QUOTE_APPROVED, 'Staff — your quote was approved'),
        (PAYMENT_REMINDER, 'Customer — payment reminder'),
        (STAFF_OVERDUE_DIGEST, 'Staff — daily overdue invoices summary'),
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
