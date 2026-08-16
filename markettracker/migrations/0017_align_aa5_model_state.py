# Generated for Alliance Auth 5 / Django 5.2 compatibility.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("eveuniverse", "0012_alter_evebloodline_eve_ship_type"),
        ("markettracker", "0016_purge_discord_webhook_logs"),
    ]

    operations = [
        # Migrations 0012 and 0014 already create this constraint in the
        # database. Correct the migration state without trying to recreate it.
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterUniqueTogether(
                    name="trackeditem",
                    unique_together={("item", "location")},
                ),
            ],
        ),
        migrations.AlterField(
            model_name="trackedcontractlocation",
            name="id",
            field=models.BigAutoField(
                auto_created=True,
                primary_key=True,
                serialize=False,
                verbose_name="ID",
            ),
        ),
        migrations.AlterField(
            model_name="trackedlocation",
            name="location_id",
            field=models.BigIntegerField(),
        ),
        migrations.RemoveField(
            model_name="trackeditem",
            name="structure",
        ),
    ]
