"""Pre-flight check for a production deployment.

``manage.py check --deploy`` covers Django's own security settings. This adds
the things specific to running InkPro on a real host, each of which has a
failure mode that is silent rather than loud:

  * SQLite on an ephemeral disk loses every quote and invoice on restart.
  * The console email backend prints quotes to the log instead of sending them.
  * A SITE_URL left at localhost puts dead links in customers' emails.
  * WeasyPrint without its native libraries downgrades PDFs to HTML.
  * An un-run collectstatic leaves the site unstyled under a manifest storage.

Run it against the production environment:

    DEBUG=False SECRET_KEY=... DATABASE_URL=... python manage.py deploycheck
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Check that this environment is ready to serve production traffic.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Exit non-zero if there are warnings as well as failures.',
        )

    def handle(self, *args, **options):
        self.failures = []
        self.warnings = []
        self.passes = []

        self.check_debug()
        self.check_secret_key()
        self.check_hosts()
        self.check_database()
        self.check_media()
        self.check_static()
        self.check_email()
        self.check_site_url()
        self.check_pdf()
        self.check_catalogue()

        for line in self.passes:
            self.stdout.write(self.style.SUCCESS(f'  PASS  {line}'))
        for line in self.warnings:
            self.stdout.write(self.style.WARNING(f'  WARN  {line}'))
        for line in self.failures:
            self.stdout.write(self.style.ERROR(f'  FAIL  {line}'))

        self.stdout.write('')
        if self.failures:
            self.stdout.write(
                self.style.ERROR(
                    f'{len(self.failures)} blocking issue(s). Do not go live until these are fixed.'
                )
            )
            raise SystemExit(1)
        if self.warnings and options['strict']:
            self.stdout.write(self.style.ERROR(f'{len(self.warnings)} warning(s), --strict set.'))
            raise SystemExit(1)
        self.stdout.write(
            self.style.SUCCESS(f'Ready to serve. {len(self.warnings)} warning(s).')
        )

    # -- individual checks ---------------------------------------------------
    def check_debug(self):
        if settings.DEBUG:
            self.failures.append('DEBUG is True — never serve production with it on.')
        else:
            self.passes.append('DEBUG is off.')

    def check_secret_key(self):
        key = settings.SECRET_KEY
        if 'insecure' in key or len(key) < 50:
            self.failures.append(
                'SECRET_KEY is the development default or too short. '
                'Generate one with: manage.py generate_secret_key'
            )
        else:
            self.passes.append('SECRET_KEY is set.')

    def check_hosts(self):
        if '*' in settings.ALLOWED_HOSTS:
            self.failures.append('ALLOWED_HOSTS is "*" — name the real domains.')
        elif not settings.ALLOWED_HOSTS:
            self.failures.append('ALLOWED_HOSTS is empty; every request will 400.')
        else:
            self.passes.append(f'ALLOWED_HOSTS: {", ".join(settings.ALLOWED_HOSTS)}')

        if not settings.DEBUG and not settings.CSRF_TRUSTED_ORIGINS:
            self.warnings.append(
                'CSRF_TRUSTED_ORIGINS is empty — form posts over HTTPS may be rejected.'
            )

    def check_database(self):
        engine = settings.DATABASES['default']['ENGINE']
        if engine.endswith('sqlite3'):
            self.failures.append(
                'Using SQLite. On a host with an ephemeral disk this loses every quote, '
                'customer and invoice on restart or redeploy. Set DATABASE_URL to Postgres.'
            )
            return
        try:
            connection.ensure_connection()
        except Exception as exc:
            self.failures.append(f'Cannot connect to the database: {exc}')
            return
        options = settings.DATABASES['default'].get('OPTIONS', {})
        if options.get('sslmode') in ('require', 'verify-full', 'verify-ca'):
            self.passes.append(f'Database reachable over TLS ({options["sslmode"]}).')
        else:
            self.warnings.append('Database connection is not requiring TLS.')

    def check_media(self):
        root = Path(settings.MEDIA_ROOT)
        if not root.exists():
            self.warnings.append(f'MEDIA_ROOT does not exist yet: {root}')
            return
        probe = root / '.write-test'
        try:
            probe.write_text('ok')
            probe.unlink()
        except OSError as exc:
            self.failures.append(f'MEDIA_ROOT is not writable ({root}): {exc}')
            return
        served = any('MediaFilesMiddleware' in m for m in settings.MIDDLEWARE)
        if served:
            self.passes.append(f'Media writable and served from {root}.')
        else:
            self.failures.append(
                'No media middleware — uploaded files and product photos will 404.'
            )

    def check_static(self):
        backend = settings.STORAGES['staticfiles']['BACKEND']
        if 'Manifest' not in backend:
            self.passes.append('Static files use a non-manifest storage.')
            return
        manifest = Path(settings.STATIC_ROOT) / 'staticfiles.json'
        if manifest.exists():
            self.passes.append('collectstatic has been run (manifest present).')
        else:
            self.failures.append(
                'Manifest static storage is configured but collectstatic has not run — '
                'every stylesheet and image will 500.'
            )

    def check_email(self):
        backend = settings.EMAIL_BACKEND
        if 'console' in backend or 'locmem' in backend:
            self.failures.append(
                'EMAIL_BACKEND is the console backend — quotes would be printed to the '
                'log instead of sent to customers.'
            )
        elif 'smtp' in backend and not settings.EMAIL_HOST:
            self.failures.append('SMTP backend selected but EMAIL_HOST is empty.')
        elif 'graph_mail' in backend:
            self.check_graph_email()
        else:
            self.passes.append(f'Email backend: {backend.rsplit(".", 2)[-2]}.')

    def check_graph_email(self):
        """The Graph backend needs its Azure app registration details.

        Only the settings are checked; ``manage.py graphcheck`` goes further and
        asks Microsoft for a token.
        """
        from main.graph_mail import GraphEmailBackend, address_only

        backend = GraphEmailBackend()
        missing = backend.missing_settings()
        if missing:
            self.failures.append(
                'Microsoft Graph email backend selected but '
                + ', '.join(missing)
                + ' is empty — no quote would ever leave the building.'
            )
            return
        mailbox = backend.mailbox_for(None) if backend.sender else ''
        if not mailbox:
            mailbox = address_only(settings.DEFAULT_FROM_EMAIL)
        if not mailbox:
            self.failures.append(
                'Microsoft Graph email backend selected but neither MS_GRAPH_SENDER '
                'nor DEFAULT_FROM_EMAIL names a mailbox to send from.'
            )
            return
        self.passes.append(f'Email backend: Microsoft Graph, sending as {mailbox}.')
        self.warnings.append(
            'Run "manage.py graphcheck --to you@inkprosamoa.com" to confirm the Graph '
            'credentials actually work.'
        )

    def check_site_url(self):
        url = settings.SITE_URL
        if 'localhost' in url or '127.0.0.1' in url:
            self.failures.append(
                f'SITE_URL is {url} — accept/decline links in emailed quotes would be dead.'
            )
        elif not url.startswith('https://'):
            self.warnings.append(f'SITE_URL is not HTTPS: {url}')
        else:
            self.passes.append(f'SITE_URL: {url}')

    def check_pdf(self):
        from main.pdf import weasyprint_available

        if weasyprint_available():
            self.passes.append('WeasyPrint is available — quotes render as PDF.')
        else:
            self.warnings.append(
                'WeasyPrint cannot load its native libraries. Quotes will still send, '
                'but as HTML attachments rather than PDFs.'
            )

    def check_catalogue(self):
        from main.models import CategoryImage, PricingRule, ServiceCategory

        try:
            categories = ServiceCategory.objects.count()
            rules = PricingRule.objects.count()
            photos = CategoryImage.objects.count()
        except Exception as exc:
            self.failures.append(f'Cannot read the catalogue: {exc}')
            return

        if not categories or not rules:
            self.failures.append(
                'The catalogue is empty — run: manage.py bootstrap'
            )
        elif not photos:
            self.warnings.append(
                'No product photography imported — run: manage.py import_ratecard_images'
            )
        else:
            self.passes.append(
                f'Catalogue: {categories} categories, {rules} rules, {photos} photos.'
            )
