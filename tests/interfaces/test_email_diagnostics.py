"""
tests/interfaces/test_email_diagnostics.py

Covers run_email_diagnostics() (`daily-runner --test-email`): the live SMTP/IMAP
check a user runs once after creating/editing .env on any machine, before trusting cron/--live
with those credentials. Mocks smtplib.SMTP and imaplib.IMAP4_SSL -- no real network involved --
so these tests confirm the pass/fail/skip logic and the targeted remediation hints, not actual
delivery (see docs/DEPLOYMENT.md's "Verify before you trust it" for the real, live check).

Run with: pytest tests/interfaces/test_email_diagnostics.py -v
"""
import imaplib
import smtplib

import pytest

import momentum_trading.interfaces.email_diagnostics as diag

ENV_VARS = [
    "SMTP_PROVIDER", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_TO_EMAIL",
    "IMAP_HOST", "IMAP_USER", "IMAP_PASS", "TRUSTED_SENDER_EMAIL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class _FakeSMTP:
    """Stands in for smtplib.SMTP's context-manager usage in _check_smtp()."""

    def __init__(self, host, port, timeout=15):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        pass


class _FakeIMAP:
    def __init__(self, host):
        pass

    def login(self, user, password):
        pass

    def logout(self):
        pass


def _configure_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "app-password")
    monkeypatch.setenv("ALERT_TO_EMAIL", "alert@example.com")


def _configure_imap(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "bot@example.com")
    monkeypatch.setenv("IMAP_PASS", "app-password")
    monkeypatch.setenv("TRUSTED_SENDER_EMAIL", "trader@example.com")


class TestSmtpCheck:
    def test_missing_vars_reported_skipped_not_failed(self, capsys):
        assert diag._check_smtp() is True
        assert "SKIPPED" in capsys.readouterr().out

    def test_successful_login_and_send_reported_ok(self, monkeypatch, capsys):
        _configure_smtp(monkeypatch)
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
        monkeypatch.setattr(diag, "authenticate_smtp", lambda server, user, password: None)

        assert diag._check_smtp() is True
        assert "OK" in capsys.readouterr().out

    def test_gmail_auth_failure_gives_app_password_hint(self, monkeypatch, capsys):
        _configure_smtp(monkeypatch)
        monkeypatch.setenv("SMTP_PROVIDER", "gmail")
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        def _raise(server, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        monkeypatch.setattr(diag, "authenticate_smtp", _raise)

        assert diag._check_smtp() is False
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "myaccount.google.com/apppasswords" in out

    def test_outlook_auth_failure_gives_oauth_hint(self, monkeypatch, capsys):
        _configure_smtp(monkeypatch)
        monkeypatch.setenv("SMTP_PROVIDER", "outlook")
        monkeypatch.setenv("MS_OAUTH_CLIENT_ID", "some-client-id")
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        def _raise(server, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.139 basic auth disabled")
        monkeypatch.setattr(diag, "authenticate_smtp", _raise)

        assert diag._check_smtp() is False
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "OAuth2" in out


class TestImapCheck:
    def test_missing_vars_reported_skipped_not_failed(self, capsys):
        assert diag._check_imap() is True
        assert "SKIPPED" in capsys.readouterr().out

    def test_successful_login_reported_ok(self, monkeypatch, capsys):
        _configure_imap(monkeypatch)
        monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)

        assert diag._check_imap() is True
        out = capsys.readouterr().out
        assert "OK" in out
        assert "trader@example.com" in out  # echoes TRUSTED_SENDER_EMAIL for eyeballing

    def test_login_failure_reported_failed(self, monkeypatch, capsys):
        _configure_imap(monkeypatch)

        class _FailingIMAP(_FakeIMAP):
            def login(self, user, password):
                raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

        monkeypatch.setattr(imaplib, "IMAP4_SSL", _FailingIMAP)

        assert diag._check_imap() is False
        assert "FAILED" in capsys.readouterr().out


class TestRunEmailDiagnostics:
    def test_all_configured_and_passing_returns_true(self, monkeypatch):
        _configure_smtp(monkeypatch)
        _configure_imap(monkeypatch)
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
        monkeypatch.setattr(diag, "authenticate_smtp", lambda server, user, password: None)
        monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)

        assert diag.run_email_diagnostics() is True

    def test_nothing_configured_returns_true_all_skipped(self):
        # Neither feature configured -- both checks skip, overall result is still "OK" since
        # nothing failed (matches the opt-in, no-noise-if-unused convention used elsewhere).
        assert diag.run_email_diagnostics() is True

    def test_smtp_failure_makes_overall_result_false_even_if_imap_ok(self, monkeypatch):
        _configure_smtp(monkeypatch)
        _configure_imap(monkeypatch)
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        def _raise(server, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")
        monkeypatch.setattr(diag, "authenticate_smtp", _raise)
        monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)

        assert diag.run_email_diagnostics() is False
