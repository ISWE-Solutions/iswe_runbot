"""All settings come from environment variables (loaded from .env by Compose)."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


WEAK_SECRETS = {"dev-secret-change-me", "changeme", "change-me", "secret"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Dashboard host; previews live at <label>.<domain>
    domain: str = "preview.localhost"
    tls_enabled: bool = False
    acme_email: str = ""
    dns_provider: str = ""
    dns_api_token: str = ""

    # Signs sessions and preview passes; required (see check_secrets)
    secret_key: str = ""
    # Fernet key (urlsafe base64, 32 bytes) for GitHub tokens
    encryption_key: str = ""

    # App database (superuser of the shared Postgres server)
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "previewer"
    postgres_password: str = "previewer"
    postgres_db: str = "previewer"
    # Old shared role all previews used to connect as. Each build now has its own role
    # (worker/pg.py); this one is only locked once no database depends on it any more.
    odoo_db_user: str = "odoo"
    odoo_db_password: str = ""
    # Odoo's connection pool per preview (Odoo's default of 64 would exhaust max_connections)
    odoo_db_maxconn: int = 12

    # Host folder holding repos/, builds/, filestore/, logs/. Mounted at the
    # same path inside app and worker so preview bind mounts resolve on the host.
    data_dir: Path = Path("/srv/previewer")
    # Control network: Traefik, socket-proxy, Postgres, app, worker
    docker_network: str = "previewer_net"
    # Previews and build containers only: Traefik, Postgres and the worker join it too, but the
    # Docker socket proxy and the app do not, so branch code cannot read their secrets.
    preview_network: str = "previewer_previews"
    docker_host: str = "unix:///var/run/docker.sock"

    # Fallback Enterprise repo when an Odoo version has none set (Odoo versions page)
    enterprise_repo_url: str = ""

    # Seeds the "Default" Git credential on first start; managed on the Git credentials page after
    github_username: str = Field("", validation_alias=AliasChoices("github_username", "github_user"))
    github_token: str = ""

    # Defaults for the `settings` table (the table wins once seeded)
    max_concurrent_builds: int = 3
    max_running_previews: int = 25
    idle_sleep_hours: int = 3
    preview_memory_limit: str = "1536m"
    preview_cpus: float = 1.0
    build_memory_limit: str = "2g"
    build_timeout_minutes: int = 20
    health_timeout_seconds: int = 120
    # Memory kept free for Postgres, Traefik, app and worker when fitting previews into the host
    reserved_memory: str = "2g"

    admin_email: str = ""
    test_database: str = "previewer_test"

    def check_secrets(self) -> None:
        """Refuse to start with a missing or weak SECRET_KEY: anyone who knows it can forge
        dashboard sessions and preview passes."""
        key = self.secret_key.strip()
        if not key:
            raise RuntimeError("SECRET_KEY is not set (see .env.example)")
        if len(key) < 32 or key in WEAK_SECRETS:
            raise RuntimeError("SECRET_KEY is too weak: use at least 32 random characters, e.g. "
                               "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")

    @property
    def database_url(self) -> str:
        return self._url(self.postgres_db)

    @property
    def test_database_url(self) -> str:
        return self._url(self.test_database)

    def _url(self, db: str) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{db}"
        )

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"

    @property
    def dashboard_url(self) -> str:
        return f"{self.scheme}://{self.domain}"

    def preview_url(self, host: str) -> str:
        return f"{self.scheme}://{host}"

    # Storage layout
    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def builds_dir(self) -> Path:
        return self.data_dir / "builds"

    @property
    def filestore_dir(self) -> Path:
        return self.data_dir / "filestore"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def checkouts_dir(self) -> Path:
        """Shared read-only exports reused across builds (e.g. Enterprise)."""
        return self.data_dir / "checkouts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
