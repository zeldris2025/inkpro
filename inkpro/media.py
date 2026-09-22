"""Decide where uploaded media lives, so a deployment cannot erase it.

Azure App Service replaces the whole deployed tree — ``/home/site/wwwroot`` —
on every deployment, and only ``/home`` survives a restart. Django's natural
default, ``BASE_DIR / 'media'``, therefore sits exactly where the next push
will wipe it: category photos, hero images and customer artwork disappear while
the PostgreSQL rows that point at them survive, leaving a site full of broken
images and no way to tell what was lost.

That failure is silent and irreversible, so the safe location is *detected*
rather than left to a ``MEDIA_ROOT`` application setting somebody has to
remember. An explicit ``MEDIA_ROOT`` always wins — a mounted Azure file share
or a blob-backed path is better still.
"""

from pathlib import Path

#: The persistent volume on an App Service Linux container.
AZURE_PERSISTENT_ROOT = Path('/home/site')
#: The deployed tree inside it, which a deployment replaces wholesale.
AZURE_DEPLOY_ROOT = AZURE_PERSISTENT_ROOT / 'wwwroot'
#: Where uploads belong instead: persistent, and outside the deployed tree.
AZURE_MEDIA_ROOT = AZURE_PERSISTENT_ROOT / 'media'


def on_azure_app_service(environ):
    """True when this process is running under Azure App Service.

    App Service always sets ``WEBSITE_SITE_NAME``; nothing else does.
    """
    return bool(environ.get('WEBSITE_SITE_NAME'))


def inside_deploy_tree(path):
    """True when *path* would be destroyed by the next deployment.

    Both the literal path and its resolved form are compared, because a
    development machine may map ``/home`` somewhere else — macOS resolves it
    through autofs — and the answer must not depend on where the check runs.
    """
    candidate = Path(path)
    for probe, root in (
        (candidate, AZURE_DEPLOY_ROOT),
        (candidate.resolve(), AZURE_DEPLOY_ROOT.resolve()),
    ):
        try:
            probe.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def resolve_media_root(base_dir, configured='', environ=None):
    """Return the directory uploads should be written to.

    An explicit *configured* path is honoured as given. Otherwise the default
    is ``base_dir/media`` locally, but ``/home/site/media`` on App Service,
    where ``base_dir`` is inside the tree a deployment replaces.
    """
    import os

    environ = os.environ if environ is None else environ
    if configured:
        return Path(configured)
    if on_azure_app_service(environ) and inside_deploy_tree(base_dir):
        return AZURE_MEDIA_ROOT
    return Path(base_dir) / 'media'
