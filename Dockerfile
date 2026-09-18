FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# WeasyPrint renders the quote PDFs and needs the cairo/pango stack; without
# these the app still runs but falls back to HTML quote documents.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libffi-dev shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

EXPOSE 8000
CMD ["gunicorn", "inkpro.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
