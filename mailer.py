"""
Minimal e-mail sender for password-reset links.

Two backends, chosen by environment variables:

1) Resend (HTTP API) -- preferred if configured:
     RESEND_API_KEY  -> your Resend API key (secret)
     MAIL_FROM       -> sender, ex: "Buffallos <nao-responda@buffallos.com.br>"
                        (the domain must be verified in Resend)

2) SMTP (e.g. Gmail) -- fallback:
     SMTP_HOST       -> ex: smtp.gmail.com
     SMTP_PORT       -> ex: 587 (default)
     SMTP_USER       -> ex: kionesperegrino91@gmail.com
     SMTP_PASSWORD   -> Gmail "app password" (secret; needs 2FA enabled)
     MAIL_FROM       -> sender (defaults to SMTP_USER)

If none is configured, enabled() is False and the app falls back to the
admin-driven password reset.
"""

import os
import smtplib
from email.message import EmailMessage

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip() or SMTP_USER


def use_resend():
    return bool(RESEND_API_KEY and MAIL_FROM)


def use_smtp():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def enabled():
    return use_resend() or use_smtp()


def backend_name():
    if use_resend():
        return "resend"
    if use_smtp():
        return "smtp"
    return "disabled"


def _send_resend(to, subject, html):
    import requests

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"from": MAIL_FROM, "to": [to], "subject": subject, "html": html},
        timeout=20,
    )
    resp.raise_for_status()


def _send_smtp(to, subject, html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM or SMTP_USER
    msg["To"] = to
    msg.set_content("Seu cliente de e-mail não suporta HTML.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def send_email(to, subject, html):
    """Returns True on success, False on failure (never raises)."""
    try:
        if use_resend():
            _send_resend(to, subject, html)
        elif use_smtp():
            _send_smtp(to, subject, html)
        else:
            return False
        return True
    except Exception as exc:
        print(f"[mailer] send failed ({backend_name()}): {exc}")
        return False
