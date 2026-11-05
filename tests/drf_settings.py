"""Django settings for the tests that need Django REST Framework.

The drf tox environment installs Django, Django REST Framework, and
django-filter. Every test module that uses them calls ``configure()``, which
configures Django once per process.
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def configure() -> None:
    """Configure and set up Django, unless that was already done."""
    import django
    from django.conf import settings

    if settings.configured:
        return

    # A file database, because the live server answers requests in another
    # thread, which an in-memory database would not share.
    database = Path(tempfile.mkdtemp(prefix="eazyrest-drf-")) / "db.sqlite3"
    settings.configure(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(database),
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "rest_framework",
            "rest_framework.authtoken",
            "django_filters",
        ],
        REST_FRAMEWORK={
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer"
            ],
            "UNAUTHENTICATED_USER": None,
        },
        ALLOWED_HOSTS=["testserver", "127.0.0.1"],
        # Session authentication, which the CSRF recipe needs, reads the user
        # that these set from the session cookie.
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ],
        ROOT_URLCONF="test_drf_client",
        SECRET_KEY="dummy-secret-for-tests",
        USE_TZ=True,
    )
    django.setup()

    from django.core.management import call_command

    call_command("migrate", verbosity=0, run_syncdb=True)
