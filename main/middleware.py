"""Serve user media in production.

Django only wires ``MEDIA_URL`` into the URLconf when ``DEBUG`` is on, and
WhiteNoise deliberately handles static files only. That leaves every uploaded
file — the product photography, customer artwork and generated quote PDFs —
returning 404 the moment the site is deployed.

This middleware is WhiteNoise re-pointed at ``MEDIA_ROOT``. It inherits the
parent's conditional-request, range-request and compression handling rather
than reimplementing it, then swaps the prefix and file list over to media.

``autorefresh`` is forced on because the media directory changes while the
process is running: a customer uploading artwork must not have to wait for a
restart to see it. That costs one stat() per media request, which is the right
trade at this traffic level.

When a remote store such as Azure Blob Storage is configured the store serves
media directly, and this middleware finds nothing and stands aside.
"""

from django.conf import settings as django_settings
from whitenoise.middleware import WhiteNoiseMiddleware


class MediaFilesMiddleware(WhiteNoiseMiddleware):
    def __init__(self, get_response=None, settings=django_settings):
        # Let the parent wire up everything it needs, then take it off the
        # static files and put it on media. Building the WhiteNoise base class
        # directly instead leaves attributes such as `use_finders` unset.
        super().__init__(get_response, settings)

        self.use_finders = False
        self.autorefresh = True
        self.files = {}
        self.directories = []
        self.static_prefix = settings.MEDIA_URL

        root = getattr(settings, 'MEDIA_ROOT', None)
        if root:
            self.add_files(str(root), prefix=settings.MEDIA_URL)
