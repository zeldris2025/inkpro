from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import api, forms, views, views_staff

urlpatterns = [
    # Marketing
    path('', views.home, name='home'),
    path('services/', views.services, name='services'),
    path('services/<slug:slug>/', views.service_detail, name='service_detail'),
    path('about/', views.about, name='about'),
    path('gallery/', views.gallery, name='gallery'),
    path('contact/', views.contact, name='contact'),
    # Quote wizard
    path('quote/', views.quote_builder, name='quote_builder'),
    path('quote/configure/<slug:slug>/', views.quote_configure, name='quote_configure'),
    path('quote/review/', views.quote_review, name='quote_review'),
    path('quote/item/<int:item_id>/remove/', views.quote_remove_item, name='quote_remove_item'),
    path('quote/urgent/', views.quote_toggle_urgent, name='quote_toggle_urgent'),
    path('quote/summary/', views.quote_summary_partial, name='quote_summary'),
    path('quote/price/', views.price_probe, name='price_probe'),
    path('quote/submitted/<str:token>/', views.quote_submitted, name='quote_submitted'),
    # Public token-authenticated quote view
    path('q/<str:token>/', views.public_quote, name='public_quote'),
    path('q/<str:token>/respond/', views.public_quote_respond, name='public_quote_respond'),
    # Accounts
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(
            template_name='main/account/login.html',
            authentication_form=forms.StyledAuthenticationForm,
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('accounts/signup/', views.signup, name='signup'),
    path('accounts/profile/', views.profile, name='profile'),
    # Forgotten password: request a link, confirm it was sent, set a new
    # password from the emailed link, confirm it worked.
    path('accounts/password-reset/', views.BrandedPasswordResetView.as_view(), name='password_reset'),
    path(
        'accounts/password-reset/sent/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='main/account/password_reset_done.html'
        ),
        name='password_reset_done',
    ),
    path(
        'accounts/password-reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='main/account/password_reset_confirm.html',
            form_class=forms.StyledSetPasswordForm,
            success_url=reverse_lazy('password_reset_complete'),
        ),
        name='password_reset_confirm',
    ),
    path(
        'accounts/password-reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='main/account/password_reset_complete.html'
        ),
        name='password_reset_complete',
    ),
    # Changing a known password while signed in.
    path(
        'accounts/password-change/',
        auth_views.PasswordChangeView.as_view(
            template_name='main/account/password_change.html',
            form_class=forms.StyledPasswordChangeForm,
            success_url=reverse_lazy('password_change_done'),
        ),
        name='password_change',
    ),
    path(
        'accounts/password-change/done/',
        auth_views.PasswordChangeDoneView.as_view(
            template_name='main/account/password_change_done.html'
        ),
        name='password_change_done',
    ),
    path('my/quotes/', views.my_quotes, name='my_quotes'),
    path('my/quotes/<int:pk>/', views.my_quote_detail, name='my_quote_detail'),
    path('my/quotes/<int:pk>/reorder/', views.reorder_quote, name='reorder_quote'),
    # Staff portal
    path('staff/', views_staff.dashboard, name='staff_dashboard'),
    path('staff/quotes/', views_staff.quote_inbox, name='staff_quote_inbox'),
    path('staff/quotes/<int:pk>/', views_staff.quote_detail, name='staff_quote_detail'),
    path('staff/quotes/<int:pk>/add-item/', views_staff.quote_add_item, name='staff_quote_add_item'),
    path('staff/quotes/<int:pk>/transition/', views_staff.quote_transition, name='staff_quote_transition'),
    path('staff/quotes/<int:pk>/pdf/', views_staff.quote_pdf, name='staff_quote_pdf'),
    path('staff/register/', views_staff.register, name='staff_register'),
    path('staff/register/new/', views_staff.invoice_edit, name='staff_invoice_new'),
    path('staff/register/<int:pk>/', views_staff.invoice_edit, name='staff_invoice_edit'),
    path('staff/register/<int:pk>/inline/', views_staff.invoice_inline_update, name='staff_invoice_inline'),
    # API
    path('api/dashboard/stats/', api.dashboard_stats, name='api_dashboard_stats'),
    path('api/pricing/rule/', api.rule_price, name='api_rule_price'),
    path('health/', views.health_check, name='health_check'),
]
