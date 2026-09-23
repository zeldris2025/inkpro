"""Tests for the parts of InkPro where a silent mistake costs real money:
pricing arithmetic, quote status transitions and invoice balance derivation.
"""

import base64
import io
import pathlib
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.core import mail
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from main import pricing
from main.graph_mail import GraphError
from main.models import (
    CategoryImage,
    Customer,
    EmailTemplate,
    Invoice,
    InvalidTransition,
    PricingRule,
    Quote,
    QuoteItem,
    ServiceCategory,
    UrgentFee,
)


class PricingArithmeticTests(TestCase):
    """The rate card is the spec; these assert we reproduce its numbers."""

    def test_flat_rate_matches_card(self):
        self.assertEqual(pricing.unit_price_for('FLAT_RATE', Decimal('15')), Decimal('15.00'))

    def test_per_meter_prices_by_area(self):
        # Banner media is $140/m2: a 2m x 1m banner is 2 square metres.
        self.assertEqual(
            pricing.unit_price_for('PER_METER', Decimal('140'), width_m=2, height_m=1),
            Decimal('280.00'),
        )
        self.assertEqual(
            pricing.unit_price_for('PER_METER', Decimal('160'), width_m=3, height_m=1),
            Decimal('480.00'),
        )

    def test_gst_matches_the_cards_second_column(self):
        for ex_gst, inc_gst in [('140', '161.00'), ('280', '322.00'), ('420', '483.00'),
                                ('160', '184.00'), ('320', '368.00'), ('480', '552.00')]:
            amount = Decimal(ex_gst)
            with self.subTest(amount=ex_gst):
                self.assertEqual(amount + pricing.add_gst(amount), Decimal(inc_gst))

    def test_ranged_pricing_quotes_the_floor_and_respects_the_ceiling(self):
        price = pricing.unit_price_for(
            'PER_UNIT_RANGE', Decimal('0'), min_price=Decimal('20'), max_price=Decimal('60')
        )
        self.assertEqual(price, Decimal('20.00'))

    def test_gst_inclusive_rule_is_backed_out(self):
        # A rule stored at $115 including GST is $100 ex-GST.
        self.assertEqual(
            pricing.unit_price_for('FLAT_RATE', Decimal('115'), gst_inclusive=True),
            Decimal('100.00'),
        )

    def test_zero_and_negative_dimensions_price_at_zero(self):
        self.assertEqual(pricing.area(0, 5), Decimal('0'))
        self.assertEqual(pricing.area(-2, 3), Decimal('0'))
        self.assertEqual(pricing.area(None, None), Decimal('0'))

    def test_negative_quantity_cannot_produce_a_credit(self):
        self.assertEqual(pricing.line_total(Decimal('50'), -3), Decimal('0.00'))

    def test_quote_totals_levies_gst_on_the_urgent_fee(self):
        totals = pricing.quote_totals([Decimal('280')], urgent_fee=Decimal('50'))
        self.assertEqual(totals['subtotal'], Decimal('280.00'))
        self.assertEqual(totals['gst_amount'], Decimal('49.50'))
        self.assertEqual(totals['total'], Decimal('379.50'))

    def test_rounding_is_half_up_at_the_cent(self):
        self.assertEqual(pricing.money(Decimal('2.345')), Decimal('2.35'))
        self.assertEqual(pricing.add_gst(Decimal('2.50')), Decimal('0.38'))


class CatalogueTests(TestCase):
    def setUp(self):
        self.category = ServiceCategory.objects.create(name='Banners', slug='banners')

    def test_size_based_rules_flag_themselves_as_needing_dimensions(self):
        rule = PricingRule.objects.create(
            category=self.category, name='Custom', pricing_type=PricingRule.PER_METER, base_price=140
        )
        self.assertTrue(rule.requires_dimensions)

    def test_flat_rules_do_not_require_dimensions(self):
        rule = PricingRule.objects.create(
            category=self.category, name='1m x 1m', base_price=140
        )
        self.assertFalse(rule.requires_dimensions)
        self.assertEqual(rule.display_price_inc_gst, Decimal('161.00'))

    def test_price_from_reports_the_cheapest_active_option(self):
        PricingRule.objects.create(category=self.category, name='A', base_price=140)
        PricingRule.objects.create(category=self.category, name='B', base_price=80)
        PricingRule.objects.create(category=self.category, name='C', base_price=10, is_active=False)
        self.assertEqual(self.category.price_from, Decimal('80.00'))


class QuoteTotalsTests(TestCase):
    def setUp(self):
        self.category = ServiceCategory.objects.create(name='Banners', slug='banners')
        self.rule = PricingRule.objects.create(
            category=self.category, name='2m x 1m', base_price=Decimal('280')
        )
        self.quote = Quote.objects.create(guest_name='Ada', guest_email='ada@example.com')

    def add_item(self, quantity=1, unit_price=None):
        return QuoteItem.objects.create(
            quote=self.quote,
            category=self.category,
            pricing_rule=self.rule,
            description='Banner',
            quantity=quantity,
            unit_price=unit_price if unit_price is not None else self.rule.unit_price(),
        )

    def test_line_total_is_derived_on_save(self):
        item = self.add_item(quantity=3)
        self.assertEqual(item.line_total, Decimal('840.00'))

    def test_recalculate_rolls_up_items_and_gst(self):
        self.add_item(quantity=2)
        self.quote.recalculate()
        self.assertEqual(self.quote.subtotal, Decimal('560.00'))
        self.assertEqual(self.quote.gst_amount, Decimal('84.00'))
        self.assertEqual(self.quote.total, Decimal('644.00'))

    def test_discount_reduces_the_gst_base(self):
        self.add_item(quantity=1)
        self.quote.discount = Decimal('80')
        self.quote.save()
        self.quote.recalculate()
        self.assertEqual(self.quote.gst_amount, Decimal('30.00'))
        self.assertEqual(self.quote.total, Decimal('230.00'))

    def test_discount_larger_than_subtotal_cannot_go_negative(self):
        self.add_item(quantity=1)
        self.quote.discount = Decimal('9999')
        self.quote.save()
        self.quote.recalculate()
        self.assertEqual(self.quote.total, Decimal('0.00'))

    def test_total_override_wins(self):
        self.add_item(quantity=2)
        self.quote.total_override = Decimal('500')
        self.quote.save()
        self.quote.recalculate()
        self.assertEqual(self.quote.total, Decimal('500.00'))
        # The calculated components are still reported for transparency.
        self.assertEqual(self.quote.subtotal, Decimal('560.00'))

    def test_quote_numbers_increment_within_the_year(self):
        year = timezone.localdate().year
        second = Quote.objects.create()
        self.assertTrue(self.quote.quote_number.startswith(f'INK-Q-{year}-'))
        self.assertNotEqual(self.quote.quote_number, second.quote_number)
        self.assertEqual(
            int(second.quote_number.rsplit('-', 1)[1]),
            int(self.quote.quote_number.rsplit('-', 1)[1]) + 1,
        )

    def test_size_label_is_filled_in_from_dimensions(self):
        item = QuoteItem.objects.create(
            quote=self.quote, category=self.category, description='Custom',
            width_m=Decimal('2.5'), height_m=Decimal('1'), quantity=1, unit_price=Decimal('350'),
        )
        self.assertEqual(item.size, '2.5m x 1m')


class QuoteTransitionTests(TestCase):
    def setUp(self):
        self.quote = Quote.objects.create(guest_name='Ada', guest_email='ada@example.com')
        self.user = User.objects.create_user('staffer', 'staff@example.com', 'pw', is_staff=True)

    def test_submitting_stamps_the_timestamp(self):
        self.quote.transition_to(Quote.SUBMITTED)
        self.assertEqual(self.quote.status, Quote.SUBMITTED)
        self.assertIsNotNone(self.quote.submitted_at)

    def test_review_records_the_reviewer(self):
        self.quote.transition_to(Quote.SUBMITTED)
        self.quote.transition_to(Quote.IN_REVIEW, user=self.user)
        self.assertEqual(self.quote.reviewed_by, self.user)
        self.assertIsNotNone(self.quote.reviewed_at)

    def test_sending_sets_a_validity_window(self):
        self.quote.transition_to(Quote.SUBMITTED)
        self.quote.transition_to(Quote.APPROVED, user=self.user)
        self.quote.transition_to(Quote.SENT, user=self.user)
        self.assertIsNotNone(self.quote.valid_until)
        self.assertGreater(self.quote.valid_until, timezone.localdate())

    def test_illegal_skips_are_rejected(self):
        with self.assertRaises(InvalidTransition):
            self.quote.transition_to(Quote.SENT)
        self.assertEqual(self.quote.status, Quote.DRAFT)

    def test_accepted_is_terminal(self):
        for status in (Quote.SUBMITTED, Quote.APPROVED, Quote.SENT, Quote.ACCEPTED):
            self.quote.transition_to(status)
        with self.assertRaises(InvalidTransition):
            self.quote.transition_to(Quote.DECLINED)

    def test_staying_put_is_always_allowed(self):
        self.assertTrue(self.quote.can_transition_to(Quote.DRAFT))


class InvoiceBalanceTests(TestCase):
    def test_balance_and_status_derive_from_the_amounts(self):
        cases = [
            (Decimal('100'), Decimal('0'), Decimal('100.00'), Invoice.NOT_PAID),
            (Decimal('100'), Decimal('40'), Decimal('60.00'), Invoice.PARTIAL),
            (Decimal('100'), Decimal('100'), Decimal('0.00'), Invoice.PAID),
            (Decimal('100'), Decimal('120'), Decimal('-20.00'), Invoice.PAID),
            (Decimal('0'), Decimal('0'), Decimal('0.00'), Invoice.TBC),
        ]
        for amount, received, balance, status in cases:
            with self.subTest(amount=amount, received=received):
                invoice = Invoice.objects.create(
                    client_name='Test', invoice_amount=amount, amount_received=received
                )
                self.assertEqual(invoice.balance, balance)
                self.assertEqual(invoice.payment_status, status)

    def test_manual_status_survives_a_save(self):
        invoice = Invoice.objects.create(
            client_name='Test',
            invoice_amount=Decimal('500'),
            payment_status=Invoice.TBC,
            status_is_manual=True,
        )
        self.assertEqual(invoice.payment_status, Invoice.TBC)
        invoice.save()
        self.assertEqual(invoice.payment_status, Invoice.TBC)

    def test_overdue_needs_an_unpaid_balance_and_an_old_date(self):
        old = timezone.localdate() - timezone.timedelta(days=45)
        unpaid = Invoice.objects.create(client_name='X', invoice_amount=100, date=old)
        paid = Invoice.objects.create(
            client_name='Y', invoice_amount=100, amount_received=100, date=old
        )
        recent = Invoice.objects.create(
            client_name='Z', invoice_amount=100, date=timezone.localdate()
        )
        self.assertTrue(unpaid.is_overdue)
        self.assertFalse(paid.is_overdue)
        self.assertFalse(recent.is_overdue)

    def test_client_falls_back_to_the_free_text_name(self):
        customer = Customer.objects.create(company_name='Harbour Cafe')
        self.assertEqual(Invoice.objects.create(customer=customer).client, 'Harbour Cafe')
        self.assertEqual(Invoice.objects.create(client_name='Legacy Co').client, 'Legacy Co')


