# Brand assets

These files are **generated**, not hand-placed. They are derived from the master
InkPro logo artwork by:

```bash
python manage.py build_logo_assets
```

The source artwork lives in `assets/` (see `assets/README.md`), deliberately
outside this folder so a rebuild cannot overwrite it.

Re-run that command whenever the master artwork changes.

| File | Used by | Treatment |
| --- | --- | --- |
| `inkpro-logo.png` | White footer, email, quote PDF | The light-background master, background removed and trimmed |
| `inkpro-logo-on-dark.png` | Site header | The dark-background master, background removed and trimmed |
| `favicon.png` | Manifests, link previews | The splat alone, yellow on black, 512px |
| `favicon-32.png` | Browser tab, HiDPI | The same tile rendered at 32px rather than downscaled by the browser |
| `favicon.ico` | Browser tab, `/favicon.ico` | 16/32/48 pack, for bookmarks and history entries |
| `apple-touch-icon.png` | iOS home screen | The same tile at 180px |
| `press-fallback.svg` | Homepage, when the 3D layer is declined | Hand-authored flat illustration; not generated |

The splat cannot be separated from the wordmark cleanly — in the master
artwork the script sits on top of it and the swoosh runs into it, so there is
no gap to cut along. The favicon therefore takes a square window anchored to
the lockup's right edge, a little wider than the mark is tall so the droplets
are not sliced flat, and flood-fills the wordmark into one solid silhouette.

## Why variants are needed

The master lockup is drawn for light backgrounds: the splat and the tagline are
both black. Used unchanged on the near-black site chrome it becomes a white box
with an invisible mark, which is why the header and footer take the `on-dark`
variant while email and the PDF — which render on white — take the original.

If these files are absent the templates fall back to a text lockup rather than a
broken image, so the site still renders correctly.
