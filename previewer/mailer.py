"""Outgoing email over SMTP, configured on System > Mail (settings table; the password is
Fernet-encrypted). Alerts call send(); it raises MailError with a message fit for the screen
and never logs or shows the password."""
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from sqlalchemy.orm import Session

from . import appsettings
from .security import decrypt

TIMEOUT = 15
SECURITY = {"starttls": "STARTTLS (usually port 587)", "ssl": "SSL/TLS (usually port 465)",
            "none": "None (internal relay, usually port 25)"}


class MailError(Exception):
    pass


@dataclass
class MailConfig:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""


def config(s: Session) -> MailConfig:
    enc = appsettings.get(s, "smtp_password_enc")
    return MailConfig(
        host=appsettings.get(s, "smtp_host").strip(),
        port=int(appsettings.get(s, "smtp_port") or 587),
        security=appsettings.get(s, "smtp_security") or "starttls",
        username=appsettings.get(s, "smtp_username").strip(),
        password=decrypt(enc) if enc else "",
        sender=appsettings.get(s, "smtp_from").strip(),
    )


def missing(c: MailConfig) -> list[str]:
    need = [label for label, v in (("SMTP server", c.host), ("From address", c.sender)) if not v]
    if c.username and not c.password:
        need.append("Password")
    return need


def ready(s: Session) -> bool:
    return not missing(config(s))


def send(s: Session, to: list[str], subject: str, body: str, *, cfg: MailConfig | None = None) -> None:
    c = cfg or config(s)
    need = missing(c)
    if need:
        raise MailError(f"Email is not set up yet (missing: {', '.join(need)}).")
    msg = EmailMessage()
    name, addr = parseaddr(c.sender)
    msg["From"] = formataddr((name or "Previewer", addr))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if c.security == "ssl":
            server = smtplib.SMTP_SSL(c.host, c.port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(c.host, c.port, timeout=TIMEOUT)
        with server:
            if c.security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if c.username:
                server.login(c.username, c.password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"The mail server refused the username or password ({e.smtp_code}).") from None
    except smtplib.SMTPRecipientsRefused:
        raise MailError(f"The mail server refused the recipient {', '.join(to)}.") from None
    except smtplib.SMTPSenderRefused as e:
        raise MailError(f"The mail server refused the From address ({e.smtp_code}): the account "
                        "may not be allowed to send as it.") from None
    except smtplib.SMTPNotSupportedError:
        raise MailError("The mail server does not support STARTTLS here: try SSL/TLS or None.") from None
    except smtplib.SMTPException as e:
        raise MailError(f"The mail server answered: {str(e)[:200]}") from None
    except ssl.SSLError as e:
        raise MailError(f"TLS failed ({e.reason or 'handshake'}): check the port and security.") from None
    except (socket.timeout, TimeoutError):
        raise MailError(f"No answer from {c.host}:{c.port} within {TIMEOUT}s (blocked port or wrong "
                        "security setting?).") from None
    except OSError as e:
        raise MailError(f"Cannot reach {c.host}:{c.port}: {e.strerror or type(e).__name__}.") from None
