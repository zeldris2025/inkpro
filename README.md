# InkPro

> Think Ink, Think Pro

A Django application for InkPro, a printing company. It serves three audiences
from one codebase:

- **Public visitors** — an animated marketing site built on the live rate card.
- **Customers** — a multi-step quote builder with live pricing, plus an optional
  account for tracking and re-ordering.
- **Staff** — a quote review pipeline with one-click approve-and-send, a sales
  dashboard, and the Recharge Register.

---

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # optional; sensible defaults apply without it

python manage.py bootstrap      # migrate, seed, build assets, import photos
python manage.py createsuperuser
python manage.py runserver
```

`bootstrap` is the whole build in one step, and every part of it is idempotent,
so re-run it any time — especially after a `git pull`. Add `--demo` to also
load sample quotes and register entries so the staff dashboard has something to
show.

### Why a fresh clone starts empty

The database (`db.sqlite3`), the uploaded media directory and the compiled
static files are **build products, not source**, so they are deliberately not
in version control — a shared SQLite file would collide on every pull, and
`media/` holds customer artwork.

What *is* committed is everything needed to rebuild them: the rate card PDF and
the logo masters in [`assets/`](assets/). That is what `bootstrap` reads. So a
clone that has not been bootstrapped has no services, no pricing and no
photography — it is not broken, it just has not been built yet.

| Step | Rebuilds |
| --- | --- |
| `migrate` | The database schema |
| `seed_ratecard` | 10 service categories, 28 pricing rules, the urgent fee band, email templates |
| `bootstrap_groups` | The Customer / Staff / Owner role groups |
| `build_logo_assets` | `static/img/` logo variants and favicon, from `assets/` |
| `import_ratecard_images` | The 32 product photos into `media/`, from `assets/rate-card.pdf` |

Real business data is **not** committed and has to be imported separately on
each machine — see [Importing the existing register](#importing-the-existing-register).

| Area | URL |
| --- | --- |
| Marketing site | `/` |
| Quote builder | `/quote/` |
| Customer account | `/my/quotes/` |
| Staff dashboard | `/staff/` |
| Quote inbox | `/staff/quotes/` |
| Recharge Register | `/staff/register/` |
| Django admin | `/admin/` |

---

## Logo assets

There are two masters, because the mark is drawn differently for light and dark
backgrounds. They live in **`assets/`**, not `static/img/`, so regenerating the
site assets can never overwrite the source artwork:

| Master | Artwork |
| --- | --- |
| `assets/logo-master-on-light.png` | Black splat, yellow script, black tagline |
| `assets/logo-master-on-dark.png` | Yellow splat, black script, yellow tagline |

```bash
python manage.py build_logo_assets
```

Each master arrives as flat artwork with generous margins — one on white, one
on black. The command reads the background colour from the corners rather than
assuming white, floods it out to transparency from the edges, trims the
margins, and writes:

| File | Used by |
| --- | --- |
| `inkpro-logo.png` | White footer, transactional email, quote PDF |
| `inkpro-logo-on-dark.png` | Site header |
| `favicon.png` | Browser tab — the splat alone, legible at 16px |

Flooding from the border rather than replacing every matching pixel means areas
*inside* the artwork that share the background colour survive: the black script
on the dark master, the white letter counters on the light one.

**Nothing is recoloured.** Each surface gets the artwork that was drawn for it,
with its own colours intact. If the generated files are missing, the templates
fall back to a text lockup rather than a broken image.

---

## Product photography

InkPro's rate card PDF is the only place the product photos exist, so they are
extracted from it:

```bash
python manage.py import_ratecard_images "INKPRO Material Rates.pdf"
python manage.py import_ratecard_images rates.pdf --dry-run
```

This pulls the 32 embedded photos, downscales them to 1280px and re-encodes
them as progressive JPEG (roughly 1.6MB total, down from print weight), then
files each one against its service category as a `CategoryImage`.

Photos are matched by **content hash**, not page position, so the mapping
survives the PDF being re-exported or re-ordered, and re-running updates rows
in place rather than duplicating them. Categories without photography fall back
to their emoji icon, so nothing breaks before the import is run.

The photos drive the service cards, the per-service galleries, the quote
wizard's category picker and the portfolio page. Clicking any of them opens a
lightbox — one shared partial, `main/partials/_lightbox.html`, closing on
Escape, backdrop click or the close button, and locking page scroll while open.
Images render at their natural size rather than being upscaled.

Everything is editable in the admin afterwards; `CategoryImage` is an ordinary
model with an inline on `ServiceCategory`.

---

## Importing the existing register

The historical "INKPRO Recharge Register" spreadsheet imports straight into the
`Invoice` table:

```bash
python manage.py import_register "INKPRO Recharge Register 2026.xlsx"
python manage.py import_register register.xlsx --sheet 2026 --dry-run
python manage.py import_register register.xlsx --link-customers
```

The importer is written for a hand-maintained workbook. It finds the header row
anywhere in the first 20 rows, matches columns by fuzzy name rather than fixed
position, parses money and dates whether they are real Excel values or text
(`$1,234.50`, `12/03/2026`), skips blank and `TOTAL` rows, and is idempotent —
rows are matched on invoice number, so re-running updates rather than
duplicates. A spreadsheet status of `TBC` is preserved; every other status is
re-derived from the balance.

`--dry-run` parses and reports without writing anything.

Validated against the real *INKPRO Recharge Register 2026* workbook: the
importer finds the header on row 8, matches all 12 columns, skips the title and
summary blocks and the alternating blank rows, and lands all 9 data rows.
Totals reconcile against the spreadsheet's own summary block — **total
invoiced, total received, paid count and unpaid count all match exactly**.

The one figure that differs is `OUTSTANDING`. The workbook reports 8,685, which
is simply its total-invoiced figure: its per-row Balance column was never
updated on the two paid invoices, so summing that column double-counts them.
The correct outstanding balance is 8,685 − 2,961 = **5,724**, which is what
this system computes, because `Invoice.save()` always derives the balance from
`invoice_amount − amount_received` rather than trusting a stored value.

---

## Launch promotion

A marquee in the homepage hero announces the launch offer. Both the copy and
the on/off switch are settings, because a launch offer is temporary by
definition and retiring it after the 20th sale should not need a code change:

```
LAUNCH_PROMO_ENABLED=True
LAUNCH_PROMO_TEXT=We just launched — our first 20 sales will receive a massive 50% discount
```

It is pure CSS. The track holds the message twice and slides exactly -50%, so
the second copy lands where the first began and the loop is seamless with no
JavaScript — only `transform` animates, so it stays on the compositor and never
triggers layout. It pauses on hover and on keyboard focus.

The moving copies are hidden from assistive technology, which would otherwise
read the message several times in a jumble; a single static copy is exposed
instead. Under `prefers-reduced-motion` the scrolling stops entirely and one
centred, legible copy is shown.

---

## Currency and locale

Prices are **Samoan Tala (WST)**, and the business runs on `Pacific/Apia` time —
which matters, because the same-day urgency fee and every quote timestamp
depend on the clock. WST shares the `$` symbol with several other currencies,
so the code and name are stated explicitly wherever an amount could be
misread: the public rate card, the site footer, the emailed quote and the
quote PDF.

`LANGUAGE_CODE` is `en-nz` purely for its `d/m/Y` date and number formatting;
Django ships no `en-ws` locale.

---

## How pricing works

All arithmetic lives in [`main/pricing.py`](main/pricing.py), free of model
imports so it can be unit tested on its own. Money is `Decimal` throughout,
rounded half-up to cents at each boundary.

| Pricing type | Behaviour |
| --- | --- |
| `FLAT_RATE` | `base_price` per unit — A4 heat press at $15 |
| `PER_METER` | `base_price` is a **per-square-metre** rate: a 2m × 1m banner at $140/m² is $280 |
| `CUSTOM_SIZE_FORMULA` | Same as above, for sizes outside the standard tiers |
| `PER_UNIT_RANGE` | Open-ended rows (vehicle decals, $20–$60). Quotes the floor until staff pin down a figure |

Prices are stored and quoted **GST-exclusive**. GST (15%, configurable via
`GST_RATE`) is levied on the subtotal less any discount, plus the urgency fee.
`Quote.total_override` lets staff force a negotiated number onto a quote while
still showing the calculated components.

The rate card tiers seeded by `seed_ratecard` reproduce the published figures
exactly — a 2m × 1m banner is $280, or $322 including GST.

---

## The quote workflow

```
DRAFT → SUBMITTED → IN_REVIEW ⇄ REVISED → APPROVED → SENT → ACCEPTED
                                                          ↘ DECLINED
