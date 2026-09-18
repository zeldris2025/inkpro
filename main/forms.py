"""Forms for the public quote wizard, the customer account area and staff screens."""

from decimal import Decimal

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.contrib.auth.models import User

from .models import Customer, Invoice, PricingRule, Quote, QuoteItem, ServiceCategory

INPUT = (
    'w-full rounded-lg border border-white/15 bg-black/40 px-4 py-3 text-white '
    'placeholder-white/40 focus:border-ink-yellow focus:outline-none focus:ring-2 '
    'focus:ring-ink-yellow/40 transition'
)
LIGHT_INPUT = (
    'w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-zinc-900 '
    'focus:border-ink-yellow focus:outline-none focus:ring-2 focus:ring-ink-yellow/40'
)


def style(fields, css=INPUT):
    for field in fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault('class', 'h-5 w-5 rounded accent-ink-yellow')
        elif isinstance(widget, (forms.FileInput, forms.ClearableFileInput)):
            widget.attrs.setdefault(
                'class',
                'w-full text-sm text-white/70 file:mr-4 file:rounded-full file:border-0 '
                'file:bg-ink-yellow file:px-4 file:py-2 file:text-sm file:font-bold '
                'file:text-black hover:file:bg-yellow-300',
            )
        else:
            widget.attrs.setdefault('class', css)