class QuoteWizardFlowTests(TestCase):
    """End-to-end: build a quote as a guest, submit it, then send it as staff."""

    def setUp(self):
        self.category = ServiceCategory.objects.create(name='Banners', slug='banners')
        self.rule = PricingRule.objects.create(
            category=self.category, name='2m x 1m', base_price=Decimal('280')
        )
        UrgentFee.objects.create(min_amount=Decimal('50'), max_amount=Decimal('100'))
        self.staff = User.objects.create_user('staffer', 'staff@example.com', 'pw', is_staff=True)

    def test_guest_can_build_and_submit_a_quote(self):
        add = self.client.post(
            reverse('quote_configure', args=['banners']),
            {'pricing_rule': self.rule.pk, 'quantity': 2},
        )
        self.assertRedirects(add, reverse('quote_review'))

        quote = Quote.objects.get(status=Quote.DRAFT)
        self.assertEqual(quote.total, Decimal('644.00'))

        submit = self.client.post(
            reverse('quote_review'),
            {
                'guest_name': 'Ada Lovelace',
                'guest_email': 'ada@example.com',
                'guest_phone': '021 555 0000',
                'customer_notes': 'Need it by Friday.',
                'is_urgent': 'on',
            },
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.SUBMITTED)
        self.assertEqual(quote.urgent_fee, Decimal('50.00'))
        self.assertEqual(quote.total, Decimal('701.50'))
        self.assertRedirects(submit, reverse('quote_submitted', args=[quote.access_token]))
        # Customer acknowledgement and the staff alert both go out.
        self.assertEqual(len(mail.outbox), 2)

    def test_minimum_quantity_is_enforced(self):
        self.rule.min_qty = 10
        self.rule.save()
        response = self.client.post(
            reverse('quote_configure', args=['banners']),
            {'pricing_rule': self.rule.pk, 'quantity': 3},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'minimum order')
        self.assertFalse(QuoteItem.objects.exists())

    def test_size_based_rule_requires_dimensions(self):
        rule = PricingRule.objects.create(
            category=self.category, name='Custom', pricing_type=PricingRule.PER_METER, base_price=140
        )
        response = self.client.post(
            reverse('quote_configure', args=['banners']), {'pricing_rule': rule.pk, 'quantity': 1}
        )
        self.assertContains(response, 'Required for size-based pricing.')

    def test_review_redirects_when_the_quote_is_empty(self):
        self.assertRedirects(self.client.get(reverse('quote_review')), reverse('quote_builder'))

    def test_approve_and_send_emails_the_customer_and_opens_the_link(self):
        quote = Quote.objects.create(
            guest_name='Ada', guest_email='ada@example.com', status=Quote.SUBMITTED
        )
        QuoteItem.objects.create(
            quote=quote, category=self.category, pricing_rule=self.rule,
            description='Banner', quantity=1, unit_price=Decimal('280'),
        )
        quote.recalculate()

        self.client.force_login(self.staff)
        self.client.post(
            reverse('staff_quote_transition', args=[quote.pk]), {'status': 'APPROVE_AND_SEND'}
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.SENT)
        self.assertTrue(quote.pdf_file)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(quote.access_token, mail.outbox[0].alternatives[0][0])
        self.assertTrue(mail.outbox[0].attachments)

        # The customer's token link now works without logging in.
        self.client.logout()
        public = self.client.get(reverse('public_quote', args=[quote.access_token]))
        self.assertEqual(public.status_code, 200)

    def test_accepting_opens_a_register_entry(self):
        quote = Quote.objects.create(
            guest_name='Ada', guest_email='ada@example.com', status=Quote.SENT, total=Decimal('322')
        )
        QuoteItem.objects.create(
            quote=quote, category=self.category, description='Banner',
            quantity=1, unit_price=Decimal('280'),
        )
        self.client.post(
            reverse('public_quote_respond', args=[quote.access_token]), {'decision': 'accept'}
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.ACCEPTED)

        invoice = Invoice.objects.get(quote=quote)
        self.assertEqual(invoice.invoice_amount, quote.total)
        self.assertEqual(invoice.payment_status, Invoice.NOT_PAID)

    def test_accepting_twice_does_not_duplicate_the_invoice(self):
        quote = Quote.objects.create(
            guest_name='Ada', guest_email='ada@example.com', status=Quote.SENT, total=Decimal('322')
        )
        url = reverse('public_quote_respond', args=[quote.access_token])
        self.client.post(url, {'decision': 'accept'})
        self.client.post(url, {'decision': 'accept'})
        self.assertEqual(Invoice.objects.filter(quote=quote).count(), 1)

    def test_a_draft_quote_is_not_visible_on_the_public_link(self):
        quote = Quote.objects.create(guest_name='Ada', status=Quote.SUBMITTED)
        response = self.client.get(reverse('public_quote', args=[quote.access_token]))
        self.assertEqual(response.status_code, 403)


class AccessControlTests(TestCase):
    def setUp(self):
        self.customer_user = User.objects.create_user('cust', 'c@example.com', 'pw')
        self.staff_user = User.objects.create_user('staffer', 's@example.com', 'pw', is_staff=True)

    def test_staff_pages_reject_anonymous_visitors(self):
        for name in ('staff_dashboard', 'staff_quote_inbox', 'staff_register'):
            with self.subTest(view=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('login'), response['Location'])

    def test_staff_pages_reject_ordinary_customers(self):
        self.client.force_login(self.customer_user)
        response = self.client.get(reverse('staff_dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_staff_can_reach_the_portal(self):
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(reverse('staff_dashboard')).status_code, 200)

    def test_dashboard_api_requires_staff(self):
        self.assertEqual(self.client.get(reverse('api_dashboard_stats')).status_code, 403)
        self.client.force_login(self.customer_user)
        self.assertEqual(self.client.get(reverse('api_dashboard_stats')).status_code, 403)
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(reverse('api_dashboard_stats')).status_code, 200)

    def test_customers_cannot_read_another_customers_quote(self):
        other = Quote.objects.create(guest_email='someone@else.example', status=Quote.SENT)
        self.client.force_login(self.customer_user)
        response = self.client.get(reverse('my_quote_detail', args=[other.pk]))
        self.assertEqual(response.status_code, 404)


class PublicPageTests(TestCase):
    def setUp(self):
        category = ServiceCategory.objects.create(
            name='Banners', slug='banners', description='Outdoor banner media.'
        )
        PricingRule.objects.create(category=category, name='2m x 1m', base_price=Decimal('280'))

    def test_every_public_page_renders(self):
        for name in ('home', 'services', 'about', 'gallery', 'contact', 'quote_builder',
                     'login', 'signup', 'health_check'):
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_service_detail_renders_the_rate_table(self):
        response = self.client.get(reverse('service_detail', args=['banners']))
        self.assertContains(response, '2m x 1m')
        self.assertContains(response, '280')

    def test_live_price_endpoint_returns_the_rate_cards_figures(self):
        rule = PricingRule.objects.get(name='2m x 1m')
        response = self.client.get(reverse('api_rule_price'), {'rule': rule.pk, 'quantity': 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['line_total'], 560.0)
        self.assertEqual(response.json()['inc_gst'], 644.0)

    def test_live_price_endpoint_handles_an_unknown_rule(self):
        self.assertEqual(
            self.client.get(reverse('api_rule_price'), {'rule': 'nope'}).status_code, 404
        )


class DashboardApiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('s', 's@example.com', 'pw', is_staff=True)
        self.client.force_login(self.staff)
        Invoice.objects.create(
            client_name='Alpha', invoice_amount=Decimal('1000'),
            amount_received=Decimal('1000'), date=timezone.localdate(),
        )
        Invoice.objects.create(
            client_name='Beta', invoice_amount=Decimal('500'), date=timezone.localdate()
        )

    def test_kpis_match_the_registers_summary(self):
        data = self.client.get(reverse('api_dashboard_stats')).json()
        self.assertEqual(data['kpi']['total_invoiced'], 1500.0)
        self.assertEqual(data['kpi']['total_received'], 1000.0)
        self.assertEqual(data['kpi']['outstanding'], 500.0)
        self.assertEqual(data['kpi']['paid_count'], 1)

    def test_funnel_never_widens_as_it_descends(self):
        Quote.objects.create(status=Quote.SUBMITTED)
        Quote.objects.create(status=Quote.SENT)
        Quote.objects.create(status=Quote.ACCEPTED)
        funnel = self.client.get(reverse('api_dashboard_stats')).json()['funnel']
        self.assertGreaterEqual(funnel['submitted'], funnel['sent'])
        self.assertGreaterEqual(funnel['sent'], funnel['accepted'])
        self.assertEqual(funnel['accepted'], 1)

    def test_top_clients_merges_linked_and_free_text_names(self):
        data = self.client.get(reverse('api_dashboard_stats')).json()
        labels = [row['label'] for row in data['top_clients']]
        self.assertIn('Alpha', labels)
        self.assertEqual(data['top_clients'][0]['value'], 1000.0)


class RegisterViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user('s', 's@example.com', 'pw', is_staff=True)
        self.client.force_login(self.staff)
        self.invoice = Invoice.objects.create(
            client_name='Harbour Cafe', job_details='Banner', invoice_amount=Decimal('322'),
            invoice_no='INV-1002', date=timezone.localdate(),
        )

    def test_register_renders_the_spreadsheet_columns(self):
        response = self.client.get(reverse('staff_register'))
        for header in ('Recharge / Job Details', 'Invoice No.', 'Amount Received', 'Balance'):
            self.assertContains(response, header)

    def test_csv_export_includes_the_rows(self):
        response = self.client.get(reverse('staff_register'), {'export': 'csv'})
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn('Harbour Cafe', response.content.decode())

    def test_inline_edit_recalculates_balance_and_status(self):
        response = self.client.post(
            reverse('staff_invoice_inline', args=[self.invoice.pk]),
            {'field': 'amount_received', 'value': '322'},
        )
        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.balance, Decimal('0.00'))
        self.assertEqual(self.invoice.payment_status, Invoice.PAID)

    def test_inline_edit_refuses_fields_that_are_not_editable(self):
        response = self.client.post(
            reverse('staff_invoice_inline', args=[self.invoice.pk]),
            {'field': 'payment_status', 'value': 'PAID'},
        )
        self.assertEqual(response.status_code, 400)

    def test_search_filters_the_rows(self):
        Invoice.objects.create(client_name='Someone Else', invoice_amount=Decimal('50'))
        response = self.client.get(reverse('staff_register'), {'q': 'Harbour'})
        self.assertContains(response, 'Harbour Cafe')
        self.assertNotContains(response, 'Someone Else')


