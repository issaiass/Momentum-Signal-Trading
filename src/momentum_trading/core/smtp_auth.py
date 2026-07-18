"""
core/smtp_auth.py

Shared SMTP authentication for daily_runner.py's send_alert_email() and
interfaces/notifications.py's category emails. Two mechanisms, picked via the
explicit SMTP_PROVIDER env var ("gmail" or "outlook"):

  - gmail (default if SMTP_PROVIDER is unset): classic SMTP AUTH LOGIN with
    SMTP_PASS — a Gmail App Password, not your real password.

  - outlook: XOAUTH2 via an Azure AD app registration — required for
    Outlook.com/Hotmail/Microsoft 365, which reject password-based SMTP AUTH
    outright ("5.7.139 Authentication unsuccessful, basic authentication is
    disabled"). See docs/DEPLOYMENT.md for how to register the app. On first
    use, MSAL's device-code flow prints a URL + one-time code to log in a
    browser; the resulting refresh token is cached in
    data_dir()/ms_oauth_token_cache.json so scheduled/unattended runs
    afterward re-authenticate silently.

SMTP_PROVIDER is explicit (rather than just inferring from which vars happen
to be set) so switching providers doesn't depend on remembering to blank out
the other provider's leftover config — e.g. a stale MS_OAUTH_CLIENT_ID
sitting in .env would otherwise silently force an OAuth2 attempt against a
Gmail account you meant to send through instead.
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
        raise ValueError(f"SMTP_PROVIDER={provider!r} is not supported — must be one of {VALID_PROVIDERS}")
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
        raise RuntimeError("SMTP_PROVIDER=outlook requires MS_OAUTH_CLIENT_ID — see docs/DEPLOYMENT.md")
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


def authenticate(server: smtplib.SMTP, user: str, password: str | None) -> None:
    """Authenticates an already-STARTTLS'd connection, using OAuth2 if configured."""
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