```

Transitions are enforced by `Quote.transition_to()`, which raises
`InvalidTransition` rather than silently allowing an illegal move, and stamps
the relevant timestamps as it goes. `ACCEPTED` is terminal.

The wizard writes straight to a `DRAFT` quote rather than a session cart, so
artwork uploads land in real storage and the live sidebar totals come from the
same code that prices the final quote.

**Approve & send** (one button on the staff review screen) approves the quote,
renders the branded PDF, and emails it with accept/decline links that work
without a login — they authenticate on a per-quote random token. Acceptance
opens a `NOT_PAID` entry in the register automatically, and does so idempotently
so a double-click cannot create two invoices.

---

## Layout

```
inkpro/            project settings, URLs, Celery app
main/
  models.py        catalogue, quotes, invoices, email templates
  pricing.py       pure pricing arithmetic (unit tested)
  views.py         marketing, quote wizard, customer account
  views_staff.py   staff portal, approve-and-send, register
  api.py           DRF endpoints for the dashboard and live pricing
  emails.py        branded transactional email
  pdf.py           WeasyPrint quote documents
  tasks.py         Celery tasks with an inline fallback
  permissions.py   role groups and access helpers
  management/commands/
    seed_ratecard.py    load the published rate card
    import_register.py  import the historical spreadsheet
    bootstrap_groups.py create the role groups
    seed_demo.py        demo data for development
