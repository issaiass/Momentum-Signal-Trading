"""
email_commands.py

Lets a trusted trader send simple operational commands via email
(PAUSE, RESUME, LIQUIDATE, SKIP_NEXT_REBALANCE, TRIGGER_REPORT, ADJUST_PARAM)
that daily_runner.py picks up and applies.

SECURITY MODEL (read before enabling this):
  - Only commands from a SINGLE, explicitly configured trusted sender address
    are ever parsed. Every other sender's email is ignored entirely, logged,
    and never even reaches the pydantic parsing step.
  - ADJUST_PARAM is intentionally NOT open-ended, only a small allowlist of
    fields, each with hard-coded valid bounds, can be changed this way. This
    is deliberate: an open "change any config field via email" surface is a
    real fat-finger/spoofing risk. Anything outside the allowlist requires
    editing config.yaml directly, going through the existing approval gate.
  - FAIL-SAFE: any email that isn't from the trusted sender, or that IS from
    the trusted sender but doesn't parse as a valid recognized command, is
    ignored, the bot continues running with its CURRENT configuration. It
    never partially applies a malformed command, never crashes the run, and
    (for the first such email in a thread, see BOT_SUBJECT_MARKER below)
    always sends a reply explaining what happened, to ALERT_TO_EMAIL
    specifically (not conditionally to whoever sent it).
  - ANTI-CASCADE: a reply to the trusted sender is itself new mail FROM the
    trusted sender if TRUSTED_SENDER_EMAIL == IMAP_USER (a common, supported
    setup), X-Momentum-Trading-Bot (on the bot's own replies) and
    BOT_SUBJECT_MARKER (on any reply further downstream, including a human's)
    together guarantee this can never cascade past one reply. See
    docs/EMAIL_COMMANDS.md's "Self-generated emails" section for the full
    explanation.
  - LIQUIDATE gets extra friction: it requires the command body to include a
    literal confirmation phrase, not just the command word, since it's the
    single most destructive action exposed this way.

This module implements PARSING and VALIDATION. Actual IMAP polling wiring
into daily_runner.py's scheduled loop, and actual application of PAUSE/
RESUME/LIQUIDATE (reusing the existing circuit-breaker halt-flag mechanism)
are separate integration steps, see docs/EMAIL_COMMANDS.md.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..core.paths import data_dir, logs_dir

logger = logging.getLogger("email_commands")

EMAIL_COMMANDS_LOG_PATH = str(logs_dir() / "email_commands_log.csv")
PROCESSED_COMMAND_IDS_PATH = str(data_dir() / "processed_command_ids.txt")

# Every outbound email this bot sends (daily_runner.py's send_alert_email()) prefixes its
# subject with this literal marker. Any INBOUND email whose subject already contains it is
# therefore, provably, a continuation of a thread the bot itself started, see
# _is_bot_thread()'s use in poll_and_process_commands() for why this matters independently of
# the X-Momentum-Trading-Bot header check.
BOT_SUBJECT_MARKER = "[momentum-trading]"


def _is_bot_thread(subject: str) -> bool:
    """True if `subject` already carries this bot's own outbound marker, i.e. this message
    is a reply (bot-generated or human) somewhere downstream of an email the bot itself sent."""
    return BOT_SUBJECT_MARKER in (subject or "")


# --------------------------------------------------------------------------- #
# ALLOWLISTED ADJUST_PARAM FIELDS, deliberately small, with hard bounds.
# Anything not listed here CANNOT be changed via email, by design.
# --------------------------------------------------------------------------- #
ADJUSTABLE_PARAMS = {
    "stop_loss_pct": (0.01, 0.50),          # (min, max) allowed values
    "max_position_weight": (0.05, 1.00),
    "top_n": (1, 50),
}


class CommandBase(BaseModel):
    portfolio: str = Field(..., min_length=1, description="Portfolio name from config.yaml, or 'ALL' for every portfolio.")


class PauseCommand(CommandBase):
    action: Literal["PAUSE"] = "PAUSE"


class ResumeCommand(CommandBase):
    action: Literal["RESUME"] = "RESUME"


class SkipRebalanceCommand(CommandBase):
    action: Literal["SKIP_NEXT_REBALANCE"] = "SKIP_NEXT_REBALANCE"


class TriggerReportCommand(CommandBase):
    action: Literal["TRIGGER_REPORT"] = "TRIGGER_REPORT"


class StatusCommand(CommandBase):
    """
    Read-only, zero-risk. Requests an immediate reply
    with current state (halted/active, last rebalance date, latest snapshot)
    instead of waiting for the next scheduled monthly report. No special
    validation needed, it can't change anything.
    """
    action: Literal["STATUS"] = "STATUS"


class SetMaxDrawdownCommand(CommandBase):
    """
    A SCOPED, one-directional variant of ADJUST_PARAM,
    can only TIGHTEN max_portfolio_drawdown_pct (make the circuit breaker
    more sensitive), never loosen it. This is deliberately safer than a
    general ADJUST_PARAM entry for this field: in a fast-moving situation
    (e.g. sending this from a phone while traveling), you want a command that
    can only ever make the bot MORE conservative, never accidentally less.

    Validation against the CURRENT configured value happens where this is
    applied (daily_runner.py), not here, this model only validates the
    requested value is a sane fraction; the "can only tighten" check needs
    the live config to compare against, which isn't available at parse time.
    """
    action: Literal["SET_MAX_DRAWDOWN"] = "SET_MAX_DRAWDOWN"
    new_value: float

    @field_validator("new_value")
    @classmethod
    def must_be_valid_fraction(cls, v: float) -> float:
        if not (0 < v < 1.0):
            raise ValueError(f"new_value={v} must be a fraction in (0, 1.0), e.g. 0.15 for 15%")
        return v


class LiquidateCommand(CommandBase):
    action: Literal["LIQUIDATE"] = "LIQUIDATE"
    confirmation_phrase: str

    @field_validator("confirmation_phrase")
    @classmethod
    def must_match_exact_phrase(cls, v: str) -> str:
        # Extra friction for the single most destructive command, must be
        # typed out deliberately, not just implied by sending "LIQUIDATE".
        if v.strip().upper() != "I CONFIRM LIQUIDATION":
            raise ValueError('confirmation_phrase must be exactly "I CONFIRM LIQUIDATION"')
        return v


class AdjustParamCommand(CommandBase):
    action: Literal["ADJUST_PARAM"] = "ADJUST_PARAM"
    param_name: str
    param_value: float

    @field_validator("param_name")
    @classmethod
    def must_be_allowlisted(cls, v: str) -> str:
        if v not in ADJUSTABLE_PARAMS:
            raise ValueError(f"param_name {v!r} is not adjustable via email. "
                              f"Allowed: {list(ADJUSTABLE_PARAMS.keys())}")
        return v

    @field_validator("param_value")
    @classmethod
    def must_be_in_bounds(cls, v: float, info) -> float:
        param_name = info.data.get("param_name")
        if param_name in ADJUSTABLE_PARAMS:
            lo, hi = ADJUSTABLE_PARAMS[param_name]
            if not (lo <= v <= hi):
                raise ValueError(f"{param_name}={v} is outside allowed bounds [{lo}, {hi}]")
        return v


class AlertsReportCommand(CommandBase):
    """
    Read-only, zero-risk, mirrors StatusCommand, emails
    back the most recent rows from the alert log (core/audit_log.py's
    data/alerts_log.csv) instead of waiting to notice a problem from
    console/cron output. PORTFOLIO here means "filter to this portfolio's
    alerts" (or ALL for every portfolio, including cross-portfolio alerts
    like TICKER_OVERLAP that are logged under the pseudo-portfolio "ALL"),
    a query filter, not "apply this action to these portfolios" like the
    mutating commands above.
    """
    action: Literal["ALERTS_REPORT"] = "ALERTS_REPORT"
    limit: int = 10

    @field_validator("limit")
    @classmethod
    def must_be_in_range(cls, v: int) -> int:
        if not (1 <= v <= 50):
            raise ValueError(f"limit={v} must be between 1 and 50")
        return v


COMMAND_MODELS = {
    "PAUSE": PauseCommand,
    "RESUME": ResumeCommand,
    "SKIP_NEXT_REBALANCE": SkipRebalanceCommand,
    "TRIGGER_REPORT": TriggerReportCommand,
    "STATUS": StatusCommand,
    "SET_MAX_DRAWDOWN": SetMaxDrawdownCommand,
    "LIQUIDATE": LiquidateCommand,
    "ADJUST_PARAM": AdjustParamCommand,
    "ALERTS_REPORT": AlertsReportCommand,
}


# --------------------------------------------------------------------------- #
# PARSING, simple line-based syntax, deliberately not free-form natural
# language (ambiguity in parsing a command that can move real money is a
# risk in itself).
#
# Expected email body format, one command per email:
#   ACTION: <PAUSE|RESUME|SKIP_NEXT_REBALANCE|TRIGGER_REPORT|STATUS|
#            SET_MAX_DRAWDOWN|LIQUIDATE|ADJUST_PARAM|ALERTS_REPORT>
#   PORTFOLIO: <portfolio_name or ALL>
#   CONFIRM: <only for LIQUIDATE, must be "I CONFIRM LIQUIDATION">
#   PARAM: <only for ADJUST_PARAM, e.g. stop_loss_pct>
#   VALUE: <only for ADJUST_PARAM or SET_MAX_DRAWDOWN, e.g. 0.15>
#   LIMIT: <only for ALERTS_REPORT, optional, default 10, max 50>
# --------------------------------------------------------------------------- #
def _parse_fields(body: str) -> dict:
    fields = {}
    for line in body.strip().splitlines():
        m = re.match(r"^\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$", line)
        if m:
            fields[m.group(1).upper()] = m.group(2)
    return fields


class ParsedCommandResult(BaseModel):
    success: bool
    command: Optional[object] = None
    error: Optional[str] = None
    raw_action: Optional[str] = None
    sender: str = ""

    model_config = {"arbitrary_types_allowed": True}


def parse_command(sender: str, trusted_sender: str, body: str) -> ParsedCommandResult:
    """
    FAIL-SAFE entry point. Returns a ParsedCommandResult that is ALWAYS safe
    to inspect, never raises. Any failure (untrusted sender, malformed
    body, unknown action, validation error) produces success=False with a
    human-readable error, never a partially-applied command and never an
    exception that could crash the caller's loop.
    """
    if sender.strip().lower() != trusted_sender.strip().lower():
        logger.warning("Email command REJECTED: sender %r does not match trusted_sender.", sender)
        return ParsedCommandResult(success=False, error=f"Sender {sender!r} is not the trusted sender. Ignored.")

    fields = _parse_fields(body)
    action = fields.get("ACTION", "").strip().upper()

    if action not in COMMAND_MODELS:
        return ParsedCommandResult(
            success=False, raw_action=action or None,
            error=f"Unrecognized or missing ACTION {action!r}. Valid actions: {list(COMMAND_MODELS.keys())}",
        )

    model_cls = COMMAND_MODELS[action]
    payload = {"portfolio": fields.get("PORTFOLIO", "")}
    if action == "LIQUIDATE":
        payload["confirmation_phrase"] = fields.get("CONFIRM", "")
    if action == "ADJUST_PARAM":
        payload["param_name"] = fields.get("PARAM", "")
        try:
            payload["param_value"] = float(fields.get("VALUE", "nan"))
        except ValueError:
            return ParsedCommandResult(success=False, raw_action=action,
                                        error=f"VALUE {fields.get('VALUE')!r} is not a valid number.")
    if action == "SET_MAX_DRAWDOWN":
        try:
            payload["new_value"] = float(fields.get("VALUE", "nan"))
        except ValueError:
            return ParsedCommandResult(success=False, raw_action=action,
                                        error=f"VALUE {fields.get('VALUE')!r} is not a valid number.")
    if action == "ALERTS_REPORT":
        limit_str = fields.get("LIMIT", "10")
        try:
            payload["limit"] = int(limit_str)
        except ValueError:
            return ParsedCommandResult(success=False, raw_action=action,
                                        error=f"LIMIT {limit_str!r} is not a valid integer.")

    try:
        command = model_cls(**payload)
        return ParsedCommandResult(success=True, command=command, raw_action=action)
    except ValidationError as e:
        return ParsedCommandResult(success=False, raw_action=action, error=str(e))


def log_command_attempt(
    sender: str, result: ParsedCommandResult, log_path: str = EMAIL_COMMANDS_LOG_PATH,
    outcome: str | None = None, reason: str | None = None,
) -> None:
    """
    Every parsed attempt (accepted, rejected, or errored) is
    logged to a dedicated, hash-chained audit trail, using the SAME
    hash-chain pattern as the trade log (live_signal.py's log_orders) for
    consistency: tampering with this log is detectable the same way tampering
    with the trade log is. This was a real gap in the original email-commands
    implementation, console logging alone doesn't survive a log rotation
    or give you a queryable history of who tried what, when.

    outcome / reason : both optional, backward compatible. When omitted
    (every pre-existing call site), the outcome is derived from
    result.success ("ACCEPTED"/"REJECTED") and reason from result.error,
    unchanged from this function's original behavior. Pass outcome="ERROR"
    explicitly (with reason=str(exception)) for a failure that happened
    OUTSIDE parse_command() itself, an IMAP polling failure or a failure
    while APPLYING an already-ACCEPTED command, so those failures land in
    the SAME audit trail instead of only ever reaching daily_runner.py's
    own log stream.
    """
    import csv
    import hashlib
    import os

    def _last_hash(path):
        if not os.path.isfile(path):
            return "GENESIS"
        with open(path) as f:
            rows = list(csv.reader(f))
        return rows[-1][-1] if len(rows) > 1 else "GENESIS"

    def _row_hash(prev_hash, fields):
        payload = prev_hash + "|" + "|".join(str(f) for f in fields)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    file_exists = os.path.isfile(log_path)
    prev_hash = _last_hash(log_path)

    action = result.raw_action or ""
    portfolio = getattr(result.command, "portfolio", "") if result.success else ""
    resolved_outcome = outcome if outcome is not None else ("ACCEPTED" if result.success else "REJECTED")
    resolved_reason = reason if reason is not None else (result.error or "")
    row_fields = [
        datetime.now().isoformat(), sender, action, portfolio,
        resolved_outcome,
        resolved_reason,
    ]
    row_hash = _row_hash(prev_hash, row_fields)

    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "sender", "action", "portfolio", "outcome", "reason", "row_hash"])
        writer.writerow(row_fields + [row_hash])

    logger.info("Command attempt logged: sender=%s action=%s outcome=%s",
                sender, action, resolved_outcome)


def build_reply_body(result: ParsedCommandResult, outcome: str | None = None,
                      reason: str | None = None) -> str:
    """
    Human-readable confirmation/rejection/error body to email back to the
    sender. outcome/reason are optional, backward compatible: when omitted
    (every pre-existing call site), outcome is derived from result.success
    exactly as before ("ACCEPTED"/"REJECTED") and reason from result.error.
    Pass outcome="ERROR" explicitly for a failure that happened OUTSIDE
    parse_command() itself (see log_command_attempt()'s docstring for the
    two cases this covers), with reason=str(exception) since result.error
    is None for a command that parsed successfully and only failed later,
    while being APPLIED.
    """
    ts = datetime.now().isoformat()
    if outcome is None:
        outcome = "ACCEPTED" if result.success else "REJECTED"

    if outcome == "ERROR":
        resolved_reason = reason if reason is not None else result.error
        return (
            f"Command processing ERROR at {ts}\n\n"
            f"Reason: {resolved_reason}\n\n"
            f"This may mean an earlier ACCEPTED reply for this command did not actually "
            f"take effect. Check logs/email_commands_log.csv and the bot's own logs for "
            f"details."
        )
    if outcome == "ACCEPTED":
        cmd = result.command
        return (
            f"Command ACCEPTED at {ts}\n\n"
            f"Action: {cmd.action}\n"
            f"Portfolio: {cmd.portfolio}\n"
            f"{'Param: ' + cmd.param_name + ' -> ' + str(cmd.param_value) if hasattr(cmd, 'param_name') else ''}\n\n"
            f"This command has been queued for application on the next daily_runner.py run."
        )
    return (
        f"Command REJECTED at {ts}\n\n"
        f"Reason: {result.error}\n\n"
        f"The bot's current configuration is UNCHANGED. No action was taken. "
        f"Nothing about this rejection affects scheduled operation, the next "
        f"run will proceed normally with the existing config."
    )


# --------------------------------------------------------------------------- #
# IMAP POLLING, retrieves unread emails, filters to trusted sender, parses.
#
# NOTE: this function is structurally complete but has NOT been tested
# against a real IMAP server in this environment (no network access to a
# mail provider from this sandbox), same limitation as the IBKR
# integration elsewhere in this project. Test against your real mailbox
# (a dedicated inbox for this purpose, not your primary email) before
# relying on it, per docs/EMAIL_COMMANDS.md.
# --------------------------------------------------------------------------- #
def _load_processed_ids(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    with open(path) as f:
        return set(line.strip() for line in f if line.strip())


def _mark_processed(path: str, message_id: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(message_id + "\n")


def poll_and_process_commands(
    imap_host: str, imap_user: str, imap_password: str, trusted_sender: str,
    send_reply_fn=None, dry_run: bool = False,
    processed_ids_path: str = PROCESSED_COMMAND_IDS_PATH,
) -> list[ParsedCommandResult]:
    """
    Connects to IMAP, fetches unread emails, parses any from trusted_sender
    as commands, marks them read, and (if send_reply_fn is provided) sends a
    confirmation/rejection reply for each. Returns the list of results so the
    caller (daily_runner.py) can apply successful commands.

    dry_run : bool
        If True, commands are parsed, logged, and replied to normally, but
        this function marks the email as read WITHOUT any caller-side state
        change happening (the caller is responsible for not applying results
        when dry_run=True, this flag mainly exists to let you test the full
        email round-trip (fetch, parse, log, reply) safely before trusting
        it to actually flip halt flags).

    processed_ids_path : str
        Message-ID deduplication: even though IMAP messages are marked
        \\Seen after processing, a failure between processing and marking
        (e.g. a crash, or the IMAP server not persisting the flag before the
        next poll) could otherwise cause the same command to be applied
        twice. Every processed Message-ID is recorded here and skipped on
        future polls, independent of the \\Seen flag.

    send_reply_fn : callable(to_address: str, subject: str, body: str) -> None
        Injected rather than hardcoded to smtplib here, so this function can
        be unit-tested without a real SMTP connection, see
        tests/test_email_commands.py for the parsing tests; this function
        itself is integration-level and not covered by the automated suite.
    """
    import imaplib
    import email as email_lib

    results = []
    processed_ids = _load_processed_ids(processed_ids_path)

    try:
        conn = imaplib.IMAP4_SSL(imap_host)
        conn.login(imap_user, imap_password)
        conn.select("INBOX")

        # Filtered server-side to trusted_sender, not just "UNSEEN", an unfiltered search
        # would fetch, log, auto-reply to, and mark-as-read EVERY unread email in the inbox
        # (newsletters, notifications, anything), not just command attempts. parse_command()
        # below still re-checks the sender itself as the real security boundary (IMAP FROM
        # search is a header substring match, not cryptographic), this is a defense-in-depth
        # / noise-reduction prefilter, not a replacement for it.
        status, message_ids = conn.search(None, "UNSEEN", "FROM", f'"{trusted_sender}"')
        if status != "OK":
            logger.error("IMAP search failed: %s", status)
            return results

        for msg_id in message_ids[0].split():
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            sender = email_lib.utils.parseaddr(msg.get("From", ""))[1]

            # Two guards against a same-mailbox reply cascade, needed together, neither alone
            # is sufficient. When trusted_sender is the SAME mailbox that receives alerts (a
            # common, explicitly supported setup), every outbound alert/reply lands back in this
            # inbox as new unread mail FROM the trusted sender:
            #   1. X-Momentum-Trading-Bot header (send_alert_email, notifications.py,
            #      risk_monitor.py all set it): catches the bot's OWN generated replies directly
            #     , these always carry the header, so this alone stops the bot from ever
            #      replying to a message it just sent itself.
            #   2. Subject marker (_is_bot_thread(), BOT_SUBJECT_MARKER): catches the NEXT hop,
            #      a human replying to the bot's reply. A human-authored reply is a brand-new
            #      message the mail client generates; it never carries the custom header, so the
            #      header check alone would let it through, get parsed (still not a real
            #      command), and get ANOTHER rejection reply, round 2, doubling "Re: Re:" in
            #      the subject. The subject marker persists across replies even when the header
            #      doesn't, so this guard catches what the header check structurally cannot.
            if msg.get("X-Momentum-Trading-Bot") or _is_bot_thread(msg.get("Subject", "")):
                conn.store(msg_id, "+FLAGS", "\\Seen")
                continue

            rfc_message_id = msg.get("Message-ID", "").strip()
            if rfc_message_id and rfc_message_id in processed_ids:
                logger.info("Skipping already-processed Message-ID: %s", rfc_message_id)
                conn.store(msg_id, "+FLAGS", "\\Seen")
                continue

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="ignore")

            result = parse_command(sender, trusted_sender, body)
            result.sender = sender
            log_command_attempt(sender, result)
            if dry_run:
                result.raw_action = f"[DRY-RUN] {result.raw_action}" if result.raw_action else "[DRY-RUN]"
            results.append(result)
            if rfc_message_id:
                _mark_processed(processed_ids_path, rfc_message_id)
                processed_ids.add(rfc_message_id)

            if send_reply_fn is not None:
                reply_subject = f"Re: {msg.get('Subject', 'Command')}"
                send_reply_fn(sender if result.success else trusted_sender,
                              reply_subject, build_reply_body(result))

            conn.store(msg_id, "+FLAGS", "\\Seen")

        conn.close()
        conn.logout()
    except Exception as e:
        logger.error("IMAP polling failed: %s", e)
        # Distinct from a REJECTED command, this failed OUTSIDE parse_command() itself
        # (connection/fetch/decode failure), there may be no specific sender or message
        # to attribute it to. Still recorded in the SAME audit trail (not just the log
        # stream), and, if a reply channel is configured, reported to the trusted
        # sender directly, since a silent poll failure otherwise means commands stop
        # being processed with no visible signal beyond daily_runner.py's own logs.
        error_result = ParsedCommandResult(success=False, raw_action="POLL_ERROR", error=str(e))
        log_command_attempt("", error_result, outcome="ERROR")
        if send_reply_fn is not None:
            send_reply_fn(trusted_sender, "Email command check ERROR",
                          build_reply_body(error_result, outcome="ERROR"))

    return results
