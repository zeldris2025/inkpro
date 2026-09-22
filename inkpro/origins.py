"""Derive the CSRF trusted origins from the hosts the site is served on.

Django rejects any POST whose ``Origin`` header is absent from
``CSRF_TRUSTED_ORIGINS``. Leaving that setting to a second environment
variable means one missing App Service application setting breaks every form
on the site, with a 403 that names an origin the operator can plainly see is
correct — "https://inkprosamoa.com does not match any trusted origins".

A host trusted enough to serve the site is trusted enough to post to it, so
the origins are derived from ``ALLOWED_HOSTS`` instead. An explicit
``CSRF_TRUSTED_ORIGINS`` is still honoured and takes precedence.
"""

import re

#: Hosts served over plain HTTP; everything else is assumed to be behind TLS.
LOCAL_HOSTS = ('localhost', '127.0.0.1', '[::1]')
#: A dotted name that is all digits is an IPv4 address, not a domain.
_IPV4 = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')


def trusted_origins(allowed_hosts, site_url='', configured=()):
    """Origins to trust, given ``ALLOWED_HOSTS`` and ``SITE_URL``.

    Wildcards are skipped: ``*`` says nothing about which origin to trust, and
    a wildcard subdomain pattern is passed through unchanged for Django to
    match. A bare apex domain also trusts its ``www.`` form, because the site
    answers on both — but an IP address or a local host does not, since
    ``www.127.0.0.1`` is not a name anything resolves.
    """
    origins = list(configured)

    def trust(origin):
        if origin not in origins:
            origins.append(origin)

    for host in (h.strip().lstrip('.') for h in allowed_hosts):
        if not host or host == '*':
            continue
        scheme = 'http' if host in LOCAL_HOSTS else 'https'
        trust(f'{scheme}://{host}')
        if (
            '.' in host
            and host not in LOCAL_HOSTS
            and not _IPV4.match(host)
            and not host.startswith(('www.', '*.'))
        ):
            trust(f'{scheme}://www.{host}')

    site_url = (site_url or '').rstrip('/')
    if site_url.startswith(('http://', 'https://')):
        trust(site_url)
    return origins
