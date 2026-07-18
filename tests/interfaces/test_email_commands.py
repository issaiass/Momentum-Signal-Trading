"""
tests/test_email_commands.py

Covers security-critical email command parsing: sender
authentication, pydantic validation per command type, the ADJUST_PARAM
allowlist (the single most important security boundary in this module),
and fail-safe behavior on malformed input.

Run with: pytest tests/test_email_commands.py -v
"""
import imaplib
from email.message import EmailMessage

import pytest

import momentum_trading.interfaces.email_commands as email_commands_module
from momentum_trading.interfaces.email_commands import (
    parse_command, build_reply_body, ADJUSTABLE_PARAMS, poll_and_process_commands,
    _is_bot_thread, BOT_SUBJECT_MARKER,
)

TRUSTED = "trader@example.com"


class TestSenderAuthentication:
    """
    The single most important security property of this module: only the
    configured trusted_sender's emails are ever parsed. Anything else must
    be rejected BEFORE reaching command parsing, regardless of how
    well-formed the body looks — a spoofed or compromised third-party
    address sending a perfectly valid-looking PAUSE command must still fail.
    """

    def test_untrusted_sender_rejected_even_with_valid_body(self):
        result = parse_command("attacker@evil.com", TRUSTED, "ACTION: PAUSE\nPORTFOLIO: ALL")
        assert result.success is False
        assert "not the trusted sender" in result.error

    def test_trusted_sender_case_insensitive_match(self):
        result = parse_command("TRADER@EXAMPLE.COM", TRUSTED, "ACTION: RESUME\nPORTFOLIO: p1")
        assert result.success is True  # email addresses are conventionally case-insensitive


