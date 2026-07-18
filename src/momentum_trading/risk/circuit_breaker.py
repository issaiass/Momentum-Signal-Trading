"""
risk/circuit_breaker.py

Circuit-breaker state management extracted out of
daily_runner.py into its own risk-domain module.

Design note: alerting is DEPENDENCY-INJECTED (an `alert_fn` callable passed
in by the caller) rather than importing daily_runner's send_alert_email
directly. This keeps risk/ decoupled from interfaces/ (no import cycle risk,
and this module doesn't need to know anything about SMTP/email specifics),
daily_runner.py wires the real send_alert_email in when it calls these.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..backtest.momentum_backtest import BacktestConfig
from ..core.paths import data_dir
from ..core.audit_log import log_alert, ALERTS_LOG_PATH

logger = logging.getLogger("circuit_breaker")

LOCK_DIR = data_dir()


def _peak_equity_path(name: str) -> Path:
    return LOCK_DIR / f"peak_equity_{name}.txt"


def _halt_flag_path(name: str) -> Path:
    return LOCK_DIR / f"circuit_breaker_halted_{name}.flag"


def _skip_next_flag_path(name: str) -> Path:
    return LOCK_DIR / f"skip_next_rebalance_{name}.flag"


def _max_drawdown_override_path(name: str) -> Path:
    return LOCK_DIR / f"max_drawdown_override_{name}.txt"


def get_effective_max_drawdown_pct(name: str, configured_value: float) -> float:
    """
    Returns the tighter of the config.yaml value and any active
    SET_MAX_DRAWDOWN email override, enforced here,
    at the point of USE, not at command-parse time. This is where the "can
    only tighten, never loosen" safety property is actually guaranteed: even
    if an override file somehow ended up containing a looser value than
    config.yaml, this function still returns whichever is more conservative.
    """
    override_path = _max_drawdown_override_path(name)
    if not override_path.exists():
        return configured_value
    try:
        override_value = float(override_path.read_text().strip())
    except (ValueError, OSError):
        return configured_value
    if configured_value <= 0:
        return override_value
    return min(configured_value, override_value)


def check_circuit_breaker(name: str, total_value: float, cfg: BacktestConfig, alert_fn=None) -> bool:
    """
    Tracks peak equity across runs (persisted to disk, since daily_runner.py
    runs once per day as a fresh process). Two INDEPENDENT breakers, either
    of which can trip a halt:
      - cfg.max_portfolio_drawdown_pct (or a tighter email override): halt
        if drawdown from peak exceeds this %
      - cfg.max_dollar_drawdown: halt if drawdown from peak exceeds this $

    IMPORTANT: once halted, this does NOT auto-resume even if equity
    recovers, a human must explicitly call resume_trading(), so a
    temporary recovery during a volatile period doesn't silently re-enable
    trading without review.

    alert_fn : callable(subject: str, body: str) -> None, optional
        Dependency-injected alerting (see module docstring for why).

    Returns True if trading should be halted (skip rebalance) for this portfolio.
    """
    if (cfg.max_portfolio_drawdown_pct <= 0 and cfg.max_dollar_drawdown is None
            and not _max_drawdown_override_path(name).exists()):
        return False  # both config breakers disabled and no active email override

    LOCK_DIR.mkdir(exist_ok=True)
    peak_path = _peak_equity_path(name)
    halt_path = _halt_flag_path(name)

    if halt_path.exists():
        logger.warning("[%s] Circuit breaker HALT still in effect (from %s). "
                        "Call resume_trading() to clear it after review.",
                        name, halt_path.read_text().strip())
        return True

    prior_peak = float(peak_path.read_text()) if peak_path.exists() else total_value
    new_peak = max(prior_peak, total_value)
    peak_path.write_text(str(new_peak))

    drawdown_pct = (total_value - new_peak) / new_peak if new_peak > 0 else 0.0
    drawdown_dollar = new_peak - total_value

    effective_max_drawdown_pct = get_effective_max_drawdown_pct(name, cfg.max_portfolio_drawdown_pct)
    tripped_pct = effective_max_drawdown_pct > 0 and drawdown_pct <= -effective_max_drawdown_pct
    tripped_dollar = cfg.max_dollar_drawdown is not None and drawdown_dollar >= cfg.max_dollar_drawdown

    if tripped_pct or tripped_dollar:
        halt_path.write_text(datetime.now().isoformat())
        reason = []
        if tripped_pct:
            reason.append(f"drawdown {drawdown_pct:.1%} <= -{effective_max_drawdown_pct:.1%} "
                          f"(percentage breaker, config={cfg.max_portfolio_drawdown_pct:.1%})")
        if tripped_dollar:
            reason.append(f"drawdown ${drawdown_dollar:,.2f} >= ${cfg.max_dollar_drawdown:,.2f} (dollar breaker)")
        reason_str = " AND ".join(reason)
        logger.warning("[%s] CIRCUIT BREAKER TRIPPED: %s. Halting rebalances.", name, reason_str)
        log_alert(name, "CIRCUIT_BREAKER_TRIPPED", "CRITICAL",
                  f"{reason_str}. Peak equity ${new_peak:,.2f}, current ${total_value:,.2f}.",
                  log_path=ALERTS_LOG_PATH)
        if alert_fn:
            alert_fn(
                f"CIRCUIT BREAKER TRIPPED: {name}",
                f"Portfolio '{name}' tripped: {reason_str}\n"
                f"Peak equity ${new_peak:,.2f}, current ${total_value:,.2f}.\n\n"
                f"Rebalancing is now HALTED for this portfolio until you review the "
                f"situation and resume it explicitly.",
            )
        return True
    return False


def resume_trading(name: str, alert_fn=None) -> None:
    halt_path = _halt_flag_path(name)
    if halt_path.exists():
        halt_path.unlink()
        peak_path = _peak_equity_path(name)
        if peak_path.exists():
            peak_path.unlink()  # reset peak so drawdown is measured fresh from here
        logger.info("[%s] Circuit breaker RESUMED by explicit operator action.", name)
        log_alert(name, "CIRCUIT_BREAKER_RESUMED", "INFO",
                  "Circuit breaker manually cleared by explicit operator action.",
                  log_path=ALERTS_LOG_PATH)
        if alert_fn:
            alert_fn(f"Trading resumed: {name}", f"Circuit breaker for '{name}' was manually cleared.")
    else:
        logger.info("[%s] No active halt to resume.", name)
