"""Decide where uploaded media lives, so a restart cannot erase it.

On Azure App Service only ``/home`` is a real, persistent volume. Everything
else the app can see is container-local scratch space that is thrown away when
the container recycles — which App Service does on its own schedule, often
several times a day, not only when you deploy. Two paths look deceptively
permanent and are not:

* ``/home/site/wwwroot`` is persistent but is replaced wholesale on deploy.
* The directory the app actually runs from. An Oryx build extracts the app to
  ``/tmp/8d…`` at boot, and a container image runs it from ``/app``. Both are
  inside the container, so ``BASE_DIR / 'media'`` there survives exactly until
  the next restart.

So on App Service the default is ``/home/site/media`` — persistent, and outside
the tree a deployment replaces — no matter where ``BASE_DIR`` happens to be.
An explicit ``MEDIA_ROOT`` always wins; a mounted Azure file share or a
blob-backed storage backend is better still.
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


def is_persistent(path):
    """True when *path* survives a container restart and a deployment.

    Only ``/home`` is mounted from durable storage, and the deployed tree
    inside it is replaced on every push, so neither end of that is safe.
    """
    candidate = Path(path)
    for probe in (candidate, candidate.resolve()):
        try:
            probe.relative_to(AZURE_PERSISTENT_ROOT.resolve())
        except ValueError:
            try:
                probe.relative_to(AZURE_PERSISTENT_ROOT)
            except ValueError:
                return False
        return not inside_deploy_tree(probe)
    return False


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
    where nothing outside ``/home`` outlives a container restart.
    """
    import os

    environ = os.environ if environ is None else environ
    if configured:
        return Path(configured)
    if on_azure_app_service(environ) and not is_persistent(base_dir):
        return AZURE_MEDIA_ROOT
    return Path(base_dir) / 'media'
