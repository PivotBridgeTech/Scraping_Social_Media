"""
email_sender.py — Email outreach module

Providers:
  - Resend (recommended) — set RESEND_API_KEY, EMAIL_FROM, EMAIL_FROM_NAME in .env
  - SMTP (Gmail/Outlook)  — set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in .env

Personalisation placeholders: {name}, {username}, {location}
Every send appends a List-Unsubscribe header and footer link.
"""

import hashlib
import json
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_UNSUB_FILE = Path("data/unsubscribed.json")


# ── Unsubscribe store ──────────────────────────────────────────────────────

def email_token(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


def _load_unsub() -> set:
    if not _UNSUB_FILE.exists():
        return set()
    try:
        return set(json.loads(_UNSUB_FILE.read_text()))
    except Exception:
        return set()


def is_unsubscribed(email: str) -> bool:
    return email_token(email) in _load_unsub()


def mark_unsubscribed(token: str) -> None:
    tokens = _load_unsub()
    tokens.add(token)
    _UNSUB_FILE.parent.mkdir(parents=True, exist_ok=True)
    _UNSUB_FILE.write_text(json.dumps(list(tokens)))


# ── Personalisation ────────────────────────────────────────────────────────

def personalise(template: str, username: str = "", full_name: str = "", location: str = "") -> str:
    first_name = full_name.strip().split()[0] if full_name.strip() else username
    return (
        template
        .replace("{username}", username)
        .replace("{name}",     first_name)
        .replace("{location}", location or "your area")
    )


def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


# ── Resend provider ────────────────────────────────────────────────────────

def _send_resend(
    api_key: str, from_email: str, from_name: str,
    to_email: str, subject: str,
    body_text: str, body_html: str | None,
    unsubscribe_url: str,
) -> dict:
    try:
        import resend
    except ImportError:
        return {"sent": False, "error": "resend package not installed — run: pip install resend"}

    resend.api_key = api_key
    text = body_text + f"\n\n---\nUnsubscribe: {unsubscribe_url}"
    params: dict = {
        "from":    f"{from_name} <{from_email}>",
        "to":      [to_email],
        "subject": subject,
        "text":    text,
        "headers": {"List-Unsubscribe": f"<{unsubscribe_url}>"},
    }
    if body_html:
        params["html"] = (
            body_html
            + f'<p style="font-size:11px;color:#aaa;margin-top:32px">'
              f'To unsubscribe, <a href="{unsubscribe_url}">click here</a>.</p>'
        )
    try:
        result = resend.Emails.send(params)
        return {"sent": True, "id": result.get("id"), "error": None}
    except Exception as e:
        return {"sent": False, "id": None, "error": str(e)}


# ── SMTP provider ──────────────────────────────────────────────────────────

def _send_smtp(
    host: str, port: int, user: str, password: str,
    from_email: str, from_name: str,
    to_email: str, subject: str,
    body_text: str, body_html: str | None,
    unsubscribe_url: str,
    tracking_pixel_url: str = "",
) -> dict:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"]         = subject
        msg["From"]            = f"{from_name} <{from_email}>"
        msg["To"]              = to_email
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"

        text = body_text + f"\n\n---\nUnsubscribe: {unsubscribe_url}"
        msg.attach(MIMEText(text, "plain"))

        if body_html:
            pixel = (
                f'<img src="{tracking_pixel_url}" width="1" height="1" style="display:none" />'
                if tracking_pixel_url else ""
            )
            html = (
                body_html
                + pixel
                + f'<p style="font-size:11px;color:#aaa;margin-top:32px">'
                  f'To unsubscribe, <a href="{unsubscribe_url}">click here</a>.</p>'
            )
            msg.attach(MIMEText(html, "html"))

        if port == 465:
            with smtplib.SMTP_SSL(host, port) as server:
                server.login(user, password)
                server.sendmail(from_email, to_email, msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                server.starttls()
                server.login(user, password)
                server.sendmail(from_email, to_email, msg.as_string())

        return {"sent": True, "id": None, "error": None}
    except Exception as e:
        return {"sent": False, "id": None, "error": str(e)}


# ── Public interface ───────────────────────────────────────────────────────

def send_email(
    provider: str,
    provider_cfg: dict,
    to_email: str,
    to_name: str,
    subject: str,
    body_text: str,
    body_html: str | None,
    unsubscribe_url: str,
    tracking_pixel_url: str = "",
) -> dict:
    """
    Send one email via `provider` ("resend" or "smtp").
    Returns {"sent": bool, "id": str|None, "error": str|None}.
    Silently skips unsubscribed addresses (error = "unsubscribed").
    """
    if is_unsubscribed(to_email):
        return {"sent": False, "id": None, "error": "unsubscribed"}

    if provider == "resend":
        return _send_resend(
            api_key      = provider_cfg["api_key"],
            from_email   = provider_cfg["from_email"],
            from_name    = provider_cfg["from_name"],
            to_email     = to_email,
            subject      = subject,
            body_text    = body_text,
            body_html    = body_html,
            unsubscribe_url = unsubscribe_url,
        )

    if provider == "smtp":
        return _send_smtp(
            host        = provider_cfg["host"],
            port        = int(provider_cfg.get("port", 587)),
            user        = provider_cfg["user"],
            password    = provider_cfg["password"],
            from_email  = provider_cfg.get("from_email") or provider_cfg["user"],
            from_name   = provider_cfg["from_name"],
            to_email    = to_email,
            subject     = subject,
            body_text   = body_text,
            body_html   = body_html,
            unsubscribe_url     = unsubscribe_url,
            tracking_pixel_url  = tracking_pixel_url,
        )

    return {"sent": False, "id": None, "error": f"Unknown provider: {provider}"}
