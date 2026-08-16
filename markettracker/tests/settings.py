"""Minimal Alliance Auth 5 settings for MarketTracker's test suite."""

import allianceauth.utils.cache as aa_cache
from allianceauth.project_template.project_name.settings.base import *  # noqa: F403
from fakeredis import FakeRedis

INSTALLED_APPS = ["modeltranslation"] + INSTALLED_APPS + [  # noqa: F405
    "allianceauth.services.modules.discord",
    "eve_sde",
    "fittings",
    "eveuniverse",
    "markettracker",
]

ROOT_URLCONF = "markettracker.tests.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "markettracker-tests",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LOGGING = None

SITE_URL = "https://example.test"
CSRF_TRUSTED_ORIGINS = [SITE_URL]
ESI_SSO_CLIENT_ID = "test-client"
ESI_SSO_CLIENT_SECRET = "test-secret"
ESI_SSO_CALLBACK_URL = f"{SITE_URL}/sso/callback"
ESI_USER_CONTACT_EMAIL = "tests@example.test"

DISCORD_GUILD_ID = "123456789"
DISCORD_BOT_TOKEN = "test-token"
DISCORD_INVITE_CODE = "test-invite"
DISCORD_APP_ID = "test-app"
DISCORD_APP_SECRET = "test-secret"
DISCORD_CALLBACK_URL = f"{SITE_URL}/discord/callback"

CELERY_TASK_ALWAYS_EAGER = True
ALLIANCEAUTH_DASHBOARD_TASK_STATISTICS_DISABLED = True

# AA's Discord client needs Redis for production rate limiting. Unit tests do
# not make Discord API calls, so provide an in-process stand-in during app setup.
class TestRedis(FakeRedis):
    def info(self, section=None, *args, **kwargs):
        return {"redis_version": "7.4.0"}


_redis_stub = TestRedis()
aa_cache.get_redis_client = lambda: _redis_stub
