"""Django email backend that sends through Microsoft Graph (Office 365).

InkPro's mailbox lives in Microsoft 365, where basic SMTP authentication is
disabled by default. Instead of an app password we authenticate as a registered
Azure AD application — the OAuth 2.0 *client credentials* flow — and post each
message to Graph's ``sendMail`` endpoint on behalf of a named mailbox.

Set-up, once, in the Azure portal (Entra ID > App registrations):

  1. Register an application; note its *Application (client) ID* and the
     *Directory (tenant) ID*.
  2. Certificates & secrets > New client secret; copy the **value**.
  3. API permissions > Microsoft Graph > *Application* permissions >
     ``Mail.Send`` > Grant admin consent. Delegated permission is not enough:
     nobody is signed in when a quote goes out.
  4. Optionally scope the app to the one mailbox with an Exchange
     ApplicationAccessPolicy so it cannot mail as anyone else.

Then fill in ``MS_GRAPH_*`` in ``.env`` and switch the backend over:

    EMAIL_BACKEND=main.graph_mail.GraphEmailBackend

``manage.py graphcheck`` verifies the credentials before any customer does.

Only stdlib HTTP is used, so there is no new dependency to install on the host.
"""

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

LOGIN_HOST = 'https://login.microsoftonline.com'
GRAPH_HOST = 'https://graph.microsoft.com/v1.0'

# Graph rejects a sendMail request whose whole JSON body exceeds ~4 MB, and
# base64 inflates an attachment by a third. Anything larger needs an upload
# session against a draft, which the quote PDFs have never come close to.
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024


class GraphError(Exception):
    """Raised when Graph or the token endpoint answers with an error."""


def _setting(name, default=''):
    return getattr(settings, name, default)