class EmailTemplateTests(TestCase):
    def test_placeholders_are_filled_in(self):
        template = EmailTemplate.objects.create(
            key=EmailTemplate.QUOTE_SENT,
            subject='Quote {quote_number} for {name}',
            intro='Hi {name}.',
        )
        rendered = template.render(quote_number='INK-Q-2026-0001', name='Ada')
        self.assertEqual(rendered['subject'], 'Quote INK-Q-2026-0001 for Ada')
        self.assertEqual(rendered['intro'], 'Hi Ada.')

    def test_an_unknown_placeholder_is_left_alone_rather_than_crashing(self):
        template = EmailTemplate.objects.create(
            key=EmailTemplate.QUOTE_SENT, subject='Hello {nonexistent}', intro=''
        )
        self.assertEqual(template.render(name='Ada')['subject'], 'Hello {nonexistent}')


class BrandAssetTests(TestCase):
    """The logo variants each surface needs, and the fact that a missing file
    degrades to the text lockup rather than a broken image."""

    def test_the_built_logo_variants_exist(self):
        from django.conf import settings

        img = settings.STATICFILES_DIRS[0] / 'img'
        for name in ('inkpro-logo.png', 'inkpro-logo-on-dark.png', 'favicon.png'):
            with self.subTest(asset=name):
                self.assertTrue((img / name).exists(), f'{name} missing — run build_logo_assets')

    def test_each_surface_gets_the_artwork_drawn_for_it(self):
        # Black header takes the dark-background artwork; white footer takes
        # the light-background one. Neither is recoloured.
        body = self.client.get(reverse('contact')).content.decode()
        header = body[body.index('<header'):body.index('</header>')]
        footer = body[body.index('<footer'):body.index('</footer>')]
        self.assertIn('inkpro-logo-on-dark.png', header)
        self.assertIn('inkpro-logo.png', footer)
        self.assertNotIn('inkpro-logo-on-dark.png', footer)

    def test_both_masters_are_kept_outside_the_generated_output(self):
        # Regenerating static/img must never clobber the source artwork.
        from django.conf import settings

        assets = pathlib.Path(settings.BASE_DIR) / 'assets'
        for name in ('logo-master-on-light.png', 'logo-master-on-dark.png'):
            with self.subTest(master=name):
                self.assertTrue((assets / name).exists())

    def test_the_dark_variant_has_its_background_knocked_out(self):
        # The supplied dark master is on opaque black; left as-is it renders as
        # a visible box against the header's #0A0A0A.
        from django.conf import settings
        from PIL import Image

        art = Image.open(settings.STATICFILES_DIRS[0] / 'img' / 'inkpro-logo-on-dark.png')
        self.assertEqual(art.mode, 'RGBA')
        self.assertEqual(art.convert('RGBA').getpixel((0, 0))[3], 0)

    @override_settings(ENABLE_3D_HERO=True)
    def test_the_3d_stamp_texture_uses_the_on_dark_variant(self):
        # That stage renders against the dark page body, not the chrome.
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'data-logo-url="/static/img/inkpro-logo-on-dark.png"')

    def test_email_and_pdf_use_the_original_colours_on_an_absolute_url(self):
        from main.context_processors import absolute_logo_url

        url = absolute_logo_url()
        self.assertTrue(url.startswith('http'), 'mail clients cannot resolve a relative URL')
        self.assertIn('inkpro-logo.png', url)
        self.assertNotIn('on-dark', url)

    def test_a_missing_asset_degrades_to_the_text_lockup(self):
        from main import context_processors

        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        with mock.patch.object(context_processors, '_static_if_present', return_value=None):
            context = context_processors.site_settings(request)
        self.assertIsNone(context['LOGO_URL'])
        self.assertEqual(context['TAGLINE'], 'Think Ink, Think Pro')


class WebGLLayerTests(TestCase):
    """The scroll-driven 3D layer is progressive enhancement: the homepage must
    be complete without it, and it must never load on other pages."""

    def setUp(self):
        category = ServiceCategory.objects.create(name='Banners', slug='banners')
        PricingRule.objects.create(category=category, name='2m x 1m', base_price=Decimal('280'))

    def test_the_layer_is_off_by_default(self):
        # Currently disabled. The code stays in the tree; ENABLE_3D_HERO brings
        # it back without a template edit.
        response = self.client.get(reverse('home'))
        self.assertNotContains(response, 'ink3d-layer')
        self.assertNotContains(response, 'ink3d-canvas')
        self.assertNotContains(response, 'inkpro3d-loader.js')
        self.assertNotContains(response, 'press-fallback.svg')
        # The bare [data-scene] anchors stay on the sections on purpose: they
        # are inert attributes, and leaving them means the flag alone controls
        # the feature with no template edit needed to switch it back on.

    def test_the_staging_section_is_hidden_with_it(self):
        # That section exists only to stage the finale; without the scene it is
        # a full-height empty block.
        self.assertNotContains(self.client.get(reverse('home')), 'Straight off')

    @override_settings(ENABLE_3D_HERO=True)
    def test_the_flag_brings_back_the_whole_layer(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'ink3d-layer')
        self.assertContains(response, 'press-fallback.svg')
        for scene in ('press', 'sheet', 'cmyk', 'showcase', 'stamp'):
            with self.subTest(scene=scene):
                self.assertContains(response, f'data-scene="{scene}"')

    @override_settings(ENABLE_3D_HERO=True)
    def test_only_the_gate_is_a_script_tag(self):
        # The Three.js scene is dynamically imported from a data attribute
        # after first paint, never loaded up front.
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'inkpro3d-loader.js')
        self.assertNotContains(response, '<script src="/static/js/inkpro3d.js"')
        self.assertContains(response, 'data-module-url=')

    @override_settings(ENABLE_3D_HERO=True)
    def test_the_layer_is_homepage_only_even_when_enabled(self):
        for name in ('services', 'about', 'gallery', 'contact', 'quote_builder'):
            with self.subTest(view=name):
                self.assertNotContains(self.client.get(reverse(name)), 'ink3d-layer')

    def test_the_scene_assets_exist(self):
        from django.conf import settings

        static_dir = settings.STATICFILES_DIRS[0]
        for path in ('js/inkpro3d.js', 'js/inkpro3d-loader.js', 'img/press-fallback.svg'):
            with self.subTest(asset=path):
                self.assertTrue((static_dir / path).exists())


class CategoryImageTests(TestCase):
    """Product photography imported from the rate card PDF."""

    def setUp(self):
        self.category = ServiceCategory.objects.create(name='Banners', slug='banners')

    def make_image(self, **kwargs):
        defaults = {'category': self.category, 'image': 'services/test.jpg'}
        return CategoryImage.objects.create(**{**defaults, **kwargs})

    def test_card_image_prefers_the_flagged_hero(self):
        self.make_image(caption='first', display_order=1)
        hero = self.make_image(caption='hero', is_hero=True, display_order=2)
        self.assertEqual(self.category.card_image.name, hero.image.name)

    def test_card_image_falls_back_to_the_first_photo(self):
        first = self.make_image(caption='only', display_order=1)
        self.make_image(caption='second', display_order=2)
        self.assertEqual(self.category.card_image.name, first.image.name)

    def test_card_image_is_none_when_there_are_no_photos(self):
        # Templates rely on this to fall back to the emoji icon.
        self.assertIsNone(self.category.card_image)

    def test_description_falls_back_through_alt_then_caption(self):
        self.assertEqual(self.make_image(alt_text='Alt', caption='Cap').description, 'Alt')
        self.assertEqual(self.make_image(caption='Cap').description, 'Cap')
        self.assertEqual(self.make_image().description, 'Banners')

    def test_source_hash_makes_reimport_idempotent(self):
        # The importer matches on content hash so re-running updates in place.
        CategoryImage.objects.update_or_create(
            source_hash='abc123', defaults={'category': self.category, 'image': 'a.jpg'}
        )
        CategoryImage.objects.update_or_create(
            source_hash='abc123', defaults={'category': self.category, 'image': 'b.jpg'}
        )
        self.assertEqual(CategoryImage.objects.filter(source_hash='abc123').count(), 1)

    def test_gallery_lists_photos_and_filters_by_category(self):
        other = ServiceCategory.objects.create(name='Canvas', slug='canvas')
        self.make_image(caption='Banner shot')
        CategoryImage.objects.create(category=other, image='x.jpg', caption='Canvas shot')

        everything = self.client.get(reverse('gallery'))
        self.assertContains(everything, 'Banner shot')
        self.assertContains(everything, 'Canvas shot')

        filtered = self.client.get(reverse('gallery'), {'category': 'banners'})
        self.assertContains(filtered, 'Banner shot')
        self.assertNotContains(filtered, 'Canvas shot')

    def test_service_detail_shows_the_category_gallery(self):
        self.make_image(caption='Hemmed 2m banner')
        response = self.client.get(reverse('service_detail', args=['banners']))
        self.assertContains(response, 'Hemmed 2m banner')
        self.assertContains(response, 'Recent work')