templates/         base, marketing, wizard, account, staff, email, pdf
static/            brand CSS, animation JS, logo assets
```

A single `main` app is used rather than five separate ones. The domain is small
and tightly coupled — quotes reference the catalogue, invoices reference quotes
— and splitting it would add import ceremony without buying isolation.

---

## Front end

Tailwind, Alpine, HTMX, GSAP and Chart.js all load from CDNs, so there is **no
build step** — clone and run. Tailwind's brand tokens (`ink-yellow`,
`ink-black`, `ink-cyan`, `ink-magenta`, `font-display`) are configured inline in
`templates/base.html`; reusable component classes and effects Tailwind cannot
express live in `static/css/inkpro.css`.

Interactivity is server-rendered wherever it touches money. The wizard's live
price and the quote sidebar are HTMX fragments rendered by Django, so the figure
on screen always comes from the same code that builds the quote. Alpine handles
only local UI state — menus, toggles, the testimonial carousel.

Animations degrade safely: if GSAP fails to load, or the visitor has
`prefers-reduced-motion` set, `static/js/inkpro.js` clears the reveal classes
immediately so no content is ever trapped behind an animation that will not run.

> For a production deployment under sustained traffic, compile Tailwind to a
> static stylesheet and self-host the libraries instead of using the Play CDN.

---

## The scroll-driven 3D layer (homepage only, currently disabled)

**This layer is switched off.** Set `ENABLE_3D_HERO=True` to bring it back —
the scene, its loader, the static fallback and the staging section all remain
in the tree, and the `[data-scene]` anchors stay on the sections, so no
template edit is needed. Everything below describes it as it behaves when
enabled.

A Three.js scene sits in a fixed canvas behind the homepage content (`z-0`,
content at `z-10`), with five stages bound to the `[data-scene]` sections:

| Section | Stage | What happens |
| --- | --- | --- |
| Hero | `press` | The press activates — rollers spin up, ink drips fall |
| Services | `sheet` | A printed sheet peels off and cycles through shirt → banner → sticker |
| Process | `cmyk` | Four ink planes separate and recombine while the camera orbits |
| Testimonials | `showcase` | A printed panel floats and flips, catching the key light |
| Stamp | `stamp` | The press stamps the logo, ink bursts, everything settles |

Each stage owns a ScrollTrigger with `scrub`, so scroll position drives a
normalised 0–1 progress that in turn drives rotation, camera, materials and
particles. Triggers run **centre to centre** (`top center` → `bottom center`)
so adjacent sections hand over cleanly at the viewport midline; exactly one
stage group is ever visible, and only its update function runs.

All geometry is procedural — the logo is the single texture the scene
downloads — which keeps the polycount in the low thousands and the payload
small.

### It is progressive enhancement, not a dependency

`inkpro3d-loader.js` is ~3KB and is the only `<script>` the page ships for this.
It decides whether the device can afford the scene, and only then dynamically
imports the Three.js bundle — **after** the `load` event and a
`requestIdleCallback`. First paint of the hero and its quote CTA never waits on
it. The scene is declined outright, leaving a static SVG illustration with CSS
parallax in place, when any of these hold:

- `prefers-reduced-motion: reduce`
- Data Saver is on, or the connection reports 2G
- `deviceMemory < 4` or `hardwareConcurrency < 4`
- WebGL is unavailable, or the renderer is a software rasteriser
  (SwiftShader/llvmpipe report as WebGL but crawl)

Phones that do pass get a reduced budget: lower pixel-ratio cap, no
antialiasing, fewer particles and coarser geometry. Rendering also stops
entirely when the tab is hidden. A CDN failure is caught and leaves the page
untouched.

The existing GSAP scroll-reveals, stat counters and card animations are
unchanged — this layer supplements them.

> The canvas fades in to 65% opacity (45% on mobile) by design: it is scenery
> behind the copy, not the subject of the page.

---

## Roles

| Group | Access |
| --- | --- |
| `Customer` | Quote builder, own quote history only |
| `Staff` | Quote review, register, dashboard. No user management |
| `Owner` | Everything, including pricing and email template management |

Run `manage.py bootstrap_groups` to create them. Staff views gate on
`is_staff`, so superusers always get through; add owners to the `Owner` group
**and** tick `is_staff` so they can reach the portal.

---

## Email

All messages render a branded black/yellow HTML shell with a plain text
alternative. The opening and closing paragraphs come from `EmailTemplate` rows,
so the owner can reword them in the admin without a deploy; hard-coded defaults
apply when a row is missing.

Sent on:

- **Quote submitted** — acknowledgement to the customer, alert to the team
  (email, plus Slack if `SLACK_WEBHOOK_URL` is set).
- **Approve & send** — the quote PDF with one-click accept/decline links.
- **Overdue invoices** — a weekday-morning Celery Beat reminder.

The default `EMAIL_BACKEND` prints to the console. Switch to SMTP in `.env` to
send for real.

---

## Async and PDFs

Both are optional dependencies, by design.

**Celery** — with no `CELERY_BROKER_URL` set, `main/tasks.py` runs email and PDF
work inline, so a fresh checkout works without Redis or a worker. Point the
setting at a broker and the same call sites start dispatching asynchronously:

```bash
celery -A inkpro worker -l info
celery -A inkpro beat -l info    # payment reminders
```

**WeasyPrint** — needs native cairo/pango libraries. When they are missing the
quote still goes out: the customer gets the same branded document as an HTML
attachment, and the "view online" link is unaffected. The Dockerfile installs
the native stack, so PDFs work there.

---

## Configuration

Everything is environment-driven via `django-environ`; see `.env.example`.
SQLite is the zero-config default — set `DATABASE_URL` to a `postgres://` DSN to
switch, with no code change. Key settings: `SECRET_KEY`, `DEBUG`,
`ALLOWED_HOSTS`, `SITE_URL` (used to build the emailed quote links),
`GST_RATE`, `QUOTE_VALID_DAYS`, `STAFF_NOTIFY_EMAILS`, `ENABLE_3D_HERO`.

