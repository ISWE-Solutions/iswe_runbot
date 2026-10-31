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
        # Fernet-encrypted Slack incoming-webhook URL; blank = no Slack messages
        "slack_webhook_url_enc": "",
    }


def get(s: Session, key: str) -> str:
    row = s.get(Setting, key)
    return row.value if row else defaults()[key]


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