class RateCardFidelityTests(TestCase):
    """The seeded catalogue must reproduce the published rate card exactly.

    These assert against the figures printed on InkPro's own PDF, so a future
    edit to the seed that drifts from the rate card fails here.
    """

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command('seed_ratecard', verbosity=0)

    def rule(self, slug, name):
        return PricingRule.objects.get(category__slug=slug, name=name)

    def test_headline_prices_match_the_printed_card(self):
        expected = [
            ('heat-press', 'A5', '5.00'),
            ('heat-press', 'A4', '15.00'),
            ('heat-press', 'A3', '25.00'),
            ('names-numbers', 'Name only', '15.00'),
            ('names-numbers', 'Name + number', '30.00'),
            ('banner-prints', '1m x 1m', '140.00'),
            ('banner-prints', '2m x 1m', '280.00'),
            ('banner-prints', '3m x 1m', '420.00'),
            ('sticker-prints', '1m x 1m', '160.00'),
            ('sticker-prints', '2m x 1m', '320.00'),
            ('sticker-prints', '3m x 1m', '480.00'),
            ('vehicle-decals', 'Windscreen cutout name', '120.00'),
            ('vehicle-decals', 'Door sticker print', '30.00'),
            ('one-way-vision', 'One-way vision film', '200.00'),
            ('canvas-prints', 'A3', '80.00'),
            ('canvas-prints', 'A2', '100.00'),
            ('canvas-prints', 'A1', '170.00'),
            ('canvas-prints', 'A0', '270.00'),
            ('pull-up-banners', '850mm x 2m', '450.00'),
            ('pull-up-banners', '1.2m x 2m', '600.00'),
            ('funeral-badges', 'Medium', '2.50'),
            ('funeral-badges', 'Large', '3.50'),
        ]
        for slug, name, price in expected:
            with self.subTest(rule=f'{slug}/{name}'):
                self.assertEqual(self.rule(slug, name).base_price, Decimal(price))

    def test_gst_column_matches_the_printed_card(self):
        # The card prints both a "Normal" and a "GST" price for each tier.
        for slug, name, inc_gst in [
            ('banner-prints', '1m x 1m', '161.00'),
            ('banner-prints', '2m x 1m', '322.00'),
            ('banner-prints', '3m x 1m', '483.00'),
            ('sticker-prints', '1m x 1m', '184.00'),
            ('sticker-prints', '2m x 1m', '368.00'),
            ('sticker-prints', '3m x 1m', '552.00'),
        ]:
            with self.subTest(rule=f'{slug}/{name}'):
                self.assertEqual(self.rule(slug, name).display_price_inc_gst, Decimal(inc_gst))

    def test_ranged_decal_prices_match_the_card(self):
        small = self.rule('vehicle-decals', 'Small decals')
        self.assertEqual((small.min_price, small.max_price), (Decimal('20'), Decimal('60')))
        door = self.rule('vehicle-decals', 'Door decal (cut vinyl)')
        self.assertEqual((door.min_price, door.max_price), (Decimal('30'), Decimal('100')))

    def test_small_format_stickers_carry_the_minimum_order(self):
        rule = self.rule('small-format-stickers', 'Labels & packaging (below A5)')
        self.assertEqual(rule.min_qty, 10)

    def test_urgent_fee_band_matches_the_card(self):
        fee = UrgentFee.current()
        self.assertEqual((fee.min_amount, fee.max_amount), (Decimal('50'), Decimal('100')))

    def test_canvas_rules_record_the_printed_trim_sizes(self):
        self.assertEqual(self.rule('canvas-prints', 'A0').notes, '800mm x 1.2m')
        self.assertEqual(self.rule('canvas-prints', 'A1').notes, '640mm x 900mm')


class LightboxTests(TestCase):
    """Every page that shows product photos must open them in the lightbox."""

    def setUp(self):
        self.category = ServiceCategory.objects.create(
            name='Vehicle Decals', slug='vehicle-decals', description='Fleet branding.'
        )
        PricingRule.objects.create(category=self.category, name='Door decal', base_price=30)
        CategoryImage.objects.create(
            category=self.category,
            image='services/decal.jpg',
            caption='Door decal on a work ute',
            alt_text='Door decal on a work ute — Vehicle Decals by InkPro',
        )

    def test_services_detail_and_gallery_all_mount_the_lightbox(self):
        for name, args in [('services', []), ('service_detail', ['vehicle-decals']), ('gallery', [])]:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=args))
                self.assertContains(response, 'aria-label="Image preview"')
                self.assertContains(response, 'aria-label="Enlarge:')

    def test_thumbnails_are_buttons_not_bare_images(self):
        # A bare <img> is not keyboard reachable; the trigger has to be a button.
        response = self.client.get(reverse('services'))
        self.assertContains(response, 'Enlarge: Door decal on a work ute')

    def test_lightbox_shows_the_short_caption_not_the_alt_text(self):
        # The alt text repeats the category, which already has its own line.
        response = self.client.get(reverse('services'))
        self.assertContains(response, "caption: 'Door decal on a work ute'")
        self.assertNotContains(response, "caption: 'Door decal on a work ute \\u2014")

    def test_the_overlay_markup_lives_in_one_partial(self):
        from django.template.loader import get_template

        get_template('main/partials/_lightbox.html')  # raises if missing
        for name in ('services.html', 'service_detail.html', 'gallery.html'):
            source = (settings_templates_dir() / 'main' / name).read_text()
            with self.subTest(template=name):
                self.assertIn('main/partials/_lightbox.html', source)


def settings_templates_dir():
    from django.conf import settings
    import pathlib

    return pathlib.Path(settings.BASE_DIR) / 'templates'


class CurrencyAndLocaleTests(TestCase):
    """Prices are Samoan Tala, and the clock is Samoan."""

    def test_currency_is_wst_not_a_nz_or_us_dollar(self):
        from django.conf import settings

        self.assertEqual(settings.CURRENCY_CODE, 'WST')
        self.assertEqual(settings.CURRENCY_NAME, 'Samoan Tala')

    def test_business_runs_on_samoan_time(self):
        # The same-day urgency rule and every quote timestamp depend on this.
        from django.conf import settings

        self.assertEqual(settings.TIME_ZONE, 'Pacific/Apia')

    def test_public_pages_state_the_currency(self):
        response = self.client.get(reverse('services'))
        self.assertContains(response, 'Samoan Tala')
        self.assertContains(response, 'WST')

    def test_the_quote_pdf_states_the_currency(self):
        from main.pdf import render_quote_html

        quote = Quote.objects.create(guest_name='Ada', guest_email='a@example.com')
        html = render_quote_html(quote)
        self.assertIn('Samoan Tala (WST)', html)
        self.assertNotIn('NZD', html)

    def test_no_template_still_claims_nzd(self):
        import pathlib

        from django.conf import settings

        for path in (pathlib.Path(settings.BASE_DIR) / 'templates').rglob('*.html'):
            with self.subTest(template=path.name):
                self.assertNotIn('NZD', path.read_text())


class ServiceIconTests(TestCase):
    """The emoji service icons were removed; photos carry the cards instead."""

    def setUp(self):
        self.category = ServiceCategory.objects.create(
            name='Banners', slug='banners', icon='🎌', description='Outdoor media.'
        )
        PricingRule.objects.create(category=self.category, name='1m x 1m', base_price=140)

    def test_pages_do_not_render_the_category_icon(self):
        for name, args in [('home', []), ('services', []),
                           ('service_detail', ['banners']), ('quote_builder', [])]:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=args))
                self.assertNotContains(response, '🎌')
                self.assertNotContains(response, '🖨️')


class LaunchPromoMarqueeTests(TestCase):
    """The launch offer marquee in the hero."""

    def test_it_shows_on_the_homepage_with_the_offer_copy(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'ink-marquee')
        self.assertContains(response, 'first 20 sales')
        self.assertContains(response, '50% discount')

    def test_it_sits_below_the_heading(self):
        body = self.client.get(reverse('home')).content.decode()
        self.assertLess(body.index('THINK PRO.'), body.index('ink-marquee'))
        # ...and above the supporting paragraph.
        self.assertLess(body.index('ink-marquee'), body.index('Banners, stickers'))

    @override_settings(LAUNCH_PROMO_ENABLED=False)
    def test_it_can_be_retired_without_a_code_change(self):
        # A launch offer is temporary; switching it off is a setting.
        self.assertNotContains(self.client.get(reverse('home')), 'ink-marquee')

    @override_settings(LAUNCH_PROMO_TEXT='Half price for the first 5 orders')
    def test_the_copy_is_configurable(self):
        self.assertContains(self.client.get(reverse('home')), 'Half price for the first 5 orders')

    def test_screen_readers_get_one_clean_copy_not_the_repeated_track(self):
        # The duplicated items exist only to make the loop seamless; read aloud
        # they would be a jumble, so the moving track is hidden from assistive
        # tech and a single static copy is exposed instead.
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'aria-label="Launch promotion"')
        self.assertContains(response, 'class="ink-marquee__track" aria-hidden="true"')
        self.assertContains(response, '<span class="sr-only">')

    def test_it_is_homepage_only(self):
        for name in ('services', 'about', 'gallery', 'contact', 'quote_builder'):
            with self.subTest(view=name):
                self.assertNotContains(self.client.get(reverse(name)), 'ink-marquee')


class SignedInPrefillTests(TestCase):
    """A signed-in customer should never retype their own contact details."""

    def setUp(self):
        self.category = ServiceCategory.objects.create(name='Banners', slug='banners')
        self.rule = PricingRule.objects.create(
            category=self.category, name='2m x 1m', base_price=Decimal('280')
        )
        self.user = User.objects.create_user(
            'ada', 'ada@example.com', 'pw', first_name='Ada', last_name='Lovelace'
        )
        Customer.objects.create(
            user=self.user, name='Ada Lovelace', email='ada@example.com',
            company_name='Analytical Engines', phone='021 555 0000',
        )

    def start_a_quote(self):
        self.client.post(
            reverse('quote_configure', args=['banners']),
            {'pricing_rule': self.rule.pk, 'quantity': 1},
        )

    def test_the_details_step_is_prefilled_from_the_account(self):
        self.client.force_login(self.user)
        self.start_a_quote()
        response = self.client.get(reverse('quote_review'))
        form = response.context['form']
        self.assertEqual(form['guest_name'].value(), 'Ada Lovelace')
        self.assertEqual(form['guest_email'].value(), 'ada@example.com')
        self.assertEqual(form['guest_phone'].value(), '021 555 0000')

    def test_the_prefilled_values_are_rendered_into_the_inputs(self):
        # The bound values must actually reach the HTML, not just the form.
        self.client.force_login(self.user)
        self.start_a_quote()
        response = self.client.get(reverse('quote_review'))
        self.assertContains(response, 'value="ada@example.com"')
        self.assertContains(response, 'value="Ada Lovelace"')

    def test_a_user_without_a_customer_record_still_gets_prefilled(self):
        bare = User.objects.create_user('bob', 'bob@example.com', 'pw', first_name='Bob')
        self.client.force_login(bare)
        self.start_a_quote()
        form = self.client.get(reverse('quote_review')).context['form']
        self.assertEqual(form['guest_email'].value(), 'bob@example.com')
        self.assertEqual(form['guest_name'].value(), 'Bob')

    def test_your_name_prefers_the_person_over_the_company(self):
        # The field asks "Your name", so a business customer should still see
        # their own name rather than the trading name.
        self.client.force_login(self.user)
        self.start_a_quote()
        form = self.client.get(reverse('quote_review')).context['form']
        self.assertEqual(form['guest_name'].value(), 'Ada Lovelace')

    def test_company_name_is_used_when_there_is_no_personal_name(self):
        customer = self.user.customer
        customer.name = ''
        customer.save()
        self.user.first_name = self.user.last_name = ''
        self.user.save()

        self.client.force_login(self.user)
        self.start_a_quote()
        form = self.client.get(reverse('quote_review')).context['form']
        self.assertEqual(form['guest_name'].value(), 'Analytical Engines')

    def test_guests_still_get_an_empty_form(self):
        self.start_a_quote()
        form = self.client.get(reverse('quote_review')).context['form']
        self.assertFalse(form['guest_name'].value())
        self.assertFalse(form['guest_email'].value())

    def test_details_already_on_the_draft_are_not_overwritten(self):
        # If the visitor typed something before signing in, keep what they typed.
        self.start_a_quote()
        draft = Quote.objects.get(status=Quote.DRAFT)
        draft.guest_name = 'Typed By Hand'
        draft.guest_email = 'typed@example.com'
        draft.save()

        self.client.force_login(self.user)
        form = self.client.get(reverse('quote_review')).context['form']
        self.assertEqual(form['guest_name'].value(), 'Typed By Hand')
        self.assertEqual(form['guest_email'].value(), 'typed@example.com')

    def test_submitting_links_the_quote_to_the_customer(self):
        self.client.force_login(self.user)
        self.start_a_quote()
        self.client.post(
            reverse('quote_review'),
            {'guest_name': 'Ada Lovelace', 'guest_email': 'ada@example.com',
             'guest_phone': '021 555 0000', 'customer_notes': ''},
        )
        quote = Quote.objects.get(status=Quote.SUBMITTED)
        self.assertEqual(quote.customer, self.user.customer)


