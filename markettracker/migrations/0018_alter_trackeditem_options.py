from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("markettracker", "0017_align_aa5_model_state"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="trackeditem",
            options={
                "default_permissions": (),
                "permissions": (
                    (
                        "can_move_tracked_items",
                        "Can bulk move tracked items between locations",
                    ),
                ),
            },
        ),
    ]
