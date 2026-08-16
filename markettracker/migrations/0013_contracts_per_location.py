from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    TrackedContract = apps.get_model("markettracker", "TrackedContract")
    TrackedLocation = apps.get_model("markettracker", "TrackedLocation")
    TrackedContractLocation = apps.get_model("markettracker", "TrackedContractLocation")

    default_loc = TrackedLocation.objects.filter(is_default=True).first()
    if not default_loc:
        default_loc = TrackedLocation.objects.filter(is_active=True).first()

    if not default_loc:
        return

    for tc in TrackedContract.objects.all().iterator():
        TrackedContractLocation.objects.get_or_create(
            tracked_contract_id=tc.id,
            location_id=default_loc.id,
            defaults={
                "desired_quantity": int(getattr(tc, "desired_quantity", 0) or 0),
                "last_status": getattr(tc, "last_status", "OK") or "OK",
                "is_active": bool(getattr(tc, "is_active", True)),
            },
        )


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("markettracker", "0012_items_move_to_location_fk"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackedContractLocation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("desired_quantity", models.PositiveIntegerField(default=0)),
                ("last_status", models.CharField(default="OK", max_length=10)),
                ("is_active", models.BooleanField(default=True)),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tracked_contracts", to="markettracker.trackedlocation")),
                ("tracked_contract", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="by_location", to="markettracker.trackedcontract")),
            ],
            options={
                "default_permissions": (),
            },
        ),
        migrations.AlterUniqueTogether(
            name="trackedcontractlocation",
            unique_together={("tracked_contract", "location")},
        ),
        migrations.RunPython(forwards, backwards),
    ]
