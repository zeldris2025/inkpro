"""Print a fresh SECRET_KEY suitable for production."""

from django.core.management.base import BaseCommand
from django.core.management.utils import get_random_secret_key


class Command(BaseCommand):
    help = 'Generate a random SECRET_KEY for a production environment.'

    def handle(self, *args, **options):
        self.stdout.write(get_random_secret_key())
