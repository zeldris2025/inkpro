"""Pull the product photography out of InkPro's rate card PDF.

The rate card is the only place these photos exist, so this command extracts
the embedded images, optimises them for the web and files them against the
right service category.

Images are matched by **content hash**, not by page position, so the mapping
below survives the PDF being re-exported or re-ordered. Re-running the command
updates the existing rows rather than duplicating them.

    python manage.py import_ratecard_images "INKPRO Material Rates.pdf"
    python manage.py import_ratecard_images rates.pdf --dry-run
"""

import hashlib
import io

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from main.models import CategoryImage, ServiceCategory

#: Longest edge, in pixels, for the web-optimised copy. The rate card art tops
#: out around 1272px and one file is nearly 1MB; nothing on the site displays
#: these larger than a card or a lightbox.
MAX_EDGE = 1280
JPEG_QUALITY = 82

#: content-hash prefix -> (category slug, caption, is_hero, order)
#: Hashes are the first 10 hex characters of the MD5 of the embedded image.
IMAGE_MAP = {
    # Heat press — garment printing, sized A5/A4/A3
    '904b868acf': ('heat-press', 'A5 heat press print', False, 1),
    'd8133a6265': ('heat-press', 'A4 heat press print', True, 2),
    '727fe1f8ff': ('heat-press', 'A3 heat press print', False, 3),
    # Names / numbers — cut vinyl and DTF
    'b37258be86': ('names-numbers', 'Cut vinyl name on a long-sleeve tee', False, 1),
    '92b2778485': ('names-numbers', 'Back print with name and logo', False, 2),
    '8f73581539': ('names-numbers', 'Cut vinyl name across the shoulders', True, 3),
    '1d01d4faf4': ('names-numbers', 'Name and number on a singlet', False, 4),
    # Banner prints
    'fc91dd1016': ('banner-prints', '1m x 1m printed banner', True, 1),
    '0cf9762b6d': ('banner-prints', '2m x 1m printed banner', False, 2),
    '939b2e03d9': ('banner-prints', '3m x 1m printed banner', False, 3),
    # Sticker prints — large format
    'edd6fbb46c': ('sticker-prints', '1m x 1m printed sticker', True, 1),
    '7710e4a20c': ('sticker-prints', '2m x 1m printed sticker', False, 2),
    # Small format — bottles, labels, packaging
    'fa9754d1df': ('small-format-stickers', 'Printed jar label', False, 1),
    '5486f15ff6': ('small-format-stickers', 'Preserve labels in situ', True, 2),
    'd30f5b8c16': ('small-format-stickers', 'Printed tub packaging', False, 3),
    '6b2bf16ec0': ('small-format-stickers', 'Wedding bottle label', False, 4),
    # Vehicle decals
    '4f879faf2c': ('vehicle-decals', 'Windscreen cutout lettering', False, 1),
    'ee3e1ebd38': ('vehicle-decals', 'Custom text side decal', False, 2),
    '53b2ed4c3a': ('vehicle-decals', 'Rear window name decal', False, 3),
    '50c19732eb': ('vehicle-decals', 'Rear windscreen cutout name', False, 4),
    '6e76b73949': ('vehicle-decals', 'Door decal on a work ute', True, 5),
    '814b64acb1': ('vehicle-decals', 'Full door decal, cut vinyl', False, 6),
    '9fa682c08c': ('vehicle-decals', 'Printed door sticker', False, 7),
    '564688db19': ('vehicle-decals', 'Fleet door branding', False, 8),
    'ad6581bb61': ('vehicle-decals', 'Taxi door signage', False, 9),
    # One-way vision film
    'c30c3bb843': ('one-way-vision', 'Showroom window graphics', True, 1),
    'a8f0cb1979': ('one-way-vision', 'Full shopfront window wrap', False, 2),
    # Pull-up banners
    '290efc25b8': ('pull-up-banners', '1.2m x 2m pull-up banner', True, 1),
    '0e12627801': ('pull-up-banners', '850mm x 2m banner with stand and bag', False, 2),
    # Funeral badges
    '5143636ffb': ('funeral-badges', 'Memorial badges, medium and large', True, 1),
    '06073e4188': ('funeral-badges', 'Large memorial badge', False, 2),
    '75c32b7ca4': ('funeral-badges', 'Large memorial badge', False, 3),
}


