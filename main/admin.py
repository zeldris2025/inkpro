"""Django admin — the owner's control surface for pricing, roles and email copy."""

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    CategoryImage,
    Customer,
    EmailTemplate,
    Invoice,
    PricingRule,
    Quote,
    QuoteItem,
    ServiceCategory,
    UrgentFee,
)


class CategoryImageInline(admin.TabularInline):
    model = CategoryImage
    extra = 0
    fields = ('preview', 'image', 'caption', 'alt_text', 'is_hero', 'display_order')
    readonly_fields = ('preview',)

    @admin.display(description='Preview')
    def preview(self, obj):
        if not obj.image:
            return '—'
        return format_html(
            '<img src="{}" style="height:56px;border-radius:4px;">', obj.image.url
        )


class PricingRuleInline(admin.TabularInline):
    model = PricingRule
    extra = 0
    fields = (
        'name',
        'pricing_type',
        'base_price',
        'min_price',
        'max_price',
        'unit_label',
        'min_qty',
        'gst_inclusive',
        'display_order',
        'is_active',
    )


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'rule_count', 'photo_count', 'display_order', 'is_active')
    list_editable = ('display_order', 'is_active')
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ('name', 'description')
    inlines = [CategoryImageInline, PricingRuleInline]

    @admin.display(description='Pricing rules')
    def rule_count(self, obj):
        return obj.pricing_rules.count()

    @admin.display(description='Photos')
    def photo_count(self, obj):
        return obj.images.count()


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'category',
        'pricing_type',
        'base_price',
        'price_range_label',
        'unit_label',
        'min_qty',
        'is_active',
    )
    list_filter = ('category', 'pricing_type', 'is_active', 'gst_inclusive')
    search_fields = ('name', 'category__name', 'notes')
    list_editable = ('base_price', 'is_active')


@admin.register(UrgentFee)
class UrgentFeeAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'description', 'is_active')


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'contact_email', 'phone', 'customer_type', 'created_at')
    list_filter = ('customer_type',)
    search_fields = ('company_name', 'name', 'email', 'phone', 'user__username')
    raw_id_fields = ('user',)


class QuoteItemInline(admin.TabularInline):
    model = QuoteItem
    extra = 0
    fields = ('description', 'category', 'size', 'quantity', 'unit_price', 'line_total', 'added_by_staff')
    readonly_fields = ('line_total',)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = (
        'quote_number',
        'contact_name',
        'status',
        'total',
        'is_urgent',
        'created_at',
        'review_link',
    )
    list_filter = ('status', 'is_urgent', 'created_at')
    search_fields = ('quote_number', 'guest_name', 'guest_email', 'customer__company_name')
    readonly_fields = (
        'quote_number',
        'access_token',
        'subtotal',
        'gst_amount',
        'total',
        'created_at',
        'updated_at',
        'submitted_at',
        'reviewed_at',
        'sent_at',
        'responded_at',
        'public_link',
    )
    inlines = [QuoteItemInline]
    date_hierarchy = 'created_at'

    @admin.display(description='Review')
    def review_link(self, obj):
        if not obj.pk:
            return '—'
        return format_html(
            '<a href="{}">Open in staff portal</a>', reverse('staff_quote_detail', args=[obj.pk])
        )

    @admin.display(description='Customer link')
    def public_link(self, obj):
        if not obj.pk:
            return '—'
        return format_html('<a href="{0}" target="_blank">{0}</a>', obj.public_url())

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate()


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'invoice_no',
        'client',
        'date',
        'invoice_amount',
        'amount_received',
        'balance',
        'payment_status',
        'payment_method',
    )
    list_filter = ('payment_status', 'payment_method', 'date')
    search_fields = ('invoice_no', 'receipt_no', 'client_name', 'job_details', 'customer__company_name')
    readonly_fields = ('balance', 'created_at')
    date_hierarchy = 'date'
    raw_id_fields = ('customer', 'quote')


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ('get_key_display', 'subject', 'is_active')
    list_filter = ('is_active',)