With `DEBUG=False`, SSL redirect, secure cookies and HSTS switch on
automatically.

---

## Tests

```bash
python manage.py test main
```

106 tests covering the places where a silent mistake costs money: pricing
arithmetic against the published rate card, quote status transitions including
illegal ones, invoice balance and payment-status derivation, the guest wizard
end to end, approve-and-send, access control, the dashboard API, the brand
assets, the 3D layer's progressive-enhancement contract, the lightbox, and
the seeded catalogue's fidelity to the printed rate card.

---

## Deploying to Azure App Service

Target: **App Service (Linux, Python runtime)** + **Azure Database for
PostgreSQL** + the domain **inkprosamoa.com**.

### 1. Provision

```bash
# PostgreSQL flexible server
az postgres flexible-server create \
  --name inkpro-db --resource-group inkpro-rg \
  --location australiaeast --tier Burstable --sku-name Standard_B1ms \
  --database-name inkpro --public-access 0.0.0.0 --version 16

# App Service
az webapp up --name inkpro --resource-group inkpro-rg \
  --runtime "PYTHON:3.12" --sku B1
```

> **Runtime matters for the driver.** On Python 3.14, `psycopg[binary]` must be
> at least 3.2.10 — earlier releases ship no cp314 wheel and the install fails
> with *"No matching distribution found for psycopg-binary"*. The pin in
> `requirements.txt` satisfies this, and a test enforces it.