class TestSimpleCommands:
    """PAUSE/RESUME/SKIP_NEXT_REBALANCE/TRIGGER_REPORT have no special validation beyond a portfolio name."""

    @pytest.mark.parametrize("action", ["PAUSE", "RESUME", "SKIP_NEXT_REBALANCE", "TRIGGER_REPORT"])
    def test_valid_simple_command_parses(self, action):
        result = parse_command(TRUSTED, TRUSTED, f"ACTION: {action}\nPORTFOLIO: portfolio1")
        assert result.success is True
        assert result.command.action == action
        assert result.command.portfolio == "portfolio1"

    def test_all_portfolios_keyword_accepted(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: PAUSE\nPORTFOLIO: ALL")
        assert result.success is True
        assert result.command.portfolio == "ALL"

    def test_missing_portfolio_rejected(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: PAUSE")
        assert result.success is False


class TestLiquidateExtraFriction:
    """
    LIQUIDATE is the single most destructive command exposed via email —
    these tests confirm it requires an EXACT confirmation phrase, not just
    the presence of the LIQUIDATE action word, and that anything less
    (missing, wrong, or approximate) is rejected.
    """

    def test_missing_confirmation_rejected(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: LIQUIDATE\nPORTFOLIO: p1")
        assert result.success is False

    def test_wrong_confirmation_phrase_rejected(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: LIQUIDATE\nPORTFOLIO: p1\nCONFIRM: yes do it")
        assert result.success is False

    def test_correct_confirmation_phrase_accepted(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: LIQUIDATE\nPORTFOLIO: p1\nCONFIRM: I confirm liquidation")
        assert result.success is True

    def test_confirmation_phrase_case_insensitive(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: LIQUIDATE\nPORTFOLIO: p1\nCONFIRM: i CONFIRM Liquidation")
        assert result.success is True


class TestAdjustParamAllowlist:
    """
    THE critical security boundary of this module: ADJUST_PARAM can only
    touch a small, hard-coded allowlist of fields, each with hard bounds.
    A test failure here would mean an email could alter config fields never
    intended to be remotely adjustable (e.g. initial_capital, commission) —
    this is deliberately tested exhaustively, not just the happy path.
    """

    def test_allowlisted_param_in_bounds_accepted(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: stop_loss_pct\nVALUE: 0.15")
        assert result.success is True
        assert result.command.param_value == 0.15

    def test_non_allowlisted_param_rejected(self):
        # This is THE test that must never regress: initial_capital is not in
        # ADJUSTABLE_PARAMS and must never be settable via email.
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: initial_capital\nVALUE: 999999")
        assert result.success is False
        assert "not adjustable" in result.error

    def test_out_of_bounds_value_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: stop_loss_pct\nVALUE: 5.0")
        assert result.success is False

    def test_non_numeric_value_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: stop_loss_pct\nVALUE: not_a_number")
        assert result.success is False

    def test_all_allowlisted_params_have_valid_bounds_tuples(self):
        # Sanity check on the allowlist definition itself — every entry must
        # be a proper (min, max) tuple with min < max.
        for param, (lo, hi) in ADJUSTABLE_PARAMS.items():
            assert lo < hi, f"{param} has invalid bounds ({lo}, {hi})"

    def test_top_n_in_bounds_accepted(self):
        # top_n joined the allowlist as a real, live-wired
        # concentration lever — same category as the two
        # existing entries (defensive, bounded, safe to tweak mid-day).
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: top_n\nVALUE: 3")
        assert result.success is True
        assert result.command.param_value == 3

    def test_top_n_out_of_bounds_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ADJUST_PARAM\nPORTFOLIO: p1\nPARAM: top_n\nVALUE: 500")
        assert result.success is False


class TestNewCommandsStatusAndDrawdown:
    """
    STATUS (read-only, zero-risk) and SET_MAX_DRAWDOWN (scoped,
    one-directional — can only tighten, never loosen, the circuit breaker).
    The bounds check here only validates the requested value is a sane
    fraction; the "can only tighten vs. current config" enforcement happens
    at application time in daily_runner.py (see get_effective_max_drawdown_pct),
    not in this parsing layer, since parsing doesn't have access to the live
    config to compare against.
    """

    def test_status_command_parses(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: STATUS\nPORTFOLIO: portfolio1")
        assert result.success is True
        assert result.command.action == "STATUS"

    def test_set_max_drawdown_valid_fraction_accepted(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: SET_MAX_DRAWDOWN\nPORTFOLIO: portfolio1\nVALUE: 0.10")
        assert result.success is True
        assert result.command.new_value == 0.10

    def test_set_max_drawdown_out_of_range_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: SET_MAX_DRAWDOWN\nPORTFOLIO: portfolio1\nVALUE: 1.5")
        assert result.success is False

    def test_set_max_drawdown_non_numeric_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: SET_MAX_DRAWDOWN\nPORTFOLIO: portfolio1\nVALUE: not_a_number")
        assert result.success is False


class TestAlertsReportCommand:
    """
    Read-only, zero-risk, mirrors STATUS — these tests
    cover PARSING only (default/explicit LIMIT, bounds enforcement, ALL vs a
    specific portfolio). The actual alert-log READ + email reply is exercised
    end-to-end in tests/test_daily_runner.py, since that's where
    read_recent_alerts() and send_alert_email() are wired together.
    """

    def test_parses_with_default_limit(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: ALERTS_REPORT\nPORTFOLIO: portfolio1")
        assert result.success is True
        assert result.command.action == "ALERTS_REPORT"
        assert result.command.limit == 10

    def test_parses_with_explicit_limit(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ALERTS_REPORT\nPORTFOLIO: portfolio1\nLIMIT: 25")
        assert result.success is True
        assert result.command.limit == 25

    def test_all_portfolios_keyword_accepted(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: ALERTS_REPORT\nPORTFOLIO: ALL")
        assert result.success is True
        assert result.command.portfolio == "ALL"

    def test_limit_above_cap_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ALERTS_REPORT\nPORTFOLIO: portfolio1\nLIMIT: 51")
        assert result.success is False

    def test_limit_below_one_rejected(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ALERTS_REPORT\nPORTFOLIO: portfolio1\nLIMIT: 0")
        assert result.success is False

    def test_non_numeric_limit_rejected_not_raised(self):
        result = parse_command(TRUSTED, TRUSTED,
                                "ACTION: ALERTS_REPORT\nPORTFOLIO: portfolio1\nLIMIT: not_a_number")
        assert result.success is False
        assert "not a valid integer" in result.error


class TestAuditLogging:
    """
    Every parsed attempt — accepted or rejected — must
    be logged to the hash-chained audit trail, not just printed to console.
    This is what makes "who tried to do what, and when" queryable after the
    fact, and (via the hash chain) tamper-evident the same way the trade log is.
    """

    def test_accepted_command_is_logged(self, tmp_path):
        from momentum_trading.interfaces.email_commands import log_command_attempt
        log_path = str(tmp_path / "cmd_log.csv")
        result = parse_command(TRUSTED, TRUSTED, "ACTION: RESUME\nPORTFOLIO: p1")
        log_command_attempt(TRUSTED, result, log_path)

        import pandas as pd
        df = pd.read_csv(log_path)
        assert len(df) == 1
        assert df.iloc[0]["outcome"] == "ACCEPTED"
        assert df.iloc[0]["action"] == "RESUME"

    def test_rejected_command_is_logged_with_reason(self, tmp_path):
        from momentum_trading.interfaces.email_commands import log_command_attempt
        log_path = str(tmp_path / "cmd_log.csv")
        result = parse_command("evil@attacker.com", TRUSTED, "ACTION: PAUSE\nPORTFOLIO: p1")
        log_command_attempt("evil@attacker.com", result, log_path)

        import pandas as pd
        df = pd.read_csv(log_path)
        assert df.iloc[0]["outcome"] == "REJECTED"
        assert df.iloc[0]["sender"] == "evil@attacker.com"
        assert "trusted sender" in df.iloc[0]["reason"]

    def test_log_is_hash_chained_and_tamper_detectable(self, tmp_path):
        from momentum_trading.interfaces.email_commands import log_command_attempt
        from momentum_trading.execution.live_signal import verify_log_integrity
        log_path = str(tmp_path / "cmd_log.csv")

        r1 = parse_command(TRUSTED, TRUSTED, "ACTION: RESUME\nPORTFOLIO: p1")
        log_command_attempt(TRUSTED, r1, log_path)
        r2 = parse_command(TRUSTED, TRUSTED, "ACTION: PAUSE\nPORTFOLIO: p1")
        log_command_attempt(TRUSTED, r2, log_path)

        result = verify_log_integrity(log_path)
        assert result["valid"] is True

        # tamper with a field and confirm detection (reuses the same
        # hash-chain verification already used for the trade log)
        import csv
        with open(log_path) as f:
            rows = list(csv.reader(f))
        rows[1][2] = "TAMPERED_ACTION"
        with open(log_path, "w", newline="") as f:
            csv.writer(f).writerows(rows)

        result_tampered = verify_log_integrity(log_path)
        assert result_tampered["valid"] is False


class TestFailSafeBehavior:
    """
    Nothing here should ever raise an exception, regardless of how malformed
    the input is — an exception in email command parsing could crash the
    daily_runner.py process that's meant to be checking for these commands.
    """

    def test_unknown_action_rejected_not_raised(self):
        result = parse_command(TRUSTED, TRUSTED, "ACTION: DELETE_EVERYTHING\nPORTFOLIO: ALL")
        assert result.success is False
        assert "Unrecognized" in result.error

    def test_completely_garbage_body_rejected_not_raised(self):
        result = parse_command(TRUSTED, TRUSTED, "this is not a command at all")
        assert result.success is False

    def test_empty_body_rejected_not_raised(self):
        result = parse_command(TRUSTED, TRUSTED, "")
        assert result.success is False

    def test_reply_body_generation_never_raises(self):
        ok = parse_command(TRUSTED, TRUSTED, "ACTION: RESUME\nPORTFOLIO: p1")
        bad = parse_command("evil@x.com", TRUSTED, "ACTION: PAUSE\nPORTFOLIO: p1")
        assert len(build_reply_body(ok)) > 0
        assert len(build_reply_body(bad)) > 0
        assert "REJECTED" in build_reply_body(bad)
        assert "ACCEPTED" in build_reply_body(ok)


class TestIsBotThread:
    """_is_bot_thread() is the second of two guards against a same-inbox reply cascade — the
    X-Momentum-Trading-Bot header catches the bot's own generated replies, this catches a
    human's reply to those replies (which never carries the header, but keeps the subject)."""

    def test_marker_present_detected(self):
        assert _is_bot_thread(f"Re: {BOT_SUBJECT_MARKER} Re: something") is True

    def test_marker_absent_not_detected(self):
        assert _is_bot_thread("Re: something entirely unrelated") is False

    def test_empty_subject_safe(self):
        assert _is_bot_thread("") is False


def _raw_email_bytes(subject: str, from_addr: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "bot@example.com"
    msg["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


class _FakeIMAPConnection:
    """Hand-rolled fake standing in for imaplib.IMAP4_SSL — mirrors this project's existing
    style of dependency-injected fakes (e.g. send_reply_fn) rather than introducing a mocking
    library. Serves exactly one message per test, which is all these tests need."""

    def __init__(self, raw_email: bytes):
        self._raw = raw_email
        self.stored_flags = []

    def login(self, user, password):
        pass

    def select(self, mailbox):
        pass

    def search(self, charset, *criteria):
        return "OK", [b"1"]

    def fetch(self, msg_id, parts):
        return "OK", [(b"1 (RFC822 {10})", self._raw)]

    def store(self, msg_id, flag_cmd, flags):
        self.stored_flags.append((msg_id, flag_cmd, flags))

    def close(self):
        pass

    def logout(self):
        pass


class TestReplyCascadeGuard:
    """
    Reproduces the real incident this was built to fix — a same-address
    IMAP_USER/TRUSTED_SENDER_EMAIL setup where ordinary correspondence from that address (e.g.
    a reply to an unrelated thread) gets treated as a failed command attempt and replied to.
    Without a subject-marker guard, a human's reply to THAT reply cascades into a second round
    (subject: "[momentum-trading] Re: [momentum-trading] Re: ..."). These tests prove the
    cascade is capped at exactly one bounce, without suppressing the legitimate first rejection
    — per the requirement that this be provable in tests, not discovered against a real inbox.
    """

    def _run(self, monkeypatch, raw_email: bytes, processed_ids_path: str):
        fake_conn = _FakeIMAPConnection(raw_email)
        monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host: fake_conn)
        logged = []
        monkeypatch.setattr(email_commands_module, "log_command_attempt",
                             lambda sender, result: logged.append((sender, result)))
        replies = []
        results = poll_and_process_commands(
            "imap.example.com", "trader@example.com", "pw", "trader@example.com",
            send_reply_fn=lambda to_addr, subject, body: replies.append((to_addr, subject, body)),
            processed_ids_path=processed_ids_path,
        )
        return results, replies, logged, fake_conn

    def test_reply_to_bot_thread_is_skipped_no_reply_no_log(self, tmp_path, monkeypatch):
        raw = _raw_email_bytes(
            subject=f'Re: {BOT_SUBJECT_MARKER} Re: "Robotics Engineer": recruiter thread',
            from_addr="trader@example.com",
            body="Thanks, not interested.",
        )
        results, replies, logged, conn = self._run(monkeypatch, raw, str(tmp_path / "processed.txt"))

        assert results == []
        assert replies == []
        assert logged == []
        assert conn.stored_flags == [(b"1", "+FLAGS", "\\Seen")]

    def test_first_time_non_command_email_still_rejected_and_replied(self, tmp_path, monkeypatch):
        raw = _raw_email_bytes(
            subject='Re: "Robotics Engineer": recruiter thread',
            from_addr="trader@example.com",
            body="Thanks, not interested.",
        )
        results, replies, logged, conn = self._run(monkeypatch, raw, str(tmp_path / "processed.txt"))

        assert len(results) == 1
        assert results[0].success is False
        assert "Unrecognized or missing ACTION" in results[0].error
        assert len(replies) == 1
        assert len(logged) == 1

    def test_malformed_command_attempt_still_rejected_and_replied(self, tmp_path, monkeypatch):
        raw = _raw_email_bytes(
            subject="My command",
            from_addr="trader@example.com",
            body="ACTION: PUASE\nPORTFOLIO: ALL",
        )
        results, replies, logged, conn = self._run(monkeypatch, raw, str(tmp_path / "processed.txt"))

        assert len(results) == 1
        assert results[0].success is False
        assert len(replies) == 1
        assert len(logged) == 1
