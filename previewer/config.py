"""All settings come from environment variables (loaded from .env by Compose)."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Dashboard host; previews live at <label>.<domain>
    domain: str = "preview.localhost"
    tls_enabled: bool = False
    acme_email: str = ""
    dns_provider: str = ""
    dns_api_token: str = ""

    secret_key: str = "dev-secret-change-me"
    # Fernet key (urlsafe base64, 32 bytes) for GitHub tokens
    encryption_key: str = ""

    # App database (superuser of the shared Postgres server)
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "previewer"
    postgres_password: str = "previewer"
    postgres_db: str = "previewer"
    # Role the Odoo previews connect as (CREATEDB, not superuser)
    odoo_db_user: str = "odoo"
    odoo_db_password: str = "odoo"

    # Host folder holding repos/, builds/, filestore/, logs/. Mounted at the
    # same path inside app and worker so preview bind mounts resolve on the host.
    data_dir: Path = Path("/srv/previewer")
    docker_network: str = "previewer_net"
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

    admin_email: str = ""
    test_database: str = "previewer_test"

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
