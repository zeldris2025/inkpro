"""Populate a demo dataset so the dashboard and register have something to show.

Development convenience only — refuses to run unless ``DEBUG`` is on or
``--force`` is passed, so it can never be fired at production data.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from main.models import Customer, Invoice, PricingRule, Quote, QuoteItem, UrgentFee

CLIENTS = [
    ('Kaiwaka Rugby Club', Customer.BUSINESS, 'Teamwear names & numbers'),
    ('Harbour Cafe', Customer.BUSINESS, 'A-frame and window graphics'),
    ('Te Awa Motors', Customer.BUSINESS, 'Fleet door decals'),
    ('Whangarei Funeral Home', Customer.BUSINESS, 'Memorial badges'),
    ('Northland Scaffolding', Customer.BUSINESS, 'Site banners'),
    ('Bright Sparks Electrical', Customer.BUSINESS, 'Van signage'),
    ('Coast Surf Club', Customer.BUSINESS, 'Event banners'),
    ('Ana Whitiora', Customer.INDIVIDUAL, 'Canvas print'),
]


class Command(BaseCommand):
    help = 'Create demo customers, quotes and register entries for development.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Allow running with DEBUG off.')
        parser.add_argument('--quotes', type=int, default=24)
        parser.add_argument('--invoices', type=int, default=40)

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options['force']:
            raise CommandError('Refusing to seed demo data with DEBUG off. Pass --force if you mean it.')

        rules = list(PricingRule.objects.filter(is_active=True).select_related('category'))
        if not rules:
            raise CommandError('No pricing rules found. Run `manage.py seed_ratecard` first.')

        random.seed(20260916)
        today = timezone.localdate()
        urgent_fee = UrgentFee.default_amount()

        customers = []
        for name, kind, _ in CLIENTS:
            customer, _ = Customer.objects.get_or_create(
                company_name=name,
                defaults={
                    'customer_type': kind,
                    'email': name.lower().replace(' ', '.').replace('&', 'and') + '@example.com',
                    'phone': f'021 555 {random.randint(1000, 9999)}',
                },
            )
            customers.append(customer)

        statuses = (
            [Quote.SUBMITTED] * 4 + [Quote.IN_REVIEW] * 3 + [Quote.APPROVED] * 2
            + [Quote.SENT] * 5 + [Quote.ACCEPTED] * 8 + [Quote.DECLINED] * 2
        )

        quotes_made = 0
        for index in range(options['quotes']):
            customer = random.choice(customers)
            status = statuses[index % len(statuses)]
            urgent = random.random() < 0.2
            created = timezone.now() - timedelta(days=random.randint(0, 330))

            quote = Quote.objects.create(
                customer=customer,
                status=status,
                is_urgent=urgent,
                urgent_fee=urgent_fee if urgent else Decimal('0'),
                customer_notes=random.choice(
                    ['Needed before the weekend.', 'Artwork to follow by email.', '', 'Match last order please.']
                ),
            )
            # auto_now_add blocks setting created_at on create.
            Quote.objects.filter(pk=quote.pk).update(created_at=created, submitted_at=created)

            for _ in range(random.randint(1, 3)):
                rule = random.choice(rules)
                width = height = None
                if rule.requires_dimensions:
                    width = Decimal(str(random.choice([1, 1.5, 2, 2.5, 3])))
                    height = Decimal(str(random.choice([1, 1.2, 2])))
                quantity = max(rule.min_qty, random.randint(1, 12))
                QuoteItem.objects.create(
                    quote=quote,
                    category=rule.category,
                    pricing_rule=rule,
                    description=f'{rule.category.name} — {rule.name}',
                    width_m=width,
                    height_m=height,
                    quantity=quantity,
                    unit_price=rule.unit_price(width, height),
                )
            quote.recalculate()
            quotes_made += 1

            if status == Quote.ACCEPTED:
                Invoice.objects.create(
                    quote=quote,
                    customer=customer,
                    job_details='; '.join(quote.items.values_list('description', flat=True))[:400],
                    date=created.date(),
                    qty=sum(quote.items.values_list('quantity', flat=True)),
                    invoice_no=f'INV-{2600 + quotes_made}',
                    invoice_amount=quote.total,
                    amount_received=quote.total if random.random() < 0.7 else Decimal('0'),
                    receipt_no=f'RCT-{900 + quotes_made}' if random.random() < 0.7 else '',
                    payment_method=random.choice(
                        [Invoice.BANK_TRANSFER, Invoice.CASH_CHQ, Invoice.PO_ON_ACCOUNT, Invoice.CARD]
                    ),
                )

        # Historical register entries, spread across the year for the charts.
        for index in range(options['invoices']):
            name, _, job = random.choice(CLIENTS)
            amount = Decimal(random.choice([80, 140, 161, 322, 450, 525, 600, 840, 1200]))
            paid_ratio = random.choice([0, 0, 0.5, 1, 1, 1])
            Invoice.objects.create(
                client_name=name,
                job_details=job,
                date=today - timedelta(days=random.randint(5, 350)),
                qty=random.randint(1, 60),
                invoice_no=f'INV-{1000 + index}',
                invoice_amount=amount,
                amount_received=(amount * Decimal(str(paid_ratio))).quantize(Decimal('0.01')),
                receipt_no=f'RCT-{500 + index}' if paid_ratio else '',
                payment_method=random.choice(
                    [Invoice.BANK_TRANSFER, Invoice.CASH_CHQ, Invoice.PO_ON_ACCOUNT, Invoice.CARD, Invoice.OTHER]
                ),
            )

        # A staff login to look around with.
        staff, created = User.objects.get_or_create(
            username='staff', defaults={'email': 'staff@inkpro.example', 'is_staff': True}
        )
        if created:
            staff.set_password('inkpro123')
            staff.save()
        group, _ = Group.objects.get_or_create(name='Staff')
        staff.groups.add(group)

        self.stdout.write(
            self.style.SUCCESS(
                f'Seeded {quotes_made} quotes, {Invoice.objects.count()} register entries '
                f'and {len(customers)} customers.'
            )
        )
        if created:
            self.stdout.write(self.style.WARNING('Staff login created — username "staff", password "inkpro123".'))
