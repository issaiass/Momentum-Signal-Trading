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
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import yaml
import pandas as pd

from .execution.live_signal import (
    is_rebalance_day, is_holding_period_too_frequent, is_lookback_period_too_short,
    is_lookback_shorter_than_holding, is_lookback_to_holding_ratio_too_low,
    compute_turnover, is_turnover_too_high,
    compute_low_capital_drop_fraction, is_low_capital_drop_too_high,
    most_recent_rebalance_target_date,
    run, run_multi_portfolio,
    get_ibkr_positions, get_ibkr_account_value, with_retry,
    place_orders_ibkr, log_orders, write_portfolio_snapshot, get_latest_snapshot,
    derive_entry_date, measure_live_performance, fetch_ohlcv_for_tickers,
    build_position_performance, reconstruct_dry_run_positions, derive_own_live_positions,
    resolve_ticker_stop_loss_pct,
)
from .core.smtp_auth import authenticate as authenticate_smtp, smtp_ready, connect as smtp_connect, send_with_retry
from .core.audit_log import (
    log_alert, read_recent_alerts, ALERTS_LOG_PATH,
    compute_retention_window_days, rotate_hash_chained_log, rotate_plain_log,
)
from .core.paths import data_dir, logs_dir
from .core.technical_indicators import compute_latest_indicators
from .core.fundamentals import get_cached_or_fetch_fundamentals
from .core.macro_data import get_cached_or_fetch_macro_indicators
from .backtest.momentum_backtest import BacktestConfig
from .risk.circuit_breaker import (
    LOCK_DIR, check_circuit_breaker, resume_trading, get_effective_max_drawdown_pct,
    _halt_flag_path, _peak_equity_path, _skip_next_flag_path, _max_drawdown_override_path,
    compute_account_wide_drawdown, ACCOUNT_WIDE_PEAK_NAME,
)
from .interfaces.notifications import (
    NotificationCategory, send_action_email, send_standard_action,
    build_rebalance_summary_html, build_no_action_summary_html, build_signal_universe_html,
    send_monthly_report, send_daily_report,
)
from .interfaces.email_commands import (
    poll_and_process_commands, PauseCommand, ResumeCommand, LiquidateCommand,
    SkipRebalanceCommand, StatusCommand, SetMaxDrawdownCommand, AlertsReportCommand,
    log_command_attempt, build_reply_body, EMAIL_COMMANDS_LOG_PATH,
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

    def _do_send():
        with smtp_connect(host, port) as server:
            authenticate_smtp(server, user, password)
            server.sendmail(user, [to_addr], msg.as_string())

    try:
        send_with_retry(_do_send)
        logger.info("Alert email sent: %s", subject)
    except Exception as e:
        logger.error("Failed to send alert email (%s): %s", subject, e)


# --------------------------------------------------------------------------- #
# IDEMPOTENCY (item 3)
# --------------------------------------------------------------------------- #
def already_ran_today(tag: str = "rebalance", as_of=None) -> bool:
    """
    as_of : date | None, optional, backward compatible. Every pre-existing call site (no as_of
    passed) checks TODAY's own lock file, unchanged. Pass a specific date to check whether a
    PAST date's lock file exists instead, used by the missed-rebalance-day check to ask "was
    there a lock file for the date that got missed", not just today.
    """
    LOCK_DIR.mkdir(exist_ok=True)
    day = as_of if as_of is not None else datetime.today().date()
    lock_file = LOCK_DIR / f"last_run_{tag}_{day.strftime('%Y%m%d')}.lock"
    return lock_file.exists()


def has_run_on_or_after(tag: str, since_date) -> bool:
    """
    True if ANY rebalance lock file for this tag is dated on or after since_date, not just an
    exact match. Used by the missed-rebalance-day check: an exact-date match
    (already_ran_today(as_of=...)) would keep warning forever after a manual catch-up, since a
    --force-rebalance run always marks TODAY's own date, never retroactively marks the missed
    period's original target date. This answers the real question, "has ANYTHING run to handle
    this period since it was missed", so a manual --force-rebalance catch-up correctly clears
    the warning on the next run instead of nagging indefinitely.
    """
    LOCK_DIR.mkdir(exist_ok=True)
    for lock_file in LOCK_DIR.glob(f"last_run_{tag}_*.lock"):
        date_str = lock_file.stem.rsplit("_", 1)[-1]
        try:
            lock_date = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            continue  # unexpected filename shape, ignore rather than crash the check
        if lock_date >= since_date:
            return True
    return False


def mark_ran_today(tag: str = "rebalance") -> None:
    LOCK_DIR.mkdir(exist_ok=True)
    lock_file = LOCK_DIR / f"last_run_{tag}_{datetime.today().strftime('%Y%m%d')}.lock"
    lock_file.write_text(datetime.now().isoformat())


def _rebalance_in_progress_marker_path(name: str) -> Path:
    return LOCK_DIR / f"rebalance_in_progress_{name}.marker"


def _write_rebalance_in_progress_marker(name: str) -> None:
    """
    Written immediately BEFORE run() is called for a rebalance, cleared immediately after
    (success or a handled exception, see the try/finally at the call site). A marker still
    present on a LATER run means a previous process crashed mid-rebalance, purely a visibility
    signal (the stale-marker WARNING near the top of the per-portfolio loop), the diff-based
    order generation this project already uses makes a retry safe against duplicating completed
    actions on its own, this doesn't change that.

    Written atomically (temp file + os.replace()) rather than the plain write_text() this
    project's other flag files use, since this one specifically exists to be read reliably even
    by a concurrently-running process: risk_monitor.py's independent hourly cron CAN overlap
    daily_runner.py's run under the default Docker schedule (confirmed by reading
    docker-entrypoint.sh's cron expressions), though risk_monitor.py itself never actually reads
    this particular file, it stays fully independent, same principle as the six other risk
    constraints it's deliberately blind to.
    """
    LOCK_DIR.mkdir(exist_ok=True)
    path = _rebalance_in_progress_marker_path(name)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(datetime.now().isoformat())
    os.replace(tmp_path, path)


def _clear_rebalance_in_progress_marker(name: str) -> None:
    _rebalance_in_progress_marker_path(name).unlink(missing_ok=True)


def _classify_orphaned_tickers(current_holdings: dict, tickers: list[str],
                                trade_log_path: str) -> tuple[list[str], list[str]]:
    """
    Partitions tickers in current_holdings (the REAL, unfiltered whole-account positions from
    IBKR) that are NOT in this portfolio's configured tickers: list into two groups:

    confirmed_orphaned : this portfolio's OWN trade log (derive_entry_date(), the same
    function check_and_handle_time_stops() already relies on) shows an open BUY history for
    the ticker, it was legitimately held here before being removed from config, safe to price
    and let the normal diff-based rotation logic reconcile (sell if not re-selected).

    unrecognized : not confirmed by this portfolio's own log. Could belong to a SIBLING
    portfolio sharing the same real IBKR account (config.yaml supports multiple portfolios on
    one account, get_ibkr_positions() returns the WHOLE account unfiltered to every one of
    them, this is the documented multi-portfolio ticker-leakage scenario in README.md's Known
    Gaps). MUST NOT be auto-priced or auto-traded, stays exactly as conservative as today
    (HOLD, no live price available). Known, acceptable failure direction: if this portfolio's
    own trade log was ever archived/reset, or the position shows currently flat in the log
    despite real shares being held, a legitimately-owned ticker could also land here, that's
    the SAFE direction to fail (more conservative, not less).
    """
    configured = set(tickers)
    confirmed_orphaned = []
    unrecognized = []
    for t in current_holdings:
        if t in configured:
            continue
        if derive_entry_date(t, trade_log_path) is not None:
            confirmed_orphaned.append(t)
        else:
            unrecognized.append(t)
    return confirmed_orphaned, unrecognized


def _compute_scoped_positions_value(current_positions: dict, latest_prices: dict,
                                     tickers: list[str], confirmed_orphaned: list[str]) -> float:
    """
    Real market value of just THIS portfolio's own positions (its configured tickers plus any
    confirmed_orphaned ones, see _classify_orphaned_tickers()), an EXPLICIT set intersection,
    not the pre-existing positions_value/write_portfolio_snapshot() computation's implicit
    (price-availability-only) scoping, which double-counts a ticker legitimately shared
    between two portfolios under the documented TICKER OVERLAP scenario
    (check_ticker_overlap()). Used only for the total_value drift warning, deliberately not
    reused for the pre-existing positions_value/snapshot computation, that's a separate,
    out-of-scope fix.
    """
    scoped = set(tickers) | set(confirmed_orphaned)
    return sum(
        p["shares"] * latest_prices[t]
        for t, p in current_positions.items()
        if t in scoped and t in latest_prices
    )


def scope_overlapping_holdings(
    current_positions: dict, tickers: list[str], overlap: dict[str, list[str]],
    trade_log_path: str, portfolio: str,
) -> tuple[dict, list[str]]:
    """
    Real, confirmed incident fix (2026-07-16: portfolio1's rebalance sold portfolio2's real
    TLT/XLE/XLF shares). get_ibkr_positions() returns the WHOLE real account's positions,
    unfiltered, to every portfolio's loop iteration; generate_orders() computes drift against
    whatever current_holdings it's handed with zero per-portfolio attribution, so for a ticker
    configured in >1 portfolio sharing this account, one portfolio's rebalance could (and did)
    see a SIBLING portfolio's legitimately-held shares as its own over-allocation and sell them
    down. This caps that at the source, BEFORE current_holdings is built or used anywhere.

    For every ticker BOTH in this portfolio's own `tickers` list AND present in `overlap` (i.e.
    check_ticker_overlap()'s {ticker: [portfolio names]} map, shared with >=1 sibling), caps
    `shares` at min(broker_reported_shares, this_portfolio's_own_derive_own_live_positions()
    shares), and substitutes this portfolio's own FIFO avg_entry_price for that ticker (the
    broker's avgCost is a blended cost basis across ALL shares including a sibling's, which
    would also corrupt stop-loss threshold accuracy for that ticker otherwise). The broker is
    ground truth for EXISTENCE (this portfolio's own claim can never exceed it); this
    portfolio's own trade log is ground truth for ATTRIBUTION (a sibling's shares can never get
    misattributed here). A ticker with zero FIFO history for this portfolio scopes to 0 even if
    the broker shows a large combined position, the safe failure direction, symmetric with
    _classify_orphaned_tickers()'s existing "unrecognized -> untouched" philosophy. A
    non-overlapping ticker passes through completely untouched, no unnecessary FIFO
    substitution for a ticker that was never actually shared.

    Returns (scoped_current_positions, capped_tickers), capped_tickers backing the
    OVERLAPPING_TICKER_SCOPED alert (fired only when this actually did something this run).
    """
    scoped = dict(current_positions)
    capped = []
    configured = set(tickers)
    tickers_needing_scoping = [t for t in current_positions if t in configured and t in overlap]
    if not tickers_needing_scoping:
        return scoped, capped

    own_positions = derive_own_live_positions(trade_log_path)
    for t in tickers_needing_scoping:
        own = own_positions.get(t, {"shares": 0.0, "avg_entry_price": 0.0})
        broker_shares = current_positions[t]["shares"]
        own_shares = own["shares"]
        if own_shares < broker_shares:
            scoped[t] = {"shares": own_shares, "avg_entry_price": own["avg_entry_price"]}
            capped.append(t)
    return scoped, capped


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

    # --- account_wide_max_drawdown_pct is account-scoped, not per-portfolio, deliberately a
    #     TOP-LEVEL field, not inside default_risk/risk_overrides (a per-portfolio override of
    #     an account-wide concept would be incoherent, which portfolio's override would win?).
    #     0.0 (default, absent) = disabled, matches max_portfolio_drawdown_pct's convention. ---
    account_wide_max_drawdown_pct = raw.get("account_wide_max_drawdown_pct", 0.0)
    if not isinstance(account_wide_max_drawdown_pct, (int, float)) or isinstance(account_wide_max_drawdown_pct, bool) \
            or not (0 <= account_wide_max_drawdown_pct < 1.0):
        errors.append(
            f"account_wide_max_drawdown_pct: must be a number in [0, 1.0), got "
            f"{account_wide_max_drawdown_pct!r}, 0 disables it (the default)."
        )

    # --- total_value: null is valid for ANY number of portfolios (zero, one, or several).
    #     null means "an equal share of the account remainder after every fixed portfolio's
    #     allocation", resolve_total_values() splits the remainder equally across every null
    #     portfolio (its own hard remainder<=0 check still fires if fixed portfolios already
    #     consume the whole account, regardless of how many null portfolios would share it).
    #     No schema-level restriction needed here anymore, see resolve_total_values() for the
    #     split logic itself. ---

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

    # --- notifications.send_email_command_feedback must be a real bool if present, same
    #     YAML-truthiness footgun as send_warning above, this one gates the
    #     ACCEPTED/REJECTED/ERROR reply emails for email-commanded remote actions. ---
    send_email_command_feedback = (
        raw.get("notifications", {}).get("send_email_command_feedback")
        if isinstance(raw.get("notifications"), dict) else None
    )
    if send_email_command_feedback is not None and not isinstance(send_email_command_feedback, bool):
        errors.append(
            f"notifications.send_email_command_feedback: must be true/false, got "
            f"{send_email_command_feedback!r} ({type(send_email_command_feedback).__name__}), "
            f"a non-boolean value here can silently enable or disable email-command feedback "
            f"replies against your intent."
        )

    if errors:
        raise ValueError(f"Invalid {path}:\n  - " + "\n  - ".join(errors))


# Preset field bundles per strategy_type (docs/MOMENTUM_STRATEGIES.md, "Selectable Momentum
# Strategy Types" plan), only for the strategies that map onto EXISTING BacktestConfig fields.
# "momentum"/"relative_momentum" (the base signal, aliases of each other) and every strategy
# needing genuinely new ranking logic (multi_timeframe_composite, absolute_momentum,
# residual_momentum, path_dependent_momentum, hybrid_multi_factor) have no bundle here, they're
# dispatched directly by core/strategy_signals.py's resolve_strategy_scores() router instead.
STRATEGY_TYPE_PRESETS = {
    "dual_momentum": {"use_absolute_momentum": True, "use_regime_filter": True},
    "volatility_scaled_momentum": {"sizing_method": "inverse_vol"},
    "correlation_weighted_momentum": {"use_correlation_penalty": True},
    "rank_sign_momentum": {"sizing_method": "equal_weight"},
}


def apply_strategy_type_preset(merged: dict) -> dict:
    """
    Expands merged["strategy_type"] (default_risk + risk_overrides already merged, BEFORE
    BacktestConfig construction) into its preset field bundle, per STRATEGY_TYPE_PRESETS above.
    An explicit field value already present in `merged` (the portfolio's own config set it
    directly) always wins, the preset only fills in keys that are genuinely absent, this is
    where "explicit user value always wins over the implied preset" is actually enforced, at the
    dict level, before BacktestConfig ever sees it, no dataclass-level sentinel tracking needed.

    Returns a NEW dict, does not mutate `merged`.
    """
    strategy_type = merged.get("strategy_type", "momentum")
    preset = STRATEGY_TYPE_PRESETS.get(strategy_type, {})
    result = dict(merged)
    for key, value in preset.items():
        if key not in result:
            result[key] = value
    return result


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
        merged = apply_strategy_type_preset({**raw.get("default_risk", {}), **cfg_overrides})
        try:
            cfg = BacktestConfig(**merged)
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
        ticker_stop_loss_pct = resolve_ticker_stop_loss_pct(ticker, cfg)
        if ticker_stop_loss_pct is None:
            continue  # stop-loss disabled for this ticker (ticker_risk_overrides), never checked
        drawdown = (latest_prices[ticker] - entry_price) / entry_price
        if drawdown <= -ticker_stop_loss_pct:
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


def check_account_wide_drawdown_breaker(
    portfolio_names: list[str], current_account_value: float, max_drawdown_pct: float, alert_fn=None,
) -> bool:
    """
    Account-wide (Recommended tier, docs/RISK_CONSTRAINTS.md) circuit breaker, distinct from
    check_circuit_breaker()'s existing PER-PORTFOLIO one: tracks ONE peak equity for the SUM of
    every portfolio's resolved capital (current_account_value, the caller passes
    sum(resolve_total_values(...).values()), not a second real-account-value fetch), and when
    the drawdown from that peak exceeds max_drawdown_pct, halts EVERY portfolio in
    portfolio_names at once.

    Reuses the EXISTING per-portfolio halt-flag mechanism (writes circuit_breaker_halted_<name>.flag
    for each name, the exact file the rebalance gate already checks via check_circuit_breaker()),
    no new gating code path needed downstream, and resuming a portfolio reuses the EXISTING
    resume_trading(name), no new resume mechanism.

    Peak-equity persistence is separate from every per-portfolio peak (a distinct file, keyed by
    ACCOUNT_WIDE_PEAK_NAME), so resuming one portfolio does NOT reset the account-wide peak, an
    intentional, conservative design: if the account's real capital hasn't actually recovered
    above the tripped threshold, this breaker will re-trip and re-halt every portfolio again on
    the next run, even ones just individually resumed. This is a genuine capital-preservation
    kill-switch property, not a bug, see docs/RISK_CONSTRAINTS.md. Delete
    data/peak_equity___account__.txt manually (no code path does this automatically) to force a
    fresh account-wide peak baseline despite an unrecovered loss, if that's a deliberate,
    reviewed decision.

    max_drawdown_pct <= 0 disables this entirely (matches max_portfolio_drawdown_pct's exact
    convention), the default.
    """
    if max_drawdown_pct <= 0:
        return False

    LOCK_DIR.mkdir(exist_ok=True)
    peak_path = _peak_equity_path(ACCOUNT_WIDE_PEAK_NAME)
    prior_peak = float(peak_path.read_text()) if peak_path.exists() else current_account_value
    new_peak = max(prior_peak, current_account_value)
    peak_path.write_text(str(new_peak))

    result = compute_account_wide_drawdown(current_account_value, new_peak)
    if result["drawdown_pct"] > -max_drawdown_pct:
        return False

    for name in portfolio_names:
        _halt_flag_path(name).write_text(f"{datetime.now().isoformat()} | account-wide circuit breaker")

    reason = (f"Account-wide drawdown {result['drawdown_pct']:.1%} <= -{max_drawdown_pct:.1%} "
              f"(peak account value ${new_peak:,.2f}, current ${current_account_value:,.2f}).")
    logger.warning("ACCOUNT-WIDE CIRCUIT BREAKER TRIPPED: %s Halting rebalances for ALL portfolios: %s.",
                    reason, portfolio_names)
    log_alert("ALL", "ACCOUNT_WIDE_CIRCUIT_BREAKER_TRIPPED", "CRITICAL", reason, log_path=ALERTS_LOG_PATH)
    if alert_fn:
        alert_fn(
            "ACCOUNT-WIDE CIRCUIT BREAKER TRIPPED",
            f"{reason}\n\nEvery portfolio sharing this account has been HALTED: "
            f"{', '.join(portfolio_names)}.\n\n"
            f"Review before resuming each individually with daily-runner --resume-trading <name>.",
        )
    return True


def _check_account_wide_drawdown_breaker_with_alert(
    portfolio_names: list[str], current_account_value: float, max_drawdown_pct: float,
) -> bool:
    return check_account_wide_drawdown_breaker(
        portfolio_names, current_account_value, max_drawdown_pct, alert_fn=send_alert_email,
    )


def _rotate_log_with_alert(name: str, log_path: str, cutoff: pd.Timestamp, plain: bool = False,
                            timestamp_col: str = "timestamp") -> None:
    """
    Thin wrapper around core/audit_log.py's rotate_hash_chained_log()/rotate_plain_log(): fires
    a LOG_ROTATED INFO alert (log_alert(), same triple-step pattern as every other advisory
    check in this file) whenever a rotation actually moved rows, so a future drop in a log's row
    count is self-explanatory in the audit trail rather than a mystery. See
    docs/LOG_RETENTION.md.
    """
    rotate_fn = rotate_plain_log if plain else rotate_hash_chained_log
    result = rotate_fn(log_path, cutoff.to_pydatetime(), timestamp_col=timestamp_col)
    if result.get("rotated"):
        logger.info("[%s] Rotated %s: archived %d rows to %s (%d rows kept)",
                    name, log_path, result["archived_rows"], result["archive_path"], result["kept_rows"])
        log_alert(name, "LOG_ROTATED", "INFO",
                  f"Archived {result['archived_rows']} rows from {os.path.basename(log_path)} "
                  f"to {os.path.basename(result['archive_path'])} ({result['kept_rows']} rows kept)",
                  log_path=ALERTS_LOG_PATH)


def apply_portfolio_log_retention(name: str, cfg: BacktestConfig, trade_log_path: str,
                                   signal_rankings_log_path: str) -> None:
    """
    Time-based log retention (BacktestConfig.enable_log_retention, opt-in, default False is a
    complete no-op). Archives (never deletes) rows older than
    compute_retention_window_days(lookback_period, holding_period) out of this portfolio's own
    trade log, signal rankings log, and portfolio snapshot, once per run, cheap no-op on every
    run nothing is old enough to move (see core/audit_log.py's rotate_hash_chained_log()/
    rotate_plain_log()). Shared logs (alerts_log.csv, email_commands_log.csv) are NOT rotated
    here, they don't belong to any single portfolio, see main()'s post-loop shared-log rotation
    instead. See docs/LOG_RETENTION.md.
    """
    if not cfg.enable_log_retention:
        return
    cutoff = pd.Timestamp.now() - pd.Timedelta(
        days=compute_retention_window_days(cfg.lookback_period, cfg.holding_period))
    _rotate_log_with_alert(name, trade_log_path, cutoff)
    _rotate_log_with_alert(name, signal_rankings_log_path, cutoff)
    snapshot_path = str(data_dir() / f"portfolio_snapshot_{name}.csv")
    _rotate_log_with_alert(name, snapshot_path, cutoff, plain=True, timestamp_col="date")


def apply_shared_log_retention(portfolios: dict) -> None:
    """
    alerts_log.csv/email_commands_log.csv are shared across every portfolio (see
    docs/ALERT_LOG.md), so they can't be rotated per-portfolio the way
    apply_portfolio_log_retention() rotates trade/signal-rankings/snapshot logs. Rotated using
    the LARGEST resolved retention window across every portfolio with
    enable_log_retention=True (the conservative choice: guarantees at least as much history is
    kept as every opted-in portfolio individually needs). Entirely skipped (no-op) if zero
    portfolios opt in, byte-identical to before this feature existed. See docs/LOG_RETENTION.md.
    """
    windows = [
        compute_retention_window_days(spec["cfg"].lookback_period, spec["cfg"].holding_period)
        for spec in portfolios.values() if spec["cfg"].enable_log_retention
    ]
    if not windows:
        return
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=max(windows))
    _rotate_log_with_alert("ALL", ALERTS_LOG_PATH, cutoff)
    _rotate_log_with_alert("ALL", EMAIL_COMMANDS_LOG_PATH, cutoff)


