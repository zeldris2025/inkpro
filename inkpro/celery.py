"""Celery application.

Only used when ``CELERY_BROKER_URL`` is set; otherwise ``main.tasks`` runs work
inline. Start a worker with::

    celery -A inkpro worker -l info
    celery -A inkpro beat -l info      # for the payment reminder schedule
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'inkpro.settings')

app = Celery('inkpro')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'send-payment-reminders': {
        'task': 'main.send_payment_reminders_task',
        # Weekday mornings, so reminders land during business hours.
        'schedule': crontab(hour=9, minute=0, day_of_week='mon-fri'),
    },
}
