"""Derive the site's logo assets from the InkPro master artwork.

There are two masters, because the mark is drawn differently for light and
dark backgrounds:

``assets/logo-master-on-light.png``
    Black splat, yellow script, black tagline. Correct on white — the email
    shell, the quote PDF and the site footer.

``assets/logo-master-on-dark.png``
    Yellow splat, black script, yellow tagline. Correct on the near-black
    site header.

Both arrive as flat artwork — one on white, one on black — with generous
margins. This command detects whichever flat colour surrounds the mark, knocks
it out to transparency, trims the margins and writes the web assets. It never
recolours anything: the artwork's own colours are preserved exactly.

    python manage.py build_logo_assets
    python manage.py build_logo_assets --on-dark path/to/other.png
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: How far a pixel may drift from the corner colour and still count as background.
BACKGROUND_TOLERANCE = 32
#: Anything at or below this on all channels counts as the black artwork.
DARK_THRESHOLD = 110
BRAND_YELLOW = (255, 210, 0)


class Command(BaseCommand):
    help = 'Generate the site logo variants from the master InkPro artwork.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--on-light',
            default=None,
            help='Master drawn for light backgrounds (default: assets/logo-master-on-light.png).',
        )
        parser.add_argument(
            '--on-dark',
            default=None,
            help='Master drawn for dark backgrounds (default: assets/logo-master-on-dark.png).',
        )
        parser.add_argument(
            '--out',
            default=None,
            help='Output directory (defaults to the first STATICFILES_DIRS entry + /img).',
        )

    def handle(self, *args, **options):
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise CommandError('Pillow is required: pip install Pillow') from exc

        base = Path(settings.BASE_DIR) / 'assets'
        on_light = Path(options['on_light'] or base / 'logo-master-on-light.png').expanduser()
        on_dark = Path(options['on_dark'] or base / 'logo-master-on-dark.png').expanduser()

        out_dir = (
            Path(options['out']).expanduser()
            if options['out']
            else Path(settings.STATICFILES_DIRS[0]) / 'img'
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        light = self._prepare(Image, on_light, 'light-background master')
        out_dir_light = out_dir / 'inkpro-logo.png'
        light.save(out_dir_light)
        self.stdout.write(
            self.style.SUCCESS('  ✓ inkpro-logo.png — for the white footer, email and PDF')
        )

        if on_dark.exists():
            dark = self._prepare(Image, on_dark, 'dark-background master')
            dark.save(out_dir / 'inkpro-logo-on-dark.png')
            self.stdout.write(
                self.style.SUCCESS('  ✓ inkpro-logo-on-dark.png — for the black header')
            )
        else:
            self.stdout.write(
                self.style.WARNING(f'  ! no dark master at {on_dark}; kept the existing one')
            )

        self._build_favicon(light).save(out_dir / 'favicon.png')
        self.stdout.write(self.style.SUCCESS('  ✓ favicon.png — splat, yellow on black'))
        self.stdout.write(self.style.SUCCESS(f'\nWrote logo assets to {out_dir}'))

    def _prepare(self, Image, path, label):
        """Knock out the flat background and trim, preserving every colour."""
        if not path.exists():
            raise CommandError(f'Master artwork not found: {path}')
        master = Image.open(path).convert('RGBA')
        knocked = self._knockout_background(master)
        trimmed = self._trim(knocked)
        self.stdout.write(
            f'  {label}: {master.size[0]}x{master.size[1]} → '
            f'{trimmed.size[0]}x{trimmed.size[1]} (background removed, margins trimmed)'
        )
        return trimmed

    # -- image operations ----------------------------------------------------
    def _knockout_background(self, image):
        """Make the flat surround transparent by flooding in from the edges.

        The background colour is read from the corners rather than assumed to
        be white: the light master sits on white, the dark master on black.
        Flooding from the border (instead of replacing every matching pixel)
        means areas *inside* the artwork that happen to share the background
        colour — the black script on the dark master, the white counters on
        the light one — survive untouched.
        """
        image = image.copy()
        width, height = image.size
        pixels = image.load()

        corners = [
            pixels[0, 0], pixels[width - 1, 0],
            pixels[0, height - 1], pixels[width - 1, height - 1],
        ]
        # If the corners disagree there is no flat surround to remove.
        reference = corners[0]
        if any(self._distance(c, reference) > BACKGROUND_TOLERANCE for c in corners):
            return image
        if reference[3] == 0:
            return image  # already transparent

        seen = bytearray(width * height)
        stack = (
            [(x, 0) for x in range(width)]
            + [(x, height - 1) for x in range(width)]
            + [(0, y) for y in range(height)]
            + [(width - 1, y) for y in range(height)]
        )
        while stack:
            x, y = stack.pop()
            if x < 0 or y < 0 or x >= width or y >= height:
                continue
            index = y * width + x
            if seen[index]:
                continue
            current = pixels[x, y]
            if self._distance(current, reference) > BACKGROUND_TOLERANCE:
                continue
            seen[index] = 1
            pixels[x, y] = (current[0], current[1], current[2], 0)
            stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
        return image

    @staticmethod
    def _distance(colour, reference):
        return max(abs(colour[i] - reference[i]) for i in range(3))

    def _trim(self, image):
        bbox = image.getbbox()
        return image.crop(bbox) if bbox else image

    def _recolour_dark(self, image, colour):
        """Repaint the black artwork in *colour*, leaving the yellow alone."""
        image = image.copy()
        pixels = image.load()
        width, height = image.size
        for y in range(height):
            for x in range(width):
                red, green, blue, alpha = pixels[x, y]
                if alpha and red < DARK_THRESHOLD and green < DARK_THRESHOLD and blue < DARK_THRESHOLD:
                    pixels[x, y] = (*colour, alpha)
        return image

    def _build_favicon(self, logo, size=512):
        """Square yellow-on-black ink splat, lifted from the master artwork.

        The full lockup is illegible at 16px, so the favicon uses the splat
        alone — the most recognisable fragment of the mark. The splat is
        isolated by dropping the tagline (found via the blank row between it
        and the lockup) and cropping to the right of the script wordmark, then
        flood-filling from the corners so the wordmark sitting on top of the
        splat is absorbed into a single solid silhouette.
        """
        from PIL import Image, ImageDraw

        width, height = logo.size
        alpha = logo.getchannel('A').point(lambda v: 255 if v > 40 else 0)

        # First fully blank row below the midpoint separates mark from tagline.
        row_counts = [
            sum(alpha.crop((0, y, width, y + 1)).getdata()) for y in range(height)
        ]
        split = next(
            (y for y, count in enumerate(row_counts) if count == 0 and y > height * 0.5),
            int(height * 0.72),
        )

        left = int(width * 0.52)
        silhouette = alpha.crop((left, 0, width, split))

        flood = silhouette.copy()
        for corner in [
            (0, 0),
            (flood.size[0] - 1, 0),
            (0, flood.size[1] - 1),
            (flood.size[0] - 1, flood.size[1] - 1),
        ]:
            try:
                ImageDraw.floodfill(flood, corner, 128, thresh=10)
            except (ValueError, IndexError):  # pragma: no cover - corner already set
                pass
        filled = flood.point(lambda v: 0 if v == 128 else 255)

        pad = int(max(filled.size) * 0.10)
        side = max(filled.size) + pad * 2
        canvas = Image.new('RGBA', (side, side), (10, 10, 10, 255))
        yellow = Image.new('RGBA', filled.size, (*BRAND_YELLOW, 255))
        canvas.paste(
            yellow,
            ((side - filled.size[0]) // 2, (side - filled.size[1]) // 2),
            filled,
        )
        return canvas.resize((size, size), Image.LANCZOS)
