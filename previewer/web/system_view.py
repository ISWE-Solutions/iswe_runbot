"""System page data: worker heartbeat (disk, Docker, preview resources, scheduler), settings
groups and validation. The worker writes DATA_DIR/worker.heartbeat; any key may be missing."""
import json
import re
import time

from sqlalchemy.orm import Session

from .. import appsettings, capacity
from ..config import get_settings
from ..db import SessionLocal

HEARTBEAT_STALE_SECONDS = 60

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
GH_CLIENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
GH_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
# provider -> (key, label, problem when the value does not match, pattern)
SIGNIN_FIELDS: dict[str, list[tuple[str, str, str, re.Pattern]]] = {
    "microsoft": [
        ("microsoft_tenant_id", "Tenant ID",
         "Must be the Directory (tenant) ID, a GUID, not a domain", GUID_RE),
        ("microsoft_client_id", "Client ID", "Must be the Application (client) ID, a GUID", GUID_RE),
    ],
    "github": [
        ("github_client_id", "Client ID", "Must be the OAuth App's Client ID (letters, digits, dots)",
         GH_CLIENT_RE),
        ("github_signin_org", "Organisation", "A GitHub organisation name, like ISWE-Solutions", GH_ORG_RE),
    ],
}
SECRET_MAX = 500
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
ADDRESS_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

# (key, label, kind, help); kinds: int, percent, float, memory, bool, secret
GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Limits", [
        ("max_running_previews", "Previews running at once", "int",
         "When exceeded, the longest-idle preview is put to sleep. Capped to what fits in the "
         "Docker host's memory."),
        ("max_concurrent_builds", "Builds at the same time", "int",
         "Applies after the worker restarts."),
        ("preview_memory_limit", "Memory per preview", "memory", "e.g. 1536m or 2g"),
        ("preview_cpus", "CPU cores per preview", "float", ""),
        ("build_memory_limit", "Memory per build", "memory", "Installing Enterprise with demo data needs more."),
        ("memory_overcommit", "Memory overcommit", "float",
         "1 = previews and builds may never use more than the host has. 2 bets they stay at half "
         "their limit (more previews, but a busy host may kill containers)."),
    ]),
    ("Builds", [
        ("build_timeout_minutes", "Build time limit (minutes)", "int", ""),
        ("health_timeout_seconds", "Start-up check time limit (seconds)", "int", ""),
        ("poll_minutes", "Poll GitHub every (minutes)", "int",
         "Fallback for webhooks; 0 turns polling off."),
        ("use_db_templates", "Use ready-made base databases", "bool",
         "Builds copy a prepared database instead of installing Odoo from scratch."),
    ]),
    ("Sleep & cleanup", [
        ("idle_sleep_hours", "Sleep previews idle for (hours)", "int", "0 turns sleeping off."),
        ("stale_after_days", "Stop branches with no commits for (days)", "int", ""),
        ("stale_delete_after_days", "Delete stale branches after (days)", "int", ""),
        ("deleted_branch_grace_hours", "Keep deleted branches for (hours)", "int", ""),
        ("failed_log_retention_days", "Keep failed build logs for (days)", "int", ""),
        ("image_prune_days", "Prune unused build images every (days)", "int", ""),
        ("backup_keep_days", "Keep app database backups for (days)", "int", ""),
        ("disk_warning_percent", "Warn when the disk is this full (%)", "percent", ""),
        ("disk_min_free_gb", "Pause new builds below this much free disk (GB)", "int", ""),
    ]),
    ("Notifications", [
        ("slack_webhook_url_enc", "Slack incoming-webhook URL", "secret",
         "Build failures and recoveries are posted here. Stored encrypted; blank keeps the current one."),
    ]),
]


def heartbeat() -> dict:
    """Full heartbeat plus ok/age; {} keys when the worker has never written one."""
    path = get_settings().data_dir / "worker.heartbeat"
    try:
        beat = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"ok": False, "reason": "no heartbeat"}
    age = time.time() - float(beat.get("ts", 0))
    beat["age_seconds"] = round(age, 1)
    beat["ok"] = age <= HEARTBEAT_STALE_SECONDS
    return beat


def disk_warning(s: Session) -> dict | None:
    """{percent_used, free_gb, threshold} when the data disk is over the warning level."""
    disk = heartbeat().get("disk") or {}
    used = disk.get("percent_used")
    if used is None:
        return None
    try:
        threshold = float(appsettings.get(s, "disk_warning_percent"))
    except ValueError:
        return None
    return {"percent_used": used, "free_gb": disk.get("free_gb"), "threshold": threshold} if used >= threshold else None


