"""Environment-driven configuration. No credentials live in source files."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Databases this application must never touch (highest-priority requirement).
PROTECTED_DATABASES = frozenset({"impaco_test_dev", "impac_test_dev"})
REQUIRED_DB_NAME = "Routematic"
# Production copy of Routematic on the Hostinger server (migrated 2026-10-15).
APP_DATABASES = frozenset({REQUIRED_DB_NAME, "routematic_db"})

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = ""
    db_name: str = REQUIRED_DB_NAME
    db_admin_db: str = "postgres"
    jwt_secret: str = ""
    # access-token lifetime (minutes). ACCESS_TOKEN_MINUTES wins; the older JWT_EXPIRE_MINUTES is still
    # honoured when it is the only one set; neither set = 30 minutes. The app renews silently with its
    # refresh token (REFRESH_TOKEN_DAYS), so a short lifetime does not sign people out.
    access_token_minutes: int | None = None
    jwt_expire_minutes: int | None = None
    refresh_token_days: int = 7
    cors_origins: str = ""
    # development convenience: any http://localhost:<port> may call the API. Off by default; a dev .env
    # may set CORS_ALLOW_LOCALHOST=true. Production lists its real web origin(s) in CORS_ORIGINS.
    cors_allow_localhost: bool = False
    # sign-in brute-force protection: failed attempts per (client, email) within the window
    login_max_failures: int = 10
    login_window_seconds: int = 900
    # ... per client across all emails (password spraying) and per email across all clients
    login_ip_max_failures: int = 100
    login_account_max_failures: int = 30
    # PBKDF2-SHA256 work factor for new / re-hashed passwords (OWASP 2023+: 600,000)
    password_hash_iterations: int = 600_000
    # JWT issuer / audience written into and required on every token
    jwt_issuer: str = "routematic-api"
    jwt_audience: str = "routematic-app"
    # Swagger UI / ReDoc / openapi.json (off in production)
    enable_docs: bool = True

    # Strict-Transport-Security header: enable only when the API is served over HTTPS
    hsts_enabled: bool = False
    # largest accepted request body (bytes); larger requests get 413
    max_body_bytes: int = 20 * 1024 * 1024
    # comma separated Host header allow-list (empty = any host)
    allowed_hosts: str = ""
    # PostgreSQL TLS: disable | prefer | require | verify-ca | verify-full. Empty = "require" for a remote
    # DB_HOST, "prefer" for localhost. DB_SSLROOTCERT = CA file for verify-ca / verify-full.
    db_sslmode: str = ""
    db_sslrootcert: str = ""
    # Least-privilege layout (scripts/db_least_privilege.sql): the NOLOGIN role that owns the schemas.
    # When set, schema-changing work (company provisioning, migrations) runs as that role via
    # SET LOCAL ROLE; routine requests keep the runtime login's DML-only rights. Empty = off.
    db_owner_role: str = ""
    google_maps_api_key: str = ""
    # time zone of the companies' business day (dates such as "trips today" in server aggregates)
    business_timezone: str = "Asia/Kolkata"
    # Automated tests run against a throw-away database with this prefix,
    # never against Routematic or impaco_test_dev.
    allow_test_db_prefix: str = "routematic_test_"

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def access_minutes(self) -> int:
        return self.access_token_minutes or self.jwt_expire_minutes or 30

    @property
    def hosts(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def effective_sslmode(self) -> str:
        if self.db_sslmode:
            return self.db_sslmode
        return "prefer" if self.db_host.strip().lower() in LOCAL_HOSTS else "require"


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})
# Example / placeholder values that must never be used as the signing secret.
PLACEHOLDER_SECRETS = frozenset({
    "change-me-to-a-long-random-string", "change-me", "changeme", "secret", "jwt-secret", "your-secret",
})
SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})


def validate(s: "Settings") -> None:
    """Fail at start-up with a clear message instead of on the first request."""
    problems = []
    if len(s.jwt_secret) < 32:
        problems.append("JWT_SECRET is missing or shorter than 32 characters "
                        "(generate one: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
    elif s.jwt_secret.strip().lower() in PLACEHOLDER_SECRETS:
        problems.append("JWT_SECRET is still the example placeholder; generate a random value")
    if s.db_sslmode and s.db_sslmode not in SSL_MODES:
        problems.append(f"DB_SSLMODE must be one of {sorted(SSL_MODES)}")
    if not 1 <= s.access_minutes <= 1440:
        problems.append("ACCESS_TOKEN_MINUTES must be between 1 and 1440")
    if not 1 <= s.refresh_token_days <= 90:
        problems.append("REFRESH_TOKEN_DAYS must be between 1 and 90")
    if s.max_body_bytes < 1024:
        problems.append("MAX_BODY_BYTES is too small")
    if s.password_hash_iterations < 100_000:
        problems.append("PASSWORD_HASH_ITERATIONS must be at least 100000")
    if not s.db_name:
        problems.append("DB_NAME is empty")
    if s.db_name.lower() in PROTECTED_DATABASES:
        problems.append(f"DB_NAME '{s.db_name}' is a protected database")
    if s.login_max_failures < 1 or s.login_window_seconds < 1:
        problems.append("LOGIN_MAX_FAILURES and LOGIN_WINDOW_SECONDS must be positive")
    if problems:
        raise RuntimeError("Invalid configuration (backend/.env): " + "; ".join(problems) + ".")


@lru_cache
def get_settings() -> Settings:
    return Settings()
