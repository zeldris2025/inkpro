"""Bring a fresh checkout up to a working state in one step.

The database, the media directory and the generated static assets are all
build products, not source, so none of them are in version control. A clone
therefore starts with no catalogue, no photography and no logo files — which
looks like "the site is broken" rather than "the site has not been built yet".

This command runs the whole build in the right order:

    python manage.py bootstrap
    python manage.py bootstrap --demo     # also load demo quotes and invoices

Everything it calls is idempotent, so it is safe to re-run after a pull.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Migrate, seed the catalogue, build logo assets and import the rate card photos.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--demo', action='store_true', help='Also load demo quotes and register entries.'
        )
        parser.add_argument(
            '--skip-images',
            action='store_true',
            help='Skip extracting the rate card photography.',
        )

    def handle(self, *args, **options):
        steps = [
            ('Applying migrations', 'migrate', {'interactive': False}),
            ('Seeding the rate card', 'seed_ratecard', {}),
            ('Creating role groups', 'bootstrap_groups', {}),
            ('Building logo assets', 'build_logo_assets', {}),
        ]
        if not options['skip_images']:
            steps.append(('Importing rate card photos', 'import_ratecard_images', {}))
        if options['demo']:
            steps.append(('Loading demo data', 'seed_demo', {}))

        failures = []
        for index, (label, command, kwargs) in enumerate(steps, start=1):
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n[{index}/{len(steps)}] {label}'))
            try:
                call_command(command, **kwargs)
            except Exception as exc:
                # One broken step should not abort the rest of the build, but it
                # must not be whispered either: a skipped photo import used to
                # print a warning and still report "Ready", which read as
                # success on a site that had no photography.
                self.stdout.write(self.style.ERROR(f'  FAILED: {exc}'))
                failures.append((label, exc))

        self.stdout.write('\n' + self.summary(failures))
        if failures:
            raise CommandError(
                f'{len(failures)} step(s) failed — the site is not fully built. '
                'Fix the errors above and re-run; bootstrap is safe to repeat.'
            )

    def summary(self, failures=()):
        from main.models import CategoryImage, PricingRule, ServiceCategory

        if not connection.introspection.table_names():
            return self.style.ERROR('Nothing was created — check the errors above.')

        categories = ServiceCategory.objects.count()
        rules = PricingRule.objects.count()
        photos = CategoryImage.objects.count()

        lines = [
            self.style.ERROR('Built with errors.') if failures else self.style.SUCCESS('Ready.'),
            f'  {categories} service categories',
            f'  {rules} pricing rules',
            self.style.ERROR(f'  {photos} product photos  <- expected 32')
            if photos == 0
            else f'  {photos} product photos',
        ]
        if photos == 0 and not failures:
            lines.append(
                self.style.WARNING(
                    '\n  No photography imported. Check that pypdf is installed\n'
                    '  (pip install -r requirements.txt) and that assets/rate-card.pdf exists.'
                )
            )
        lines += [
            '',
            'Create a login with:  python manage.py createsuperuser',
            'Then:                 python manage.py runserver',
        ]
        return '\n'.join(lines)
