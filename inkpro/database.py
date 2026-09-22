"""Resolve the database configuration, including Azure's injected variables.

Attaching a PostgreSQL server to an App Service through the portal (Service
Connector) does **not** set ``DATABASE_URL``. It injects its own variables —
``AZURE_POSTGRESQL_CONNECTIONSTRING``, or a set of
``AZURE_POSTGRESQL_HOST``/``USER``/``PASSWORD``/``DATABASE`` values, or a
legacy ``POSTGRESQLCONNSTR_*`` app setting.

Reading only ``DATABASE_URL`` means the app silently falls back to SQLite on a
correctly-provisioned App Service: the site comes up, then every page that
touches the database fails with "no such table", because the tables were never
created in the throwaway file.

This module checks each source in turn so the app finds the database however
it was wired up.
"""

import os
import re
from urllib.parse import quote

#: libpq keyword/value connection string, e.g.
#: "dbname=inkpro host=x.postgres.database.azure.com user=u password=p sslmode=require"
_KEYWORD_PAIR = re.compile(r"(\w+)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)")


def _clean(value):
    return value.strip().strip('\'"') if value else value


def _dsn_from_keyword_string(raw):
    """Turn a libpq keyword string into a postgres:// URL."""
    parts = {key.lower(): _clean(value) for key, value in _KEYWORD_PAIR.findall(raw)}
    host = parts.get('host')
    name = parts.get('dbname') or parts.get('database')
    if not host or not name:
        return None
    user = quote(parts.get('user') or parts.get('username') or '', safe='')
    password = quote(parts.get('password') or '', safe='')
    port = parts.get('port') or '5432'
    credentials = f'{user}:{password}@' if user else ''
    sslmode = parts.get('sslmode') or 'require'
    return f'postgres://{credentials}{host}:{port}/{name}?sslmode={sslmode}'


def _dsn_from_parts(environ):
    """Build a DSN from Azure's individual AZURE_POSTGRESQL_* variables."""
    host = environ.get('AZURE_POSTGRESQL_HOST')
    name = environ.get('AZURE_POSTGRESQL_DATABASE') or environ.get('AZURE_POSTGRESQL_NAME')
    if not host or not name:
        return None
    user = quote(environ.get('AZURE_POSTGRESQL_USER', ''), safe='')
    password = quote(environ.get('AZURE_POSTGRESQL_PASSWORD', ''), safe='')
    port = environ.get('AZURE_POSTGRESQL_PORT', '5432')
    ssl = environ.get('AZURE_POSTGRESQL_SSL', 'require')
    credentials = f'{user}:{password}@' if user else ''
    return f'postgres://{credentials}{host}:{port}/{name}?sslmode={ssl}'


def resolve_database_url(environ=None, default=None):
    """The DSN to use, checking every place a host might have put it.

    Returns *default* only when nothing else is configured.
    """
    environ = os.environ if environ is None else environ

    explicit = _clean(environ.get('DATABASE_URL'))
    if explicit:
        return explicit

    for key in (
        'AZURE_POSTGRESQL_CONNECTIONSTRING',
        'AZURE_POSTGRESQL_CONNECTION_STRING',
        # App Service surfaces portal-defined connection strings with a
        # type-specific prefix.
        'POSTGRESQLCONNSTR_DATABASE_URL',
        'POSTGRESQLCONNSTR_DEFAULTCONNECTION',
    ):
        raw = _clean(environ.get(key))
        if not raw:
            continue
        if raw.startswith(('postgres://', 'postgresql://')):
            return raw
        dsn = _dsn_from_keyword_string(raw)
        if dsn:
            return dsn

    from_parts = _dsn_from_parts(environ)
    if from_parts:
        return from_parts

    return default
