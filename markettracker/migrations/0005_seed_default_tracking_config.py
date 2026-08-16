from django.db import migrations

FORGE_REGION_ID = 10000002  # "The Forge" in EVE

def create_default_tracking_config(apps, schema_editor):
    Config = apps.get_model("markettracker", "MarketTrackingConfig")
    # If something already exists, do nothing
    if Config.objects.exists():
        return

    # Create the default configuration
    try:
        Config.objects.create(
            scope="region",
            location_id=FORGE_REGION_ID,
            yellow_threshold=50,
            red_threshold=20,
        )
    except Exception:
              pass


def remove_default_tracking_config(apps, schema_editor):
    Config = apps.get_model("markettracker", "MarketTrackingConfig")
    for obj in Config.objects.all():
        if (
            getattr(obj, "scope", None) == "region"
            and getattr(obj, "location_id", None) == FORGE_REGION_ID
            and getattr(obj, "yellow_threshold", None) == 50
            and getattr(obj, "red_threshold", None) == 20
        ):
            try:
                obj.delete()
            except Exception:
                pass

class Migration(migrations.Migration):

    dependencies = [
        ("markettracker", "0004_trackedcontract_last_status"), 
    ]

    operations = [
        migrations.RunPython(create_default_tracking_config, remove_default_tracking_config),
    ]
