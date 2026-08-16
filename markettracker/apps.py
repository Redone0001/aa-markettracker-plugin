import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

class MarkettrackerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'markettracker'
    label = 'markettracker'
    verbose_name = "Market Tracker"

    def ready(self):
        return


