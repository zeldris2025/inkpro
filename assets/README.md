# Logo masters

Source artwork. **These are inputs, not outputs** — they live here rather than
in `static/img/` so that regenerating the site assets can never overwrite them.

| File | Artwork |
| --- | --- |
| `logo-master-on-light.png` | Black splat, yellow script, black tagline — drawn for light backgrounds |
| `logo-master-on-dark.png` | Yellow splat, black script, yellow tagline — drawn for dark backgrounds |

Both arrive with a flat background (white and black respectively) and generous
margins. Regenerate the web assets from them with:

```bash
python manage.py build_logo_assets
```

That trims the margins, knocks the background out to transparency and writes
`static/img/inkpro-logo.png`, `inkpro-logo-on-dark.png` and `favicon.png`.
