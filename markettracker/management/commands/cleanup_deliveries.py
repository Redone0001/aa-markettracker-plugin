from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from markettracker.models import Delivery


class Command(BaseCommand):
    help = "Delete fulfilled deliveries older than configured retention days"

    def handle(self, *args, **options):
        retention_days = getattr(settings, "DELIVERIES_RETENTION_DAYS", 30)
        cutoff_date = timezone.now() - timedelta(days=retention_days)
        deleted_count, _ = Delivery.objects.filter(created_at__lt=cutoff_date).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} fulfilled deliveries older than {retention_days} days."
            )
        )
