"""System > Mail: SMTP settings saved in the `settings` table (password encrypted), the test
email, and mailer.send's readable errors."""
import smtplib

import pytest

from previewer import appsettings, mailer
from previewer.models import Setting
from previewer.security import decrypt

from test_web_app import client, login, make_user, user_client  # noqa: F401  (fixture)

PASSWORD = "Smtp-Secret-DO-NOT-RENDER"
FORM = {"smtp_host": "smtp.office365.com", "smtp_port": "587", "smtp_security": "starttls",
        "smtp_username": "previewer@iswe.co.zm", "smtp_password": PASSWORD,
        "smtp_from": "Previewer <previewer@iswe.co.zm>"}


class FakeSMTP:
    """Records what mailer.send does instead of talking to a server."""
    sent: list = []
    calls: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.calls.append(("connect", host, port, context is not None))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.calls, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def page(c) -> str:
    r = c.get("/admin/system", params={"tab": "mail"})
    assert r.status_code == 200, r.text
    return r.text


def test_mail_tab_starts_not_set_up(s, user_client):
    html = page(user_client)
    assert 'data-status="mail">Not set up' in html and "Still needed: SMTP server, From address" in html
    assert "Send test email</button>" in html and "disabled" in html


def test_save_mail_settings_encrypts_and_never_renders_the_password(s, user_client):
    r = user_client.post("/admin/system/mail", data=FORM)
    assert r.status_code == 303 and r.headers["location"] == "/admin/system?tab=mail"
    s.expire_all()
    assert appsettings.get(s, "smtp_host") == "smtp.office365.com"
    enc = s.get(Setting, "smtp_password_enc").value
    assert PASSWORD not in enc and decrypt(enc) == PASSWORD
    html = page(user_client)
    assert PASSWORD not in html and enc not in html
    assert 'data-status="mail">Set up' in html and 'data-secret="saved"' in html

    # a blank password keeps the saved one; the checkbox removes it
    user_client.post("/admin/system/mail", data={**FORM, "smtp_password": "", "smtp_port": "465",
                                                 "smtp_security": "ssl"})
    s.expire_all()
    assert decrypt(appsettings.get(s, "smtp_password_enc")) == PASSWORD
    assert appsettings.get(s, "smtp_security") == "ssl"
    user_client.post("/admin/system/mail", data={**FORM, "smtp_password": "", "smtp_password_clear": "on"})
    s.expire_all()
    assert appsettings.get(s, "smtp_password_enc") == ""
    assert "Still needed: Password" in page(user_client)


@pytest.mark.parametrize("field,value,error", [
    ("smtp_host", "https://smtp.office365.com", "without https://"),
    ("smtp_port", "99999", "A port number"),
    ("smtp_security", "tls13", "Choose one of the options"),
    ("smtp_from", "not-an-address", "An email address"),
])
def test_mail_settings_validation(s, user_client, field, value, error):
    r = user_client.post("/admin/system/mail", data={**FORM, field: value})
    assert r.status_code == 400 and error in r.text
    assert s.get(Setting, "smtp_host") is None  # nothing saved


def test_test_email_sends_with_the_saved_settings(s, user_client, smtp):
    user_client.post("/admin/system/mail", data=FORM)
    r = user_client.post("/admin/system/mail-test", data={"to": "ops@iswe.co.zm"})
    assert "✓ Sent to ops@iswe.co.zm" in r.text
    assert smtp.calls == [("connect", "smtp.office365.com", 587, False), ("starttls",),
                          ("login", "previewer@iswe.co.zm", PASSWORD)]
    msg = smtp.sent[0]
    assert msg["To"] == "ops@iswe.co.zm" and msg["From"] == "Previewer <previewer@iswe.co.zm>"
    assert msg["Subject"] == "Previewer test email"


def test_test_email_reports_problems_readably(s, user_client, smtp):
    r = user_client.post("/admin/system/mail-test", data={"to": "ops@iswe.co.zm"})
    assert "Email is not set up yet (missing: SMTP server, From address)" in r.text
    user_client.post("/admin/system/mail", data=FORM)
    assert "Enter the email address" in user_client.post("/admin/system/mail-test", data={"to": "x"}).text
    smtp.fail = smtplib.SMTPAuthenticationError(535, b"5.7.139 Authentication unsuccessful")
    r = user_client.post("/admin/system/mail-test", data={"to": "ops@iswe.co.zm"})
    assert "refused the username or password (535)" in r.text and PASSWORD not in r.text
    assert smtp.sent == []


def test_ssl_and_relay_without_login(s, smtp):
    cfg = mailer.MailConfig(host="relay.local", port=25, security="none", sender="alerts@iswe.co.zm")
    mailer.send(s, ["a@iswe.co.zm"], "Hi", "Body", cfg=cfg)
    assert smtp.calls == [("connect", "relay.local", 25, False)]
    smtp.calls.clear()
    cfg = mailer.MailConfig(host="smtp.x", port=465, security="ssl", username="u", password="p",
                            sender="alerts@iswe.co.zm")
    mailer.send(s, ["a@iswe.co.zm"], "Hi", "Body", cfg=cfg)
    assert smtp.calls == [("connect", "smtp.x", 465, True), ("login", "u", "p")]


def test_unreachable_server_is_explained(s, monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    cfg = mailer.MailConfig(host="smtp.x", port=587, sender="alerts@iswe.co.zm")
    with pytest.raises(mailer.MailError, match=r"Cannot reach smtp.x:587: Connection refused"):
        mailer.send(s, ["a@iswe.co.zm"], "Hi", "Body", cfg=cfg)
