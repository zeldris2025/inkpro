"""Pure pricing helpers.

Kept free of Django model imports so the arithmetic can be unit tested on its
own and reused by the API, the quote wizard and the staff screens without
pulling a database row along for the ride.

All money is handled as ``Decimal`` and rounded half-up to cents at each
boundary, matching how the printed rate card quotes its figures.
"""

from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal('0.01')

#: Fallback GST rate used when Django settings are unavailable (e.g. in a bare
#: unit test). ``gst_rate()`` prefers the configured value.
DEFAULT_GST_RATE = Decimal('0.15')


def money(value) -> Decimal:
    """Coerce *value* to a Decimal rounded to whole cents."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def gst_rate() -> Decimal:
    """The configured GST rate, defaulting to the 15% used on the rate card."""
    try:
        from django.conf import settings

        return Decimal(str(settings.GST_RATE))
    except Exception:  # pragma: no cover - settings not configured
        return DEFAULT_GST_RATE


def add_gst(amount) -> Decimal:
    """GST component payable on a GST-exclusive *amount*."""
    return money(Decimal(str(amount)) * gst_rate())


def strip_gst(amount) -> Decimal:
    """Back the GST out of a GST-inclusive *amount*."""
    return money(Decimal(str(amount)) / (Decimal('1') + gst_rate()))


def area(width_m, height_m) -> Decimal:
    """Square metres for a width x height in metres, floored at zero."""
    width = Decimal(str(width_m or 0))
    height = Decimal(str(height_m or 0))
    if width <= 0 or height <= 0:
        return Decimal('0')
    return width * height


def unit_price_for(
    pricing_type,
    base_price,
    *,
    width_m=None,
    height_m=None,
    min_price=None,
    max_price=None,
    gst_inclusive=False,
) -> Decimal:
    """GST-exclusive price for a single unit of a line item.

    ``PER_METER`` and ``CUSTOM_SIZE_FORMULA`` both read ``base_price`` as a
    rate per square metre, which is how the rate card's banner and sticker
    tiers work: a 2m x 1m banner at $140/m is 2 square metres, so $280.
    ``PER_UNIT_RANGE`` covers the open-ended rows (vehicle decals at $20-$60);
    it quotes the bottom of the range until staff pin down a figure.
    """
    base = Decimal(str(base_price or 0))

    if pricing_type in ('PER_METER', 'CUSTOM_SIZE_FORMULA'):
        price = base * area(width_m, height_m)
    elif pricing_type == 'PER_UNIT_RANGE':
        price = Decimal(str(min_price if min_price is not None else base))
    else:  # FLAT_RATE and anything unrecognised
        price = base

    if max_price:
        cap = Decimal(str(max_price))
        # Only clamp open-ended range rows; area pricing legitimately exceeds
        # the per-unit ceiling once the job gets big.
        if pricing_type == 'PER_UNIT_RANGE' and price > cap:
            price = cap

    if gst_inclusive:
        price = strip_gst(price)
    return money(price)


def line_total(unit_price, quantity) -> Decimal:
    """GST-exclusive total for a line, quantity floored at zero."""
    qty = Decimal(str(quantity or 0))
    if qty < 0:
        qty = Decimal('0')
    return money(Decimal(str(unit_price or 0)) * qty)


def quote_totals(line_totals, urgent_fee=0):
    """Roll GST-exclusive *line_totals* up into a quote's money fields.

    The urgent/same-day surcharge is treated as a GST-exclusive charge added
    alongside the line items, so GST is levied on the combined figure. Returns
    a dict of ``subtotal``, ``urgent_fee``, ``gst_amount`` and ``total``.
    """
    subtotal = money(sum((Decimal(str(v)) for v in line_totals), Decimal('0')))
    urgent = money(urgent_fee or 0)
    taxable = subtotal + urgent
    gst = add_gst(taxable)
    return {
        'subtotal': subtotal,
        'urgent_fee': urgent,
        'gst_amount': gst,
        'total': money(taxable + gst),
    }