def check_and_apply_email_commands(portfolio_names: list[str], ibkr_port: int, dry_run: bool,
                                     send_email_command_feedback: bool = True) -> None:
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

    send_email_command_feedback : gates ACCEPTED/REJECTED/ERROR reply EMAILS
    only (notifications.send_email_command_feedback in config.yaml). Every
    attempt is still logged to logs/email_commands_log.csv regardless, see
    log_command_attempt().

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

    def _maybe_send(subject, body):
        if not send_email_command_feedback:
            logger.info("Email command feedback suppressed (send_email_command_feedback: "
                        "false): %s", subject)
            return
        send_alert_email(subject, body)  # reuses existing SMTP reply path

    def _reply(to_addr, subject, body):
        _maybe_send(subject, body)

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
            _maybe_send(f"ALERTS_REPORT: {cmd.portfolio}", report_body)
            logger.info("ALERTS_REPORT reply sent via email command (portfolio=%s, limit=%d, rows=%d).",
                        cmd.portfolio, cmd.limit, len(rows))
            continue

        targets = portfolio_names if cmd.portfolio == "ALL" else [cmd.portfolio]
        for name in targets:
            if name not in portfolio_names:
                logger.warning("Email command referenced unknown portfolio %r, skipped.", name)
                continue

            try:
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
                    _maybe_send(f"STATUS: {name}", status_body)
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
                    _maybe_send(
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
                    _maybe_send(
                        f"Email command needs manual action: {cmd.action} ({name})",
                        f"Command parsed and validated successfully but is not auto-applied.\n"
                        f"Action: {cmd.action}\nPortfolio: {name}\n\nReview and apply manually.",
                    )
            except Exception as e:
                # Isolated to THIS command/portfolio only, deliberately not re-raised, one
                # command's apply failure must not abort every OTHER command still queued in
                # this same batch (a single poll can return several results, and one command
                # can target several portfolios via PORTFOLIO: ALL). An earlier ACCEPTED
                # reply may already have promised this would be applied; it wasn't (or
                # wasn't fully), so this is recorded as its own ERROR row in the SAME audit
                # trail as ACCEPTED/REJECTED, not just daily_runner.py's own log stream.
                logger.error("[%s] Email command %s failed to APPLY: %s", name, cmd.action, e)
                log_command_attempt(result.sender, result, outcome="ERROR", reason=str(e))
                _maybe_send(
                    f"Email command APPLY ERROR: {cmd.action} ({name})",
                    build_reply_body(result, outcome="ERROR", reason=str(e)),
                )


# --------------------------------------------------------------------------- #
# MULTI-PORTFOLIO CAPITAL SAFETY, resolved ONCE per run, before the
# per-portfolio loop, so portfolios sharing one real IBKR account can't silently
# double-count or over-allocate the same capital.
# --------------------------------------------------------------------------- #
def resolve_total_values(portfolios: dict, dry_run: bool, account_value_fn=None) -> dict:
    """
    total_value: null means "an equal share of the account remainder after every fixed
    portfolio's total_value is reserved", not "the full account", which would silently
    double-count real capital across portfolios sharing one IBKR account. Zero, one, or
    several portfolios may be null (validate_config_schema() no longer restricts this),
    the remainder is split equally across every null portfolio.

    account_value_fn : callable() -> float, injected (not called directly) so this
    is unit-testable without a real IBKR connection. Only invoked in --live mode when
    at least one portfolio needs it (has total_value: null).

    Returns {name: resolved_total_value}. In --live mode, raises ValueError if the
    shared remainder would be <= 0 (the other portfolios' fixed allocations already
    consume the whole account), proceeding with zero/negative real capital is never
    safe; the error names every null portfolio that would have shared it. In dry-run,
    EACH null portfolio independently gets a flat $1000 placeholder (NOT divided among
    them, NOT reduced by other portfolios' total_value), dry-run exists to test signal/
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
        if dry_run:
            for null_name in null_names:
                resolved[null_name] = 1000.0
        else:
            account_value = account_value_fn()
            remainder = account_value - sum_of_fixed
            if remainder <= 0:
                raise ValueError(
                    f"portfolios {sorted(null_names)} (total_value: null) would share "
                    f"${remainder:,.2f} (account value ${account_value:,.2f} minus other "
                    f"portfolios' fixed total_value ${sum_of_fixed:,.2f}), those portfolios "
                    f"already consume the whole account."
                )
            per_null_share = remainder / len(null_names)
            for null_name in null_names:
                resolved[null_name] = per_null_share

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
    #     submit orders against the same position, with no coordination between them.
    #     `overlap` itself is HOISTED here (not just computed inside the `if overlap:` block
    #     below), unconditionally available (empty dict for a single-portfolio config) before
    #     the per-portfolio loop begins, since scope_overlapping_holdings() needs it every
    #     iteration to prevent the destructive cross-portfolio sell this warning only used to
    #     flag, never actually prevent, see that function's own docstring for the real
    #     2026-07-16 incident this fixes. ---
    overlap = check_ticker_overlap(portfolios) if len(portfolios) > 1 else {}
    if overlap:
        overlap_desc = "; ".join(f"{t}: {names}" for t, names in overlap.items())
        logger.warning("TICKER OVERLAP across portfolios (destructive cross-portfolio "
                        "sells are prevented, see scope_overlapping_holdings(), but signal/"
                        "exposure computation for each portfolio still doesn't see the "
                        "sibling's position in the same name): %s", overlap_desc)
        log_alert("ALL", "TICKER_OVERLAP", "WARNING", overlap_desc,
                  log_path=ALERTS_LOG_PATH)
        overlap_text = (
            f"The following tickers appear in more than one portfolio in this run:\n"
            f"{overlap_desc}\n\nEach portfolio computes and submits orders independently. "
            f"Destructive cross-portfolio sells are now prevented (scope_overlapping_"
            f"holdings() caps each portfolio's view of a shared ticker at its own trade "
            f"log's real shares), but aggregate exposure/vol-targeting for each portfolio "
            f"still doesn't account for the sibling's position in the same name. Review if "
            f"unintentional."
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

    # --- Log each portfolio's resolved capital, this is the authoritative number a human
    #     should read off when setting risk_monitor.py's --initial-capital for a
    #     total_value: null portfolio, risk_monitor.py cannot compute an equal-split share
    #     itself (it never imports this module's allocation logic), see
    #     docs/DEPLOYMENT.md's "Independent risk oversight" section. ---
    for name, value in resolved_total_values.items():
        is_null = portfolios[name]["total_value"] is None
        logger.info("Portfolio '%s' resolved total_value: $%.2f%s", name, value,
                    " (split from total_value: null)" if is_null else "")

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

    # --- Account-wide drawdown circuit breaker (Recommended tier, docs/RISK_CONSTRAINTS.md),
    #     evaluated ONCE per run (not per-portfolio), against the SUM of every portfolio's
    #     already-resolved capital (reuses resolve_total_values()'s output above, not a second
    #     real-account-value fetch). Disabled by default (account_wide_max_drawdown_pct: 0.0).
    #     Runs in BOTH dry-run and --live, same as the existing per-portfolio circuit breaker
    #     check inside the rebalance loop below, harmless in dry-run since no real orders are
    #     ever placed there regardless. ---
    _check_account_wide_drawdown_breaker_with_alert(
        list(portfolios.keys()), sum(resolved_total_values.values()),
        cfg_raw.get("account_wide_max_drawdown_pct", 0.0),
    )

    # --- Check for and apply email commands (opt-in, silent no-op if unconfigured) ---
    try:
        check_and_apply_email_commands(
            list(portfolios.keys()), args.port, dry_run=not args.live,
            send_email_command_feedback=notification_cfg.get("send_email_command_feedback", True),
        )
    except Exception as e:
        logger.warning("Email command check failed (non-fatal, continuing with normal run): %s", e)

    # --- Macro indicators (Fed Funds Rate, CPI), fetched ONCE per run, not per-portfolio,
    #     macro conditions apply market-wide, not per-ticker. Cached (see core/macro_data.py),
    #     and the FRED_API_KEY presence check happens inside get_cached_or_fetch_macro_indicators()
    #     before any network attempt, so an unconfigured key costs nothing every run. ---
    macro_indicators = get_cached_or_fetch_macro_indicators(fred_api_key=os.environ.get("FRED_API_KEY"))

    # --- Time-based log retention for the two SHARED logs (alerts_log.csv, email_commands_log.csv),
    #     ONCE per run, not per-portfolio, see apply_shared_log_retention()'s own docstring for
    #     why these two can't be rotated inside the per-portfolio loop below. No-op entirely if
    #     no portfolio has enable_log_retention=True. ---
    try:
        apply_shared_log_retention(portfolios)
    except Exception as e:
        logger.warning("Shared log retention check failed (non-fatal, continuing with normal run): %s", e)

    try:
        for name, spec in portfolios.items():
            cfg = spec["cfg"]
            tickers = spec["tickers"]
            trade_log_path = str(logs_dir() / f"live_trades_log_{name}.csv")
            signal_rankings_log_path = str(logs_dir() / f"signal_rankings_log_{name}.csv")

            # --- Time-based log retention (opt-in, cfg.enable_log_retention), this portfolio's
            #     OWN trade/signal-rankings/snapshot logs only, see
            #     apply_portfolio_log_retention()'s own docstring. ---
            try:
                apply_portfolio_log_retention(name, cfg, trade_log_path, signal_rankings_log_path)
            except Exception as e:
                logger.warning("[%s] Log retention check failed (non-fatal, continuing): %s", name, e)

            # --- Stale rebalance-in-progress marker (non-blocking WARNING): a marker written
            #     immediately before a PREVIOUS run()'s rebalance, still present now, means that
            #     earlier process crashed mid-rebalance (killed, container stopped, order
            #     submission or fill-polling never completed cleanly). Purely a visibility
            #     signal, this project's diff-based order generation already makes a retry safe
            #     against duplicating completed actions on its own (see docs/RUNNING.md's
            #     "Restart and Resume Behavior"), this does NOT block the current run, it just
            #     flags that actual IBKR state is worth a manual glance before trusting
            #     automated reconciliation this cycle. Consumed (deleted) after firing once,
            #     same "one-time, not persistent" pattern as the skip-next-rebalance flag below,
            #     a human has been notified, no need to nag every subsequent run forever. ---
            stale_marker = _rebalance_in_progress_marker_path(name)
            if stale_marker.exists():
                marker_ts = stale_marker.read_text().strip()
                logger.warning(
                    "[%s] Found a stale rebalance-in-progress marker (written %s), a previous "
                    "run may have crashed mid-rebalance. Verify actual IBKR state before "
                    "trusting automated reconciliation this cycle.", name, marker_ts,
                )
                log_alert(
                    name, "STALE_REBALANCE_MARKER", "WARNING",
                    f"Rebalance-in-progress marker from {marker_ts} was still present at the "
                    f"start of this run, a previous process may have crashed mid-rebalance.",
                    log_path=ALERTS_LOG_PATH,
                )
                marker_text = (
                    f"Portfolio '{name}' has a stale rebalance-in-progress marker written at "
                    f"{marker_ts}.\n\n"
                    f"This usually means a previous daily-runner process crashed or was killed "
                    f"mid-rebalance (order submission or fill confirmation never completed). "
                    f"The diff-based order generation this project uses makes a retry safe "
                    f"against duplicating already-completed actions in the common case, but "
                    f"verify actual IBKR positions/orders manually before trusting automated "
                    f"reconciliation this cycle. See docs/RUNNING.md's \"Restart and Resume "
                    f"Behavior\" section."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Stale rebalance marker: {name}",
                    f"<pre>{marker_text}</pre>", notification_cfg,
                    plain_text_fallback=marker_text,
                )
                stale_marker.unlink(missing_ok=True)  # consumed, one-time, not persistent

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

            # --- Lookback-period-too-short warning (non-blocking), only meaningful in the
            #     weekly regime (holding_period < 1), where lookback_period is interpreted in
            #     week-quarters by resolve_momentum_scores(); a sub-2-week momentum window is a
            #     real, well-defined signal, just a noisy/whipsaw-prone one. ---
            if is_lookback_period_too_short(cfg.lookback_period, cfg.holding_period):
                logger.warning(
                    "[%s] lookback_period=%s (holding_period=%s) is shorter than 2 weeks, "
                    "not recommended. A momentum window this short is dominated by noise "
                    "rather than real trend.", name, cfg.lookback_period, cfg.holding_period,
                )
                log_alert(
                    name, "LOOKBACK_PERIOD_TOO_SHORT", "WARNING",
                    f"lookback_period={cfg.lookback_period} (holding_period={cfg.holding_period}) "
                    f"is shorter than 2 weeks.",
                    log_path=ALERTS_LOG_PATH,
                )
                lookback_period_text = (
                    f"Portfolio '{name}' is configured with lookback_period={cfg.lookback_period} "
                    f"under holding_period={cfg.holding_period}, a momentum-ranking window "
                    f"shorter than 2 weeks.\n\n"
                    f"This is not recommended: a lookback window this short is dominated by "
                    f"single-day price noise rather than real trend, single-day moves can flip "
                    f"the ranking. This run is proceeding normally, nothing was blocked, but "
                    f"consider setting lookback_period to at least 0.5 (2 weeks) under a weekly "
                    f"holding_period."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Lookback period too short: {name}",
                    f"<pre>{lookback_period_text}</pre>", notification_cfg,
                    plain_text_fallback=lookback_period_text,
                )

            # --- Momentum Persistence constraint (non-blocking): lookback_period must be
            #     strictly older than holding_period (in the same regime-appropriate unit),
            #     otherwise a position is held based on already-stale signal dynamics. ---
            if is_lookback_shorter_than_holding(cfg.lookback_period, cfg.holding_period):
                logger.warning(
                    "[%s] lookback_period=%s is not older than holding_period=%s "
                    "(Momentum Persistence constraint), not recommended.",
                    name, cfg.lookback_period, cfg.holding_period,
                )
                log_alert(
                    name, "MOMENTUM_PERSISTENCE_VIOLATION", "WARNING",
                    f"lookback_period={cfg.lookback_period} is not older than "
                    f"holding_period={cfg.holding_period}.",
                    log_path=ALERTS_LOG_PATH,
                )
                persistence_text = (
                    f"Portfolio '{name}' is configured with lookback_period={cfg.lookback_period} "
                    f"and holding_period={cfg.holding_period}: the momentum signal is not older "
                    f"than the period you intend to hold the resulting position.\n\n"
                    f"This is not recommended (the \"Momentum Persistence\" constraint): a signal "
                    f"must be older than your holding period, otherwise you're holding assets "
                    f"based on signal dynamics that are already stale by the time you exit. This "
                    f"run is proceeding normally, nothing was blocked, but consider increasing "
                    f"lookback_period relative to holding_period. See docs/RISK_CONSTRAINTS.md."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Momentum Persistence constraint violated: {name}",
                    f"<pre>{persistence_text}</pre>", notification_cfg,
                    plain_text_fallback=persistence_text,
                )

            # --- Lookback-to-Hold Ratio constraint (non-blocking): lookback_period / holding_period
            #     below 3 risks "whipsawing", the position gets exited/re-entered based on noise
            #     within a lookback window barely longer than the holding period. Deliberately
            #     independent of the Momentum Persistence check above, not suppressed when that
            #     one already fired. ---
            if is_lookback_to_holding_ratio_too_low(cfg.lookback_period, cfg.holding_period):
                logger.warning(
                    "[%s] lookback_period=%s / holding_period=%s ratio is below 3 "
                    "(Lookback-to-Hold Ratio constraint), risks whipsawing.",
                    name, cfg.lookback_period, cfg.holding_period,
                )
                log_alert(
                    name, "LOOKBACK_TO_HOLD_RATIO_TOO_LOW", "WARNING",
                    f"lookback_period={cfg.lookback_period} / holding_period={cfg.holding_period} "
                    f"ratio is below 3.",
                    log_path=ALERTS_LOG_PATH,
                )
                ratio_text = (
                    f"Portfolio '{name}' is configured with lookback_period={cfg.lookback_period} "
                    f"and holding_period={cfg.holding_period}, a lookback-to-holding ratio below "
                    f"3.\n\n"
                    f"This is not recommended (the \"Lookback-to-Hold Ratio\" constraint): for "
                    f"stable momentum, the signal's history should be meaningfully longer than "
                    f"the trade duration, a ratio below 3 risks whipsawing. This run is "
                    f"proceeding normally, nothing was blocked, but consider a larger "
                    f"lookback_period relative to holding_period. See docs/RISK_CONSTRAINTS.md."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Lookback-to-Hold Ratio too low: {name}",
                    f"<pre>{ratio_text}</pre>", notification_cfg,
                    plain_text_fallback=ratio_text,
                )

            # --- Missed-rebalance-day constraint (non-blocking): the process/container was not
            #     running on a date that should have been a scheduled rebalance day, so it never
            #     happened, silently, by construction (is_rebalance_day() only ever asks "is
            #     TODAY the day", it has no memory of a day it never got to check). Alert-only,
            #     no automatic catch-up, see docs/RUNNING.md. Skipped entirely when today IS
            #     itself a rebalance day (about to run normally below, nothing was missed), when
            #     --force-rebalance is set (not the automatic path this targets), or when this
            #     portfolio has never successfully run before (a brand-new deployment has
            #     nothing to have missed). Uses has_run_on_or_after(), not an exact-date lock
            #     check, specifically so that manually running --force-rebalance to catch up
            #     clears this warning on the NEXT run, rather than nagging forever, since a
            #     forced run marks TODAY's own date, never the missed period's original
            #     target date. ---
            has_run_before = any(LOCK_DIR.glob(f"last_run_rebalance_{name}_*.lock"))
            if (not args.force_rebalance and has_run_before
                    and not is_rebalance_day(holding_period_months=cfg.holding_period)):
                missed_date = most_recent_rebalance_target_date(holding_period_months=cfg.holding_period)
                if missed_date is not None and not has_run_on_or_after(
                    f"rebalance_{name}", missed_date.date(),
                ):
                    logger.warning(
                        "[%s] Missed scheduled rebalance on %s, the process was not running "
                        "that day, no automatic catch-up was performed.",
                        name, missed_date.date(),
                    )
                    log_alert(
                        name, "MISSED_REBALANCE_DAY", "WARNING",
                        f"Scheduled rebalance on {missed_date.date()} was missed, no lock file "
                        f"found for that date.",
                        log_path=ALERTS_LOG_PATH,
                    )
                    missed_text = (
                        f"Portfolio '{name}' had a scheduled rebalance on "
                        f"{missed_date.date()} (holding_period={cfg.holding_period}), but no "
                        f"record of it running exists, the process or container was not "
                        f"running that day.\n\n"
                        f"No automatic catch-up was performed, this run is proceeding "
                        f"normally against today's own schedule. If you want that missed "
                        f"rebalance applied now, using today's prices, run daily-runner "
                        f"--force-rebalance --live for '{name}' manually."
                    )
                    send_action_email(
                        NotificationCategory.WARNING, f"Missed rebalance day: {name}",
                        f"<pre>{missed_text}</pre>", notification_cfg,
                        plain_text_fallback=missed_text,
                    )

            # --- item 1: real positions from IBKR, never local memory. total_value comes
            #     from resolved_total_values, resolved once above the loop. ---
            if args.live:
                current_positions = with_retry(get_ibkr_positions, 3, 2.0, args.port)
                # scope_overlapping_holdings() MUST run immediately here, before
                # current_holdings is built or used by anything below (orphaned-ticker
                # classification, run()/generate_orders(), stop-loss checks, snapshot writing),
                # so every downstream consumer automatically sees the corrected, per-portfolio
                # numbers, closing the real 2026-07-16 cross-portfolio-sell incident this fixes.
                # Dry-run/backtest never query a shared real account, nothing to scope there.
                current_positions, capped_tickers = scope_overlapping_holdings(
                    current_positions, tickers, overlap, trade_log_path, name,
                )
                if capped_tickers:
                    capped_desc = ", ".join(capped_tickers)
                    logger.warning(
                        "[%s] OVERLAPPING_TICKER_SCOPED: %s shared with a sibling portfolio "
                        "(%s), this portfolio's own view capped at its own trade log's real "
                        "shares, preventing a sell sized off the sibling's shares.",
                        name, capped_desc, "; ".join(f"{t}: {overlap[t]}" for t in capped_tickers),
                    )
                    log_alert(
                        name, "OVERLAPPING_TICKER_SCOPED", "WARNING",
                        f"{capped_desc} shared with a sibling portfolio, this portfolio's view "
                        f"capped at its own trade log's real shares this run.",
                        log_path=ALERTS_LOG_PATH,
                    )
            elif cfg.persist_dry_run_state:
                # Opt-in (default False, see BacktestConfig.persist_dry_run_state), reconstructs
                # a simulated portfolio from the trade log's own dry_run=True rows, so dry-run
                # OPTIONALLY behaves like a persistent, no-IBKR-required paper ledger across
                # separate invocations, instead of always starting flat. Never affects --live.
                current_positions = reconstruct_dry_run_positions(trade_log_path)
            else:
                current_positions = {}   # dry-run default: no real broker state to reconcile against
            total_value = resolved_total_values[name]

            current_holdings = {t: p["shares"] for t, p in current_positions.items()}

            # --- Orphaned/unrecognized ticker classification and alerts, see
            #     _classify_orphaned_tickers()'s own docstring for the full multi-portfolio
            #     safety rationale (a held-but-unconfigured ticker could belong to a SIBLING
            #     portfolio sharing this real IBKR account, not just be a stale drop-out from
            #     THIS portfolio's own history, these must be told apart before touching
            #     either one). confirmed_orphaned gets priced below (widened fetch) and passed
            #     into run() as extra_price_tickers so the normal diff-based rotation logic can
            #     reconcile it; unrecognized stays completely untouched, exactly as
            #     conservative as before this feature existed. ---
            confirmed_orphaned, unrecognized = _classify_orphaned_tickers(
                current_holdings, tickers, trade_log_path,
            )
            for t in confirmed_orphaned:
                logger.warning(
                    "[%s] %s is held but no longer in the configured tickers list, this "
                    "portfolio's own trade log confirms it was previously held here, pricing "
                    "it for reconciliation this run.", name, t,
                )
                log_alert(
                    name, "ORPHANED_POSITION", "WARNING",
                    f"{t} is held but not in the configured tickers list, confirmed via this "
                    f"portfolio's own trade log, being priced/reconciled this run.",
                    log_path=ALERTS_LOG_PATH,
                )
                orphaned_text = (
                    f"Portfolio '{name}' is holding {t}, which is no longer in its configured "
                    f"tickers list, but this portfolio's own trade log confirms {t} was "
                    f"legitimately held here before.\n\n"
                    f"It will be priced and made eligible for exit this run (sold if not "
                    f"re-selected), same as any other rotation drop-out. See "
                    f"docs/RUNNING.md's \"Restart and Resume Behavior\" section."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Orphaned position: {t} ({name})",
                    f"<pre>{orphaned_text}</pre>", notification_cfg,
                    plain_text_fallback=orphaned_text,
                )
            for t in unrecognized:
                logger.warning(
                    "[%s] %s is held but not in the configured tickers list, and this "
                    "portfolio's own trade log has no record of it, NOT auto-priced or "
                    "auto-traded, may belong to a different portfolio sharing this account, "
                    "investigate manually.", name, t,
                )
                log_alert(
                    name, "UNRECOGNIZED_POSITION", "WARNING",
                    f"{t} is held but not in the configured tickers list, and not confirmed "
                    f"by this portfolio's own trade log, left untouched.",
                    log_path=ALERTS_LOG_PATH,
                )
                unrecognized_text = (
                    f"Portfolio '{name}' account shows a position in {t}, which is not in "
                    f"this portfolio's configured tickers list, and this portfolio's own "
                    f"trade log has no record of ever holding it.\n\n"
                    f"It is being left untouched (not priced, not traded), since it may "
                    f"belong to a different portfolio sharing this real IBKR account. "
                    f"Investigate manually. See docs/RUNNING.md's \"Restart and Resume "
                    f"Behavior\" section."
                )
                send_action_email(
                    NotificationCategory.WARNING, f"Unrecognized position: {t} ({name})",
                    f"<pre>{unrecognized_text}</pre>", notification_cfg,
                    plain_text_fallback=unrecognized_text,
                )

            # --- ALWAYS runs: fetch latest prices once, used by stop-loss check + snapshot.
            #     Widened by confirmed_orphaned so those positions also regain stop-loss/
            #     time-stop protection (both already skip any ticker missing from
            #     latest_prices), not just order-generation reconciliation. ---
            from .execution.live_signal import (
                fetch_live_prices, check_price_staleness, compute_required_lookback_days,
            )
            price_fetch_tickers = tickers + confirmed_orphaned if confirmed_orphaned else tickers
            daily_prices = with_retry(
                fetch_live_prices, 3, 2.0, price_fetch_tickers,
                lookback_days=compute_required_lookback_days(cfg),
            )
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

            # --- total_value drift warning (non-blocking, --live only, fixed total_value
            #     portfolios only): total_value: null already tracks real account value by
            #     definition (resolve_total_values()), nothing to compare. A fixed total_value
            #     never auto-refreshes from real account P&L (a deliberate, documented choice,
            #     an allocation ceiling, not auto-compounding), this only makes that silent
            #     drift VISIBLE. Real per-portfolio cash can't be isolated on a shared IBKR
            #     account without deeper accounting than this project attempts, so only the
            #     POSITION side is compared (explicitly scoped, see
            #     _compute_scoped_positions_value()'s own docstring for why this must NOT reuse
            #     the pre-existing positions_value computation's implicit scoping), not a full
            #     "total value" reconstruction. Only warns on the anomalous high side: real
            #     positions alone exceeding the entire configured capital base is a clear,
            #     honest signal, not a guess. ---
            if args.live and spec["total_value"] is not None and total_value > 0:
                scoped_value = _compute_scoped_positions_value(
                    current_positions, latest_prices, tickers, confirmed_orphaned,
                )
                if scoped_value > total_value * (1 + cfg.total_value_drift_warning_pct):
                    drift_pct = (scoped_value / total_value) - 1
                    logger.warning(
                        "[%s] Real position value $%.2f exceeds configured total_value $%.2f "
                        "by %.1f%%, the configured capital base may be stale.",
                        name, scoped_value, total_value, drift_pct * 100,
                    )
                    log_alert(
                        name, "TOTAL_VALUE_DRIFT", "WARNING",
                        f"Real position value ${scoped_value:,.2f} exceeds configured "
                        f"total_value ${total_value:,.2f} by {drift_pct:.1%}.",
                        log_path=ALERTS_LOG_PATH,
                    )
                    drift_text = (
                        f"Portfolio '{name}' has a fixed total_value=${total_value:,.2f} in "
                        f"config.yaml, but its real position value (this portfolio's own "
                        f"tickers only) is now ${scoped_value:,.2f}, {drift_pct:.1%} higher.\n\n"
                        f"total_value never auto-refreshes from real account P&L (a "
                        f"deliberate, documented choice, an allocation ceiling, not "
                        f"auto-compounding), but this divergence is large enough to review: "
                        f"either update total_value in config.yaml to reflect real growth, or "
                        f"investigate why positions exceed the configured ceiling. See "
                        f"docs/RUNNING.md's \"Restart and Resume Behavior\" section."
                    )
                    send_action_email(
                        NotificationCategory.WARNING, f"total_value drift: {name}",
                        f"<pre>{drift_text}</pre>", notification_cfg,
                        plain_text_fallback=drift_text,
                    )

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
                _write_rebalance_in_progress_marker(name)
                try:
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
                        extra_price_tickers=confirmed_orphaned,
                        daily_prices=daily_prices,
                        signal_rankings_log_path=signal_rankings_log_path,
                    )
                finally:
                    _clear_rebalance_in_progress_marker(name)
                mark_ran_today(f"rebalance_{name}")

                # --- Turnover Limit constraint (non-blocking): Total_Positions_Changed /
                #     Total_Positions for this rebalance, high turnover is a sign the momentum
                #     ranking is over-sensitive to noise rather than tracking a persistent
                #     trend. Only meaningful on an actual rebalance (orders_result is {} when
                #     AGGREGATE_DRIFT_SKIP fired, compute_turnover({}) correctly returns 0.0). ---
                turnover = compute_turnover(orders_result)
                if is_turnover_too_high(turnover, cfg.max_turnover_pct):
                    logger.warning(
                        "[%s] turnover=%.1f%% exceeds max_turnover_pct=%.1f%% "
                        "(Turnover Limit constraint), flagged for review.",
                        name, turnover * 100, cfg.max_turnover_pct * 100,
                    )
                    log_alert(
                        name, "TURNOVER_TOO_HIGH", "WARNING",
                        f"turnover={turnover:.2%} exceeds max_turnover_pct={cfg.max_turnover_pct:.2%}.",
                        log_path=ALERTS_LOG_PATH,
                    )
                    turnover_text = (
                        f"Portfolio '{name}' rebalanced with turnover={turnover:.2%} "
                        f"(Total_Positions_Changed / Total_Positions), exceeding the configured "
                        f"max_turnover_pct={cfg.max_turnover_pct:.2%}.\n\n"
                        f"This is flagged for review (the \"Turnover Limit\" constraint): high "
                        f"turnover is almost always a sign of an over-sensitive signal. This run "
                        f"is proceeding normally, nothing was blocked. See "
                        f"docs/RISK_CONSTRAINTS.md."
                    )
                    send_action_email(
                        NotificationCategory.WARNING, f"Turnover too high: {name}",
                        f"<pre>{turnover_text}</pre>", notification_cfg,
                        plain_text_fallback=turnover_text,
                    )

                # --- Low-capital / fractional-share sizing constraint (non-blocking): IBKR has
                #     no fractional-equity order support, place_orders_ibkr() floors any BUY
                #     that computes to under 1 share and drops it entirely. When too much of a
                #     rebalance's intended BUYs get dropped this way, total_value is too small
                #     relative to top_n and this portfolio's ticker prices for capital to
                #     actually get deployed, distinct from turnover above (signal noise, not
                #     capital sizing). Fires in BOTH dry-run and --live (see
                #     compute_low_capital_drop_fraction()'s own docstring for why). ---
                drop_fraction, dropped_tickers = compute_low_capital_drop_fraction(orders_result)
                if is_low_capital_drop_too_high(drop_fraction, cfg.low_capital_drop_warning_pct):
                    logger.warning(
                        "[%s] %.1f%% of intended BUYs dropped (floor to 0 shares) exceeds "
                        "low_capital_drop_warning_pct=%.1f%%: %s",
                        name, drop_fraction * 100, cfg.low_capital_drop_warning_pct * 100,
                        ", ".join(dropped_tickers),
                    )
                    log_alert(
                        name, "LOW_CAPITAL_FRACTIONAL_DROP", "WARNING",
                        f"{drop_fraction:.2%} of intended BUYs dropped for flooring to 0 shares "
                        f"(tickers: {', '.join(dropped_tickers)}), exceeds "
                        f"low_capital_drop_warning_pct={cfg.low_capital_drop_warning_pct:.2%}.",
                        log_path=ALERTS_LOG_PATH,
                    )
                    low_capital_text = (
                        f"Portfolio '{name}' had {drop_fraction:.2%} of its intended BUYs "
                        f"dropped this rebalance, IBKR has no fractional-equity order support, "
                        f"so a computed position under 1 share is dropped entirely rather than "
                        f"ever reaching the broker: {', '.join(dropped_tickers)}.\n\n"
                        f"This means total_value=${total_value:,.2f} is likely too small "
                        f"relative to top_n={min(cfg.top_n, len(tickers))} and this portfolio's "
                        f"ticker prices for real capital to actually get deployed. Concrete "
                        f"levers: increase total_value, reduce top_n (fewer, larger positions), "
                        f"or prefer lower-priced tickers. This run is proceeding normally, "
                        f"nothing was blocked."
                    )
                    send_action_email(
                        NotificationCategory.WARNING, f"Low-capital fractional drop: {name}",
                        f"<pre>{low_capital_text}</pre>", notification_cfg,
                        plain_text_fallback=low_capital_text,
                    )

                # --- STANDARD-category notification (filterable). Always sent when a
                #     rebalance actually ran (this branch only runs on a rebalance day or
                #     --force-rebalance), whether or not it produced any orders, so a quiet
                #     day (e.g. AGGREGATE_DRIFT_SKIP) still gets a confirmation instead of
                #     silence indistinguishable from a failed/skipped run. ---
                if orders_result:
                    rebalance_html = build_rebalance_summary_html(name, orders_result, dry_run=not args.live)
                    if orders_result.full_signal_universe:
                        rebalance_html += build_signal_universe_html(
                            orders_result.full_signal_universe, orders_result, top_n=min(cfg.top_n, len(tickers)),
                            strategy_type=cfg.strategy_type, dry_run=not args.live,
                        )
                    send_standard_action(
                        f"Rebalance executed: {name}",
                        rebalance_html,
                        notification_cfg,
                    )
                else:
                    send_standard_action(
                        f"Rebalance checked, no changes: {name}",
                        build_no_action_summary_html(name, picks_were_empty=orders_result.picks_were_empty),
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
