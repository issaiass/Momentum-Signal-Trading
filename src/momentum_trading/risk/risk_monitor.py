#!/usr/bin/env python3
"""
risk_monitor.py

Independent oversight process, deliberately separate from
daily_runner.py's trading logic. This script has READ-ONLY access to trade
logs -- it cannot place, modify, or cancel orders, and does not import
live_signal.py's order-execution functions at all. Its only power is to write
a halt flag file that daily_runner.py checks and respects.

This segregation matters: if a bug in the trading logic itself causes runaway
losses, a monitor built from the SAME code sharing the SAME assumptions is a
weak safeguard. This script is intentionally minimal, independent, and reads
only the CSV audit trail -- the same artifact a human reviewer would look at.

Run this as a SEPARATE scheduled job from daily_runner.py (different cron
entry, different container/process), ideally more frequently (e.g. hourly)
than the trading job itself.

Usage:
    python risk_monitor.py --portfolio portfolio1 --max-loss-pct 0.20
    # --initial-capital defaults to portfolios.<name>.total_value in config.yaml;
    # pass it explicitly to override (e.g. --initial-capital 1000).
"""

import argparse
import glob
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from momentum_trading.core.paths import data_dir

logger = logging.getLogger("risk_monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

LOCK_DIR = data_dir()


def compute_realized_and_open_pnl(log_path: str) -> dict:
    """
    Independent re-derivation of P&L directly from the trade log CSV --
    intentionally NOT importing measure_live_performance() from live_signal.py,
    so a bug in that function doesn't also blind the monitor watching for it.
    Simpler FIFO logic, same algorithm, separately implemented.
    """
    if not os.path.isfile(log_path):
        return {"realized_pnl": 0.0, "trade_count": 0}

    log = pd.read_csv(log_path, parse_dates=["timestamp"])
    log = log[log["action"].isin(["BUY", "SELL"])].sort_values("timestamp")

    realized = 0.0
    lots: dict[str, list] = {}
    for _, row in log.iterrows():
        ticker, action = row["ticker"], row["action"]
        shares, price = float(row["shares"]), float(row["price"])
        if shares <= 0:
            continue
        queue = lots.setdefault(ticker, [])
        if action == "BUY":
            queue.append([shares, price])
        else:
            remaining = shares
            while remaining > 1e-9 and queue:
                lot_shares, lot_price = queue[0]
                matched = min(remaining, lot_shares)
                realized += matched * (price - lot_price)
                lot_shares -= matched
                remaining -= matched
                if lot_shares <= 1e-9:
                    queue.pop(0)
                else:
                    queue[0][0] = lot_shares

    return {"realized_pnl": realized, "trade_count": len(log)}


def write_halt_flag(portfolio: str, reason: str) -> None:
    LOCK_DIR.mkdir(exist_ok=True)
    halt_path = LOCK_DIR / f"circuit_breaker_halted_{portfolio}.flag"
    halt_path.write_text(f"{datetime.now().isoformat()} | risk_monitor.py: {reason}")
    logger.warning("[%s] HALT FLAG WRITTEN by risk_monitor.py: %s", portfolio, reason)


def send_monitor_alert(subject: str, body: str) -> None:
    """
    Deliberately separate alert path from daily_runner.py's send_alert_email()
    -- true segregation means even the notification channel isn't shared, so a
    bug affecting one doesn't silence the other. Reads the SAME env vars for
    simplicity, but is its own independent code path.
    """
    import smtplib
    from email.mime.text import MIMEText

    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("ALERT_TO_EMAIL")
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([host, user, password, to_addr]):
        logger.error("SMTP not configured -- MONITOR ALERT NOT SENT. Subject: %s | Body: %s", subject, body)
        return

    msg = MIMEText(body)
    msg["Subject"] = f"[risk_monitor] {subject}"
    msg["From"] = user
    msg["To"] = to_addr
    msg["X-Momentum-Trading-Bot"] = "1"
    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, [to_addr], msg.as_string())
        logger.info("Monitor alert sent: %s", subject)
    except Exception as e:
        logger.error("Monitor alert send failed: %s", e)


def load_initial_capital(portfolio: str, config_path: str) -> float | None:
    """
    Independent, minimal read of portfolios.<name>.total_value from config.yaml --
    deliberately does NOT import daily_runner.load_config()/BacktestConfig (see module
    docstring: this monitor must not share code/assumptions with the trading logic it watches).
    """
    if not os.path.isfile(config_path):
        return None
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    spec = (raw.get("portfolios") or {}).get(portfolio) or {}
    return spec.get("total_value")


def main():
    parser = argparse.ArgumentParser(
        description="Independent, read-only risk monitor. Watches trade logs and can halt "
                     "trading (via a flag file daily_runner.py respects) but cannot place orders."
    )
    parser.add_argument("--portfolio", required=True, help="Portfolio name, matches config.yaml.")
    parser.add_argument("--initial-capital", type=float, default=None,
                         help="Defaults to portfolios.<name>.total_value in --config if omitted.")
    parser.add_argument("--max-loss-pct", type=float, default=0.25,
                         help="Halt if realized loss exceeds this fraction of initial capital.")
    parser.add_argument("--log-dir", default=str(data_dir()))
    parser.add_argument("--config", default="config.yaml",
                         help="Path to config.yaml, used only to look up total_value when "
                              "--initial-capital is omitted.")
    args = parser.parse_args()

    initial_capital = args.initial_capital
    if initial_capital is None:
        initial_capital = load_initial_capital(args.portfolio, args.config)
    if initial_capital is None:
        raise SystemExit(
            f"No initial capital available for portfolio '{args.portfolio}': pass "
            f"--initial-capital explicitly, or set portfolios.{args.portfolio}.total_value "
            f"in {args.config} (it must be a number, not null)."
        )

    log_path = os.path.join(args.log_dir, f"live_trades_log_{args.portfolio}.csv")
    result = compute_realized_and_open_pnl(log_path)

    loss_pct = -result["realized_pnl"] / initial_capital if initial_capital > 0 else 0.0
    logger.info("[%s] Realized P&L: $%.2f (%.1f%% of capital) across %d trades",
                args.portfolio, result["realized_pnl"], loss_pct * 100, result["trade_count"])

    if loss_pct > args.max_loss_pct:
        reason = f"Realized loss {loss_pct:.1%} exceeds max_loss_pct {args.max_loss_pct:.1%}"
        write_halt_flag(args.portfolio, reason)
        send_monitor_alert(
            f"RISK MONITOR HALT: {args.portfolio}",
            f"{reason}\n\nRealized P&L: ${result['realized_pnl']:,.2f}\n"
            f"Trading has been independently halted for this portfolio. "
            f"Review before running daily_runner.py --resume-trading {args.portfolio}.",
        )
    else:
        logger.info("[%s] Within risk limits, no action taken.", args.portfolio)


if __name__ == "__main__":
    main()
