#!/usr/bin/env python3
"""
daily_runner.py

Operational wrapper: schedule this ONE script to run daily (cron/Task Scheduler).
- Every day: pulls REAL positions/account value from IBKR (never trusts local
  memory), checks stop-losses, and either flags or auto-executes them per config.
- Only on scheduled rebalance days (is_rebalance_day()): runs the full monthly
  signal + order generation.
- Idempotent: refuses to run a rebalance twice on the same date unless forced.
- Alerts via email on any failure (fetch, IBKR connection, order rejection,
  unhandled exception) so a silent cron failure doesn't go unnoticed.
- Config-driven: portfolio/ticker/risk settings load from config.yaml instead
  of being hardcoded here, so adding portfolios doesn't require editing code.
"""

import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import yaml
import pandas as pd

from .execution.live_signal import (
    is_rebalance_day, is_holding_period_too_frequent, run, run_multi_portfolio,
    get_ibkr_positions, get_ibkr_account_value, with_retry,
    place_orders_ibkr, log_orders, write_portfolio_snapshot, get_latest_snapshot,
    derive_entry_date, measure_live_performance, fetch_ohlcv_for_tickers,
    build_position_performance,
)
from .core.smtp_auth import authenticate as authenticate_smtp, smtp_ready
from .core.audit_log import log_alert, read_recent_alerts, ALERTS_LOG_PATH
from .core.paths import data_dir, logs_dir
from .core.technical_indicators import compute_latest_indicators
from .core.fundamentals import get_cached_or_fetch_fundamentals
from .core.macro_data import get_cached_or_fetch_macro_indicators
from .backtest.momentum_backtest import BacktestConfig
from .risk.circuit_breaker import (
    LOCK_DIR, check_circuit_breaker, resume_trading, get_effective_max_drawdown_pct,
    _halt_flag_path, _peak_equity_path, _skip_next_flag_path, _max_drawdown_override_path,
)
from .interfaces.notifications import (
    NotificationCategory, send_action_email, send_standard_action,
    build_rebalance_summary_html, send_monthly_report, send_daily_report,
)
from .interfaces.email_commands import (
    poll_and_process_commands, PauseCommand, ResumeCommand, LiquidateCommand,
    SkipRebalanceCommand, StatusCommand, SetMaxDrawdownCommand, AlertsReportCommand,
)
from .core import functions_quant_extensions as fnx

