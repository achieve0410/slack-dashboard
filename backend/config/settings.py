import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file.

    Never overrides a variable that is already set in the real environment
    (os.environ.setdefault), so `FOO=bar ./manage.py ...` still wins over
    whatever is in .env.
    """
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", True)

_secret_key = os.getenv("DJANGO_SECRET_KEY", "")
if not _secret_key:
    if DEBUG:
        _secret_key = "insecure-dev-key-do-not-use-in-production"
    else:
        raise RuntimeError(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is not enabled. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )
SECRET_KEY = _secret_key

DASHBOARD_AUTH_REQUIRED = env_bool("DASHBOARD_AUTH_REQUIRED", not DEBUG)
DASHBOARD_INTERNAL_API_TOKEN = os.getenv("DASHBOARD_INTERNAL_API_TOKEN", "")
DASHBOARD_OWNER_USERNAME = os.getenv("DASHBOARD_OWNER_USERNAME", "owner")
ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1",
)
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://127.0.0.1:3000",
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "dashboard.middleware.DashboardAccessMiddleware",
    "dashboard.middleware.ConsumptionStateReadOnlyMiddleware",
    "dashboard.middleware.DashboardCsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DB_ENGINE = os.getenv("DJANGO_DB_ENGINE", "sqlite").lower()
if DB_ENGINE == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ["DJANGO_DB_NAME"],
            "USER": os.environ["DJANGO_DB_USER"],
            "PASSWORD": os.environ["DJANGO_DB_PASSWORD"],
            "HOST": os.getenv("DJANGO_DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DJANGO_DB_PORT", "13307"),
            "CONN_MAX_AGE": int(os.getenv("DJANGO_DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(
                os.getenv("DJANGO_SQLITE_PATH", PROJECT_ROOT / "db" / "dashboard.sqlite3")
            ),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Seoul")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "frontend_static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "0"))
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

CONSUMPTION_STATE_READ_ONLY = env_bool("CONSUMPTION_STATE_READ_ONLY", False)
DASHBOARD_PLATFORM_ARTIFACT_ROOT = Path(
    os.path.expanduser(
        os.getenv(
            "DASHBOARD_PLATFORM_ARTIFACT_ROOT",
            PROJECT_ROOT / "db" / "platform-artifacts",
        )
    )
).resolve()
DASHBOARD_PLATFORM_TOKEN_ROOT = Path(
    os.path.expanduser(
        os.getenv(
            "DASHBOARD_PLATFORM_TOKEN_ROOT",
            PROJECT_ROOT / "db" / "platform-tokens",
        )
    )
).resolve()
DASHBOARD_PLATFORM_MAX_ARTIFACT_BYTES = int(
    os.getenv("DASHBOARD_PLATFORM_MAX_ARTIFACT_BYTES", "2000000")
)
