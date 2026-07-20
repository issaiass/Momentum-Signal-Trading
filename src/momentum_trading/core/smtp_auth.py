"""
core/smtp_auth.py

Shared SMTP authentication for daily_runner.py's send_alert_email() and
interfaces/notifications.py's category emails. Two mechanisms, picked via the
explicit SMTP_PROVIDER env var ("gmail" or "outlook"):

  - gmail (default if SMTP_PROVIDER is unset): classic SMTP AUTH LOGIN with
    SMTP_PASS, a Gmail App Password, not your real password.

  - outlook: XOAUTH2 via an Azure AD app registration, required for
    Outlook.com/Hotmail/Microsoft 365, which reject password-based SMTP AUTH
    outright ("5.7.139 Authentication unsuccessful, basic authentication is
    disabled"). See docs/DEPLOYMENT.md for how to register the app. On first
    use, MSAL's device-code flow prints a URL + one-time code to log in a
    browser; the resulting refresh token is cached in
    data_dir()/ms_oauth_token_cache.json so scheduled/unattended runs
    afterward re-authenticate silently.

SMTP_PROVIDER is explicit (rather than just inferring from which vars happen
to be set) so switching providers doesn't depend on remembering to blank out
the other provider's leftover config, e.g. a stale MS_OAUTH_CLIENT_ID
sitting in .env would otherwise silently force an OAuth2 attempt against a
Gmail account you meant to send through instead.

connect() picks the SMTP connection type from SMTP_PORT: implicit TLS
(SMTP_SSL) for port 465, STARTTLS for everything else (587, the common
default). Some networks/Docker hosts block port 587 at the raw TCP level
while leaving 465 open, see connect()'s own docstring for how this was
confirmed. SMTP_TIMEOUT_SECONDS (default 30) and send_with_retry() (2
attempts) are shared across every SMTP call site in this project
(daily_runner.py's send_alert_email(), interfaces/notifications.py's
category emails, interfaces/email_diagnostics.py's --test-email check).
"""

from __future__ import annotations

import base64
import logging
import os
import smtplib

from .paths import data_dir

logger = logging.getLogger("smtp_auth")

MS_OAUTH_SCOPES = ["https://outlook.office365.com/SMTP.Send"]
VALID_PROVIDERS = ("gmail", "outlook")


def _token_cache_path():
    return data_dir() / "ms_oauth_token_cache.json"


def get_provider() -> str:
    provider = os.environ.get("SMTP_PROVIDER", "gmail").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"SMTP_PROVIDER={provider!r} is not supported, must be one of {VALID_PROVIDERS}")
    return provider


def oauth2_configured() -> bool:
    return get_provider() == "outlook"


def smtp_ready(host: str | None, user: str | None, to_addr: str | None, password: str | None) -> bool:
    """True if there's enough config to attempt a send, for the selected SMTP_PROVIDER."""
    if not all([host, user, to_addr]):
        return False
    if oauth2_configured():
        return bool(os.environ.get("MS_OAUTH_CLIENT_ID"))
    return bool(password)


def _acquire_ms_access_token(user: str) -> str:
    import msal

    client_id = os.environ.get("MS_OAUTH_CLIENT_ID")
    if not client_id:
        raise RuntimeError("SMTP_PROVIDER=outlook requires MS_OAUTH_CLIENT_ID, see docs/DEPLOYMENT.md")
    tenant = os.environ.get("MS_OAUTH_TENANT", "consumers")  # "consumers" = personal MS accounts (outlook.com/hotmail.com)

    cache = msal.SerializableTokenCache()
    cache_path = _token_cache_path()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())

    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache,
    )

    result = None
    accounts = app.get_accounts(username=user)
    if accounts:
        result = app.acquire_token_silent(MS_OAUTH_SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=MS_OAUTH_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start Microsoft OAuth2 device flow: {flow}")
        logger.warning(flow["message"])  # "To sign in, open https://microsoft.com/devicelogin and enter code XXXXXXX"
        result = app.acquire_token_by_device_flow(flow)  # blocks until browser completion or flow expiry

    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())

    if not result or "access_token" not in result:
        error = (result or {}).get("error_description", "no token returned")
        raise RuntimeError(f"Failed to acquire Microsoft OAuth2 token: {error}")
    return result["access_token"]


def smtp_timeout() -> float:
    return float(os.environ.get("SMTP_TIMEOUT_SECONDS", "30"))


def connect(host: str, port: int, timeout: float | None = None) -> smtplib.SMTP:
    """
    Opens an SMTP connection appropriate for the port: implicit TLS (SMTP_SSL) for 465,
    STARTTLS for everything else (587 is the common default). Some networks block port
    587's plaintext-then-upgrade handshake at the raw TCP level while leaving 465
    (encrypted from connect) open, confirmed directly: a raw socket to port 587 timed
    out after 8s while port 465 connected in 0.11s, from inside this project's own
    Docker container, against the exact same Gmail host. Set SMTP_PORT=465 in .env if
    every SMTP send is timing out despite correct credentials.
    """
    timeout = smtp_timeout() if timeout is None else timeout
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    server = smtplib.SMTP(host, port, timeout=timeout)
    server.starttls()
    return server


def send_with_retry(send_fn, max_attempts: int = 2, backoff_seconds: float = 3.0):
    """
    Retries a zero-arg SMTP send callable up to max_attempts times, re-raising the last
    exception if every attempt fails so the caller's own try/except still sees a real
    error. Mirrors execution/live_signal.py's with_retry() pattern for transient
    network failures, kept local here (not imported from execution/) to avoid a new
    cross-domain dependency from interfaces/ into execution/.
    """
    import time
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return send_fn()
        except Exception as e:
            last_exc = e
            logger.warning("SMTP send attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(backoff_seconds)
    raise last_exc


def authenticate(server: smtplib.SMTP, user: str, password: str | None) -> None:
    """Authenticates an already-encrypted connection (via connect()'s STARTTLS or
    implicit-TLS SMTP_SSL), using OAuth2 if configured."""
    if not oauth2_configured():
        server.login(user, password)
        return

    access_token = _acquire_ms_access_token(user)
    auth_string = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    b64_auth = base64.b64encode(auth_string.encode()).decode()

    server.putcmd("AUTH", "XOAUTH2 " + b64_auth)
    code, response = server.getreply()
    if code == 334:
        # Server rejected the token and sent a base64 JSON error challenge;
        # an empty reply ends the exchange and surfaces the real failure code.
        server.putcmd("")
        code, response = server.getreply()
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, response)