class Command(BaseCommand):
    help = "Extract the rate card PDF's product photos and attach them to service categories."

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to the rate card PDF.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing.')
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Delete existing imported images for the matched categories first.',
        )

    def handle(self, *args, **options):
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise CommandError('pypdf is required: pip install pypdf') from exc
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise CommandError('Pillow is required: pip install Pillow') from exc

        try:
            reader = PdfReader(options['path'])
        except FileNotFoundError as exc:
            raise CommandError(f'PDF not found: {options["path"]}') from exc

        categories = {c.slug: c for c in ServiceCategory.objects.all()}
        if not categories:
            raise CommandError('No service categories. Run `manage.py seed_ratecard` first.')

        if options['replace'] and not options['dry_run']:
            removed = CategoryImage.objects.exclude(source_hash='').delete()[0]
            self.stdout.write(self.style.WARNING(f'Removed {removed} previously imported images.'))

        seen = set()
        created = updated = skipped = 0

        with transaction.atomic():
            for page_number, page in enumerate(reader.pages, start=1):
                for embedded in page.images:
                    digest = hashlib.md5(embedded.data).hexdigest()[:10]
                    if digest in seen:
                        continue
                    seen.add(digest)

                    mapping = IMAGE_MAP.get(digest)
                    if not mapping:
                        skipped += 1
                        self.stdout.write(
                            f'  · page {page_number}: unmapped image {digest} — skipped'
                        )
                        continue

                    slug, caption, is_hero, order = mapping
                    category = categories.get(slug)
                    if not category:
                        skipped += 1
                        self.stdout.write(
                            self.style.WARNING(f'  · unknown category "{slug}" for {digest}')
                        )
                        continue

                    if options['dry_run']:
                        created += 1
                        self.stdout.write(f'  · would import {digest} → {slug}: {caption}')
                        continue

                    payload, filename = self._optimise(Image, embedded.data, digest, slug)
                    record, was_created = CategoryImage.objects.update_or_create(
                        source_hash=digest,
                        defaults={
                            'category': category,
                            'caption': caption,
                            'alt_text': f'{caption} — {category.name} by InkPro',
                            'is_hero': is_hero,
                            'display_order': order,
                        },
                    )
                    record.image.save(filename, ContentFile(payload), save=True)
                    created += was_created
                    updated += not was_created

            if options['dry_run']:
                transaction.set_rollback(True)

        verb = 'Would import' if options['dry_run'] else 'Imported'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {created} new and {updated} updated photos across '
                f'{len({v[0] for v in IMAGE_MAP.values()})} categories ({skipped} unmapped).'
            )
        )
        if not options['dry_run']:
            for slug in sorted({v[0] for v in IMAGE_MAP.values()}):
                count = CategoryImage.objects.filter(category__slug=slug).count()
                self.stdout.write(f'  {slug:24} {count} photo{"s" if count != 1 else ""}')

    def _optimise(self, Image, data, digest, slug):
        """Downscale and re-encode so the site is not serving print-weight art."""
        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode in ('RGBA', 'LA', 'P'):
            # Flatten onto white: these sit on light cards, and JPEG has no alpha.
            flattened = Image.new('RGB', image.size, (255, 255, 255))
            converted = image.convert('RGBA')
            flattened.paste(converted, mask=converted.split()[-1])
            image = flattened
        else:
            image = image.convert('RGB')

        if max(image.size) > MAX_EDGE:
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=JPEG_QUALITY, optimize=True, progressive=True)
        return buffer.getvalue(), f'{slug}-{digest}.jpg'
