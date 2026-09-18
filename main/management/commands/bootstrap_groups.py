"""Create the Customer / Staff / Owner groups and attach their permissions."""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from main.permissions import GROUP_PERMISSIONS, OWNER_GROUP


class Command(BaseCommand):
    help = 'Create the InkPro role groups and sync their permissions.'

    def handle(self, *args, **options):
        for name, codenames in GROUP_PERMISSIONS.items():
            group, created = Group.objects.get_or_create(name=name)
            if codenames == '__all__':
                perms = Permission.objects.filter(
                    content_type__app_label__in=['main', 'auth']
                )
            else:
                perms = Permission.objects.filter(
                    codename__in=codenames, content_type__app_label='main'
                )
            group.permissions.set(perms)
            self.stdout.write(
                f'  {"Created" if created else "Updated"} {name} ({perms.count()} permissions)'
            )
        self.stdout.write(
            self.style.SUCCESS(
                f'Role groups ready. Add owners to "{OWNER_GROUP}" and tick is_staff '
                'so they can reach the staff portal.'
            )
        )
