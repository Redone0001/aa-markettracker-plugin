from django.db import migrations


def purge_discord_webhook_logs(apps, schema_editor):
    """Remove rows created by versions that persisted complete webhook URLs."""
    task_log = apps.get_model("markettracker", "MTTaskLog")
    task_log.objects.filter(source="discord").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("markettracker", "0015_discordmessage_contract_alert_enabled_and_more"),
    ]

    operations = [
        migrations.RunPython(purge_discord_webhook_logs, migrations.RunPython.noop),
    ]