### 2. Configure

Set everything from [`.env.production.example`](.env.production.example) as
App Service **Application settings** (not a `.env` file — App Service injects
them as environment variables). Generate the secret key first:

```bash
python manage.py generate_secret_key
```

Two platform settings are easy to miss and both cause data loss:

| Setting | Why |
| --- | --- |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` | Without it `/home` is not persistent and every uploaded file vanishes on restart |
| `SCM_DO_BUILD_DURING_DEPLOYMENT=true` | Without it Oryx never installs `requirements.txt` |

**Pass `--version` explicitly.** Azure's default major version trails the
current release, and the server version caps which Django the app can run:
Django 6.x needs PostgreSQL 15 or later, and starting on an older default is
what pinned `requirements.txt` to the 5.2 LTS line. A server already on 14
reports `NotSupportedError: PostgreSQL 15 or later is required` the moment the
app touches the database. Check and upgrade in place with:

```bash
az postgres flexible-server show \
  --name inkpro-db --resource-group inkpro-rg --query version

# Major-version upgrades take the server offline and cannot be rolled back.
az postgres flexible-server upgrade \
  --name inkpro-db --resource-group inkpro-rg --version 16
```

Once the server is on 15+, `Django==5.2.17` in `requirements.txt` can go back
to the current release.

### 2b. If you attached Postgres through the portal

Azure's **Service Connector** does not set `DATABASE_URL` — it injects its own
variables (`AZURE_POSTGRESQL_CONNECTIONSTRING`, or a set of
`AZURE_POSTGRESQL_HOST`/`USER`/`PASSWORD`/`DATABASE`). The app reads all of
those, so an attached server is picked up with no extra configuration.

If it is *not* picked up, the app falls back to SQLite on a disk that a deploy
replaces — which shows up as **`no such table: main_invoice`** on any page that
touches the database. Confirm which database is actually in use with:

```bash
python manage.py deploycheck
```

It fails outright on SQLite for exactly this reason.

### 3. Startup command

Set the App Service startup command — **this is the step whose absence causes
`no such table`**, because without it App Service auto-detects Django and runs
gunicorn directly, skipping migrations, seeding and `collectstatic`:

```bash
az webapp config set --name inkpro --resource-group inkpro-rg \
  --startup-file "bash /home/site/wwwroot/startup.sh"
```

Or in the portal: **Configuration → General settings → Startup Command**.

Then confirm it took effect:

```bash
curl https://inkprosamoa.com/health/
```

```json
{"status": "ok", "checks": {"database": "ok", "migrations": "applied",
                            "catalogue": "ok", "engine": "postgresql"}}