class TemplateHygieneTests(TestCase):
    """Guards against template mistakes that render as visible page text."""

    def template_files(self):
        from django.conf import settings

        return sorted((pathlib.Path(settings.BASE_DIR) / 'templates').rglob('*.html'))

    def test_no_unterminated_single_line_comments(self):
        """Django's ``{# #}`` comment is single-line only.

        Its lexer matches ``{#.*?#}`` without DOTALL, so a ``{#`` whose ``#}``
        sits on a later line is not a comment at all — the whole block renders
        into the page as literal text. Multi-line comments must use
        ``{% comment %}``. This has bitten twice; hence the test.
        """
        offenders = []
        for path in self.template_files():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                index = line.find('{#')
                if index != -1 and '#}' not in line[index:]:
                    offenders.append(f'{path.name}:{number}: {line.strip()[:60]}')
        self.assertEqual(
            offenders, [], 'Unterminated {# #} comment — use {% comment %} for multi-line:\n'
            + '\n'.join(offenders)
        )

    def test_no_template_syntax_leaks_into_rendered_pages(self):
        category = ServiceCategory.objects.create(name='Stickers', slug='stickers')
        PricingRule.objects.create(category=category, name='Labels', base_price=5, min_qty=10)

        pages = [
            reverse('home'), reverse('services'), reverse('about'), reverse('gallery'),
            reverse('contact'), reverse('quote_builder'),
            reverse('service_detail', args=['stickers']),
            reverse('quote_configure', args=['stickers']),
        ]
        for url in pages:
            with self.subTest(page=url):
                body = self.client.get(url).content.decode()
                for marker in ('{#', '#}', '{%', '%}', '{{', '}}'):
                    self.assertNotIn(marker, body, f'Unrendered template syntax on {url}')