class QuoteItemForm(forms.ModelForm):
    """Step 2/3 of the wizard — pick an option, size it and set a quantity."""

    class Meta:
        model = QuoteItem
        fields = ['pricing_rule', 'quantity', 'width_m', 'height_m', 'artwork']
        widgets = {
            # `:min` is bound by Alpine to the chosen rule's minimum. Without it
            # the field inherits min=0 from PositiveIntegerField and the browser
            # happily accepts a quantity the server is about to reject.
            'quantity': forms.NumberInput(
                attrs={
                    'min': 1,
                    'x-model.number': 'quantity',
                    ':min': 'current ? current.minQty : 1',
                }
            ),
            'width_m': forms.NumberInput(
                attrs={'step': '0.01', 'min': '0', 'placeholder': 'e.g. 2.5', 'x-model.number': 'width'}
            ),
            'height_m': forms.NumberInput(
                attrs={'step': '0.01', 'min': '0', 'placeholder': 'e.g. 1', 'x-model.number': 'height'}
            ),
        }

    def __init__(self, *args, category=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.category = category
        rules = PricingRule.objects.filter(is_active=True)
        if category is not None:
            rules = rules.filter(category=category)
        self.fields['pricing_rule'].queryset = rules
        self.fields['pricing_rule'].empty_label = 'Choose an option…'
        self.fields['pricing_rule'].label = 'Option'
        self.fields['width_m'].label = 'Width (metres)'
        self.fields['height_m'].label = 'Height (metres)'
        self.fields['artwork'].label = 'Artwork or reference image (optional)'
        self.fields['artwork'].required = False
        style(self.fields)

    def clean(self):
        cleaned = super().clean()
        rule = cleaned.get('pricing_rule')
        quantity = cleaned.get('quantity') or 0
        if not rule:
            return cleaned

        if quantity < rule.min_qty:
            # Deliberately no unit_label here: it is "each" for most rules, and
            # "a minimum order of 10 each" does not read as English.
            self.add_error(
                'quantity',
                f'This option has a minimum order of {rule.min_qty}. '
                f'Enter {rule.min_qty} or more to continue.',
            )
        if rule.requires_dimensions:
            for field in ('width_m', 'height_m'):
                if not cleaned.get(field):
                    self.add_error(field, 'Required for size-based pricing.')
        return cleaned

    def save(self, quote, commit=True):
        item = super().save(commit=False)
        rule = item.pricing_rule
        item.quote = quote
        item.category = rule.category
        item.description = f'{rule.category.name} — {rule.name}'
        item.unit_price = rule.unit_price(item.width_m, item.height_m)
        item.display_order = quote.items.count()
        if commit:
            item.save()
        return item


class QuoteSubmitForm(forms.ModelForm):
    """Final wizard step — who we're quoting and how fast they need it."""

    class Meta:
        model = Quote
        fields = ['guest_name', 'guest_email', 'guest_phone', 'customer_notes', 'is_urgent']
        labels = {
            'guest_name': 'Your name',
            'guest_email': 'Email',
            'guest_phone': 'Phone',
            'customer_notes': 'Anything else we should know?',
            'is_urgent': 'I need this urgently (same-day / rush)',
        }
        widgets = {
            'customer_notes': forms.Textarea(
                attrs={'rows': 4, 'placeholder': 'Deadlines, colours, delivery details…'}
            ),
            'is_urgent': forms.CheckboxInput(attrs={'x-model': 'urgent'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for field in ('guest_name', 'guest_email'):
            self.fields[field].required = True
        self.prefill_from(user)
        style(self.fields)

    def prefill_from(self, user):
        """Seed the contact fields from the signed-in account.

        Writes into ``self.initial`` rather than ``field.initial``. This is a
        ModelForm, so Django has already populated ``self.initial`` from
        ``model_to_dict(instance)``, and ``get_initial_for_field`` reads
        ``self.initial.get(name, field.initial)`` — a draft quote's blank
        ``guest_name`` is an empty string that is *present* in that dict, so
        the field-level default is never consulted and the prefill silently
        does nothing.

        Only blank fields are filled, so anything the visitor typed before
        signing in survives.
        """
        if user is None or not user.is_authenticated:
            return

        customer = getattr(user, 'customer', None)
        # The field is labelled "Your name", so prefer the person over the
        # company. `display_name` puts the company first, which is right for
        # identifying an account but wrong for asking who we are quoting.
        name = (
            (customer.name if customer else '')
            or user.get_full_name()
            or (customer.company_name if customer else '')
            or user.get_username()
        )

        candidates = {
            'guest_name': name,
            'guest_email': (customer.contact_email if customer else '') or user.email,
            'guest_phone': customer.phone if customer else '',
        }
        for field, value in candidates.items():
            if value and not self.initial.get(field):
                self.initial[field] = value


class StaffQuoteItemForm(forms.ModelForm):
    """Inline row on the staff review screen — prices and copy are editable."""

    class Meta:
        model = QuoteItem
        fields = ['description', 'size', 'quantity', 'unit_price']
        widgets = {
            'description': forms.TextInput(attrs={'class': LIGHT_INPUT}),
            'size': forms.TextInput(attrs={'class': LIGHT_INPUT}),
            'quantity': forms.NumberInput(attrs={'min': 0, 'class': LIGHT_INPUT}),
            'unit_price': forms.NumberInput(attrs={'step': '0.01', 'class': LIGHT_INPUT}),
        }


StaffQuoteItemFormSet = forms.modelformset_factory(
    QuoteItem, form=StaffQuoteItemForm, extra=0, can_delete=True
)


class StaffQuoteForm(forms.ModelForm):
    """Quote-level adjustments staff can make during review."""

    class Meta:
        model = Quote
        fields = ['urgent_fee', 'discount', 'total_override', 'valid_until', 'staff_notes']
        widgets = {
            'urgent_fee': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'discount': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'total_override': forms.NumberInput(
                attrs={'step': '0.01', 'placeholder': 'Leave blank to use the calculated total'}
            ),
            'valid_until': forms.DateInput(attrs={'type': 'date'}),
            'staff_notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self.fields, LIGHT_INPUT)


class StaffQuoteItemAddForm(forms.ModelForm):
    """Lets staff append an adjustment or extra line to a customer's quote."""

    class Meta:
        model = QuoteItem
        fields = ['category', 'description', 'size', 'quantity', 'unit_price']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = ServiceCategory.objects.filter(is_active=True)
        style(self.fields, LIGHT_INPUT)


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = [
            'date',
            'customer',
            'client_name',
            'job_details',
            'qty',
            'invoice_no',
            'invoice_amount',
            'receipt_no',
            'amount_received',
            'payment_method',
            'payment_status',
            'status_is_manual',
            'notes',
        ]
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'job_details': forms.Textarea(attrs={'rows': 2}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['customer'].required = False
        style(self.fields, LIGHT_INPUT)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('customer') and not (cleaned.get('client_name') or '').strip():
            self.add_error('client_name', 'Give either a customer record or a client name.')
        return cleaned


class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['name', 'company_name', 'phone', 'address', 'customer_type']
        widgets = {'address': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self.fields)


class SignUpForm(UserCreationForm):
    """Account creation that also provisions the linked Customer record."""

    email = forms.EmailField(required=True)
    name = forms.CharField(max_length=200, required=False, label='Name')
    company_name = forms.CharField(max_length=200, required=False, label='Company (optional)')
    phone = forms.CharField(max_length=50, required=False)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self.fields)

    def clean_username(self):
        """Reject a username that differs from an existing one only by case.

        Django's own uniqueness check is already case-insensitive, but doing it
        here lets us give a message that points at the way out — signing in or
        resetting the password — instead of a dead end.
        """
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(
                'That username is already taken. Try signing in instead, '
                'or reset your password if you have forgotten it.'
            )
        return username

    def clean_email(self):
        # Stored lowercase so the address is unambiguous for password resets
        # and for matching guest quotes raised before the account existed.
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                'An account already uses that email address. Try signing in, '
                'or reset your password if you have forgotten it.'
            )
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
            Customer.objects.create(
                user=user,
                name=self.cleaned_data.get('name', ''),
                email=user.email,
                company_name=self.cleaned_data.get('company_name', ''),
                phone=self.cleaned_data.get('phone', ''),
                customer_type=(
                    Customer.BUSINESS
                    if self.cleaned_data.get('company_name')
                    else Customer.INDIVIDUAL
                ),
            )
        return user


class ContactForm(forms.Form):
    name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50, required=False)
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 5}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self.fields)


class QuotePriceProbeForm(forms.Form):
    """Backs the live-pricing endpoint the wizard calls as fields change."""

    pricing_rule = forms.ModelChoiceField(queryset=PricingRule.objects.filter(is_active=True))
    quantity = forms.IntegerField(min_value=0, initial=1)
    width_m = forms.DecimalField(required=False, min_value=Decimal('0'))
    height_m = forms.DecimalField(required=False, min_value=Decimal('0'))


class StyledAuthenticationForm(AuthenticationForm):
    """Sign-in form. Accepts a username or an email address, either case."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Username or email'
        self.fields['username'].widget.attrs.update(
            {'autocomplete': 'username', 'autofocus': True}
        )
        self.fields['password'].widget.attrs['autocomplete'] = 'current-password'
        style(self.fields)


class StyledPasswordResetForm(PasswordResetForm):
    """Ask for the address to send a reset link to."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].label = 'Your email address'
        self.fields['email'].widget.attrs.update(
            {'autocomplete': 'email', 'placeholder': 'you@example.com', 'autofocus': True}
        )
        style(self.fields)


class StyledSetPasswordForm(SetPasswordForm):
    """Choose a new password from a reset link."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.fields:
            self.fields[name].widget.attrs['autocomplete'] = 'new-password'
        style(self.fields)


class StyledPasswordChangeForm(PasswordChangeForm):
    """Change a password while signed in."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['old_password'].widget.attrs['autocomplete'] = 'current-password'
        for name in ('new_password1', 'new_password2'):
            self.fields[name].widget.attrs['autocomplete'] = 'new-password'
        style(self.fields)
