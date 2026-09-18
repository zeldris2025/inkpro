"""InkPro project package."""

# Import the Celery app eagerly so @shared_task-style discovery works when a
# broker is configured. A missing/ misconfigured Celery must not stop Django
# from booting, since the app is designed to run without it.
try:  # pragma: no cover
    from .celery import app as celery_app  # noqa: F401

    __all__ = ('celery_app',)
except Exception:  # pragma: no cover
    __all__ = ()