class MinimumQuantityTests(TestCase):
    """Small-format stickers carry a minimum order of 10 per the rate card.

    The rule is real, so it stays enforced — but the customer should never
    discover it by having a submission rejected.
    """

    def setUp(self):
        self.category = ServiceCategory.objects.create(
            name='Small Format Stickers', slug='small-format-stickers'
        )
        self.rule = PricingRule.objects.create(
            category=self.category,
            name='Labels & packaging (below A5)',
            pricing_type=PricingRule.PER_UNIT_RANGE,
            base_price=Decimal('5'), min_price=Decimal('5'), max_price=Decimal('20'),
            min_qty=10, unit_label='each',
        )

    def test_ordering_the_minimum_is_accepted(self):
        response = self.client.post(
            reverse('quote_configure', args=['small-format-stickers']),
            {'pricing_rule': self.rule.pk, 'quantity': 10},
        )
        self.assertRedirects(response, reverse('quote_review'))
        self.assertEqual(QuoteItem.objects.count(), 1)

    def test_below_the_minimum_is_still_rejected(self):
        response = self.client.post(
            reverse('quote_configure', args=['small-format-stickers']),
            {'pricing_rule': self.rule.pk, 'quantity': 3},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(QuoteItem.objects.exists())

    def test_the_message_reads_as_english(self):
        # It used to append unit_label, producing "a minimum order of 10 each".
        response = self.client.post(
            reverse('quote_configure', args=['small-format-stickers']),
            {'pricing_rule': self.rule.pk, 'quantity': 3},
        )
        self.assertContains(response, 'This option has a minimum order of 10.')
        self.assertContains(response, 'Enter 10 or more to continue.')
        self.assertNotContains(response, 'minimum order of 10 each')

    def test_the_quantity_field_binds_its_minimum_to_the_chosen_rule(self):
        # Otherwise the field inherits min=0 from PositiveIntegerField and the
        # browser accepts a quantity the server is about to reject.
        response = self.client.get(reverse('quote_configure', args=['small-format-stickers']))
        self.assertContains(response, ':min="current ? current.minQty : 1"')

    def test_choosing_an_option_raises_the_quantity_to_its_minimum(self):
        response = self.client.get(reverse('quote_configure', args=['small-format-stickers']))
        self.assertContains(response, 'quantity = current.minQty')
        # The per-rule minimum has to reach the client for that to work.
        self.assertContains(response, 'minQty: 10')

    def test_rules_without_a_minimum_are_unaffected(self):
        plain = ServiceCategory.objects.create(name='Canvas', slug='canvas')
        rule = PricingRule.objects.create(category=plain, name='A3', base_price=Decimal('80'))
        response = self.client.post(
            reverse('quote_configure', args=['canvas']),
            {'pricing_rule': rule.pk, 'quantity': 1},
        )
        self.assertRedirects(response, reverse('quote_review'))


class PasswordResetTests(TestCase):
    """Customers must be able to recover an account without contacting staff."""

    def setUp(self):
        self.user = User.objects.create_user('ada', 'ada@example.com', 'OldPass!2345')

    def test_the_reset_link_is_offered_on_the_sign_in_page(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, reverse('password_reset'))
        self.assertContains(response, 'Forgotten your password?')

    def test_every_step_of_the_flow_renders(self):
        for name in ('password_reset', 'password_reset_done', 'password_reset_complete'):
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_requesting_a_reset_sends_a_branded_email_with_a_working_link(self):
        response = self.client.post(reverse('password_reset'), {'email': 'ada@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)

        message = mail.outbox[0]
        self.assertEqual(message.subject, 'Reset your InkPro password')
        self.assertEqual(message.to, ['ada@example.com'])
        html = message.alternatives[0][0]
        self.assertIn('Set a new password', html)
        self.assertIn('InkPro', html)

        path = self.reset_path_from(message.body)
        self.assertEqual(self.client.get(path, follow=True).status_code, 200)

    def test_the_link_actually_changes_the_password(self):
        self.client.post(reverse('password_reset'), {'email': 'ada@example.com'})
        path = self.reset_path_from(mail.outbox[0].body)

        # Django redirects to a URL with the token swapped for a session key.
        response = self.client.get(path, follow=True)
        set_password_url = response.redirect_chain[-1][0]
        self.client.post(
            set_password_url,
            {'new_password1': 'BrandNewPass!2345', 'new_password2': 'BrandNewPass!2345'},
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('BrandNewPass!2345'))
        self.assertFalse(self.user.check_password('OldPass!2345'))

    def test_a_used_link_cannot_be_replayed(self):
        self.client.post(reverse('password_reset'), {'email': 'ada@example.com'})
        path = self.reset_path_from(mail.outbox[0].body)
        response = self.client.get(path, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {'new_password1': 'BrandNewPass!2345', 'new_password2': 'BrandNewPass!2345'},
        )
        replay = self.client.get(path, follow=True)
        self.assertContains(replay, 'That link has expired.')

    def test_an_unknown_address_does_not_reveal_whether_an_account_exists(self):
        response = self.client.post(reverse('password_reset'), {'email': 'nobody@example.com'})
        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_signed_in_users_can_change_a_known_password(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('password_change'),
            {
                'old_password': 'OldPass!2345',
                'new_password1': 'Another!Pass2345',
                'new_password2': 'Another!Pass2345',
            },
        )
        self.assertRedirects(response, reverse('password_change_done'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Another!Pass2345'))

    def test_the_profile_links_to_the_change_screen(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('profile')), reverse('password_change'))

    @staticmethod
    def reset_path_from(body):
        import re

        match = re.search(r'(/accounts/password-reset/[^/\s]+/[^/\s]+/)', body)
        assert match, f'no reset link in email body:\n{body}'
        return match.group(1)


class SignInIdentifierTests(TestCase):
    """An existing account must be recognised however the customer types it.

    Case-sensitive sign-in is a duplication trap: somebody who registered as
    "Ada" fails to sign in as "ada", concludes they have no account, and
    registers a second one.
    """

    def setUp(self):
        self.user = User.objects.create_user('Ada', 'Ada@Example.com', 'Corr3ctHorse!x')

    def test_username_matches_in_any_case(self):
        from django.contrib.auth import authenticate

        for typed in ('Ada', 'ada', 'ADA', '  ada  '):
            with self.subTest(typed=typed):
                self.assertEqual(
                    getattr(authenticate(username=typed, password='Corr3ctHorse!x'), 'pk', None),
                    self.user.pk,
                )

    def test_the_email_address_also_works_as_an_identifier(self):
        from django.contrib.auth import authenticate

        for typed in ('Ada@Example.com', 'ada@example.com', 'ADA@EXAMPLE.COM'):
            with self.subTest(typed=typed):
                self.assertEqual(
                    getattr(authenticate(username=typed, password='Corr3ctHorse!x'), 'pk', None),
                    self.user.pk,
                )

    def test_a_wrong_password_is_still_refused(self):
        from django.contrib.auth import authenticate

        self.assertIsNone(authenticate(username='ada', password='wrong'))
        self.assertIsNone(authenticate(username='nobody', password='Corr3ctHorse!x'))

    def test_an_inactive_account_cannot_sign_in(self):
        from django.contrib.auth import authenticate

        self.user.is_active = False
        self.user.save()
        self.assertIsNone(authenticate(username='ada', password='Corr3ctHorse!x'))

    def test_signing_in_through_the_form_works_in_the_wrong_case(self):
        response = self.client.post(
            reverse('login'), {'username': 'ada', 'password': 'Corr3ctHorse!x'}, follow=True
        )
        self.assertTrue(response.context['user'].is_authenticated)


class SignupDuplicationTests(TestCase):
    """Signup must not let a second account shadow an existing one."""

    def setUp(self):
        User.objects.create_user('Ada', 'ada@example.com', 'Corr3ctHorse!x')

    def post_signup(self, **overrides):
        data = {
            'username': 'newperson', 'email': 'new@example.com',
            'password1': 'Corr3ctHorse!x', 'password2': 'Corr3ctHorse!x',
        }
        data.update(overrides)
        return self.client.post(reverse('signup'), data)

    def test_a_duplicate_username_is_rejected_in_any_case(self):
        for typed in ('Ada', 'ada', 'ADA'):
            with self.subTest(typed=typed):
                response = self.post_signup(username=typed)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'already taken')
        self.assertEqual(User.objects.filter(username__iexact='ada').count(), 1)

    def test_a_duplicate_email_is_rejected_in_any_case(self):
        response = self.post_signup(email='ADA@EXAMPLE.COM')
        self.assertContains(response, 'already uses that email address')

    def test_the_rejection_points_at_the_way_out(self):
        # A dead end is what makes people create a second account.
        response = self.post_signup(username='ada')
        self.assertContains(response, 'reset your password')

    def test_emails_are_stored_lowercase(self):
        self.post_signup(username='zoe', email='Zoe@Example.COM')
        self.assertTrue(User.objects.filter(email='zoe@example.com').exists())

    def test_a_genuinely_new_account_still_works(self):
        response = self.post_signup(username='bob', email='bob@example.com')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(username='bob').exists())
        self.assertTrue(Customer.objects.filter(user__username='bob').exists())


class QuoteDocumentTests(TestCase):
    """The quote PDF is a plain white letterhead with the mark centred."""

    def setUp(self):
        category = ServiceCategory.objects.create(name='Stickers', slug='stickers')
        rule = PricingRule.objects.create(
            category=category, name='Labels', base_price=Decimal('5'), min_qty=10
        )
        self.quote = Quote.objects.create(
            guest_name='Ada Lovelace', guest_email='ada@example.com', status=Quote.SENT
        )
        QuoteItem.objects.create(
            quote=self.quote, category=category, pricing_rule=rule,
            description='Stickers — Labels', quantity=10, unit_price=Decimal('5'),
        )
        self.quote.recalculate()

    def html(self):
        from main.pdf import render_quote_html

        return render_quote_html(self.quote)

    def test_there_is_no_dark_panel_or_colour_bar(self):
        """The old letterhead put the logo on a black block with a CMYK rule
        beneath it. The logo artwork used here has a black splat and a black
        tagline, so both vanished into that panel — it read as a black bar."""
        markup = self.html()
        self.assertNotIn('#0A0A0A', markup)
        self.assertNotIn('linear-gradient', markup)
        self.assertNotIn('class="rule"', markup)

    def test_the_mark_is_centred_on_white(self):
        markup = self.html()
        self.assertIn('.header { text-align: center;', markup)
        self.assertIn('inkpro-logo.png', markup)
        # The light-background artwork, not the white-on-dark variant.
        self.assertNotIn('inkpro-logo-on-dark.png', markup)

    def test_the_document_still_carries_its_figures(self):
        markup = self.html()
        for expected in (self.quote.quote_number, 'Ada Lovelace', '50.00', '57.50',
                         'Samoan Tala (WST)'):
            with self.subTest(expected=expected):
                self.assertIn(expected, markup)

    def test_it_renders_to_a_real_pdf(self):
        from main.pdf import render_quote_pdf

        filename, content, mimetype = render_quote_pdf(self.quote)
        self.assertTrue(filename.endswith(('.pdf', '.html')))
        self.assertGreater(len(content), 1000)
        if mimetype == 'application/pdf':
            self.assertTrue(content.startswith(b'%PDF'))


class FreshCheckoutTests(TestCase):
    """A clone carries no database or media, so the build inputs must be committed.

    These guard the failure mode where someone clones the repo and finds an
    empty site: the catalogue and photography have to be rebuildable from
    files that are actually in version control.
    """

    def assets_dir(self):
        from django.conf import settings

        return pathlib.Path(settings.BASE_DIR) / 'assets'

    def test_every_build_input_is_committed(self):
        for name in ('rate-card.pdf', 'logo-master-on-light.png', 'logo-master-on-dark.png'):
            with self.subTest(asset=name):
                self.assertTrue(
                    (self.assets_dir() / name).exists(),
                    f'{name} must be committed — bootstrap rebuilds the site from it.',
                )

    def test_the_image_importer_defaults_to_the_committed_pdf(self):
        # Otherwise it only works on the machine that has the original download.
        from main.management.commands import import_ratecard_images

        command = import_ratecard_images.Command()
        parser = command.create_parser('manage.py', 'import_ratecard_images')
        self.assertIsNone(parser.parse_args([]).path)

    def test_bootstrap_builds_a_usable_catalogue(self):
        from django.core.management import call_command

        self.assertEqual(ServiceCategory.objects.count(), 0)
        call_command('bootstrap', '--skip-images', verbosity=0)

        self.assertGreaterEqual(ServiceCategory.objects.count(), 10)
        self.assertGreaterEqual(PricingRule.objects.count(), 28)
        self.assertTrue(UrgentFee.current())

    def test_bootstrap_is_safe_to_run_twice(self):
        from django.core.management import call_command

        call_command('bootstrap', '--skip-images', verbosity=0)
        first = ServiceCategory.objects.count()
        call_command('bootstrap', '--skip-images', verbosity=0)
        self.assertEqual(ServiceCategory.objects.count(), first)

    def test_generated_artefacts_stay_out_of_version_control(self):
        from django.conf import settings

        ignored = (pathlib.Path(settings.BASE_DIR) / '.gitignore').read_text()
        for pattern in ('*.sqlite3', 'media/', '.env'):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)


class DependencyDeclarationTests(TestCase):
    """Every third-party import must be declared in requirements.txt.

    An undeclared dependency works forever on the machine that happened to
    `pip install` it and fails only on a fresh checkout — which is exactly how
    `pypdf` went missing and left a cloned site with no product photography.
    """

    #: import name -> distribution name, where they differ.
    DISTRIBUTION_NAMES = {
        'PIL': 'pillow',
        'rest_framework': 'djangorestframework',
        'environ': 'django-environ',
    }

    #: This project's own packages. Everything in the standard library is
    #: excluded via sys.stdlib_module_names rather than a hand-kept list, which
    #: would quietly rot as the code grows.
    FIRST_PARTY = {'django', 'main', 'inkpro'}

    @property
    def not_dependencies(self):
        import sys

        return self.FIRST_PARTY | set(sys.stdlib_module_names)

    def declared_packages(self):
        from django.conf import settings

        text = (pathlib.Path(settings.BASE_DIR) / 'requirements.txt').read_text()
        names = set()
        for line in text.splitlines():
            line = line.split('#')[0].strip()
            if not line:
                continue
            name = re.split(r'[=<>!\[]', line)[0].strip().lower()
            if name:
                names.add(name)
        return names

    def imported_packages(self):
        from django.conf import settings

        base = pathlib.Path(settings.BASE_DIR)
        pattern = re.compile(r'^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)')
        found = set()
        for directory in ('main', 'inkpro'):
            for path in (base / directory).rglob('*.py'):
                for line in path.read_text().splitlines():
                    match = pattern.match(line)
                    if match:
                        found.add(match.group(1))
        excluded = self.not_dependencies
        return {name for name in found if name not in excluded and name.lower() not in excluded}

    def test_no_third_party_import_is_undeclared(self):
        declared = self.declared_packages()
        missing = []
        for name in sorted(self.imported_packages()):
            distribution = self.DISTRIBUTION_NAMES.get(name, name).lower()
            if distribution not in declared:
                missing.append(f'{name} (expected "{distribution}" in requirements.txt)')
        self.assertEqual(
            missing, [],
            'Undeclared dependencies — a fresh clone will fail on these:\n  '
            + '\n  '.join(missing),
        )

    def test_pypdf_specifically_is_declared(self):
        # The photo import silently produced an empty gallery without it.
        self.assertIn('pypdf', self.declared_packages())


class ProductionReadinessTests(TestCase):
    """Guards for the things that only break once the site is deployed."""

    def test_the_proxy_ssl_header_is_set_regardless_of_debug(self):
        """The host terminates TLS and forwards over plain HTTP. Without this
        setting Django reads every request as insecure, builds the request's
        good origin as http://<host>, and rejects the browser's https:// Origin
        on every form POST — the "does not match any trusted origins" 403.

        It used to sit inside the `if not DEBUG:` block, so a single unset
        environment variable took out every form on the site."""
        from django.conf import settings

        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER,
            ('HTTP_X_FORWARDED_PROTO', 'https'),
        )

    @override_settings(DEBUG=True, ALLOWED_HOSTS=['*'], CSRF_TRUSTED_ORIGINS=[])
    def test_an_https_origin_verifies_behind_the_proxy(self):
        """Reproduces the production 403 end to end: nothing configured, a
        request arriving over the proxy, and an https Origin header."""
        from django.middleware.csrf import CsrfViewMiddleware

        request = RequestFactory().post(
            '/quote/',
            HTTP_HOST='inkprosamoa.com',
            HTTP_ORIGIN='https://inkprosamoa.com',
            HTTP_X_FORWARDED_PROTO='https',
        )
        middleware = CsrfViewMiddleware(lambda r: None)
        self.assertTrue(request.is_secure())
        self.assertTrue(middleware._origin_verified(request))

    def test_the_site_host_is_a_trusted_csrf_origin(self):
        """CSRF_TRUSTED_ORIGINS is derived from ALLOWED_HOSTS, so that trusting
        a host to serve the site also trusts it as a form origin."""
        from inkpro.origins import trusted_origins

        derived = trusted_origins(
            ['inkprosamoa.com', 'inkpro.azurewebsites.net'],
            site_url='https://inkprosamoa.com',
        )
        self.assertIn('https://inkprosamoa.com', derived)
        self.assertIn('https://www.inkprosamoa.com', derived)
        self.assertIn('https://inkpro.azurewebsites.net', derived)

    def test_a_wildcard_host_yields_no_origin(self):
        """'*' says nothing about which origin to trust, so it must not become
        one — and an explicit setting is still honoured."""
        from inkpro.origins import trusted_origins

        self.assertEqual(trusted_origins(['*']), [])
        self.assertEqual(
            trusted_origins(['*'], configured=['https://inkprosamoa.com']),
            ['https://inkprosamoa.com'],
        )

    def test_local_hosts_are_trusted_over_plain_http(self):
        from inkpro.origins import trusted_origins

        self.assertEqual(
            trusted_origins(['localhost', '127.0.0.1']),
            ['http://localhost', 'http://127.0.0.1'],
        )

    def test_media_is_served_when_debug_is_off(self):
        """Django only routes MEDIA_URL when DEBUG is on. Without the media
        middleware every product photo and artwork upload 404s in production —
        which is invisible in development."""
        from django.conf import settings

        self.assertTrue(
            any('MediaFilesMiddleware' in m for m in settings.MIDDLEWARE),
            'Media middleware missing — uploads would 404 in production.',
        )

    def test_the_media_middleware_serves_a_real_file(self):
        import tempfile

        from django.test import override_settings

        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / 'probe.txt'
            target.write_text('hello')
            with override_settings(MEDIA_ROOT=tmp, MEDIA_URL='/media/'):
                from main.middleware import MediaFilesMiddleware

                middleware = MediaFilesMiddleware(lambda request: 'fell-through')
                request = RequestFactory().get('/media/probe.txt')
                response = middleware(request)
                self.assertNotEqual(response, 'fell-through')
                self.assertEqual(response.status_code, 200)

    def test_the_media_middleware_passes_unknown_paths_through(self):
        from main.middleware import MediaFilesMiddleware

        middleware = MediaFilesMiddleware(lambda request: 'fell-through')
        self.assertEqual(middleware(RequestFactory().get('/services/')), 'fell-through')

    @override_settings(DEBUG=True, SECRET_KEY='django-insecure-short')
    def test_deploycheck_blocks_an_unsafe_configuration(self):
        from django.core.management import call_command

        with self.assertRaises(SystemExit) as raised:
            call_command('deploycheck', verbosity=0)
        self.assertEqual(raised.exception.code, 1)

    def test_generate_secret_key_produces_a_usable_key(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command('generate_secret_key', stdout=out)
        key = out.getvalue().strip()
        self.assertGreaterEqual(len(key), 50)
        self.assertNotIn('insecure', key)

    def test_postgres_settings_require_tls_and_reuse_connections(self):
        """Azure Database for PostgreSQL refuses plaintext connections, and a
        new connection per request burns the instance's connection allowance."""
        import environ

        env = environ.Env()
        config = env.db_url_config(
            'postgres://u:p@srv.postgres.database.azure.com:5432/inkpro?sslmode=require'
        )
        self.assertEqual(config['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(config['OPTIONS']['sslmode'], 'require')

    def test_deployment_files_exist(self):
        from django.conf import settings

        base = pathlib.Path(settings.BASE_DIR)
        for name in ('startup.sh', '.env.production.example', 'requirements.txt'):
            with self.subTest(file=name):
                self.assertTrue((base / name).exists())

    def test_media_root_is_overridable_for_persistent_storage(self):
        """On App Service only /home survives a restart, and a deploy replaces
        /home/site/wwwroot — so MEDIA_ROOT has to be movable outside the tree."""
        import environ

        env = environ.Env(MEDIA_ROOT=(str, ''))
        self.assertEqual(env('MEDIA_ROOT'), '')  # default falls back to BASE_DIR/media
        self.assertIn('MEDIA_ROOT', pathlib.Path('inkpro/settings.py').read_text())


class AzureDatabaseResolutionTests(TestCase):
    """Find the database however the host wired it up.

    Attaching PostgreSQL to an App Service through the portal does not set
    DATABASE_URL — it injects AZURE_POSTGRESQL_* instead. Reading only
    DATABASE_URL made the app fall back to SQLite on a correctly-provisioned
    server, which surfaced as "no such table: main_invoice" on every page that
    touches the database.
    """

    def config(self, environ):
        import environ as django_environ

        from inkpro.database import resolve_database_url

        dsn = resolve_database_url(environ=environ, default='sqlite:///fallback.db')
        return django_environ.Env().db_url_config(dsn)

    def test_database_url_takes_precedence(self):
        config = self.config({
            'DATABASE_URL': 'postgres://u:p@explicit.example:5432/chosen',
            'AZURE_POSTGRESQL_HOST': 'ignored.example',
        })
        self.assertEqual(config['HOST'], 'explicit.example')
        self.assertEqual(config['NAME'], 'chosen')

    def test_service_connector_libpq_keyword_string(self):
        config = self.config({
            'AZURE_POSTGRESQL_CONNECTIONSTRING':
                'dbname=inkpro host=srv.postgres.database.azure.com port=5432 '
                'sslmode=require user=admin password=secret',
        })
        self.assertTrue(config['ENGINE'].endswith('postgresql'))
        self.assertEqual(config['HOST'], 'srv.postgres.database.azure.com')
        self.assertEqual(config['NAME'], 'inkpro')
        self.assertEqual(config['USER'], 'admin')

    def test_service_connector_url_form(self):
        config = self.config({
            'AZURE_POSTGRESQL_CONNECTIONSTRING':
                'postgresql://u:p@srv.postgres.database.azure.com:5432/inkpro',
        })
        self.assertEqual(config['NAME'], 'inkpro')

    def test_individual_azure_variables(self):
        config = self.config({
            'AZURE_POSTGRESQL_HOST': 'srv.postgres.database.azure.com',
            'AZURE_POSTGRESQL_DATABASE': 'inkpro',
            'AZURE_POSTGRESQL_USER': 'admin',
            'AZURE_POSTGRESQL_PASSWORD': 'secret',
        })
        self.assertEqual(config['HOST'], 'srv.postgres.database.azure.com')
        self.assertEqual(config['NAME'], 'inkpro')

    def test_legacy_app_service_connection_string(self):
        config = self.config({
            'POSTGRESQLCONNSTR_DATABASE_URL':
                'dbname=inkpro host=h.postgres.database.azure.com user=u password=p',
        })
        self.assertEqual(config['NAME'], 'inkpro')

    def test_credentials_with_special_characters_survive(self):
        """Azure-generated passwords routinely contain @ : / and +, which break
        a DSN unless they are percent-encoded on the way in."""
        config = self.config({
            'AZURE_POSTGRESQL_HOST': 'srv.postgres.database.azure.com',
            'AZURE_POSTGRESQL_DATABASE': 'inkpro',
            'AZURE_POSTGRESQL_USER': 'admin',
            'AZURE_POSTGRESQL_PASSWORD': 'p@ss/w:rd+123',
        })
        self.assertEqual(config['PASSWORD'], 'p@ss/w:rd+123')

    def test_quoted_libpq_password_is_unwrapped(self):
        config = self.config({
            'AZURE_POSTGRESQL_CONNECTIONSTRING':
                "host=h.postgres.database.azure.com dbname=inkpro user=u "
                "password='pa ss@word' sslmode=require",
        })
        self.assertEqual(config['PASSWORD'], 'pa ss@word')

    def test_ssl_is_required_by_default(self):
        """Azure Database for PostgreSQL refuses plaintext connections."""
        config = self.config({
            'AZURE_POSTGRESQL_HOST': 'srv.postgres.database.azure.com',
            'AZURE_POSTGRESQL_DATABASE': 'inkpro',
            'AZURE_POSTGRESQL_USER': 'u',
            'AZURE_POSTGRESQL_PASSWORD': 'p',
        })
        self.assertEqual(config['OPTIONS']['sslmode'], 'require')

    def test_falls_back_to_the_default_when_nothing_is_configured(self):
        self.assertTrue(self.config({})['ENGINE'].endswith('sqlite3'))

    def test_incomplete_azure_variables_do_not_produce_a_broken_dsn(self):
        # A host with no database name is not enough to connect; better to fall
        # back than to build a DSN that fails obscurely.
        config = self.config({'AZURE_POSTGRESQL_HOST': 'srv.postgres.database.azure.com'})
        self.assertTrue(config['ENGINE'].endswith('sqlite3'))


class PsycopgWheelTests(TestCase):
    """The pinned psycopg must be installable on the deployment runtime."""

    def test_psycopg_pin_has_cp314_wheels(self):
        """Azure App Service runs Python 3.14; psycopg-binary only began
        shipping cp314 wheels at 3.2.10, so an earlier pin cannot install."""
        from django.conf import settings

        requirements = (pathlib.Path(settings.BASE_DIR) / 'requirements.txt').read_text()
        match = re.search(r'psycopg\[binary\]==(\d+)\.(\d+)\.(\d+)', requirements)
        self.assertIsNotNone(match, 'psycopg pin not found in requirements.txt')
        major, minor, patch = (int(part) for part in match.groups())
        self.assertGreaterEqual(
            (major, minor, patch), (3, 2, 10),
            'psycopg[binary] must be >=3.2.10 for a cp314 wheel to exist.',
        )


class HealthCheckTests(TestCase):
    """The readiness probe has to distinguish "process is up" from "can serve".

    A deploy whose startup command was never set runs gunicorn directly and
    skips migrations; the site boots and then fails with "no such table" on the
    first page that touches the database. The probe surfaces that in one
    request, and is what a deployment is verified with.
    """

    def test_a_ready_site_reports_ok(self):
        ServiceCategory.objects.create(name='Banners', slug='banners')
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['checks']['database'], 'ok')
        self.assertEqual(payload['checks']['migrations'], 'applied')
        self.assertEqual(payload['checks']['catalogue'], 'ok')

    def test_an_unseeded_site_is_degraded_not_ok(self):
        # Migrated but never bootstrapped: no services would be listed.
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'degraded')
        self.assertIn('empty', response.json()['checks']['catalogue'])

    def test_it_names_the_database_engine(self):
        # "sqlite" in production means the managed server was never found.
        ServiceCategory.objects.create(name='Banners', slug='banners')
        self.assertIn('engine', self.client.get(reverse('health_check')).json()['checks'])

    def test_it_leaks_no_connection_details(self):
        from django.conf import settings

        ServiceCategory.objects.create(name='Banners', slug='banners')
        body = self.client.get(reverse('health_check')).content.decode()
        for secret in (settings.SECRET_KEY, str(settings.DATABASES['default'].get('PASSWORD') or 'x' * 40)):
            self.assertNotIn(secret, body)
        for key in ('HOST', 'USER', 'NAME'):
            value = settings.DATABASES['default'].get(key)
            if value and len(str(value)) > 6:
                self.assertNotIn(str(value), body)


class GraphEmailBackendTests(TestCase):
    """The Microsoft 365 backend: payload shape, credentials, token reuse."""

    graph_settings = {
        'EMAIL_BACKEND': 'main.graph_mail.GraphEmailBackend',
        'MS_GRAPH_TENANT_ID': 'tenant-1',
        'MS_GRAPH_CLIENT_ID': 'client-1',
        'MS_GRAPH_CLIENT_SECRET': 'secret-1',
        'MS_GRAPH_SENDER': 'sales@inkprosamoa.com',
    }

    def setUp(self):
        from main import graph_mail

        graph_mail._token_cache.clear()
        self.addCleanup(graph_mail._token_cache.clear)

    def _message(self):
        message = mail.EmailMultiAlternatives(
            subject='Your quote',
            body='Plain text body',
            from_email='InkPro <quotes@inkprosamoa.com>',
            to=['customer@example.com'],
            cc=['manager@inkprosamoa.com'],
            reply_to=['sales@inkprosamoa.com'],
        )
        message.attach_alternative('<p>HTML body</p>', 'text/html')
        message.attach('quote.pdf', b'%PDF-1.4 fake', 'application/pdf')
        return message

    def test_payload_prefers_html_and_encodes_attachments(self):
        from main.graph_mail import build_graph_message

        payload = build_graph_message(self._message())
        message = payload['message']
        self.assertEqual(message['body']['contentType'], 'HTML')
        self.assertEqual(message['body']['content'], '<p>HTML body</p>')
        self.assertEqual(
            message['toRecipients'], [{'emailAddress': {'address': 'customer@example.com'}}]
        )
        self.assertEqual(
            message['ccRecipients'], [{'emailAddress': {'address': 'manager@inkprosamoa.com'}}]
        )
        self.assertEqual(
            message['replyTo'], [{'emailAddress': {'address': 'sales@inkprosamoa.com'}}]
        )
        attachment = message['attachments'][0]
        self.assertEqual(attachment['name'], 'quote.pdf')
        self.assertEqual(base64.b64decode(attachment['contentBytes']), b'%PDF-1.4 fake')

    def test_oversized_attachment_is_dropped_not_sent(self):
        from main import graph_mail

        message = mail.EmailMessage(subject='Big', body='x', to=['a@example.com'])
        message.attach('huge.pdf', b'0' * (graph_mail.MAX_ATTACHMENT_BYTES + 1), 'application/pdf')
        with self.assertLogs('main.graph_mail', level='ERROR'):
            payload = graph_mail.build_graph_message(message)
        self.assertNotIn('attachments', payload['message'])

    def test_send_posts_to_the_configured_mailbox(self):
        from main.graph_mail import GraphEmailBackend

        with override_settings(**self.graph_settings):
            backend = GraphEmailBackend()
            with mock.patch('main.graph_mail._post_json', return_value=None) as posted, \
                    mock.patch('main.graph_mail.TokenCache._fetch', return_value=('tok', 3600)):
                self.assertEqual(backend.send_messages([self._message(), self._message()]), 2)

        self.assertEqual(posted.call_count, 2)
        url, payload, token, _timeout = posted.call_args[0]
        self.assertEqual(
            url, 'https://graph.microsoft.com/v1.0/users/sales%40inkprosamoa.com/sendMail'
        )
        self.assertEqual(token, 'tok')
        self.assertTrue(payload['saveToSentItems'])

    def test_send_falls_back_to_the_from_address_without_a_sender(self):
        from main.graph_mail import GraphEmailBackend

        with override_settings(**{**self.graph_settings, 'MS_GRAPH_SENDER': ''}):
            backend = GraphEmailBackend()
            with mock.patch('main.graph_mail._post_json', return_value=None) as posted, \
                    mock.patch('main.graph_mail.TokenCache._fetch', return_value=('tok', 3600)):
                backend.send_messages([self._message()])

        self.assertIn('quotes%40inkprosamoa.com', posted.call_args[0][0])

    def test_missing_credentials_raise_a_clear_error(self):
        from main.graph_mail import GraphEmailBackend, GraphError

        with override_settings(
            **{**self.graph_settings, 'MS_GRAPH_CLIENT_SECRET': ''}
        ):
            backend = GraphEmailBackend()
            self.assertEqual(backend.missing_settings(), ['MS_GRAPH_CLIENT_SECRET'])
            with self.assertRaises(GraphError) as caught:
                backend.get_token()
        self.assertIn('MS_GRAPH_CLIENT_SECRET', str(caught.exception))

    def test_token_is_cached_until_it_expires(self):
        from main.graph_mail import GraphEmailBackend

        with override_settings(**self.graph_settings):
            backend = GraphEmailBackend()
            with mock.patch(
                'main.graph_mail.TokenCache._fetch', return_value=('tok', 3600)
            ) as fetched:
                self.assertEqual(backend.get_token(), 'tok')
                self.assertEqual(backend.get_token(), 'tok')
        self.assertEqual(fetched.call_count, 1)

    def test_deploycheck_fails_when_graph_credentials_are_missing(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with override_settings(
            EMAIL_BACKEND='main.graph_mail.GraphEmailBackend',
            MS_GRAPH_TENANT_ID='',
            MS_GRAPH_CLIENT_ID='',
            MS_GRAPH_CLIENT_SECRET='',
        ):
            try:
                call_command('deploycheck', stdout=out)
            except SystemExit:
                pass
        self.assertIn('MS_GRAPH_TENANT_ID', out.getvalue())


class MediaRootResolutionTests(TestCase):
    """Uploads must never default into the tree an Azure deploy replaces."""

    def test_explicit_setting_always_wins(self):
        from inkpro.media import resolve_media_root

        self.assertEqual(
            resolve_media_root('/app', '/mnt/share/media', environ={}),
            pathlib.Path('/mnt/share/media'),
        )

    def test_local_default_is_beside_the_project(self):
        from inkpro.media import resolve_media_root

        self.assertEqual(
            resolve_media_root('/home/dev/inkpro', '', environ={}),
            pathlib.Path('/home/dev/inkpro/media'),
        )

    def test_azure_default_escapes_the_deployed_tree(self):
        from inkpro.media import AZURE_MEDIA_ROOT, resolve_media_root

        resolved = resolve_media_root(
            '/home/site/wwwroot', '', environ={'WEBSITE_SITE_NAME': 'inkpro'}
        )
        self.assertEqual(resolved, AZURE_MEDIA_ROOT)
        self.assertEqual(str(resolved), '/home/site/media')

    def test_azure_container_path_is_not_treated_as_persistent(self):
        """A container image serves from /app, which the next restart discards."""
        from inkpro.media import AZURE_MEDIA_ROOT, resolve_media_root

        self.assertEqual(
            resolve_media_root('/app', '', environ={'WEBSITE_SITE_NAME': 'inkpro'}),
            AZURE_MEDIA_ROOT,
        )

    def test_oryx_tmp_extraction_is_not_treated_as_persistent(self):
        """An Oryx build runs the app from /tmp/8d…, emptied on every recycle.

        This is the path that loses uploads hours after they were made: no
        deployment is involved, only App Service recycling the container.
        """
        from inkpro.media import AZURE_MEDIA_ROOT, resolve_media_root

        self.assertEqual(
            resolve_media_root(
                '/tmp/8ddf1a2b3c4d5e6', '', environ={'WEBSITE_SITE_NAME': 'inkpro'}
            ),
            AZURE_MEDIA_ROOT,
        )

    def test_persistent_home_path_is_kept(self):
        """A media directory already under /home needs no relocating."""
        from inkpro.media import resolve_media_root

        self.assertEqual(
            resolve_media_root(
                '/home/site/uploads', '', environ={'WEBSITE_SITE_NAME': 'inkpro'}
            ),
            pathlib.Path('/home/site/uploads/media'),
        )

    def test_deploycheck_fails_on_a_media_root_inside_the_deployed_tree(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with override_settings(MEDIA_ROOT='/home/site/wwwroot/media'):
            try:
                call_command('deploycheck', stdout=out)
            except SystemExit:
                pass
        self.assertIn('inside the deployed tree', out.getvalue())


class MediaCheckCommandTests(TestCase):
    """mediacheck names the uploads a deployment took with it."""

    def test_missing_file_is_reported_and_can_be_cleared(self):
        import tempfile
        from io import StringIO

        from django.core.files.base import ContentFile
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                category = ServiceCategory.objects.create(
                    name='Banners', slug='banners-mediacheck'
                )
                category.hero_image.save('hero.png', ContentFile(b'not-really-a-png'))
                path = pathlib.Path(category.hero_image.path)
                self.assertTrue(path.exists())

                out = StringIO()
                call_command('mediacheck', stdout=out)
                self.assertIn('ServiceCategory.hero_image: 1 file(s) present', out.getvalue())

                path.unlink()  # what the deployment did

                out = StringIO()
                call_command('mediacheck', '--verbose', stdout=out)
                report = out.getvalue()
                self.assertIn('GONE  ServiceCategory.hero_image: 1 missing', report)
                self.assertIn('hero', report)

                out = StringIO()
                call_command('mediacheck', '--clear-missing', stdout=out)
                category.refresh_from_db()
                self.assertFalse(category.hero_image)


class GraphCheckDiagnosticsTests(TestCase):
    """graphcheck must name the cause when Graph rejects the mailbox."""

    graph_settings = {
        'MS_GRAPH_TENANT_ID': 'tenant-1',
        'MS_GRAPH_CLIENT_ID': 'client-1',
        'MS_GRAPH_CLIENT_SECRET': 'secret-1',
        'MS_GRAPH_SENDER': 'inkpro@ahliki.com',
    }

    def setUp(self):
        from main import graph_mail

        graph_mail._token_cache.clear()
        self.addCleanup(graph_mail._token_cache.clear)

    def _run(self, *args, error_body=None):
        import urllib.error
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        out = StringIO()
        body = io.BytesIO((error_body or '').encode('utf-8'))
        failure = urllib.error.HTTPError(
            'https://graph.microsoft.com/v1.0/users/x/sendMail', 400, 'Bad Request', {}, body
        )
        with override_settings(**self.graph_settings):
            with mock.patch(
                'main.graph_mail.TokenCache._fetch', return_value=('tok', 3600)
            ), mock.patch('main.graph_mail._post_json', side_effect=failure), mock.patch(
                'main.graph_mail.graph_get', side_effect=GraphError('Graph GET failed (403): denied')
            ):
                with self.assertRaises(CommandError):
                    call_command('graphcheck', *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_invalid_user_names_the_alias_and_licence_causes(self):
        report = self._run(
            '--to', 'customer@example.com',
            error_body='{"error":{"code":"ErrorInvalidUser","message":'
                       '"The requested user \'inkpro@ahliki.com\' is invalid."}}',
        )
        self.assertIn('cannot resolve inkpro@ahliki.com', report)
        self.assertIn('alias', report)
        self.assertIn('User Principal Name', report)
        self.assertIn('licence', report)
        self.assertIn('User.Read.All', report)

    def test_access_denied_points_at_consent_and_access_policy(self):
        report = self._run(
            '--to', 'customer@example.com',
            error_body='{"error":{"code":"ErrorAccessDenied","message":"Access is denied."}}',
        )
        self.assertIn('admin consent', report)
        self.assertIn('ApplicationAccessPolicy', report)

    def test_from_flag_overrides_the_configured_mailbox(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with override_settings(**self.graph_settings):
            with mock.patch('main.graph_mail.TokenCache._fetch', return_value=('tok', 3600)):
                call_command('graphcheck', '--from', 'sales@inkprosamoa.com', stdout=out)
        self.assertIn('Mailbox: sales@inkprosamoa.com', out.getvalue())
