import os
from pathlib import Path


# ============================================================
# BASE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# SECURITY
# ============================================================

SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "dev-only-change-this-secret"
)

DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "ALLOWED_HOSTS",
        "127.0.0.1,localhost,testserver,.vercel.app"
    ).split(",")
    if host.strip()
]


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "clubbi",
]


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "clubbi.middleware.CurrentAccountMiddleware",
]


# ============================================================
# URLS / WSGI
# ============================================================

ROOT_URLCONF = "clubbi.urls"

WSGI_APPLICATION = "clubbi.wsgi.application"


# ============================================================
# TEMPLATES
# ============================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "APP_DIRS": False,
        "OPTIONS": {
            "environment": "clubbi.jinja2.environment",
            "context_processors": [
                "django.template.context_processors.request",
                "clubbi.context_processors.accounts",
            ],
        },
    }
]


# ============================================================
# DATABASE
# ============================================================
#
# For beta testing, SQLite is okay.
# Later, move to PostgreSQL for persistent production data.
#

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "instance" / "clubbi.sqlite3",
    }
}


# ============================================================
# INTERNATIONALIZATION
# ============================================================

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# ============================================================
# STATIC FILES
# ============================================================

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]


# ============================================================
# DEFAULT MODEL FIELD
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"


# ============================================================
# SESSIONS
# ============================================================

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

SESSION_COOKIE_HTTPONLY = True

SESSION_COOKIE_SAMESITE = "Lax"

SESSION_COOKIE_SECURE = (
    os.environ.get("COOKIE_SECURE", "0") == "1"
)


# ============================================================
# CSRF
# ============================================================

CSRF_COOKIE_SECURE = (
    os.environ.get("COOKIE_SECURE", "0") == "1"
)


# ============================================================
# SECURITY SETTINGS
# ============================================================

SECURE_SSL_REDIRECT = (
    os.environ.get("SECURE_SSL_REDIRECT", "0") == "1"
)

SECURE_HSTS_SECONDS = int(
    os.environ.get("SECURE_HSTS_SECONDS", "0")
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = (
    os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
)

SECURE_HSTS_PRELOAD = (
    os.environ.get("SECURE_HSTS_PRELOAD", "0") == "1"
)

SECURE_CONTENT_TYPE_NOSNIFF = True


# ============================================================
# MESSAGES
# ============================================================

MESSAGE_TAGS = {
    "error": "danger",
}