def validate(form, current: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Form -> ({key: new value}, {key: error}). Secrets are handled by the caller."""
    values, errors = {}, {}
    for _, fields in GROUPS:
        for key, label, kind, _ in fields:
            if kind == "secret":
                continue
            if kind == "bool":
                values[key] = "1" if form.get(key) in ("on", "1", "true") else "0"
                continue
            raw = (form.get(key) or "").strip()
            if raw == "":
                errors[key] = "Required"
                continue
            if kind in ("int", "percent"):
                if not raw.isdigit():
                    errors[key] = "Whole number, 0 or more"
                    continue
                if kind == "percent" and not 1 <= int(raw) <= 100:
                    errors[key] = "Between 1 and 100"
                    continue
            elif kind == "float":
                try:
                    if float(raw) <= 0:
                        raise ValueError
                except ValueError:
                    errors[key] = "A number above 0"
                    continue
            elif kind == "memory":
                try:
                    if capacity.parse_memory(raw) < capacity.MIN_MEMORY:
                        raise ValueError
                except ValueError:
                    errors[key] = "Like 1536m or 2g (at least 256m)"
                    continue
            values[key] = raw
    for key in ("max_concurrent_builds", "max_running_previews"):
        if values.get(key) == "0":
            errors[key] = "At least 1"
    if values.get("memory_overcommit") and "memory_overcommit" not in errors \
            and not 1 <= float(values["memory_overcommit"]) <= 4:
        errors["memory_overcommit"] = "Between 1 and 4"
    if not errors:
        problem = capacity_problem({**current, **values})
        if problem:
            errors["max_running_previews"] = problem
    return values, errors


def signin(s: Session) -> dict:
    """System > Sign-in: per provider its status, callback URL, field values and where each
    value comes from ("db", "env", "default" or ""). Never contains a secret."""
    from . import sso
    c = get_settings()
    v = sso.effective(s)
    out = {}
    for provider, fields in SIGNIN_FIELDS.items():
        rows = {}
        for key, label, _, _ in fields:
            saved = (appsettings.stored(s, key) or "").strip()
            env = (getattr(c, key) or "").strip()
            source = "db" if saved else ("default" if env and env == type(c).model_fields[key].default
                                         else "env" if env else "")
            rows[key] = {"label": label, "value": saved or env, "source": source}
        secret_saved = bool(appsettings.stored(s, f"{provider}_client_secret_enc"))
        need = sso.missing(provider, v)
        out[provider] = {
            "label": sso.LABELS[provider], "ready": not need, "missing": need,
            "redirect_uri": sso.redirect_uri(provider), "fields": rows,
            "secret_saved": secret_saved, "secret_env": bool(getattr(c, f"{provider}_client_secret")),
        }
    return out


def save_signin(s: Session, provider: str, form) -> dict[str, str]:
    """Validate and store one provider's System > Sign-in form; returns {key: error}. A blank
    field (or the .env value itself) removes the saved value so .env applies; a blank secret
    keeps the saved one."""
    from ..security import encrypt
    c = get_settings()
    values, errors = {}, {}
    for key, _, problem, pattern in SIGNIN_FIELDS[provider]:
        raw = (form.get(key) or "").strip()
        if raw and not pattern.match(raw):
            errors[key] = problem
        values[key] = raw
    secret_key = f"{provider}_client_secret"
    secret = (form.get(secret_key) or "").strip()
    if len(secret) > SECRET_MAX or any(ch.isspace() for ch in secret):
        errors[secret_key] = "Paste the secret's Value exactly (no spaces)"
    if errors:
        return errors
    for key, raw in values.items():
        if raw and raw != (getattr(c, key) or "").strip():
            appsettings.put(s, key, raw)
        else:
            appsettings.clear(s, key)
    if form.get(f"{secret_key}_clear") in ("on", "1", "true"):
        appsettings.clear(s, f"{secret_key}_enc")
    elif secret:
        appsettings.put(s, f"{secret_key}_enc", encrypt(secret))
    return {}


def mail(s: Session) -> dict:
    """System > Mail: saved values, status and what is still missing. Never contains the password."""
    from .. import mailer
    c = mailer.config(s)
    return {"host": c.host, "port": c.port, "security": c.security, "username": c.username,
            "sender": c.sender, "password_saved": bool(c.password), "missing": mailer.missing(c),
            "security_choices": mailer.SECURITY}


def save_mail(s: Session, form) -> dict[str, str]:
    """Validate and store the System > Mail form; returns {field: error}. A blank password
    keeps the saved one."""
    from email.utils import parseaddr

    from .. import mailer
    from ..security import encrypt
    host = (form.get("smtp_host") or "").strip()
    port = (form.get("smtp_port") or "").strip()
    security = form.get("smtp_security") or "starttls"
    username = (form.get("smtp_username") or "").strip()
    password = form.get("smtp_password") or ""
    sender = (form.get("smtp_from") or "").strip()
    errors = {}
    if host and not HOST_RE.match(host):
        errors["smtp_host"] = "A host name like smtp.office365.com, without https:// or a port"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        errors["smtp_port"] = "A port number, usually 587, 465 or 25"
    if security not in mailer.SECURITY:
        errors["smtp_security"] = "Choose one of the options"
    if len(username) > 254:
        errors["smtp_username"] = "Too long"
    if len(password) > SECRET_MAX:
        errors["smtp_password"] = "Too long"
    if sender and not ADDRESS_RE.match(parseaddr(sender)[1] or ""):
        errors["smtp_from"] = "An email address, optionally with a name: Previewer <previewer@iswe.co.zm>"
    if errors:
        return errors
    for key, value in (("smtp_host", host), ("smtp_port", port), ("smtp_security", security),
                       ("smtp_username", username), ("smtp_from", sender)):
        appsettings.put(s, key, value)
    if form.get("smtp_password_clear") in ("on", "1", "true"):
        appsettings.put(s, "smtp_password_enc", "")
    elif password:
        appsettings.put(s, "smtp_password_enc", encrypt(password))
    return {}


def host_memory() -> int | None:
    """Docker host memory in bytes, from the worker's last stats (None until it has run)."""
    gb = (heartbeat().get("docker") or {}).get("mem_total_gb")
    return int(float(gb) * 1e9) if gb else None


def running_cap(s: Session, v: dict[str, str]) -> int:
    """The running limit the worker enforces (automation.running_limit): the setting capped
    to the Docker host's memory and Postgres' max_connections."""
    from sqlalchemy import text
    c = get_settings()
    try:
        max_conn = int(s.execute(text("SHOW max_connections")).scalar())
    except Exception:  # noqa: BLE001 — unknown: memory alone decides
        max_conn = None
    try:
        return capacity.effective_limit(
            max(1, int(v["max_running_previews"])), host_memory(), preview_mem=v["preview_memory_limit"],
            builds=max(1, int(v["max_concurrent_builds"])), build_mem=v["build_memory_limit"],
            reserved=c.reserved_memory, overcommit=float(v.get("memory_overcommit") or 1),
            maxconn=c.odoo_db_maxconn, max_connections=max_conn)
    except ValueError:
        return int(v["max_running_previews"])


def capacity_problem(v: dict[str, str]) -> str | None:
    """Why these limits do not fit the Docker host's memory, or None."""
    total = host_memory()
    if not total:
        return None
    f = capacity.fit(total=total, previews=int(v["max_running_previews"]),
                     preview_mem=capacity.parse_memory(v["preview_memory_limit"]),
                     builds=int(v["max_concurrent_builds"]),
                     build_mem=capacity.parse_memory(v["build_memory_limit"]),
                     reserved=capacity.parse_memory(get_settings().reserved_memory),
                     overcommit=float(v.get("memory_overcommit") or 1))
    if int(v["max_running_previews"]) <= f.max_previews:
        return None
    return (f"Only {f.max_previews} fit: the Docker host has {capacity.fmt_gb(f.total)}"
            f"{' (with overcommit)' if float(v.get('memory_overcommit') or 1) > 1 else ''}, and these limits "
            f"could use {capacity.fmt_gb(f.needed)} including {get_settings().reserved_memory} for the "
            "system. Lower the number, the memory per preview or build, or the builds at once.")


def app_setting(key: str) -> str:
    """Template helper: one runtime setting."""
    s = SessionLocal()
    try:
        return appsettings.get(s, key)
    finally:
        s.close()


def disk_banner() -> dict | None:
    """Template helper for pages outside the System page (the board)."""
    s = SessionLocal()
    try:
        return disk_warning(s)
    finally:
        s.close()