def _post_json(url, payload, token, timeout):
    """POST *payload* as JSON. Returns the decoded body, or None for 202/204."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


def graph_get(path, token, timeout=20):
    """GET a Graph resource. Returns the decoded body.

    Raises ``GraphError`` carrying Graph's own message, which is usually the
    most specific account of what went wrong that anyone will get.
    """
    request = urllib.request.Request(
        f'{GRAPH_HOST}/{path.lstrip("/")}',
        headers={'Authorization': f'Bearer {token}'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:500]
        raise GraphError(f'Graph GET {path} failed ({exc.code}): {detail}') from exc
    except OSError as exc:
        raise GraphError(f'Could not reach Microsoft Graph: {exc}') from exc


class TokenCache:
    """Caches one app-only access token until shortly before it expires.

    Graph tokens last an hour; fetching a new one for every message would add a
    round trip to each send and hit the token endpoint's throttling limits.
    """

    # Refresh this many seconds early so a token cannot expire mid-request.
    SKEW = 120

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0

    def clear(self):
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get(self, *, tenant_id, client_id, client_secret, timeout=20):
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            token, expires_in = self._fetch(tenant_id, client_id, client_secret, timeout)
            self._token = token
            self._expires_at = time.time() + max(expires_in - self.SKEW, 0)
            return token

    def _fetch(self, tenant_id, client_id, client_secret, timeout):
        url = f'{LOGIN_HOST}/{urllib.parse.quote(tenant_id)}/oauth2/v2.0/token'
        data = urllib.parse.urlencode(
            {
                'client_id': client_id,
                'client_secret': client_secret,
                # ``.default`` asks for whatever application permissions the
                # app registration was granted consent for — Mail.Send here.
                'scope': 'https://graph.microsoft.com/.default',
                'grant_type': 'client_credentials',
            }
        ).encode('utf-8')
        request = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:500]
            raise GraphError(
                f'Microsoft rejected the client credentials ({exc.code}): {detail}'
            ) from exc
        except OSError as exc:
            raise GraphError(f'Could not reach the Microsoft token endpoint: {exc}') from exc
        token = payload.get('access_token')
        if not token:
            raise GraphError('The token response contained no access_token.')
        return token, int(payload.get('expires_in', 3600))


# Module level so every backend instance — Django builds a fresh one per send —
# shares the same token.
_token_cache = TokenCache()


def address_only(value):
    """``InkPro <sales@inkpro.ws>`` -> ``sales@inkpro.ws``."""
    return parseaddr(value or '')[1]


def _recipients(addresses):
    return [
        {'emailAddress': {'address': address_only(address)}}
        for address in addresses or []
        if address_only(address)
    ]


def build_graph_message(message, *, save_to_sent_items=True):
    """Convert a Django ``EmailMessage`` into a Graph ``sendMail`` payload."""
    body_content = message.body or ''
    body_type = 'Text'
    if getattr(message, 'content_subtype', 'plain') == 'html':
        body_type = 'HTML'
    for content, mimetype in getattr(message, 'alternatives', None) or []:
        # Django's EmailMultiAlternatives keeps the HTML part alongside the
        # plain text one; Graph carries a single body, so the HTML wins.
        if mimetype == 'text/html':
            body_content, body_type = content, 'HTML'
            break

    payload = {
        'message': {
            'subject': message.subject or '',
            'body': {'contentType': body_type, 'content': body_content},
            'toRecipients': _recipients(message.to),
        },
        'saveToSentItems': bool(save_to_sent_items),
    }
    graph_message = payload['message']
    if message.cc:
        graph_message['ccRecipients'] = _recipients(message.cc)
    if message.bcc:
        graph_message['bccRecipients'] = _recipients(message.bcc)
    if message.reply_to:
        graph_message['replyTo'] = _recipients(message.reply_to)

    attachments = []
    for attachment in message.attachments:
        prepared = _attachment_payload(attachment)
        if prepared:
            attachments.append(prepared)
    if attachments:
        graph_message['attachments'] = attachments
    return payload


def _attachment_payload(attachment):
    """One Django attachment as a Graph fileAttachment, or None to skip it."""
    if hasattr(attachment, 'get_filename'):  # a MIMEBase instance
        filename = attachment.get_filename() or 'attachment'
        content = attachment.get_payload(decode=True) or b''
        mimetype = attachment.get_content_type()
    else:
        filename, content, mimetype = attachment
    if isinstance(content, str):
        content = content.encode('utf-8')
    if len(content) > MAX_ATTACHMENT_BYTES:
        logger.error(
            'Dropping attachment %s (%d bytes): over the %d byte limit for a '
            'Graph sendMail request.',
            filename, len(content), MAX_ATTACHMENT_BYTES,
        )
        return None
    return {
        '@odata.type': '#microsoft.graph.fileAttachment',
        'name': filename,
        'contentType': mimetype or 'application/octet-stream',
        'contentBytes': base64.b64encode(content).decode('ascii'),
    }


class GraphEmailBackend(BaseEmailBackend):
    """Send Django email through Microsoft Graph with an app-only token."""

    def __init__(self, *, fail_silently=False, tenant_id=None, client_id=None,
                 client_secret=None, sender=None, timeout=None, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.tenant_id = tenant_id or _setting('MS_GRAPH_TENANT_ID')
        self.client_id = client_id or _setting('MS_GRAPH_CLIENT_ID')
        self.client_secret = client_secret or _setting('MS_GRAPH_CLIENT_SECRET')
        self.sender = sender or _setting('MS_GRAPH_SENDER')
        self.timeout = timeout or _setting('MS_GRAPH_TIMEOUT', 20)
        self.save_to_sent_items = bool(_setting('MS_GRAPH_SAVE_TO_SENT_ITEMS', True))

    # -- credentials ---------------------------------------------------------
    def missing_settings(self):
        """Names of the credentials that are still blank."""
        return [
            name
            for name, value in (
                ('MS_GRAPH_TENANT_ID', self.tenant_id),
                ('MS_GRAPH_CLIENT_ID', self.client_id),
                ('MS_GRAPH_CLIENT_SECRET', self.client_secret),
            )
            if not value
        ]

    def get_token(self, *, force_refresh=False):
        missing = self.missing_settings()
        if missing:
            raise GraphError(
                'Microsoft Graph email is not configured yet — set '
                + ', '.join(missing)
                + ' in the environment.'
            )
        if force_refresh:
            _token_cache.clear()
        return _token_cache.get(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
            timeout=self.timeout,
        )

    def mailbox_for(self, message):
        """Which mailbox Graph should send from.

        ``MS_GRAPH_SENDER`` wins when set, because the app registration is
        usually consented for exactly that mailbox; otherwise fall back to the
        message's own From address.
        """
        return address_only(self.sender) or address_only(
            message.from_email or _setting('DEFAULT_FROM_EMAIL')
        )

    # -- sending -------------------------------------------------------------
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            try:
                if self._send(message):
                    sent += 1
            except Exception:
                logger.exception('Microsoft Graph refused to send "%s".', message.subject)
                if not self.fail_silently:
                    raise
        return sent

    def _send(self, message):
        if not message.recipients():
            return False
        mailbox = self.mailbox_for(message)
        if not mailbox:
            raise GraphError(
                'No sending mailbox: set MS_GRAPH_SENDER or DEFAULT_FROM_EMAIL.'
            )
        payload = build_graph_message(
            message, save_to_sent_items=self.save_to_sent_items
        )
        url = f'{GRAPH_HOST}/users/{urllib.parse.quote(mailbox)}/sendMail'
        self._post(url, payload)
        return True

    def _post(self, url, payload, *, retried=False):
        token = self.get_token(force_refresh=retried)
        try:
            return _post_json(url, payload, token, self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:500]
            # 401 usually means the cached token was revoked early: drop it and
            # try once with a fresh one before giving up.
            if exc.code == 401 and not retried:
                logger.info('Graph returned 401; refreshing the access token.')
                return self._post(url, payload, retried=True)
            raise GraphError(f'Graph sendMail failed ({exc.code}): {detail}') from exc
        except OSError as exc:
            raise GraphError(f'Could not reach Microsoft Graph: {exc}') from exc
