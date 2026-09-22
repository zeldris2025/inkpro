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
#: The favicon tile's background, matching the site chrome.
TILE_BLACK = (10, 10, 10)
#: Clear space around the mark, as a fraction of its longest side.
FAVICON_PADDING = 0.06
#: Sizes written for the browser tab. The large PNG alone looks muddy in a
#: 16px tab, because the browser's downscale is cruder than Pillow's.
FAVICON_PNGS = {
    'favicon.png': 512,          # PWA manifests, link previews, the fallback
    'favicon-32.png': 32,        # the tab itself on a HiDPI display
    'apple-touch-icon.png': 180,  # iOS home screen
}
#: Sizes packed into favicon.ico, for the browsers that ask for /favicon.ico.
FAVICON_ICO_SIZES = [(16, 16), (32, 32), (48, 48)]


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
            dark = None
            self.stdout.write(
                self.style.WARNING(f'  ! no dark master at {on_dark}; kept the existing one')
            )

        # The tab sits on browser chrome, not on the page, so the favicon uses
        # the dark master: yellow mark on its own black tile.
        icon_source = dark if dark is not None else light
        for name, size in FAVICON_PNGS.items():
            self._build_favicon(icon_source, size).save(out_dir / name)
        self.stdout.write(
            self.style.SUCCESS(
                '  ✓ ' + ', '.join(FAVICON_PNGS) + ' — the InkPro lockup on black'
            )
        )
        # Pillow downsamples the source itself when writing the .ico, so hand it
        # the largest size we need and let it build the rest of the pack.
        self._build_favicon(icon_source, max(s for s, _ in FAVICON_ICO_SIZES)).save(
            out_dir / 'favicon.ico', sizes=FAVICON_ICO_SIZES
        )
        self.stdout.write(self.style.SUCCESS('  ✓ favicon.ico — 16/32/48 pack'))
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

    def _build_favicon(self, logo, size):
        """The InkPro lockup itself, centred on a square black tile.

        The tagline is dropped at the first blank row beneath the lockup — it
        is unreadable long before the tab size is reached — but everything
        above it is kept as drawn, script and splat together, so the tab shows
        the logo rather than an anonymous shape. The mark is wider than it is
        tall, so it is centred on a square tile with a little clear space
        rather than cropped to fit.
        """
        from PIL import Image

        mark = self._trim(self._drop_tagline(logo))
        pad = int(max(mark.size) * FAVICON_PADDING)
        side = max(mark.size) + pad * 2

        canvas = Image.new('RGBA', (side, side), (*TILE_BLACK, 255))
        canvas.alpha_composite(
            mark, ((side - mark.size[0]) // 2, (side - mark.size[1]) // 2)
        )
        return canvas.resize((size, size), Image.LANCZOS)

    def _drop_tagline(self, logo):
        """Cut the artwork at the first blank row below its midpoint.

        The lockup and the "Think Ink, Think Pro" tagline are separated by a
        band of clear space; anything below that band is tagline.
        """
        width, height = logo.size
        alpha = logo.getchannel('A').point(lambda v: 255 if v > 40 else 0)
        row_counts = [
            sum(alpha.crop((0, y, width, y + 1)).getdata()) for y in range(height)
        ]
        split = next(
            (y for y, count in enumerate(row_counts) if count == 0 and y > height * 0.5),
            height,
        )
        return logo.crop((0, 0, width, split))