```

Anything else is actionable: `"engine": "sqlite"` means the Postgres server was
not found, `"migrations": "N pending"` means the startup command is not
running, and `"catalogue": "empty"` means the seed has not run. The endpoint
returns 503 while degraded, so it also works as the App Service health check
path.

[`startup.sh`](startup.sh) installs WeasyPrint's native libraries, creates the
persistent media directory, migrates, seeds the catalogue, runs
`collectstatic`, then execs gunicorn.

### 4. Verify

```bash
python manage.py deploycheck
```

Run it against the production environment. It checks the things Django's own
`check --deploy` does not — each of which fails *silently* rather than loudly:

| Check | Silent failure it prevents |
| --- | --- |
| Database engine | SQLite on an ephemeral disk loses every quote and invoice on restart |
| Media middleware + writability | Product photos and customer artwork 404 |
| `collectstatic` has run | Manifest storage 500s on every stylesheet |
| Email backend | Quotes printed to the log instead of sent |
| `SITE_URL` | Dead accept/decline links in customers' emails |
| WeasyPrint | Quotes silently downgrade from PDF to HTML |

It exits non-zero on any blocking issue, so it can gate a release.

### Two things specific to this stack

**Media does not come from the repo.** `media/` is gitignored, and on App
Service only `/home` survives a restart — while a deploy *replaces*
`/home/site/wwwroot`. So `MEDIA_ROOT` is set to `/home/site/media`, outside the
deployed tree. `startup.sh` re-imports the product photography from the
committed rate card PDF on every boot, so the gallery is self-healing; customer
artwork uploads persist because they live outside wwwroot.

Django only routes `MEDIA_URL` when `DEBUG` is on, and WhiteNoise handles
static files only — so [`main/middleware.py`](main/middleware.py) serves
`MEDIA_ROOT` through WhiteNoise in production, with `autorefresh` on so a
customer's upload is visible without a restart.

**WeasyPrint needs native libraries the Python runtime image lacks.**
`startup.sh` attempts to `apt-get install` cairo and pango. Where the container
does not permit it, the app still starts and quotes are emailed as HTML
attachments instead of PDFs — `deploycheck` reports this as a warning, not a
failure. If PDFs are essential, deploy the [`Dockerfile`](Dockerfile) to App
Service for Containers instead; it installs the stack at build time.

---

## Troubleshooting a deployment

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no such table: main_invoice` | Running on SQLite — the database was never found, or migrations never ran | Check `deploycheck`; make sure the startup command is set so `migrate` runs |
| `No matching distribution found for psycopg-binary` | Pin predates cp314 wheels | Requires `psycopg[binary]>=3.2.10` |
| Site loads but every image is broken | Media not served, or never imported | `deploycheck` reports both; `startup.sh` re-imports photos each boot |
| Stylesheets 500 | `collectstatic` has not run under manifest storage | `startup.sh` runs it; check the build log |
| Quotes arrive as `.html` not `.pdf` | WeasyPrint's native libraries are missing | Expected on the Python runtime; deploy the container image for real PDFs |
| Quote accept links point at localhost | `SITE_URL` not set | Set `SITE_URL=https://inkprosamoa.com` |
| Emails never arrive | Console email backend still active | Set the SMTP variables |

The most common cause of a half-working deploy is the **startup command not
being set**: App Service then auto-detects Django and runs gunicorn directly,
skipping migrations, the seed and `collectstatic` entirely.

---

## Deploying with Docker

```bash
docker compose up --build
```

Brings up Postgres, Redis, Gunicorn, a Celery worker and Beat, running
migrations and the seed on boot. Put a load balancer in front for TLS;
WhiteNoise serves static files, so no separate static host is required.

---

## Before going live

- [ ] `python manage.py deploycheck` passes against the production environment.
- [ ] `SECRET_KEY` generated (`manage.py generate_secret_key`), `DEBUG=False`.
- [ ] `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` name inkprosamoa.com, not `*`.
- [ ] `DATABASE_URL` points at Postgres — **not** SQLite.
- [ ] `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` and `MEDIA_ROOT=/home/site/media`.
- [ ] SMTP configured and a test quote actually arrives in an inbox.
- [ ] `SITE_URL=https://inkprosamoa.com`, and an emailed accept link opens.
- [ ] Custom domain bound with a TLS certificate.
- [ ] Import the real register: `manage.py import_register "INKPRO Recharge Register 2026.xlsx"`.
- [ ] Re-upload the hand-added Vehicle Decals photo (it is not in the repo).
- [ ] Replace the placeholder contact details (`hello@inkpro.example`) in
      `templates/main/contact.html`, the email footer and the quote PDF.
- [ ] Add the business number and physical address to the quote PDF terms.
- [ ] Confirm the GST rate, the quote validity period and the launch promo copy.
- [ ] Swap the marketing-stat floors in `main/views.py:home` for real figures.
- [ ] Replace the placeholder testimonials in `templates/main/home.html`.
- [ ] Take a database backup schedule on the Postgres server.
