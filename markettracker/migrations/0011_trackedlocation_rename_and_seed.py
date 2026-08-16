from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    MarketTrackingConfig = apps.get_model("markettracker", "MarketTrackingConfig")
    TrackedLocation = apps.get_model("markettracker", "TrackedLocation")

    cfg = MarketTrackingConfig.objects.first()

    if cfg:
        scope = (cfg.scope or "structure").lower()
        loc_id = int(cfg.location_id)

        if scope == "region":
            name = f"Default Region {loc_id}"
        else:
            name = f"Default Structure {loc_id}"

        obj, _ = TrackedLocation.objects.get_or_create(
            scope=scope,
            location_id=loc_id,
            defaults={"name": name, "is_default": True, "is_active": True},
        )
        if not obj.is_default:
            obj.is_default = True
            obj.save(update_fields=["is_default"])


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("markettracker", "0010_discordmessage_contract_restocked_alert_header_and_more"),
    ]

    operations = [
        # Rename model TrackedStructure -> TrackedLocation
        migrations.RenameModel(
            old_name="TrackedStructure",
            new_name="TrackedLocation",
        ),

        # Rename field: structure_id -> location_id
        migrations.RenameField(
            model_name="trackedlocation",
            old_name="structure_id",
            new_name="location_id",
        ),

        # Add scope + flags
        migrations.AddField(
            model_name="trackedlocation",
            name="scope",
            field=models.CharField(
                choices=[("region", "Region"), ("structure", "Structure")],
                default="structure",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="trackedlocation",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="trackedlocation",
            name="is_active",
            field=models.BooleanField(default=True),
        ),

        # Add uniqueness across scope+location_id
        migrations.AlterUniqueTogether(
            name="trackedlocation",
            unique_together={("scope", "location_id")},
        ),

        migrations.RunPython(forwards, backwards),
    ]
