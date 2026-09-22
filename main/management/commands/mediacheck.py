"""Report database rows whose uploaded file is missing from MEDIA_ROOT.

A deployment that replaces the media directory leaves the PostgreSQL rows
intact and the files gone, so the site renders broken images with no record of
what it lost. This lists them, per field, with enough detail to re-upload:

    python manage.py mediacheck
    python manage.py mediacheck --verbose        # name every missing file
    python manage.py mediacheck --clear-missing  # blank the dead references

``--clear-missing`` only empties fields that may be blank; a required image
field is reported and left alone, because blanking it would break the page that
renders it. Category photography from the rate card does not need re-uploading
at all — ``import_ratecard_images`` re-extracts it from the committed PDF.
"""

from pathlib import Path

from django.core.management.base import BaseCommand

from main.models import CategoryImage, Quote, QuoteItem, ServiceCategory

#: (model, field name, whether the field may be left blank)
MEDIA_FIELDS = [
    (ServiceCategory, 'hero_image', True),
    (CategoryImage, 'image', False),
    (Quote, 'pdf_file', True),
    (QuoteItem, 'artwork', True),
]


class Command(BaseCommand):
    help = 'Find uploaded files referenced by the database but missing from disk.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--verbose', action='store_true', help='List every missing file by name.'
        )
        parser.add_argument(
            '--clear-missing',
            action='store_true',
            help='Blank references to missing files on fields that allow it.',
        )

    def handle(self, *args, **options):
        from django.conf import settings

        root = Path(settings.MEDIA_ROOT)
        self.stdout.write(f'MEDIA_ROOT: {root}')
        if not root.exists():
            self.stdout.write(
                self.style.WARNING('  MEDIA_ROOT does not exist — nothing has been uploaded here.')
            )

        total_missing = 0
        cleared = 0
        for model, field, nullable in MEDIA_FIELDS:
            label = f'{model.__name__}.{field}'
            rows = model.objects.exclude(**{field: ''}).exclude(**{f'{field}__isnull': True})
            missing = []
            for row in rows:
                file_field = getattr(row, field)
                if not file_field:
                    continue
                if not Path(file_field.path).exists():
                    missing.append((row, file_field.name))

            present = rows.count() - len(missing)
            if not missing:
                self.stdout.write(self.style.SUCCESS(f'  OK    {label}: {present} file(s) present.'))
                continue

            total_missing += len(missing)
            self.stdout.write(
                self.style.ERROR(
                    f'  GONE  {label}: {len(missing)} missing, {present} present.'
                )
            )
            if options['verbose']:
                for row, name in missing:
                    self.stdout.write(f'          {row.pk}: {name}  ({row})')
            if options['clear_missing']:
                if not nullable:
                    self.stdout.write(
                        '          not cleared: the field is required, so a blank would '
                        'break the page that renders it.'
                    )
                    continue
                for row, _name in missing:
                    setattr(row, field, '')
                    row.save(update_fields=[field])
                    cleared += 1

        self.stdout.write('')
        if not total_missing:
            self.stdout.write(self.style.SUCCESS('Every referenced upload is on disk.'))
            return
        self.stdout.write(f'{total_missing} referenced file(s) are missing from {root}.')
        if cleared:
            self.stdout.write(f'Cleared {cleared} dead reference(s).')
        self.stdout.write(
            'Category photography is recoverable: python manage.py import_ratecard_images'
        )
