from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from main.context_processors import favicon_ico_url

admin.site.site_header = 'InkPro Administration'
admin.site.site_title = 'InkPro'
admin.site.index_title = 'Think Ink. Think Pro.'

urlpatterns = [
    # Browsers ask for /favicon.ico directly — for bookmarks, history entries
    # and the tab before the page's <link> tags have been parsed — so point
    # that at the collected static file rather than serving a 404.
    path(
        'favicon.ico',
        RedirectView.as_view(url=favicon_ico_url(), permanent=True),
    ),
    path('admin/', admin.site.urls),
    path('', include('main.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
