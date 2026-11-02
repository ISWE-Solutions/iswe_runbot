"""Build alerts to chat channels, a generic webhook and email (System > Alerts).

Every configured channel gets the same Alert. targets() reads the settings inside a session;
deliver() posts outside it and never raises (one result per channel: None = sent, else the
reason). Webhook URLs are Fernet-encrypted in `settings` and never logged or shown."""
import logging
import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from . import appsettings, mailer
from .security import decrypt

log = logging.getLogger("previewer.alerts")

TIMEOUT = 5
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    setting: str
    placeholder: str
    pattern: re.Pattern
    problem: str
    help: str


CHANNELS: dict[str, Channel] = {c.key: c for c in (
    Channel("slack", "Slack", "slack_webhook_url_enc", "https://hooks.slack.com/services/…",
            re.compile(r"^https://hooks\.slack\.com/\S+$"), "A Slack incoming-webhook URL (https://hooks.slack.com/…)",
            "Slack → Apps → Incoming Webhooks → Add to a channel; copy the Webhook URL."),
    Channel("teams", "Microsoft Teams", "teams_webhook_url_enc", "https://….logic.azure.com/workflows/…",
            re.compile(r"^https://\S+$"), "The https:// URL the Teams workflow gives you",
            "In the channel: ⋯ → Workflows → \"Post to a channel when a webhook request is received\"; "
            "copy the URL it shows at the end."),
    Channel("discord", "Discord", "discord_webhook_url_enc", "https://discord.com/api/webhooks/…",
            re.compile(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+$"),
            "A Discord webhook URL (https://discord.com/api/webhooks/…)",
            "Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL."),
    Channel("google_chat", "Google Chat", "google_chat_webhook_url_enc", "https://chat.googleapis.com/v1/spaces/…",
            re.compile(r"^https://chat\.googleapis\.com/\S+$"),
            "A Google Chat webhook URL (https://chat.googleapis.com/…)",
            "In the space: Apps & integrations → Webhooks → Add webhook; copy its URL."),
    Channel("webhook", "Other (JSON webhook)", "alert_webhook_url_enc", "https://example.com/hooks/previewer",
            re.compile(r"^https?://\S+$"), "An http:// or https:// URL",
            "Receives a JSON POST with event, title, detail, url, project, branch and build_id: for "
            "Mattermost, Rocket.Chat, n8n, Zapier or your own service."),
)}
EMAIL = "email"

_ICON = {"failed": ":x:", "fixed": ":white_check_mark:", "tests_failed": ":warning:",
         "tests_recovered": ":white_check_mark:", "test": ":bell:"}
_TEAMS_COLOR = {"failed": "Attention", "tests_failed": "Warning", "fixed": "Good",
                "tests_recovered": "Good", "test": "Accent"}
_DISCORD_COLOR = {"failed": 0xD93025, "tests_failed": 0xF29900, "fixed": 0x1E8E3E,
                  "tests_recovered": 0x1E8E3E, "test": 0x213965}


@dataclass
class Alert:
    event: str  # failed | fixed | tests_failed | tests_recovered | test
    title: str
    url: str
    detail: str = ""
    project: str = ""
    branch: str = ""
    build_id: int | None = None
    odoo_version: str = ""


@dataclass
class Target:
    channel: str
    url: str = field(default="", repr=False)
    recipients: list[str] = field(default_factory=list)
    mail: mailer.MailConfig | None = field(default=None, repr=False)


def parse_emails(text: str) -> list[str]:
    return [e for e in re.split(r"[\s,;]+", text or "") if e]


def targets(s: Session, only: str | None = None) -> list[Target]:
    out = []
    for c in CHANNELS.values():
        enc = appsettings.get(s, c.setting)
        if enc and only in (None, c.key):
            out.append(Target(c.key, url=decrypt(enc)))
    recipients = parse_emails(appsettings.get(s, "alert_emails"))
    if recipients and only in (None, EMAIL):
        out.append(Target(EMAIL, recipients=recipients, mail=mailer.config(s)))
    return out


def payload(channel: str, a: Alert) -> dict:
    if channel == "slack":
        return {"text": "\n".join(x for x in (f"{_ICON[a.event]} {a.title}", a.detail, a.url) if x)}
    if channel == "teams":
        body = [{"type": "TextBlock", "text": a.title, "weight": "Bolder", "size": "Medium", "wrap": True,
                 "color": _TEAMS_COLOR[a.event]}]
        if a.detail:
            body.append({"type": "TextBlock", "text": a.detail, "wrap": True})
        card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                "version": "1.4", "body": body,
                "actions": [{"type": "Action.OpenUrl", "title": "Open in Previewer", "url": a.url}]}
        return {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    if channel == "discord":
        return {"embeds": [{"title": a.title[:256], "description": a.detail[:4000], "url": a.url,
                            "color": _DISCORD_COLOR[a.event]}]}
    if channel == "google_chat":
        return {"text": "\n".join(x for x in (f"*{a.title}*", a.detail, a.url) if x)}
    return {"event": a.event, "title": a.title, "detail": a.detail, "url": a.url, "project": a.project,
            "branch": a.branch, "build_id": a.build_id, "odoo_version": a.odoo_version}


def deliver(targets_: list[Target], a: Alert) -> dict[str, str | None]:
    results: dict[str, str | None] = {}
    for t in targets_:
        try:
            if t.channel == EMAIL:
                body = "\n\n".join(x for x in (a.detail, a.url) if x) + "\n\n-- Previewer"
                mailer.send(None, t.recipients, f"[Previewer] {a.title}", body, cfg=t.mail)
            else:
                r = httpx.post(t.url, json=payload(t.channel, a), timeout=TIMEOUT)
                r.raise_for_status()
            results[t.channel] = None
        except mailer.MailError as e:
            results[t.channel] = str(e)
        except httpx.HTTPStatusError as e:
            results[t.channel] = f"The webhook answered {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:  # noqa: BLE001 — one channel never stops the others
            results[t.channel] = f"Could not reach the webhook ({type(e).__name__})"
        if results[t.channel]:
            log.warning("%s alert (%s) not sent: %s", t.channel, a.event, results[t.channel])
    return results
