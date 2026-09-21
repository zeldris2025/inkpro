#!/usr/bin/env bash
# Azure App Service (Linux, Python runtime) startup command.
#
# Set this as the App Service "Startup Command":
#     bash /home/site/wwwroot/startup.sh
#
# Oryx installs requirements.txt at build time; this script does the runtime
# work — native libraries, database migrations, static files — and then hands
# off to gunicorn.
set -euo pipefail

echo "==> InkPro startup"

# --- WeasyPrint's native dependencies ---------------------------------------
# The stock Python runtime image has no cairo/pango, so quote PDFs would fall
# back to HTML attachments. Installing them here works on the Debian-based
# App Service images when the container runs as root; when it does not, the
# app still starts and PDF generation degrades gracefully rather than failing.
if ! python -c "import weasyprint" >/dev/null 2>&1; then
  echo "==> Installing WeasyPrint native libraries"
  (apt-get update -qq \
    && apt-get install -y --no-install-recommends \
       libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
       libffi-dev shared-mime-info fonts-dejavu-core \
    && echo "==> Native libraries installed") \
  || echo "!! Could not install native libraries — quotes will be sent as HTML, not PDF."
fi

# --- Persistent media --------------------------------------------------------
# /home survives restarts; /home/site/wwwroot is replaced on every deploy, so
# uploads live alongside it rather than inside it.
export MEDIA_ROOT="${MEDIA_ROOT:-/home/site/media}"
mkdir -p "$MEDIA_ROOT"
echo "==> Media root: $MEDIA_ROOT"

# --- Database ----------------------------------------------------------------
echo "==> Applying migrations"
python manage.py migrate --noinput

# Seeds are idempotent, so a redeploy re-asserts the catalogue without
# duplicating it. Photos come from the committed rate card PDF.
echo "==> Seeding catalogue"
python manage.py bootstrap --skip-images || echo "!! Catalogue seed reported errors (see above)"
python manage.py import_ratecard_images || echo "!! Photo import failed — gallery will be empty"

# --- Static files ------------------------------------------------------------
echo "==> Collecting static files"
python manage.py collectstatic --noinput

# --- Serve -------------------------------------------------------------------
echo "==> Starting gunicorn"
exec gunicorn inkpro.wsgi:application \
  --bind=0.0.0.0:"${PORT:-8000}" \
  --workers="${GUNICORN_WORKERS:-3}" \
  --timeout=120 \
  --access-logfile '-' \
  --error-logfile '-'
