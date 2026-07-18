"""
interfaces/email_diagnostics.py

`daily-runner --test-email` — a live, end-to-end check of both email features
(SMTP notifications, IMAP email commands) run once, deliberately, right after creating/editing
`.env` on any machine (new or existing). Actually connects and authenticates rather than just
checking env var presence — presence-only checks would not have caught either of the two real
failure modes this exists to catch: a Gmail password used instead of an App Password, or IMAP/SMTP
credentials that are simply wrong on a freshly-cloned machine. See docs/DEPLOYMENT.md's "Verify
before you trust it" section.

Deliberately NOT a pytest-only concern — the whole point is a human runs this once against real
credentials before trusting cron/`--live` with them, the same "verify before you trust it" pattern
this project already uses for paper-trading before going live.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText

from ..core.smtp_auth import authenticate as authenticate_smtp, get_provider, smtp_ready

GMAIL_APP_PASSWORD_HINT = (
    "Gmail rejects a normal account password for SMTP AUTH. Generate an App Password at "
    "https://myaccount.google.com/apppasswords (requires 2-Step Verification enabled first) "
    "and set SMTP_PASS to that, not your real password."
)
OUTLOOK_OAUTH_HINT = (
    "Outlook.com/Hotmail/Microsoft 365 reject password-based SMTP AUTH entirely. Set "
    "SMTP_PROVIDER=outlook and configure MS_OAUTH_CLIENT_ID — see docs/DEPLOYMENT.md's "
    "Outlook OAuth2 section."
)


def _check_smtp() -> bool:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO_EMAIL")

    if not smtp_ready(host, user, to_addr, password):
        print("SMTP: SKIPPED — SMTP_HOST/SMTP_USER/ALERT_TO_EMAIL/SMTP_PASS (or "
              "MS_OAUTH_CLIENT_ID for outlook) not fully configured.")
        return True

    msg = MIMEText(
        "This is a real test email from daily-runner --test-email, confirming SMTP send "
        "works end-to-end. No action needed."
    )
    msg["Subject"] = "[momentum-trading] Email setup test"
    msg["From"] = user
    msg["To"] = to_addr
    msg["X-Momentum-Trading-Bot"] = "1"

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            authenticate_smtp(server, user, password)
            server.sendmail(user, [to_addr], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        hint = GMAIL_APP_PASSWORD_HINT if get_provider() == "gmail" else OUTLOOK_OAUTH_HINT
        print(f"SMTP: FAILED — authentication rejected ({e}). {hint}")
        return False
    except Exception as e:
        print(f"SMTP: FAILED — {e}")
        return False

    print(f"SMTP: OK — test email sent to {to_addr}. Check that inbox to confirm delivery.")
    return True


def _check_imap() -> bool:
    import imaplib

    imap_host = os.environ.get("IMAP_HOST")
    imap_user = os.environ.get("IMAP_USER")
    imap_password = os.environ.get("IMAP_PASS")
    trusted_sender = os.environ.get("TRUSTED_SENDER_EMAIL")

    if not all([imap_host, imap_user, imap_password, trusted_sender]):
        print("IMAP: SKIPPED — email-commanded remote actions not configured "
              "(IMAP_HOST/IMAP_USER/IMAP_PASS/TRUSTED_SENDER_EMAIL not all set).")
        return True

    try:
        conn = imaplib.IMAP4_SSL(imap_host)
        conn.login(imap_user, imap_password)
        conn.logout()
    except imaplib.IMAP4.error as e:
        print(f"IMAP: FAILED — login rejected ({e}). Check IMAP_USER/IMAP_PASS — for Gmail "
              f"this must be an App Password, same as SMTP_PASS. {GMAIL_APP_PASSWORD_HINT}")
        return False
    except Exception as e:
        print(f"IMAP: FAILED — {e}")
        return False

    print(f"IMAP: OK — logged in as {imap_user}. TRUSTED_SENDER_EMAIL is set to "
          f"{trusted_sender!r} — confirm that's the exact address you'll send commands from.")
    return True


def run_email_diagnostics() -> bool:
    """Runs both live checks, prints a human-readable pass/fail summary, returns overall success."""
    print("Testing email setup (real SMTP send + real IMAP login, if configured)...\n")
    smtp_ok = _check_smtp()
    imap_ok = _check_imap()
    print()
    if smtp_ok and imap_ok:
        print("Result: OK")
    else:
        print("Result: FAILED — see above for the specific check(s) that failed.")
    return smtp_ok and imap_ok