logger = logging.getLogger("daily_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# LOCK_DIR is imported from .risk.circuit_breaker above, single source of truth,
# so monkeypatching it in tests (or an env override) affects both modules consistently.


# --------------------------------------------------------------------------- #
# EMAIL ALERTING (item 4)
# --------------------------------------------------------------------------- #
def send_alert_email(subject: str, body: str) -> None:
    """
    Sends an alert via SMTP. Reads credentials from environment variables so
    nothing sensitive is hardcoded:
        SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, ALERT_TO_EMAIL
    If any required var is missing, logs the alert instead of silently failing
    to notify, you should still SEE it in the logs even if email is unconfigured.

    Authentication is password-based (SMTP_PASS) by default, e.g. a Gmail App
    Password. For Outlook.com/Hotmail/Microsoft 365, which no longer accept
    basic auth for SMTP AUTH, set MS_OAUTH_CLIENT_ID (and optionally
    MS_OAUTH_TENANT) instead; SMTP_PASS is then unused. See core/smtp_auth.py
    and docs/DEPLOYMENT.md.
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO_EMAIL")

    if not smtp_ready(host, user, to_addr, password):
        logger.error("SMTP env vars not fully configured, ALERT NOT SENT. Subject: %s | Body: %s", subject, body)
        return

    msg = MIMEText(body)
    msg["Subject"] = f"[momentum-trading] {subject}"
    msg["From"] = user
    msg["To"] = to_addr
    msg["X-Momentum-Trading-Bot"] = "1"

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            authenticate_smtp(server, user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        logger.info("Alert email sent: %s", subject)
    except Exception as e:
        logger.error("Failed to send alert email (%s): %s", subject, e)


# --------------------------------------------------------------------------- #
# IDEMPOTENCY (item 3)
# --------------------------------------------------------------------------- #
def already_ran_today(tag: str = "rebalance") -> bool:
    LOCK_DIR.mkdir(exist_ok=True)
    lock_file = LOCK_DIR / f"last_run_{tag}_{datetime.today().strftime('%Y%m%d')}.lock"
    return lock_file.exists()


def mark_ran_today(tag: str = "rebalance") -> None:
    LOCK_DIR.mkdir(exist_ok=True)
    lock_file = LOCK_DIR / f"last_run_{tag}_{datetime.today().strftime('%Y%m%d')}.lock"
    lock_file.write_text(datetime.now().isoformat())


# --------------------------------------------------------------------------- #
# CONFIG LOADING (item 8)
# --------------------------------------------------------------------------- #
def validate_config_schema(raw: dict, path: str) -> None:
    """
    Validates config.yaml structure BEFORE building BacktestConfig objects, so a
    typo or malformed field fails at load time with a clear message naming the
    offending portfolio/field, not deep inside the daily rebalance loop.
    """
    errors = []

    if "portfolios" not in raw or not isinstance(raw["portfolios"], dict) or not raw["portfolios"]:
        raise ValueError(f"{path}: missing or empty top-level `portfolios:` key.")

    for name, spec in raw["portfolios"].items():
        if not isinstance(spec, dict):
            errors.append(f"portfolios.{name}: must be a mapping (tickers, custom_weights, total_value, risk_overrides), got {type(spec).__name__}")
            continue

        tickers = spec.get("tickers")
        if not isinstance(tickers, list) or not tickers or not all(isinstance(t, str) and t.strip() for t in tickers):
            errors.append(f"portfolios.{name}.tickers: must be a non-empty list of non-empty strings, got {tickers!r}")
            continue  # further checks on this portfolio depend on a valid ticker list

        custom_weights = spec.get("custom_weights")
        if custom_weights is not None:
            if not isinstance(custom_weights, dict) or not custom_weights:
                errors.append(f"portfolios.{name}.custom_weights: must be a non-empty mapping or null, got {custom_weights!r}")
            else:
                unknown = set(custom_weights) - set(tickers)
                if unknown:
                    errors.append(f"portfolios.{name}.custom_weights: keys {sorted(unknown)} are not in this portfolio's tickers list {tickers}")
                total_w = sum(v for v in custom_weights.values() if isinstance(v, (int, float)))
                if not all(isinstance(v, (int, float)) and v >= 0 for v in custom_weights.values()):
                    errors.append(f"portfolios.{name}.custom_weights: all weights must be numbers >= 0, got {custom_weights}")
                elif total_w > 1.0 + 1e-6:
                    errors.append(f"portfolios.{name}.custom_weights: weights sum to {total_w:.4f}, must be <= 1.0")

        total_value = spec.get("total_value")
        if total_value is not None and (not isinstance(total_value, (int, float)) or total_value <= 0):
            errors.append(f"portfolios.{name}.total_value: must be a positive number or null, got {total_value!r}")

        risk_overrides = spec.get("risk_overrides", {})
        if risk_overrides and not isinstance(risk_overrides, dict):
            errors.append(f"portfolios.{name}.risk_overrides: must be a mapping, got {type(risk_overrides).__name__}")

    default_risk = raw.get("default_risk", {})
    if default_risk and not isinstance(default_risk, dict):
        errors.append(f"top-level default_risk: must be a mapping, got {type(default_risk).__name__}")

    # --- At most one portfolio may use total_value: null.
    #     null means "the rest of the account after other portfolios' fixed allocations",
    #     with 2+ null portfolios that's ambiguous (which one gets the
    #     remainder?), and the OLD behavior (each independently pulling the FULL account
    #     value) silently double/triple-counts the same real capital. Fail fast here
    #     rather than let that reach a --live run. ---
    null_portfolios = [
        name for name, spec in raw.get("portfolios", {}).items()
        if isinstance(spec, dict) and spec.get("total_value") is None
    ]
    if len(null_portfolios) > 1:
        errors.append(
            f"portfolios with total_value: null: {sorted(null_portfolios)}, at most ONE "
            f"portfolio may use null (it means \"account value minus every other portfolio's "
            f"total_value\", which is ambiguous with more than one). Assign an explicit "
            f"total_value to all but one of these."
        )

    # --- notifications.send_warning must be a real bool if present.
    #     This field controls whether the capital-safety warnings (over-allocation,
    #     ticker overlap) actually reach you by email, a YAML footgun like
    #     send_warning: "false" (a truthy non-empty string) would otherwise silently
    #     evaluate as "send" via Python's default truthiness, the opposite of what someone
    #     writing that value almost certainly intended. Worth failing loudly specifically
    #     here, more than for most fields, because this one gates whether a real risk
    #     signal reaches you at all. ---
    send_warning = raw.get("notifications", {}).get("send_warning") if isinstance(raw.get("notifications"), dict) else None
    if send_warning is not None and not isinstance(send_warning, bool):
        errors.append(
            f"notifications.send_warning: must be true/false, got {send_warning!r} "
            f"({type(send_warning).__name__}), a non-boolean value here can silently "
            f"enable or disable capital-safety warning emails against your intent."
        )

    if errors:
        raise ValueError(f"Invalid {path}:\n  - " + "\n  - ".join(errors))


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and edit it, "
            f"or pass --config with a different path."
        )
    with open(path) as f:
        raw = yaml.safe_load(f)

    validate_config_schema(raw, path)

    # Build a BacktestConfig per portfolio (or one shared config).
    # BacktestConfig.__post_init__ provides a second layer of validation (numeric
    # range checks on risk parameters) on top of the structural checks above.
    portfolios = {}
    for name, spec in raw["portfolios"].items():
        cfg_overrides = spec.get("risk_overrides", {})
        try:
            cfg = BacktestConfig(**{**raw.get("default_risk", {}), **cfg_overrides})
        except (ValueError, TypeError) as e:
            raise ValueError(f"portfolios.{name}: invalid risk config, {e}") from e
        portfolios[name] = {
            "tickers": spec["tickers"],
            "custom_weights": spec.get("custom_weights"),
            "cfg": cfg,
            "total_value": spec.get("total_value"),  # None = pull real value from IBKR
        }
    raw["portfolios_resolved"] = portfolios
    return raw


# --------------------------------------------------------------------------- #
# STOP-LOSS CHECK, flag or auto-execute per cfg.auto_execute_stop_loss (item 2)
# --------------------------------------------------------------------------- #
def check_and_handle_stop_losses(
    tickers: list, current_positions: dict, latest_prices: dict, cfg: BacktestConfig,
    dry_run: bool, ibkr_port: int, log_path: str, portfolio: str = "",
) -> list:
    flagged = []
    for ticker, pos in current_positions.items():
        entry_price = pos.get("avg_entry_price")
        shares = pos.get("shares", 0)
        if not entry_price or shares <= 0 or ticker not in latest_prices:
            continue
        drawdown = (latest_prices[ticker] - entry_price) / entry_price
        if drawdown <= -cfg.stop_loss_pct:
            logger.warning("STOP-LOSS TRIGGERED: %s down %.1f%% from entry ($%.2f -> $%.2f)",
                            ticker, drawdown * 100, entry_price, latest_prices[ticker])
            log_alert(portfolio, "STOP_LOSS_TRIGGERED", "CRITICAL",
                      f"{ticker} down {drawdown:.1%} from entry (${entry_price:.2f} -> ${latest_prices[ticker]:.2f})",
                      log_path=ALERTS_LOG_PATH)
            flagged.append(ticker)

    if not flagged:
        return []

    if not cfg.auto_execute_stop_loss:
        logger.warning("auto_execute_stop_loss=False: flagged only, no orders placed. Tickers: %s", flagged)
        send_alert_email("Stop-loss(es) flagged (manual review needed)",
                          f"Tickers past stop-loss threshold: {flagged}\nauto_execute_stop_loss is False, review and exit manually.")
        return flagged

    # auto-execute: build SELL orders for the full flagged position
    exit_orders = {
        t: {"action": "SELL", "shares": current_positions[t]["shares"], "reason": "stop-loss auto-exit"}
        for t in flagged
    }
    log_orders(exit_orders, latest_prices, dry_run, path=log_path, cfg=cfg)

    if dry_run:
        logger.info("DRY RUN: stop-loss exits computed but not sent to broker: %s", flagged)
    else:
        logger.warning("AUTO-EXECUTING stop-loss exits via IBKR: %s", flagged)
        fill_results = place_orders_ibkr(exit_orders, port=ibkr_port, portfolio=portfolio,
                                          expected_prices=latest_prices, alerts_log_path=ALERTS_LOG_PATH,
                                          allow_extended_hours=cfg.allow_extended_hours)
        send_alert_email("Stop-loss(es) AUTO-EXECUTED",
                          f"Tickers exited: {flagged}\nFill results: {fill_results}")
    return flagged


# --------------------------------------------------------------------------- #
# TIME-BASED STOP, live-trading equivalent of the backtest's max_holding_days.
# Independent of and in addition to the price-based
# stop-loss above; shares its auto_execute_stop_loss flag rather than adding a
# second config field, since both are "auto-exit on trigger vs. flag only".
# --------------------------------------------------------------------------- #
def check_and_handle_time_stops(
    tickers: list, current_positions: dict, latest_prices: dict, cfg: BacktestConfig,
    dry_run: bool, ibkr_port: int, log_path: str, trade_log_path: str, portfolio: str = "",
) -> list:
    if cfg.max_holding_days is None:
        return []

    flagged = []
    now = pd.Timestamp.now()
    for ticker, pos in current_positions.items():
        shares = pos.get("shares", 0)
        if shares <= 0 or ticker not in latest_prices:
            continue
        entry_date = derive_entry_date(ticker, trade_log_path)
        if entry_date is None:
            continue
        days_held = (now - entry_date).days
        if days_held >= cfg.max_holding_days:
            logger.warning("TIME-STOP TRIGGERED: %s held %d days >= max_holding_days=%d",
                            ticker, days_held, cfg.max_holding_days)
            log_alert(portfolio, "TIME_STOP_TRIGGERED", "CRITICAL",
                      f"{ticker} held {days_held} days >= max_holding_days={cfg.max_holding_days}",
                      log_path=ALERTS_LOG_PATH)
            flagged.append(ticker)

    if not flagged:
        return []

    if not cfg.auto_execute_stop_loss:
        logger.warning("auto_execute_stop_loss=False: flagged only, no orders placed. Tickers: %s", flagged)
        send_alert_email("Time-stop(s) flagged (manual review needed)",
                          f"Tickers past max_holding_days: {flagged}\nauto_execute_stop_loss is False, review and exit manually.")
        return flagged

    # auto-execute: build SELL orders for the full flagged position
    exit_orders = {
        t: {"action": "SELL", "shares": current_positions[t]["shares"], "reason": "time-stop auto-exit"}
        for t in flagged
    }
    log_orders(exit_orders, latest_prices, dry_run, path=log_path, cfg=cfg)

    if dry_run:
        logger.info("DRY RUN: time-stop exits computed but not sent to broker: %s", flagged)
    else:
        logger.warning("AUTO-EXECUTING time-stop exits via IBKR: %s", flagged)
        fill_results = place_orders_ibkr(exit_orders, port=ibkr_port, portfolio=portfolio,
                                          expected_prices=latest_prices, alerts_log_path=ALERTS_LOG_PATH,
                                          allow_extended_hours=cfg.allow_extended_hours)
        send_alert_email("Time-stop(s) AUTO-EXECUTED",
                          f"Tickers exited: {flagged}\nFill results: {fill_results}")
    return flagged


# --------------------------------------------------------------------------- #
# CIRCUIT BREAKER, moved to risk/circuit_breaker.py.
# Thin wrappers here inject send_alert_email so callers in this module don't
# need to pass alert_fn explicitly every time; the underlying logic/state
# lives in the risk module, imported above.
# --------------------------------------------------------------------------- #
def _check_circuit_breaker_with_alert(name: str, total_value: float, cfg: BacktestConfig) -> bool:
    return check_circuit_breaker(name, total_value, cfg, alert_fn=send_alert_email)


def _resume_trading_with_alert(name: str) -> None:
    resume_trading(name, alert_fn=send_alert_email)


def check_and_apply_email_commands(portfolio_names: list[str], ibkr_port: int, dry_run: bool) -> None:
    """
    Polls for commands from the trusted sender and applies the ones that are
    safe to auto-apply (PAUSE/RESUME reuse the existing circuit-breaker halt
    mechanism; SKIP_NEXT_REBALANCE writes a one-time flag; STATUS replies
    immediately; SET_MAX_DRAWDOWN writes a tightening-only override, see
    get_effective_max_drawdown_pct()). LIQUIDATE and ADJUST_PARAM are
    intentionally logged/alerted but NOT auto-applied here, they're
    high-impact enough to warrant a human reviewing the parsed command and
    applying it deliberately (LIQUIDATE via a manual place_orders_ibkr call,
    ADJUST_PARAM via editing config.yaml with the validated value) rather
    than a fully automatic pipeline.

    Requires IMAP_HOST, IMAP_USER, IMAP_PASS, TRUSTED_SENDER_EMAIL env vars.
    Silently does nothing if unconfigured (email commands are opt-in).
    """
    imap_host = os.environ.get("IMAP_HOST")
    imap_user = os.environ.get("IMAP_USER")
    imap_password = os.environ.get("IMAP_PASS")
    trusted_sender = os.environ.get("TRUSTED_SENDER_EMAIL")

    if not all([imap_host, imap_user, imap_password, trusted_sender]):
        return  # email commands not configured, opt-in feature, silent no-op

    if trusted_sender.strip().lower() == imap_user.strip().lower():
        logger.warning(
            "TRUSTED_SENDER_EMAIL is the same address as IMAP_USER, every command poll will "
            "also pick up ordinary mail you send from that address (replies, correspondence, "
            "etc.), each generating one 'not a recognized command' reply. This is safe (see "
            "docs/EMAIL_COMMANDS.md) but noisy; a dedicated inbox for IMAP_USER avoids it entirely."
        )

    def _reply(to_addr, subject, body):
        send_alert_email(subject, body)  # reuses existing SMTP reply path

    results = poll_and_process_commands(imap_host, imap_user, imap_password, trusted_sender,
                                         send_reply_fn=_reply, dry_run=dry_run)

    for result in results:
        if not result.success:
            logger.warning("Email command rejected: %s", result.error)
            continue

        cmd = result.command

        if isinstance(cmd, AlertsReportCommand):
            # Read-only report query, not a per-portfolio action, PORTFOLIO
            # here means "filter to this portfolio" (or ALL for everything),
            # so it's handled once, before the targets expansion loop below
            # (which is for actions APPLIED per portfolio).
            if cmd.portfolio != "ALL" and cmd.portfolio not in portfolio_names:
                logger.warning("ALERTS_REPORT referenced unknown portfolio %r, skipped.", cmd.portfolio)
                continue
            rows = read_recent_alerts(portfolio=cmd.portfolio, limit=cmd.limit,
                                       log_path=ALERTS_LOG_PATH)
            if rows:
                lines = [f"{r['timestamp']} | {r['portfolio']} | {r['severity']} | "
                         f"{r['alert_type']} | {r['message']}" for r in rows]
                report_body = (
                    f"Most recent {len(rows)} alert(s) for '{cmd.portfolio}' "
                    f"as of {datetime.now().isoformat()}:\n\n" + "\n".join(lines)
                )
            else:
                report_body = f"No alerts recorded for '{cmd.portfolio}' as of {datetime.now().isoformat()}."
            send_alert_email(f"ALERTS_REPORT: {cmd.portfolio}", report_body)
            logger.info("ALERTS_REPORT reply sent via email command (portfolio=%s, limit=%d, rows=%d).",
                        cmd.portfolio, cmd.limit, len(rows))
            continue

        targets = portfolio_names if cmd.portfolio == "ALL" else [cmd.portfolio]
        for name in targets:
            if name not in portfolio_names:
                logger.warning("Email command referenced unknown portfolio %r, skipped.", name)
                continue

            if isinstance(cmd, PauseCommand):
                if dry_run:
                    logger.info("[%s] DRY-RUN: would PAUSE via email command (not applied).", name)
                    continue
                LOCK_DIR.mkdir(exist_ok=True)
                _halt_flag_path(name).write_text(f"{datetime.now().isoformat()} | email command: PAUSE")
                logger.info("[%s] PAUSED via email command.", name)
            elif isinstance(cmd, ResumeCommand):
                if dry_run:
                    logger.info("[%s] DRY-RUN: would RESUME via email command (not applied).", name)
                    continue
                _resume_trading_with_alert(name)
            elif isinstance(cmd, SkipRebalanceCommand):
                if dry_run:
                    logger.info("[%s] DRY-RUN: would SKIP next rebalance via email command (not applied).", name)
                    continue
                LOCK_DIR.mkdir(exist_ok=True)
                _skip_next_flag_path(name).write_text(datetime.now().isoformat())
                logger.info("[%s] Next rebalance will be SKIPPED via email command.", name)
            elif isinstance(cmd, StatusCommand):
                # read-only, safe to reply even in dry-run, nothing is applied
                snap = get_latest_snapshot(name)
                halted = _halt_flag_path(name).exists()
                status_body = (
                    f"Status for '{name}' as of {datetime.now().isoformat()}\n\n"
                    f"Circuit breaker halted: {halted}\n"
                    f"Latest snapshot: {snap if snap else 'no snapshot recorded yet'}"
                )
                send_alert_email(f"STATUS: {name}", status_body)
                logger.info("[%s] STATUS reply sent via email command.", name)
            elif isinstance(cmd, SetMaxDrawdownCommand):
                if dry_run:
                    logger.info("[%s] DRY-RUN: would set max_drawdown override to %.2f%% via email "
                               "(not applied).", name, cmd.new_value * 100)
                    continue
                LOCK_DIR.mkdir(exist_ok=True)
                _max_drawdown_override_path(name).write_text(str(cmd.new_value))
                logger.info("[%s] max_portfolio_drawdown_pct override set to %.2f%% via email "
                           "(tightening-only, effective value is min(config, override)).",
                           name, cmd.new_value * 100)
                send_alert_email(
                    f"Drawdown override applied: {name}",
                    f"max_portfolio_drawdown_pct override set to {cmd.new_value:.1%} for '{name}'.\n"
                    f"This can only TIGHTEN the effective threshold, never loosen it, the actual "
                    f"breaker will use whichever of config.yaml's value or this override is smaller.",
                )
            else:
                # LIQUIDATE / ADJUST_PARAM / TRIGGER_REPORT: flagged for manual
                # follow-through rather than auto-applied, see docstring.
                logger.warning("[%s] Email command %s parsed successfully but requires MANUAL "
                               "follow-through (not auto-applied): %s", name, cmd.action, cmd)
                send_alert_email(
                    f"Email command needs manual action: {cmd.action} ({name})",
                    f"Command parsed and validated successfully but is not auto-applied.\n"
                    f"Action: {cmd.action}\nPortfolio: {name}\n\nReview and apply manually.",
                )


# --------------------------------------------------------------------------- #
# MULTI-PORTFOLIO CAPITAL SAFETY, resolved ONCE per run, before the
# per-portfolio loop, so portfolios sharing one real IBKR account can't silently
# double-count or over-allocate the same capital.
# --------------------------------------------------------------------------- #
def resolve_total_values(portfolios: dict, dry_run: bool, account_value_fn=None) -> dict:
    """
    total_value: null means "the rest of the account after every OTHER portfolio's
    fixed total_value", not "the full account", which would silently double-count
    real capital across portfolios sharing one IBKR account. validate_config_schema()
    guarantees at most one portfolio has total_value: null, so this never has to
    choose between multiple candidates.

    account_value_fn : callable() -> float, injected (not called directly) so this
    is unit-testable without a real IBKR connection. Only invoked in --live mode for
    a portfolio that actually needs it (has total_value: null).

    Returns {name: resolved_total_value}. In --live mode, raises ValueError if the
    null portfolio's remainder would be <= 0 (the other portfolios' fixed allocations
    already consume the whole account), proceeding with zero/negative real capital
    is never safe. In dry-run, the null portfolio always gets a flat $1000 placeholder
    (NOT reduced by other portfolios' total_value), dry-run exists to test signal/
    order-generation LOGIC, not to validate real capital math, and there is no real
    account to compute an actual remainder against; forcing dry-run to also enforce
    the remainder check would break dry-run-testing of configs that work fine live
    (e.g. a fixed portfolio's total_value exceeding the $1000 placeholder alone).
    """
    fixed = {name: spec["total_value"] for name, spec in portfolios.items() if spec["total_value"] is not None}
    null_names = [name for name, spec in portfolios.items() if spec["total_value"] is None]
    sum_of_fixed = sum(fixed.values())

    resolved = dict(fixed)
    if null_names:
        null_name = null_names[0]
        if dry_run:
            resolved[null_name] = 1000.0
        else:
            account_value = account_value_fn()
            remainder = account_value - sum_of_fixed
            if remainder <= 0:
                raise ValueError(
                    f"portfolio '{null_name}' (total_value: null) would receive "
                    f"${remainder:,.2f} (account value ${account_value:,.2f} minus other "
                    f"portfolios' fixed total_value ${sum_of_fixed:,.2f}), those portfolios "
                    f"already consume the whole account."
                )
            resolved[null_name] = remainder

    return resolved


def check_ticker_overlap(portfolios: dict) -> dict[str, list[str]]:
    """
    Portfolios sharing one real IBKR account/port that also
    share a ticker will each independently compute and submit orders against the
    SAME real position, with no coordination between them (get_ibkr_positions()
    returns the whole account's positions to every portfolio's loop iteration, and
    generate_orders() only skips tickers it has no price for, a shared ticker has
    a price, so that guard doesn't apply). Deliberately a WARNING, not a blocking
    error, some setups intentionally run different weightings on overlapping
    tickers across portfolios, and forbidding it would break otherwise-valid configs.

    Returns {ticker: [portfolio names holding it]} for every ticker held by 2+
    portfolios; empty dict if there's no overlap.
    """
    ticker_owners: dict[str, list[str]] = {}
    for name, spec in portfolios.items():
        for t in spec["tickers"]:
            ticker_owners.setdefault(t, []).append(name)
    return {t: names for t, names in ticker_owners.items() if len(names) > 1}


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="Runs the momentum strategy daily: always checks stop-losses on real "
                     "positions, and runs a full rebalance only on scheduled rebalance days "
                     "(unless --force-rebalance). Reads portfolio/risk settings from --config.",
        epilog=(
            "Quick reference:\n"
            "  Single or multiple portfolios: both are defined the same way, in config.yaml's\n"
            "  `portfolios:` section, one entry for a single portfolio, several for multiple.\n"
            "  Paper trading:  python daily_runner.py --live --port 7497\n"
            "  Live trading:   python daily_runner.py --live --port 7496 --confirm-live-trading\n"
            "  Dry run (safe, default): python daily_runner.py\n"
            "See docs/RUNNING.md for full walkthroughs of each scenario."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml",
                         help="Path to the YAML file defining portfolios, tickers, custom_weights, "
                              "and risk settings. Defaults to config.yaml in the current directory. "
                              "See config.example.yaml for the expected structure.")
    parser.add_argument("--live", action="store_true",
                         help="Actually place orders through IBKR. Without this flag, the script "
                              "computes and logs orders but NEVER sends them to a broker (dry-run, "
                              "the safe default). Requires TWS/IB Gateway running and reachable on --port.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", 7497)),
                         help="IBKR socket port. 7497 = paper trading TWS (default, safe to experiment). "
                              "7496 = live/real-money TWS, using this ALSO requires --confirm-live-trading. "
                              "4001/4002 are the IB Gateway paper/live equivalents. Verify your own "
                              "TWS/Gateway configuration; these are conventions, not guarantees. Defaults "
                              "from the IBKR_PORT env var if set; this flag always overrides it.")
    parser.add_argument("--confirm-live-trading", action="store_true",
                         help="Required IN ADDITION to --live --port 7496 before any real-money order "
                              "will be placed. This is a deliberate double-confirmation, omitting it "
                              "causes the script to refuse and exit, even with --live set.")
    parser.add_argument("--force-rebalance", action="store_true",
                         help="Run the full rebalance logic even if today is not a scheduled rebalance "
                              "date (per is_rebalance_day()), and even if a rebalance already ran today "
                              "for this config (bypasses the idempotency lock). Use for manual testing; "
                              "do not use routinely in a scheduled/cron context.")
    parser.add_argument("--resume-trading", metavar="PORTFOLIO_NAME",
                         help="Clear an active circuit-breaker halt for the named portfolio (see "
                              "config.yaml's max_portfolio_drawdown_pct) after manual review, and exit. "
                              "Does not run a rebalance in the same invocation, run again normally "
                              "afterward.")
    parser.add_argument("--test-email", action="store_true",
                         help="Live end-to-end check of email setup: a real SMTP login + test send, "
                              "and a real IMAP login if email-commanded remote actions are configured. "
                              "Prints a pass/fail summary and exits, no config.yaml needed, no "
                              "portfolio logic runs. Run this once after creating/editing .env on any "
                              "machine, before trusting cron/--live with those credentials.")
    args = parser.parse_args()

    if args.test_email:
        from .interfaces.email_diagnostics import run_email_diagnostics
        sys.exit(0 if run_email_diagnostics() else 1)

    if args.resume_trading:
        _resume_trading_with_alert(args.resume_trading)
        sys.exit(0)

    if args.live and args.port == 7496 and not args.confirm_live_trading:
        logger.error("Refusing live trading on port 7496 without --confirm-live-trading.")
        sys.exit(1)

    try:
        cfg_raw = load_config(args.config)
    except Exception as e:
        logger.error("Config load failed: %s", e)
        send_alert_email("daily_runner: config load FAILED", str(e))
        sys.exit(1)

    if args.live:
        metadata = cfg_raw.get("metadata", {}) or {}
        if not metadata.get("approved_by") or not metadata.get("approved_date"):
            logger.error(
                "Refusing --live: %s is missing metadata.approved_by / metadata.approved_date. "
                "This config has not been marked as reviewed. Edit those fields once you've "
                "actually reviewed the config.", args.config,
            )
            send_alert_email("daily_runner: LIVE RUN BLOCKED (unapproved config)",
                              f"{args.config} is missing approval metadata; refused to trade live.")
            sys.exit(1)
        logger.info("Config approved by %s on %s.", metadata["approved_by"], metadata["approved_date"])

    portfolios = cfg_raw["portfolios_resolved"]
    # Extracted once here so both capital-safety warnings below can gate their
    # email through notifications.send_warning, the detailed logger.warning() calls
    # right next to each are NEVER gated by this, so the risk is always visible in logs
    # even if the email is filtered out.
    notification_cfg = cfg_raw.get("notifications", {})

    # --- Ticker-overlap warning (non-blocking), portfolios sharing
    #     a ticker on the same real IBKR account would each independently compute and
    #     submit orders against the same position, with no coordination between them. ---
    if len(portfolios) > 1:
        overlap = check_ticker_overlap(portfolios)
        if overlap:
            overlap_desc = "; ".join(f"{t}: {names}" for t, names in overlap.items())
            logger.warning("TICKER OVERLAP across portfolios (independent, uncoordinated orders "
                            "against the same real position if they share an account): %s", overlap_desc)
            log_alert("ALL", "TICKER_OVERLAP", "WARNING", overlap_desc,
                      log_path=ALERTS_LOG_PATH)
            overlap_text = (
                f"The following tickers appear in more than one portfolio in this run:\n"
                f"{overlap_desc}\n\nEach portfolio computes and submits orders independently, "
                f"if they share a real IBKR account, this can result in uncoordinated, "
                f"conflicting orders against the same position. Review if unintentional."
            )
            send_action_email(
                NotificationCategory.WARNING, "Ticker overlap across portfolios",
                f"<pre>{overlap_text}</pre>", notification_cfg, plain_text_fallback=overlap_text,
            )

    # --- Resolve every portfolio's total_value ONCE, before
    #     the loop, total_value: null means "account value minus every OTHER portfolio's
    #     fixed total_value", not "the full account" (the old per-portfolio resolution
    #     silently double-counted the same real capital across portfolios). ---
    def account_value_fn():
        return with_retry(get_ibkr_account_value, 3, 2.0, args.port)

    try:
        resolved_total_values = resolve_total_values(portfolios, dry_run=not args.live,
                                                       account_value_fn=account_value_fn)
    except ValueError as e:
        logger.error("Refusing to run: %s", e)
        log_alert("ALL", "CAPITAL_ALLOCATION_ERROR", "CRITICAL", str(e),
                  log_path=ALERTS_LOG_PATH)
        send_alert_email("daily_runner: capital allocation error", str(e))
        sys.exit(1)

    if args.live and len(portfolios) > 1 and all(s["total_value"] is not None for s in portfolios.values()):
        # No null portfolio, resolve_total_values() had no reason to fetch the real
        # account value, so the remainder<=0 check never ran either. Check
        # separately whether these fully-fixed allocations still add up to more than
        # the account actually has.
        sum_of_fixed = sum(resolved_total_values.values())
        account_value = account_value_fn()
        if sum_of_fixed > account_value:
            shortfall = sum_of_fixed - account_value
            logger.warning("Fixed total_value across portfolios ($%.2f) exceeds real account "
                            "value ($%.2f) by $%.2f.", sum_of_fixed, account_value, shortfall)
            log_alert("ALL", "OVER_ALLOCATION", "WARNING",
                      f"Fixed total_value ${sum_of_fixed:,.2f} exceeds account value ${account_value:,.2f} "
                      f"by ${shortfall:,.2f}.", log_path=ALERTS_LOG_PATH)
            overallocation_text = (
                f"Sum of all portfolios' fixed total_value: ${sum_of_fixed:,.2f}\n"
                f"Real account NetLiquidation: ${account_value:,.2f}\n"
                f"Shortfall: ${shortfall:,.2f}\n\n"
                f"Orders may be rejected or reduced by the broker. Review your portfolios' "
                f"total_value settings."
            )
            send_action_email(
                NotificationCategory.WARNING, "Portfolio allocations exceed account value",
                f"<pre>{overallocation_text}</pre>", notification_cfg,
                plain_text_fallback=overallocation_text,
            )

    # --- Check for and apply email commands (opt-in, silent no-op if unconfigured) ---
    try:
        check_and_apply_email_commands(list(portfolios.keys()), args.port, dry_run=not args.live)
    except Exception as e:
        logger.warning("Email command check failed (non-fatal, continuing with normal run): %s", e)

    # --- Macro indicators (Fed Funds Rate, CPI), fetched ONCE per run, not per-portfolio,
    #     macro conditions apply market-wide, not per-ticker. Cached (see core/macro_data.py),
    #     and the FRED_API_KEY presence check happens inside get_cached_or_fetch_macro_indicators()
    #     before any network attempt, so an unconfigured key costs nothing every run. ---
    macro_indicators = get_cached_or_fetch_macro_indicators(fred_api_key=os.environ.get("FRED_API_KEY"))

    try:
        for name, spec in portfolios.items():
            cfg = spec["cfg"]
            tickers = spec["tickers"]
            trade_log_path = str(logs_dir() / f"live_trades_log_{name}.csv")

            # --- Holding-period-too-frequent warning (non-blocking), holding_period below
            #     0.25 (faster than weekly) is a real, well-defined schedule, just an
            #     economically inadvisable one: the momentum signal is computed over a
            #     monthly-scale lookback_period, so rebalancing faster than weekly adds real
            #     commission/slippage/whole-share drift cost without improving signal quality.
            #     Fires every run (not just rebalance days), same as the ticker-overlap check
            #     below, so a persistent misconfiguration keeps surfacing until fixed. ---
            if is_holding_period_too_frequent(cfg.holding_period):
                logger.warning(
                    "[%s] holding_period=%s is faster than weekly (< 0.25), not recommended. "
                    "The momentum signal is computed over a monthly-scale lookback_period, so "
                    "rebalancing this often adds commission/slippage/whole-share drift cost "
                    "without improving signal quality.", name, cfg.holding_period,
                )
                log_alert(
                    name, "HOLDING_PERIOD_TOO_FREQUENT", "WARNING",
                    f"holding_period={cfg.holding_period} is faster than weekly (< 0.25).",
                    log_path=ALERTS_LOG_PATH,
                )
                holding_period_text = (
                    f"Portfolio '{name}' is configured with holding_period={cfg.holding_period}, "
                    f"faster than weekly (0.25).\n\n"
                    f"This is not recommended: the momentum signal is computed over a "
                    f"monthly-scale lookback_period, so rebalancing faster than weekly adds real "
                    f"commission/slippage/whole-share drift cost without any corresponding "
                    f"improvement in signal quality. This run is proceeding normally, nothing "
                    f"was blocked, but consider setting holding_period to 0.25 (weekly) or "
                    f"higher."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Holding period faster than weekly: {name}",
                    f"<pre>{holding_period_text}</pre>", notification_cfg,
                    plain_text_fallback=holding_period_text,
                )

            # --- item 1: real positions from IBKR, never local memory. total_value comes
            #     from resolved_total_values, resolved once above the loop. ---
            if args.live:
                current_positions = with_retry(get_ibkr_positions, 3, 2.0, args.port)
            else:
                current_positions = {}   # dry-run: no real broker state to reconcile against
            total_value = resolved_total_values[name]

            current_holdings = {t: p["shares"] for t, p in current_positions.items()}

            # --- ALWAYS runs: fetch latest prices once, used by stop-loss check + snapshot ---
            from .execution.live_signal import fetch_live_prices, check_price_staleness
            daily_prices = with_retry(fetch_live_prices, 3, 2.0, tickers)
            latest_prices = daily_prices.iloc[-1].to_dict() if not daily_prices.empty else {}

            # --- Abort THIS portfolio's run (not the whole process)
            #     if the price feed looks stale, trading on frozen data is worse than
            #     skipping a cycle. ---
            if cfg.max_price_staleness_minutes is not None:
                staleness = check_price_staleness(daily_prices, cfg.max_price_staleness_minutes)
                if staleness["is_stale"]:
                    logger.error("[%s] STALE PRICE FEED: latest data is %s days old (expected %s). "
                                 "Skipping this portfolio's run.", name, staleness["staleness_days"],
                                 staleness["most_recent_expected_trading_day"])
                    log_alert(name, "STALE_PRICE_FEED", "CRITICAL",
                              f"Latest data {staleness['staleness_days']} days old "
                              f"(expected {staleness['most_recent_expected_trading_day']}). Run skipped.",
                              log_path=ALERTS_LOG_PATH)
                    send_alert_email(
                        f"Stale price feed detected: {name}",
                        f"Latest available price date: {staleness['latest_available_date']}\n"
                        f"Most recent expected trading day: {staleness['most_recent_expected_trading_day']}\n"
                        f"This portfolio's run was skipped to avoid trading on frozen data.",
                    )
                    continue

            # --- ALWAYS runs: daily stop-loss check ---
            if current_positions:
                check_and_handle_stop_losses(
                    tickers, current_positions, latest_prices, cfg,
                    dry_run=not args.live, ibkr_port=args.port,
                    log_path=trade_log_path, portfolio=name,
                )
                # --- Daily time-based stop check (max_holding_days) ---
                check_and_handle_time_stops(
                    tickers, current_positions, latest_prices, cfg,
                    dry_run=not args.live, ibkr_port=args.port,
                    log_path=trade_log_path, trade_log_path=trade_log_path, portfolio=name,
                )

            # --- ALWAYS runs: portfolio snapshot, independent
            #     of rebalance schedule, so "where do things stand" stays continuous.
            #     Also stores the benchmark price so period returns are computed
            #     automatically on the NEXT run by comparing to this row. ---
            try:
                positions_value = sum(
                    p["shares"] * latest_prices.get(t, 0.0) for t, p in current_positions.items()
                )
                cash_estimate = max(total_value - positions_value, 0.0)
                write_portfolio_snapshot(
                    name, current_positions, latest_prices, total_value, cash_estimate,
                    benchmark_ticker=cfg.regime_benchmark,
                )
            except Exception as e:
                logger.warning("[%s] Portfolio snapshot skipped due to error (non-fatal): %s", name, e)

            # --- Daily report, every day regardless of rebalance schedule, gated by
            #     notifications.send_daily (default False, see docs/EMAIL_REPORTING.md). Checked
            #     BEFORE doing any of the underlying work (OHLCV fetch, indicator computation) so
            #     a portfolio with this off pays zero extra cost for it. ---
            notification_cfg = cfg_raw.get("notifications", {})
            if notification_cfg.get("send_daily", False):
                try:
                    snapshot_path = str(data_dir() / f"portfolio_snapshot_{name}.csv")
                    if os.path.isfile(snapshot_path):
                        daily_snapshot_df = pd.read_csv(snapshot_path, parse_dates=["date"])
                        daily_comparison = fnx.compare_to_benchmark(name)
                        daily_since_inception = fnx.since_inception_performance(name)
                        daily_windows = fnx.daily_window_comparison(name)
                        held_tickers = list(current_positions.keys())
                        daily_indicators = {}
                        daily_fundamentals = {}
                        if held_tickers:
                            ohlcv = fetch_ohlcv_for_tickers(held_tickers)
                            daily_indicators = {t: compute_latest_indicators(df) for t, df in ohlcv.items()}
                            daily_fundamentals = {
                                t: get_cached_or_fetch_fundamentals(
                                    t, fmp_api_key=os.environ.get("FMP_API_KEY"),
                                    eodhd_api_key=os.environ.get("EODHD_API_KEY"),
                                )
                                for t in held_tickers
                            }
                        daily_position_performance = build_position_performance(
                            current_positions, latest_prices, trade_log_path,
                        )
                        try:
                            daily_real_pnl = measure_live_performance(
                                "1970-01-01", datetime.today().strftime("%Y-%m-%d"),
                                latest_prices=latest_prices, log_path=trade_log_path,
                                initial_capital=total_value, dry_run=not args.live,
                            )
                        except FileNotFoundError:
                            daily_real_pnl = None
                        send_daily_report(
                            name, daily_snapshot_df, daily_comparison, notification_cfg,
                            daily_real_pnl, daily_since_inception, daily_windows, daily_indicators,
                            daily_fundamentals, macro_indicators, daily_position_performance,
                        )
                except Exception as e:
                    logger.warning("[%s] Daily report skipped due to error (non-fatal): %s", name, e)

            # --- item 3: idempotent rebalance, item 2 rebalance gate ---
            if args.force_rebalance or is_rebalance_day(holding_period_months=cfg.holding_period):
                if already_ran_today(f"rebalance_{name}") and not args.force_rebalance:
                    logger.info("[%s] Rebalance already ran today, skipping (use --force-rebalance to override).", name)
                    continue

                skip_flag = _skip_next_flag_path(name)
                if skip_flag.exists() and not args.force_rebalance:
                    skip_flag.unlink()  # one-time skip, consumed, not persistent
                    logger.info("[%s] Rebalance SKIPPED this cycle via email command.", name)
                    mark_ran_today(f"rebalance_{name}")  # still counts as "handled" for idempotency
                    continue

                if _check_circuit_breaker_with_alert(name, total_value, cfg):
                    logger.warning("[%s] Skipping rebalance, circuit breaker halted.", name)
                    continue

                logger.info("[%s] Rebalance day, running full signal + order generation.", name)
                orders_result = run(
                    tickers=tickers,
                    current_holdings=current_holdings,
                    total_value=total_value,
                    cfg=cfg,
                    top_n=min(cfg.top_n, len(tickers)),
                    lookback_period=cfg.lookback_period,
                    dry_run=not args.live,
                    ibkr_port=args.port,
                    log_path=trade_log_path,
                    custom_weights=spec["custom_weights"],
                    portfolio=name,
                    alerts_log_path=ALERTS_LOG_PATH,
                )
                mark_ran_today(f"rebalance_{name}")

                # --- STANDARD-category notification (filterable) ---
                if orders_result:
                    send_standard_action(
                        f"Rebalance executed: {name}",
                        build_rebalance_summary_html(name, orders_result, dry_run=not args.live),
                        notification_cfg,
                    )

                # --- Monthly report, on the configured day of month ---
                report_day = notification_cfg.get("monthly_report_day_of_month")
                if report_day and datetime.today().day == report_day:
                    snapshot_path = str(data_dir() / f"portfolio_snapshot_{name}.csv")
                    if os.path.isfile(snapshot_path):
                        snapshot_df = pd.read_csv(snapshot_path, parse_dates=["date"])
                        comparison = fnx.compare_to_benchmark(name)
                        since_inception = fnx.since_inception_performance(name)
                        window_comparison = fnx.monthly_window_comparison(name)
                        held_tickers = list(current_positions.keys())
                        indicators = {}
                        fundamentals = {}
                        if held_tickers:
                            ohlcv = fetch_ohlcv_for_tickers(held_tickers)
                            indicators = {t: compute_latest_indicators(df) for t, df in ohlcv.items()}
                            fundamentals = {
                                t: get_cached_or_fetch_fundamentals(
                                    t, fmp_api_key=os.environ.get("FMP_API_KEY"),
                                    eodhd_api_key=os.environ.get("EODHD_API_KEY"),
                                )
                                for t in held_tickers
                            }
                        position_performance = build_position_performance(
                            current_positions, latest_prices, trade_log_path,
                        )
                        # --- REAL realized+unrealized P&L from the trade log (FIFO),
                        #     distinct from the snapshot-based unrealized_pnl already in the
                        #     report, this covers cumulative gains from trades that have since
                        #     closed, not just currently-open positions. dry_run=not args.live
                        #     filters out any dry-run rows sharing this same log file. ---
                        try:
                            real_pnl = measure_live_performance(
                                "1970-01-01", datetime.today().strftime("%Y-%m-%d"),
                                latest_prices=latest_prices,
                                log_path=trade_log_path,
                                initial_capital=total_value,
                                dry_run=not args.live,
                            )
                        except FileNotFoundError:
                            real_pnl = None
                        send_monthly_report(
                            name, snapshot_df, comparison, notification_cfg, real_pnl,
                            since_inception, window_comparison, indicators,
                            fundamentals, macro_indicators, position_performance,
                        )
            else:
                logger.info("[%s] Not a rebalance day, stop-loss check complete only.", name)

    except Exception as e:
        logger.exception("Unhandled exception in daily_runner")
        send_alert_email("daily_runner: UNHANDLED EXCEPTION", f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
