"""Load InkPro's published rate card into the service catalogue.

Idempotent: re-running updates existing rows in place (matched on category
slug + rule name) rather than duplicating them, so it is safe to run after a
price change.

Sizes on the rate card are quoted as fixed tiers (a "2m x 1m banner"), so those
are seeded as flat-rate rows whose price already reflects the area. Each
size-based category also gets a ``Custom size`` rule carrying the per-square-
metre rate for jobs that fall outside the standard tiers.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from main.models import EmailTemplate, PricingRule, ServiceCategory, UrgentFee

FLAT = PricingRule.FLAT_RATE
RANGE = PricingRule.PER_UNIT_RANGE
CUSTOM = PricingRule.CUSTOM_SIZE_FORMULA

# (slug, name, icon, description, [rules])
# Each rule: (name, pricing_type, base_price, extra_kwargs)
RATE_CARD = [
    (
        'heat-press',
        'Heat Press Flat Rate',
        '👕',
        'Garment printing for tees, hoodies and teamwear. Flat rates by print size, '
        'with custom sizing priced on area.',
        [
            ('A5', FLAT, '5.00', {'unit_label': 'per print'}),
            ('A4', FLAT, '15.00', {'unit_label': 'per print'}),
            ('A3', FLAT, '25.00', {'unit_label': 'per print'}),
            (
                'Custom size',
                CUSTOM,
                '140.00',
                {
                    'unit_label': 'per m²',
                    'notes': 'Rate x size + GST = total.',
                },
            ),
        ],
    ),
    (
        'names-numbers',
        'Names / Numbers Vinyl',
        '🔢',
        'Cut vinyl and DTF names and numbers for sports kit, uniforms and teamwear.',
        [
            ('Name only', FLAT, '15.00', {'unit_label': 'per name'}),
            ('Name + number', FLAT, '30.00', {'unit_label': 'per set'}),
        ],
    ),
    (
        'banner-prints',
        'Banner Prints',
        '🎌',
        'Full-colour outdoor banner media, hemmed and eyeletted, priced per square metre.',
        [
            ('1m x 1m', FLAT, '140.00', {'unit_label': 'each'}),
            ('2m x 1m', FLAT, '280.00', {'unit_label': 'each'}),
            ('3m x 1m', FLAT, '420.00', {'unit_label': 'each'}),
            (
                'Custom size',
                CUSTOM,
                '140.00',
                {'unit_label': 'per m²', 'notes': 'Rate x size + GST = total.'},
            ),
        ],
    ),
    (
        'sticker-prints',
        'Sticker Prints',
        '🏷️',
        'Large-format printed sticker media for signage, windows and displays.',
        [
            ('1m x 1m', FLAT, '160.00', {'unit_label': 'each'}),
            ('2m x 1m', FLAT, '320.00', {'unit_label': 'each'}),
            ('3m x 1m', FLAT, '480.00', {'unit_label': 'each'}),
            (
                'Custom size',
                CUSTOM,
                '160.00',
                {'unit_label': 'per m²', 'notes': 'Rate x size + GST = total.'},
            ),
        ],
    ),
    (
        'small-format-stickers',
        'Small Format Stickers',
        '🍾',
        'Bottle, jar, label and packaging stickers below A5. Minimum order 10 units.',
        [
            (
                'Labels & packaging (below A5)',
                RANGE,
                '5.00',
                {
                    'min_price': '5.00',
                    'max_price': '20.00',
                    'unit_label': 'each',
                    'min_qty': 10,
                    'notes': 'Minimum quantity 10. Final price depends on size and finish.',
                },
            )
        ],
    ),
    (
        'vehicle-decals',
        'Vehicle Decals',
        '🚚',
        'Windscreen lettering, door decals and fleet branding applied to vehicles.',
        [
            ('Windscreen cutout name', FLAT, '120.00', {'unit_label': 'each'}),
            (
                'Small decals',
                RANGE,
                '20.00',
                {'min_price': '20.00', 'max_price': '60.00', 'unit_label': 'each'},
            ),
            (
                'Door decal (cut vinyl)',
                RANGE,
                '30.00',
                {'min_price': '30.00', 'max_price': '100.00', 'unit_label': 'per door'},
            ),
            ('Door sticker print', FLAT, '30.00', {'unit_label': 'per door'}),
        ],
    ),
    (
        'one-way-vision',
        'One-Way Vision Stickers',
        '🪟',
        'Perforated window film — full graphics outside, clear visibility from inside.',
        [
            (
                'One-way vision film',
                CUSTOM,
                '200.00',
                {'unit_label': 'per m²', 'notes': 'Rate x size + GST = total.'},
            )
        ],
    ),
    (
        'canvas-prints',
        'Canvas Prints',
        '🖼️',
        'Gallery-wrapped canvas prints stretched over a timber frame.',
        [
            # Trim sizes are InkPro's own, as printed on the rate card — they
            # differ slightly from ISO A-series nominals.
            ('A3', FLAT, '80.00', {'unit_label': 'each', 'notes': '350mm x 420mm'}),
            ('A2', FLAT, '100.00', {'unit_label': 'each', 'notes': '610mm x 420mm'}),
            ('A1', FLAT, '170.00', {'unit_label': 'each', 'notes': '640mm x 900mm'}),
            ('A0', FLAT, '270.00', {'unit_label': 'each', 'notes': '800mm x 1.2m'}),
        ],
    ),
    (
        'pull-up-banners',
        'Pull-Up Banners',
        '📣',
        'Retractable banner stands supplied complete with base and carry bag.',
        [
            ('850mm x 2m', FLAT, '450.00', {'unit_label': 'each', 'notes': 'Flat base. Banner, stand and carry bag included.'}),
            ('1.2m x 2m', FLAT, '600.00', {'unit_label': 'each', 'notes': 'Flat base. Banner, stand and carry bag included.'}),
        ],
    ),
    (
        'funeral-badges',
        'Funeral Badges',
        '🎗️',
        'Memorial badges printed and finished same day where required.',
        [
            ('Medium', FLAT, '2.50', {'unit_label': 'each'}),
            ('Large', FLAT, '3.50', {'unit_label': 'each'}),
        ],
    ),
]

EMAIL_TEMPLATES = [
    (
        EmailTemplate.QUOTE_RECEIVED,
        'We’ve received your quote request ({quote_number})',
        'Thanks {name} — your request has landed with our team. We’ll review the '
        'details and get a priced quote back to you shortly.',
        'Think Ink, Think Pro',
    ),
    (
        EmailTemplate.QUOTE_SENT,
        'Your InkPro quote {quote_number} is ready',
        'Hi {name}, your quote is ready. The full breakdown is attached as a PDF, '
        'and you can accept or decline online with one click.',
        'Questions? Just reply to this email and we’ll sort it out.',
    ),
    (
        EmailTemplate.QUOTE_ACCEPTED,
        'Confirmed: your final InkPro quote {quote_number}',
        'Thanks {name} — you’ve accepted quote {quote_number}. Your final quote is '
        'attached for your records.',
        'We’ll be in touch with timing. Questions? Just reply to this email.',
    ),
    (
        EmailTemplate.STAFF_QUOTE_RESPONSE,
        'Quote {quote_number} {decision} by {name}',
        '{name} has {decision} quote {quote_number}.',
        '',
    ),
    (
        EmailTemplate.STAFF_NEW_QUOTE,
        'New quote request: {quote_number}',
        'A new quote request came in from {name}.',
        '',
    ),
    (
        EmailTemplate.PAYMENT_REMINDER,
        'Friendly reminder: invoice {invoice_no}',
        'Hi {name}, our records show invoice {invoice_no} is still outstanding.',
        'If you’ve already paid, please ignore this note.',
    ),
]


class Command(BaseCommand):
    help = "Seed service categories, pricing rules, the urgent fee band and email templates."

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Delete existing categories and rules before seeding.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options['reset']:
            PricingRule.objects.all().delete()
            ServiceCategory.objects.all().delete()
            self.stdout.write(self.style.WARNING('Cleared existing catalogue.'))

        cats = rules = 0
        for order, (slug, name, icon, description, rule_specs) in enumerate(RATE_CARD, start=1):
            category, created = ServiceCategory.objects.update_or_create(
                slug=slug,
                defaults={
                    'name': name,
                    'icon': icon,
                    'description': description,
                    'display_order': order,
                    'is_active': True,
                },
            )
            cats += 1
            for rule_order, (rule_name, ptype, price, extra) in enumerate(rule_specs, start=1):
                defaults = {
                    'pricing_type': ptype,
                    'base_price': Decimal(price),
                    'display_order': rule_order,
                    'is_active': True,
                    'gst_inclusive': False,
                    'min_qty': 1,
                    'notes': '',
                    'min_price': None,
                    'max_price': None,
                    'unit_label': 'each',
                }
                for key, value in extra.items():
                    if key in ('min_price', 'max_price') and value is not None:
                        value = Decimal(value)
                    defaults[key] = value
                PricingRule.objects.update_or_create(
                    category=category, name=rule_name, defaults=defaults
                )
                rules += 1
            verb = 'Created' if created else 'Updated'
            self.stdout.write(f'  {verb} {name} ({len(rule_specs)} rules)')

        UrgentFee.objects.update_or_create(
            min_amount=Decimal('50.00'),
            max_amount=Decimal('100.00'),
            defaults={
                'description': 'Same-day / rush turnaround surcharge, added to the normal price.',
                'is_active': True,
            },
        )

        for key, subject, intro, outro in EMAIL_TEMPLATES:
            EmailTemplate.objects.update_or_create(
                key=key,
                defaults={'subject': subject, 'intro': intro, 'outro': outro, 'is_active': True},
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'Seeded {cats} categories, {rules} pricing rules, '
                f'1 urgent fee band and {len(EMAIL_TEMPLATES)} email templates.'
            )
        )
