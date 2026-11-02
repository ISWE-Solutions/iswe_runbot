"""Runtime settings stored in the `settings` table (editable on the System page).
Defaults come from .env via config.Settings."""
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Setting


def defaults() -> dict[str, str]:
    c = get_settings()
    return {
        "max_concurrent_builds": str(c.max_concurrent_builds),
        "max_running_previews": str(c.max_running_previews),
        "idle_sleep_hours": str(c.idle_sleep_hours),
        "preview_memory_limit": c.preview_memory_limit,
        "preview_cpus": str(c.preview_cpus),
        "build_memory_limit": c.build_memory_limit,
        "build_timeout_minutes": str(c.build_timeout_minutes),
        "health_timeout_seconds": str(c.health_timeout_seconds),
        # 1 = never allow previews more memory than the host has (see capacity.py)
        "memory_overcommit": "1",
        "stale_after_days": "30",
        "stale_delete_after_days": "60",
        "deleted_branch_grace_hours": "24",
        "failed_log_retention_days": "14",
        "disk_warning_percent": "80",
        "poll_minutes": "5",
        "disk_min_free_gb": "5",
        "backup_keep_days": "14",
        "image_prune_days": "7",
        "use_db_templates": "1",
        # Build alerts (System > Alerts, alerts.py): Fernet-encrypted webhook URLs, blank = off;
        # alert_emails = recipients, sent through the System > Mail server
        "slack_webhook_url_enc": "",
        "teams_webhook_url_enc": "",
        "discord_webhook_url_enc": "",
        "google_chat_webhook_url_enc": "",
        "alert_webhook_url_enc": "",
        "alert_emails": "",
        # Sign in with Microsoft / GitHub (System > Sign-in). No row or a blank row = the .env
        # value; the secrets are Fernet-encrypted here and plain in .env (web/sso.effective).
        "microsoft_tenant_id": c.microsoft_tenant_id,
        "microsoft_client_id": c.microsoft_client_id,
        "microsoft_client_secret_enc": "",
        "github_client_id": c.github_client_id,
        "github_client_secret_enc": "",
        "github_signin_org": c.github_signin_org,
        # Outgoing email (System > Mail, mailer.py); the password is Fernet-encrypted
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_security": "starttls",
        "smtp_username": "",
        "smtp_password_enc": "",
        "smtp_from": "",
    }


def get(s: Session, key: str) -> str:
    row = s.get(Setting, key)
    return row.value if row else defaults()[key]


def stored(s: Session, key: str) -> str | None:
    """The saved value, or None when the key has no row (the .env default applies)."""
    row = s.get(Setting, key)
    return row.value if row else None


def get_int(s: Session, key: str) -> int:
    return int(float(get(s, key)))


def put(s: Session, key: str, value: str) -> None:
    if key not in defaults():
        raise KeyError(key)
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


def clear(s: Session, key: str) -> None:
    """Remove the saved value so the .env default applies again."""
    row = s.get(Setting, key)
    if row:
        s.delete(row)
