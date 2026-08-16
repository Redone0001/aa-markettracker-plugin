from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from markettracker.models import ContractDelivery, Delivery


class Command(BaseCommand):
    help = "Delete fulfilled deliveries older than configured retention days"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the rows that would be deleted without changing the database",
        )

    def handle(self, *args, **options):
        try:
            retention_days = int(getattr(settings, "DELIVERIES_RETENTION_DAYS", 30))
        except (TypeError, ValueError) as exc:
            raise CommandError("DELIVERIES_RETENTION_DAYS must be an integer") from exc
        if retention_days < 1:
            raise CommandError("DELIVERIES_RETENTION_DAYS must be at least 1")
        cutoff_date = timezone.now() - timedelta(days=retention_days)

        delivery_rows = Delivery.objects.filter(
            status="FINISHED",
            created_at__lt=cutoff_date,
        )
        contract_delivery_rows = ContractDelivery.objects.filter(
            status="FINISHED",
            created_at__lt=cutoff_date,
        )
        matched_count = delivery_rows.count() + contract_delivery_rows.count()

        if options["dry_run"]:
            self.stdout.write(
                f"Would delete {matched_count} finished deliveries older than "
                f"{retention_days} days."
            )
            return

        item_count, _ = delivery_rows.delete()
        contract_count, _ = contract_delivery_rows.delete()
        deleted_count = item_count + contract_count
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} finished deliveries older than {retention_days} days."
            )
        )
