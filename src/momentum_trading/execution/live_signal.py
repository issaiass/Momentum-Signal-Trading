"""
live_signal.py

Turns the backtested momentum strategy into a real BUY/SELL/HOLD decision on
live data. Reuses the SAME signal functions (calculate_period_returns,
assign_ranks, get_top_etfs from Notebook 2 / functions.py) and the SAME
risk-management internals (inverse-vol sizing, position caps, regime filter,
vol targeting from momentum_backtest.py) as the backtest -- so the live logic
is not a re-implementation, it's the identical code path fed live data instead
of historical data.

SAFETY MODEL
------------
- DRY_RUN=True by default. In dry-run, orders are computed and logged but
  NEVER sent to a broker. You must explicitly pass --live to place real orders.
- Even in --live mode, this connects to IBKR's PAPER port (7497) unless you
  explicitly pass --port 7496 (live TWS) AND --confirm-live-trading.
  NOTE: IBKR convention is 7497 = paper, 7496 = live for TWS
  (4001/4002 for IB Gateway paper/live). Double-check YOUR TWS/Gateway
  configuration -- port numbers are configurable and vary by setup.
- Every run appends a full audit row (ticker, signal rank, target weight,
  drift, action, shares, price, timestamp, dry_run flag) to live_trades_log.csv
  BEFORE attempting any broker call, so you have a record even if the broker
  call fails.

USAGE
-----
    python -m momentum_trading.execution.live_signal                  # safe default (dry-run), just prints/logs orders
    python -m momentum_trading.execution.live_signal --live --port 7497          # places real paper orders
    python -m momentum_trading.execution.live_signal --live --port 7496 --confirm-live-trading   # REAL MONEY

There is no --dry-run flag -- dry-run is the default when --live is omitted (passing --dry-run
is an argparse error). In practice, daily_runner.py is the actual scheduled entry point (the
`daily-runner` console script) and calls this module's functions directly; invoking this file's
own __main__ block is mainly useful for isolated manual testing of the execution layer.

Run this once per rebalance date (monthly, matching holding_period=1), ideally
via cron/Task Scheduler shortly after market open.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..core import functions as fn
from ..core.audit_log import log_alert, ALERTS_LOG_PATH
from ..backtest.momentum_backtest import (
    BacktestConfig,
    resolve_target_weights,
    detect_correlation_spike,
)

logger = logging.getLogger("live_signal")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

TRADE_LOG_PATH = "live_trades_log.csv"


def is_rebalance_day(holding_period_months: int = 1, exchange: str = "NYSE",
                      reference_day_of_month: int = 1) -> bool:
    """
    True only on the Nth trading day of a rebalance month (default: the FIRST
    trading day of every month, matching holding_period=1). Lets you schedule
    this script to run EVERY day via cron/Task Scheduler and have it self-gate,
    instead of hand-calculating which calendar dates are holidays each year.
    """
    cal = mcal.get_calendar(exchange)
    today = pd.Timestamp.today().normalize()
    month_start = today.replace(day=1)
    month_end = (month_start + pd.DateOffset(months=1)) - pd.Timedelta(days=1)
    schedule = cal.schedule(start_date=month_start, end_date=month_end)
    trading_days_this_month = schedule.index

    if len(trading_days_this_month) == 0:
        return False

    target_day = trading_days_this_month[min(reference_day_of_month - 1, len(trading_days_this_month) - 1)]
    is_target = today.date() == target_day.date()

    # holding_period > 1 means only fire every Nth month, not every month
    if is_target and holding_period_months > 1:
        months_since_epoch = today.year * 12 + today.month
        return (months_since_epoch % holding_period_months) == 0
    return is_target


# --------------------------------------------------------------------------- #
# 1. SIGNAL GENERATION -- identical logic to Notebook 2, run on live data
# --------------------------------------------------------------------------- #
def calculate_period_returns(df_prices: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    return df_prices.ffill().pct_change(periods=period)


def assign_ranks(df_returns: pd.DataFrame) -> pd.DataFrame:
    return df_returns.rank(axis=1, ascending=False)


def get_top_etfs(df_ranks: pd.DataFrame, top_n: int = 10) -> list[str]:
    """Live version returns a plain list for "today", not a Series over history."""
    latest = df_ranks.iloc[-1]
    return latest.nsmallest(top_n).index.tolist()


def fetch_live_prices(
    tickers: list[str], lookback_days: int = 400,
    fmp_api_key: str | None = None, eodhd_api_key: str | None = None,
) -> pd.DataFrame:
    """
    Pull enough daily history (default ~400 days covers a 12-month lookback
    plus buffer) up through today via the SAME fetch path as the backtest.
    """
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    logger.info("Fetching live prices for %d tickers, %s to %s", len(tickers), start_date, end_date)
    return fn.get_bulk_prices(
        tickers, start_date, end_date, frequency="D",
        fmp_api_key=fmp_api_key, eodhd_api_key=eodhd_api_key,
    )


def check_price_staleness(daily_prices: pd.DataFrame, max_staleness_minutes: int, exchange: str = "NYSE") -> dict:
    """
    Epic 10, Story 10.3: guards against trading on a frozen/stale price feed
    (e.g. a data vendor outage that returns yesterday's data without erroring).

    NOTE on granularity: this system fetches DAILY bars, not intraday ticks, so
    a literal minute-level staleness check doesn't map cleanly onto the data --
    what actually matters is whether the latest available daily price is from
    the most recently completed trading session, not from a stale earlier one.
    This function compares the latest date in daily_prices against the most
    recent expected trading day (via the exchange calendar) and reports the
    gap in days; `max_staleness_minutes` is interpreted as "enable this check"
    with any positive value, converted to an approximate day tolerance
    (minutes / 1440, minimum 1 day) rather than used as a strict minute-level
    threshold, since sub-day staleness isn't meaningfully detectable from
    daily-close data alone.

    Returns
    -------
    dict: is_stale (bool), latest_available_date, most_recent_expected_trading_day,
    staleness_days (int or None if data is empty).
    """
    if daily_prices.empty:
        return {"is_stale": True, "latest_available_date": None,
                "most_recent_expected_trading_day": None, "staleness_days": None,
                "reason": "no price data returned at all"}

    latest_available = daily_prices.index.max()

    cal = mcal.get_calendar(exchange)
    today = pd.Timestamp.today().normalize()
    schedule = cal.schedule(start_date=today - pd.Timedelta(days=10), end_date=today)
    trading_days = schedule.index
    past_or_today = trading_days[trading_days <= today]
    most_recent_expected = past_or_today[-1] if len(past_or_today) else today

    staleness_days = (most_recent_expected.normalize() - latest_available.normalize()).days
    day_tolerance = max(1, max_staleness_minutes // 1440) if max_staleness_minutes else 0

    is_stale = staleness_days > day_tolerance

    return {
        "is_stale": is_stale,
        "latest_available_date": latest_available,
        "most_recent_expected_trading_day": most_recent_expected,
        "staleness_days": staleness_days,
    }


# --------------------------------------------------------------------------- #
# 2. RISK-MANAGED TARGET WEIGHTS -- same internals as momentum_backtest.py
# --------------------------------------------------------------------------- #
def compute_target_weights(
    picks: list[str], daily_prices: pd.DataFrame, cfg: BacktestConfig,
    custom_weights: dict | None = None, momentum_scores: pd.Series | None = None,
    portfolio: str = "", alerts_log_path: str = ALERTS_LOG_PATH,
) -> tuple[dict, float]:
    """
    Returns (weights, gross_exposure) via resolve_target_weights() -- the SAME
    shared sizing function the backtest engine calls, so live sizing decisions
    are provably the same code as what was validated historically, not a
    parallel reimplementation that could silently drift.

    custom_weights : dict, optional
        {ticker: weight} to use directly instead of algorithmic inverse-vol
        sizing (still subject to position caps). See resolve_target_weights()
        in momentum_backtest.py for details.
    momentum_scores : pd.Series, optional
        Required for cfg.sizing_method == "score_proportional" (Epic 9, Story
        9.1) -- ignored otherwise.
    """
    as_of = daily_prices.index[-1]
    weights = resolve_target_weights(picks, daily_prices, as_of, cfg, custom_weights=custom_weights,
                                      momentum_scores=momentum_scores)

    regime_scalar = 1.0
    if cfg.use_regime_filter and cfg.regime_benchmark in daily_prices.columns:
        bench = daily_prices[cfg.regime_benchmark]
        sma = bench.rolling(cfg.regime_sma_window, min_periods=cfg.regime_sma_window // 2).mean()
        bullish = bool(bench.iloc[-1] >= sma.iloc[-1]) if pd.notna(sma.iloc[-1]) else True
        regime_scalar = 1.0 if bullish else cfg.min_gross_exposure
        logger.info("Regime filter: %s is %s its %dD SMA -> scalar=%.2f",
                    cfg.regime_benchmark, "above" if bullish else "below",
                    cfg.regime_sma_window, regime_scalar)

    # --- Epic 25, Story 25.4: correlation-spike defensive scaling, live-trading
    #     equivalent of the backtest's use_correlation_spike_regime (same placement,
    #     same defensive action -- momentum_backtest.py's run_risk_managed_backtest). ---
    if cfg.use_correlation_spike_regime:
        spike = detect_correlation_spike(
            daily_prices, as_of,
            cfg.correlation_spike_short_window, cfg.correlation_spike_baseline_window,
            cfg.correlation_spike_threshold,
        )
        if spike:
            regime_scalar = min(regime_scalar, cfg.min_gross_exposure)
            logger.warning("CORRELATION SPIKE DETECTED: reducing exposure to %.0f%%",
                            cfg.min_gross_exposure * 100)
            log_alert(portfolio, "CORRELATION_SPIKE_DETECTED", "WARNING",
                      f"Reducing exposure to {cfg.min_gross_exposure:.0%}", log_path=alerts_log_path)

    gross_exposure = min(cfg.max_gross_exposure, regime_scalar)
    return weights, gross_exposure


# --------------------------------------------------------------------------- #
# 3. ORDER GENERATION -- target weights + real broker positions -> BUY/SELL/HOLD
# --------------------------------------------------------------------------- #
def compute_aggregate_drift(target_dollar: dict, current_value: dict, total_value: float) -> float:
    """
    Epic 25, Story 25.3: same formula as the backtest's aggregate-drift skip
    (run_risk_managed_backtest() in momentum_backtest.py) -- sum of absolute dollar
    drift across every ticker (current + target), as a fraction of total_value.
    Extracted as a pure function (matching this codebase's pattern of pulling out
    small pure helpers like _apply_position_caps) so it's directly unit-testable
    without a live price feed or broker connection.
    """
    if total_value <= 0:
        return 0.0
    all_tickers = set(current_value) | set(target_dollar)
    raw_trades = sum(abs(target_dollar.get(t, 0.0) - current_value.get(t, 0.0)) for t in all_tickers)
    return raw_trades / total_value


def generate_orders(
    current_holdings: dict,      # {ticker: shares}, pulled from broker, NOT local memory
    target_weights: dict,        # {ticker: weight}, from compute_target_weights()
    gross_exposure: float,
    total_value: float,
    latest_prices: dict,         # {ticker: price}
    cfg: BacktestConfig,
    signal_context: dict | None = None,  # optional {ticker: {'rank': int, 'signal_score': float}}
) -> dict:
    """
    Returns {ticker: {'action': 'BUY'|'SELL'|'HOLD', 'shares': int, 'reason': str,
                       'rank': int|None, 'signal_score': float|None}}
    Applies the same drift_threshold / min_trade_size filtering as the backtest,
    so live turnover matches the cost assumptions the backtest validated against.

    signal_context (Epic 4, Story 4.2), if provided, is carried through into
    each order dict so a trade can be reviewed later with "why" context (e.g.
    "XLK was rank 2 of 10"), not just "what" was traded.
    """
    signal_context = signal_context or {}
    current_value = {t: s * latest_prices.get(t, 0.0) for t, s in current_holdings.items()}
    target_dollar = {t: total_value * gross_exposure * w for t, w in target_weights.items()}
    all_tickers = set(current_holdings) | set(target_dollar)

    def _with_context(order: dict, ticker: str) -> dict:
        ctx = signal_context.get(ticker, {})
        order["rank"] = ctx.get("rank")
        order["signal_score"] = ctx.get("signal_score")
        return order

    orders = {}
    for t in all_tickers:
        price = latest_prices.get(t)
        if price is None or price <= 0:
            orders[t] = _with_context({"action": "HOLD", "shares": 0, "reason": "no live price available"}, t)
            continue

        c_dollar = current_value.get(t, 0.0)
        tgt_dollar = target_dollar.get(t, 0.0)
        drift_dollar = tgt_dollar - c_dollar
        is_continuing = (t in current_value) and (t in target_dollar)

        if abs(drift_dollar) < cfg.min_trade_size:
            orders[t] = _with_context({"action": "HOLD", "shares": 0, "reason": f"below min_trade_size (${abs(drift_dollar):.2f})"}, t)
            continue
        if is_continuing and total_value > 0 and abs(drift_dollar) / total_value < cfg.drift_threshold:
            orders[t] = _with_context({"action": "HOLD", "shares": 0, "reason": f"within drift_threshold ({abs(drift_dollar)/total_value:.1%})"}, t)
            continue

        shares = abs(drift_dollar) / price
        shares = (np.floor(shares * 10_000) / 10_000) if cfg.allow_fractional_shares else int(shares)
        if shares <= 0:
            orders[t] = _with_context({"action": "HOLD", "shares": 0, "reason": "drift too small for 1 share"}, t)
        else:
            action = "BUY" if drift_dollar > 0 else "SELL"
            orders[t] = _with_context({"action": action, "shares": shares, "reason": f"drift ${drift_dollar:,.2f}"}, t)

    return orders


# --------------------------------------------------------------------------- #
# 4. AUDIT LOG -- written BEFORE any broker call, regardless of outcome
# --------------------------------------------------------------------------- #
def _config_hash(cfg) -> str:
    """
    Short hash identifying the exact BacktestConfig used, so every trade in
    the audit log can be tied back to the risk settings that produced it
    (Epic 2, Story 2.3). Order-independent (sorted) so field-addition order
    doesn't change the hash unnecessarily.
    """
    import hashlib
    from dataclasses import asdict
    payload = str(sorted(asdict(cfg).items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _last_row_hash(path: str) -> str:
    """Reads the hash of the last row written, for hash-chaining (Epic 2, Story 2.5)."""
    if not os.path.isfile(path):
        return "GENESIS"
    try:
        with open(path, "r") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:  # header only, or empty
            return "GENESIS"
        return rows[-1][-1]  # last column of last row = row_hash
    except Exception:
        return "GENESIS"


def _compute_row_hash(prev_hash: str, row_fields: list) -> str:
    import hashlib
    payload = prev_hash + "|" + "|".join(str(f) for f in row_fields)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def verify_log_integrity(path: str) -> dict:
    """
    Verification utility (Epic 2, Story 2.5): re-walks the hash chain and
    confirms no row was altered or removed after the fact. A plain CSV a
    script can also freely rewrite is not tamper-evident on its own -- this
    at least makes tampering DETECTABLE (recomputed hashes won't match),
    even though the file itself isn't write-protected at the OS level.
    """
    if not os.path.isfile(path):
        return {"valid": True, "rows_checked": 0, "note": "file does not exist"}

    with open(path, "r") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return {"valid": True, "rows_checked": 0}

    header = rows[0]
    prev_hash = "GENESIS"
    for i, row in enumerate(rows[1:], start=1):
        claimed_hash = row[-1]
        recomputed = _compute_row_hash(prev_hash, row[:-1])
        if recomputed != claimed_hash:
            return {"valid": False, "rows_checked": i, "first_bad_row": i,
                     "expected": recomputed, "found": claimed_hash}
        prev_hash = claimed_hash

    return {"valid": True, "rows_checked": len(rows) - 1}


def log_orders(orders: dict, latest_prices: dict, dry_run: bool, path: str = TRADE_LOG_PATH,
                cfg=None) -> None:
    """
    NOTE on schema evolution (Epic 4, Story 4.2): this adds 'rank' and
    'signal_score' columns. If you have an existing log file from before this
    change, its header won't have these columns -- appending new-schema rows
    to an old-schema file will misalign columns. Archive/rename any pre-existing
    live_trades_log_*.csv before your first run after upgrading, so a fresh
    file with the new header gets created.
    """
    file_exists = os.path.isfile(path)
    config_hash = _config_hash(cfg) if cfg is not None else ""
    prev_hash = _last_row_hash(path)

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "ticker", "action", "shares", "price", "reason",
                              "rank", "signal_score", "dry_run", "config_hash", "row_hash"])
        ts = datetime.now().isoformat()
        for ticker, order in orders.items():
            row_fields = [
                ts, ticker, order["action"], order["shares"], latest_prices.get(ticker, ""),
                order["reason"], order.get("rank", ""), order.get("signal_score", ""),
                dry_run, config_hash,
            ]
            row_hash = _compute_row_hash(prev_hash, row_fields)
            writer.writerow(row_fields + [row_hash])
            prev_hash = row_hash
    logger.info("Logged %d order decisions to %s (config_hash=%s)", len(orders), path, config_hash)


# --------------------------------------------------------------------------- #
# 4b. REAL PROFIT MEASUREMENT (from actual live_trades_log.csv, not a simulation)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 4c. PORTFOLIO SNAPSHOT (Epic 4, Story 4.1) -- one row per run, not per trade
# --------------------------------------------------------------------------- #
def write_portfolio_snapshot(
    name: str, current_positions: dict, latest_prices: dict, total_value: float, cash: float,
    benchmark_ticker: str | None = None, snapshot_dir: str = "data",
) -> str:
    """
    Writes a single summary row capturing "where things stand today" -- distinct
    from the trade log, which only has rows on days something was bought/sold.
    Without this, answering "what's my portfolio worth right now" requires
    replaying the entire trade log through measure_live_performance(); this is
    the fast path, meant to be called once per run regardless of whether it's
    a rebalance day.

    Also stores the benchmark's current price (if benchmark_ticker + its price
    are available in latest_prices) so the NEXT call can compute a real
    period-over-period return for both portfolio and benchmark by comparing
    against the previous row (Epic 4, Story 4.3) -- without needing a separate
    price history lookup.

    Parameters
    ----------
    current_positions : dict {ticker: {'shares': float, 'avg_entry_price': float}}
    latest_prices : dict {ticker: float}
    total_value, cash : float
    benchmark_ticker : str, optional -- e.g. cfg.regime_benchmark ("SPY")

    Returns
    -------
    Path to the snapshot CSV written to.
    """
    os.makedirs(snapshot_dir, exist_ok=True)
    path = os.path.join(snapshot_dir, f"portfolio_snapshot_{name}.csv")
    file_exists = os.path.isfile(path)

    positions_value = 0.0
    unrealized_pnl = 0.0
    position_details = []
    for ticker, pos in current_positions.items():
        shares = pos.get("shares", 0)
        entry = pos.get("avg_entry_price")
        price = latest_prices.get(ticker)
        if price is None or shares <= 0:
            continue
        market_value = shares * price
        positions_value += market_value
        if entry:
            unrealized_pnl += shares * (price - entry)
        position_details.append(f"{ticker}:{shares:.4f}@{price:.2f}")

    benchmark_price = latest_prices.get(benchmark_ticker) if benchmark_ticker else None

    # --- compute period returns by comparing to the prior row, if one exists ---
    portfolio_period_return = None
    benchmark_period_return = None
    prior = get_latest_snapshot(name, snapshot_dir)
    if prior is not None:
        prior_total = prior.get("total_value")
        if prior_total not in (None, "", 0) and float(prior_total) > 0:
            portfolio_period_return = (total_value / float(prior_total)) - 1
        prior_bench_price = prior.get("benchmark_price")
        if benchmark_price is not None and prior_bench_price not in (None, "", 0):
            try:
                portfolio_period_return = portfolio_period_return  # unchanged
                benchmark_period_return = (benchmark_price / float(prior_bench_price)) - 1
            except (TypeError, ValueError):
                benchmark_period_return = None

    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date", "total_value", "cash", "positions_value", "unrealized_pnl",
                "n_positions", "positions_detail", "benchmark_ticker", "benchmark_price",
                "portfolio_period_return", "benchmark_period_return",
            ])
        writer.writerow([
            datetime.now().date().isoformat(), f"{total_value:.2f}", f"{cash:.2f}",
            f"{positions_value:.2f}", f"{unrealized_pnl:.2f}", len(position_details),
            ";".join(position_details), benchmark_ticker or "",
            f"{benchmark_price:.4f}" if benchmark_price is not None else "",
            f"{portfolio_period_return:.6f}" if portfolio_period_return is not None else "",
            f"{benchmark_period_return:.6f}" if benchmark_period_return is not None else "",
        ])
    logger.info("[%s] Portfolio snapshot written: total_value=$%.2f, %d positions",
                name, total_value, len(position_details))
    return path


def get_latest_snapshot(name: str, snapshot_dir: str = "data") -> dict | None:
    """Fast 'where do things stand today' read -- last row only, no full-log replay."""
    path = os.path.join(snapshot_dir, f"portfolio_snapshot_{name}.csv")
    if not os.path.isfile(path):
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    return df.iloc[-1].to_dict()


def measure_live_performance(
    start_date: str, end_date: str,
    latest_prices: dict | None = None,
    log_path: str = TRADE_LOG_PATH,
    initial_capital: float | None = None,
) -> dict:
    """
    Computes REAL realized + unrealized P&L from the actual order log written
    by log_orders() -- this reads what genuinely happened (or would have, in
    dry-run), not a backtest re-simulation. Uses FIFO lot matching for realized
    gains, same convention as most brokers' tax reporting.

    Parameters
    ----------
    start_date, end_date : str, 'YYYY-MM-DD'
        Window to measure. Only rows with dry_run matching your actual live
        mode are meaningful for a real account -- filter the log accordingly
        before calling this if you mix dry-run and live runs in one file.
    latest_prices : dict, optional
        {ticker: price} for marking open positions to market as of `end_date`.
        If omitted, unrealized P&L on still-open positions is left as NaN.
    initial_capital : float, optional
        If provided, also returns total_return_pct.

    Returns
    -------
    dict: realized_pnl, unrealized_pnl, total_pnl, open_positions (dict),
          trade_count, per_ticker (DataFrame breakdown).
    """
    if not os.path.isfile(log_path):
        raise FileNotFoundError(f"No trade log found at {log_path} -- nothing has been logged yet.")

    log = pd.read_csv(log_path, parse_dates=["timestamp"])
    log = log[(log["timestamp"] >= start_date) & (log["timestamp"] <= end_date)]
    log = log[log["action"].isin(["BUY", "SELL"])].sort_values("timestamp")

    realized_pnl = 0.0
    open_lots: dict[str, list] = {}   # ticker -> list of [shares, price] FIFO queue
    per_ticker_realized: dict[str, float] = {}

    for _, row in log.iterrows():
        ticker, action, shares, price = row["ticker"], row["action"], float(row["shares"]), float(row["price"])
        if shares <= 0:
            continue
        lots = open_lots.setdefault(ticker, [])

        if action == "BUY":
            lots.append([shares, price])
        elif action == "SELL":
            remaining = shares
            while remaining > 1e-9 and lots:
                lot_shares, lot_price = lots[0]
                matched = min(remaining, lot_shares)
                pnl = matched * (price - lot_price)
                realized_pnl += pnl
                per_ticker_realized[ticker] = per_ticker_realized.get(ticker, 0.0) + pnl
                lot_shares -= matched
                remaining -= matched
                if lot_shares <= 1e-9:
                    lots.pop(0)
                else:
                    lots[0][0] = lot_shares
            if remaining > 1e-9:
                logger.warning("SELL of %.4f %s exceeds logged open lots by %.4f shares -- "
                                "log may not cover full history; realized P&L may be understated.",
                                shares, ticker, remaining)

    open_positions = {t: sum(s for s, _ in lots) for t, lots in open_lots.items() if sum(s for s, _ in lots) > 1e-9}
    unrealized_pnl = 0.0
    if latest_prices:
        for t, lots in open_lots.items():
            total_shares = sum(s for s, _ in lots)
            if total_shares <= 1e-9 or t not in latest_prices:
                continue
            cost_basis = sum(s * p for s, p in lots)
            market_value = total_shares * latest_prices[t]
            unrealized_pnl += market_value - cost_basis
    else:
        unrealized_pnl = np.nan

    result = {
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": realized_pnl + (unrealized_pnl if not np.isnan(unrealized_pnl) else 0.0),
        "open_positions": open_positions,
        "trade_count": len(log),
        "per_ticker_realized": per_ticker_realized,
    }
    if initial_capital:
        result["total_return_pct"] = result["total_pnl"] / initial_capital
    return result


def derive_entry_date(ticker: str, trade_log_path: str = TRADE_LOG_PATH) -> pd.Timestamp | None:
    """
    Epic 25, Story 25.2: live-side equivalent of the backtest's entry_dates tracking
    (momentum_backtest.py's run_risk_managed_backtest), so max_holding_days means the
    same thing in live trading as it did when the strategy was backtested with the same
    setting. There's no in-memory day-by-day loop in live trading to track this the way
    the backtest does, so it's derived from the trade log instead.

    Walks the log chronologically for `ticker` and returns the timestamp of the BUY
    that started the CURRENTLY open position's unbroken holding streak -- persists
    across partial adds/trims, resets only when the position was last fully flat
    (matching the backtest's exact entry_dates semantics, not just "most recent buy").

    Returns None if the log doesn't exist, has no rows for this ticker, or the position
    is currently flat.
    """
    if not os.path.isfile(trade_log_path):
        return None

    log = pd.read_csv(trade_log_path, parse_dates=["timestamp"])
    log = log[(log["ticker"] == ticker) & (log["action"].isin(["BUY", "SELL"]))].sort_values("timestamp")
    if log.empty:
        return None

    cumulative_shares = 0.0
    streak_start = None
    for _, row in log.iterrows():
        shares = float(row["shares"])
        if shares <= 0:
            continue
        if row["action"] == "BUY":
            if cumulative_shares <= 1e-9:
                streak_start = row["timestamp"]
            cumulative_shares += shares
        else:  # SELL
            cumulative_shares -= shares
            if cumulative_shares <= 1e-9:
                cumulative_shares = 0.0
                streak_start = None

    return streak_start


# --------------------------------------------------------------------------- #
# 4c. MULTIPLE PORTFOLIOS, SAME STRATEGY
# --------------------------------------------------------------------------- #
def run_multi_portfolio(
    portfolios: dict[str, dict],
    total_value_per_portfolio: dict[str, float] | float,
    cfg: BacktestConfig | dict[str, BacktestConfig],
    top_n: int = 3,
    lookback_period: int = 12,
    dry_run: bool = True,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None,
    current_holdings_per_portfolio: dict[str, dict] | None = None,
) -> dict:
    """
    Runs the SAME strategy independently across multiple named portfolios --
    each gets its own signal, its own sizing, its own orders, and its own
    trade log (separate CSV per portfolio, so P&L doesn't mix).

    Parameters
    ----------
    portfolios : dict[name] -> {"tickers": [...], "custom_weights": {...} or None}
        Co-locating tickers and custom_weights per portfolio (rather than two
        parallel dicts) avoids a whole class of bugs where the two could
        silently get out of sync. Example:
            {"portfolio1": {"tickers": ["SPY","QQQ","XLK"], "custom_weights": None},
             "portfolio2": {"tickers": ["XLF","XLE","GLD","TLT"],
                             "custom_weights": {"XLF": 0.4, "XLE": 0.3, "GLD": 0.2, "TLT": 0.1}}}
        Backward-compatible: a plain list of tickers (the old shape) is still
        accepted and treated as {"tickers": [...], "custom_weights": None}.
    total_value_per_portfolio : dict[name] -> $ value, or a single float applied to all
    cfg : BacktestConfig or dict[name] -> BacktestConfig
        Same risk config applied to every portfolio, OR a distinct config per
        portfolio if you want different risk settings per portfolio.
    current_holdings_per_portfolio : dict[name] -> {ticker: shares}, optional
        Real per-portfolio holdings (e.g. from separate IBKR sub-accounts).
        Defaults to {} per portfolio if not provided (fresh-start assumption).

    Returns
    -------
    dict[portfolio_name] -> orders dict (same shape as run()'s return value)
    """
    results = {}
    for name, spec in portfolios.items():
        # backward-compat: accept a plain ticker list as well as the new dict shape
        if isinstance(spec, list):
            spec = {"tickers": spec, "custom_weights": None}
        tickers = spec["tickers"]
        custom_weights = spec.get("custom_weights")

        value = (total_value_per_portfolio if isinstance(total_value_per_portfolio, (int, float))
                  else total_value_per_portfolio[name])
        portfolio_cfg = cfg if isinstance(cfg, BacktestConfig) else cfg[name]
        holdings = (current_holdings_per_portfolio or {}).get(name, {})

        logger.info("=" * 40)
        logger.info("Portfolio: %s | tickers=%s | value=$%.2f | custom_weights=%s",
                    name, tickers, value, "yes" if custom_weights else "no (algorithmic sizing)")

        portfolio_log_path = f"live_trades_log_{name}.csv"

        orders = run(
            tickers=tickers,
            current_holdings=holdings,
            total_value=value,
            cfg=portfolio_cfg,
            top_n=min(top_n, len(tickers)),
            lookback_period=lookback_period,
            dry_run=dry_run,
            fmp_api_key=fmp_api_key,
            eodhd_api_key=eodhd_api_key,
            log_path=portfolio_log_path,
            custom_weights=custom_weights,
        )
        results[name] = orders

    return results


# --------------------------------------------------------------------------- #
# 5. BROKER EXECUTION -- IBKR, gated behind explicit --live flag
# --------------------------------------------------------------------------- #
def with_retry(fn_callable, max_attempts: int = 3, backoff_seconds: float = 2.0, *args, **kwargs):
    """
    Simple retry-with-backoff wrapper for transient network/API failures
    (price fetch, IBKR connection). Re-raises the last exception if all
    attempts fail, so callers still see a real error rather than a silent None.
    """
    import time
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn_callable(*args, **kwargs)
        except Exception as e:
            last_exc = e
            logger.warning("Attempt %d/%d failed for %s: %s", attempt, max_attempts, fn_callable.__name__, e)
            if attempt < max_attempts:
                time.sleep(backoff_seconds * attempt)
    raise last_exc


def get_ibkr_positions(port: int, client_id: int = 8, timeout: float = 5.0) -> dict:
    """
    Real broker positions -- {ticker: {'shares': float, 'avg_entry_price': float}} --
    via IBKR's reqPositions(). This is the source of truth for current_holdings;
    NEVER trust locally-tracked state (partial fills, manual trades, dividends
    all cause local state to drift from what the broker actually holds).
    """
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
    except ImportError:
        logger.error("ibapi not installed. Run: pip install ibapi --break-system-packages")
        return {}

    class PositionsApp(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)
            self.positions = {}
            self.done = False

        def position(self, account, contract, position, avgCost):
            if position != 0:
                self.positions[contract.symbol] = {"shares": float(position), "avg_entry_price": float(avgCost)}

        def positionEnd(self):
            self.done = True

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            logger.error("IBKR error %s: %s", errorCode, errorString)

    import threading, time
    app = PositionsApp()
    app.connect("127.0.0.1", port, clientId=client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    app.reqPositions()

    waited = 0.0
    while not app.done and waited < timeout:
        time.sleep(0.2)
        waited += 0.2

    app.disconnect()
    if not app.done:
        raise TimeoutError(f"reqPositions() did not complete within {timeout}s -- is TWS/Gateway running on port {port}?")
    return app.positions


def get_ibkr_account_value(port: int, client_id: int = 9, timeout: float = 5.0,
                            tag: str = "NetLiquidation") -> float:
    """
    Real account value via reqAccountSummary() -- replaces hardcoded total_value.

    tag : which IBKR account summary tag to fetch. Default "NetLiquidation" (total account
    equity) preserves every pre-Epic-28 call site's behavior unchanged. Also used with
    "AvailableFunds" (Epic 28, Story 28.2/28.3) -- real spendable cash, checked by
    place_orders_ibkr() before submitting BUY orders.
    """
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
    except ImportError:
        logger.error("ibapi not installed. Run: pip install ibapi --break-system-packages")
        return 0.0

    class AccountApp(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)
            self.value = None
            self.done = False

        def accountSummary(self, reqId, account, tag_name, value, currency):
            if tag_name == tag:
                self.value = float(value)

        def accountSummaryEnd(self, reqId):
            self.done = True

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            logger.error("IBKR error %s: %s", errorCode, errorString)

    import threading, time
    app = AccountApp()
    app.connect("127.0.0.1", port, clientId=client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    app.reqAccountSummary(1, "All", tag)

    waited = 0.0
    while not app.done and waited < timeout:
        time.sleep(0.2)
        waited += 0.2

    app.disconnect()
    if not app.done or app.value is None:
        raise TimeoutError(f"reqAccountSummary({tag}) did not complete within {timeout}s -- is TWS/Gateway running on port {port}?")
    return app.value


def check_slippage_tolerance(expected_price: float, actual_price: float, tolerance_pct: float) -> dict:
    """
    Epic 10, Story 10.2: pure function for the slippage-tolerance comparison,
    factored out of place_orders_ibkr() so the math is unit-testable without
    a real (or mocked) broker connection.
    """
    if expected_price <= 0 or actual_price <= 0:
        return {"exceeded": False, "deviation_pct": None}
    deviation = abs(actual_price - expected_price) / expected_price
    return {"exceeded": deviation > tolerance_pct, "deviation_pct": deviation}


def place_orders_ibkr(orders: dict, port: int, client_id: int = 7,
                       expected_prices: dict | None = None,
                       max_slippage_tolerance_pct: float | None = None,
                       auto_reduce_on_insufficient_cash: bool = False,
                       available_cash_fn=None, portfolio: str = "",
                       alerts_log_path: str = ALERTS_LOG_PATH) -> dict:
    """
    Requires `ibapi` (pip install ibapi --break-system-packages) and a running
    TWS or IB Gateway instance listening on `port`. Only called when --live
    is passed; dry-run mode never reaches this function.

    Returns {ticker: {'status': str, 'filled': float, 'avg_fill_price': float}}
    by polling orderStatus/execDetails after submission instead of firing
    orders and disconnecting blind after a fixed sleep.

    Epic 28: SELLs are always submitted first and confirmed (terminal status) before any
    BUY is submitted -- a BUY submitted before its funding SELL clears can be rejected on a
    cash account, or silently rely on margin buying power this code never used to check.
    Mirrors the backtest engine's explicit sells-first/buys-second structure.

    auto_reduce_on_insufficient_cash : bool
        After sells clear, if the real available cash (queried fresh from IBKR) can't cover
        every BUY at its computed size: False (default) logs a warning and submits BUYs as
        computed anyway, letting IBKR's own fill/reject be the backstop. True proportionally
        scales down BUY share counts (floored to whole shares) to fit. Either way, the
        shortfall is always logged -- this flag only controls whether anything is done about
        it here, never whether it's visible.
    available_cash_fn : callable() -> float, optional
        Injected so this is unit-testable without a real IBKR account-summary round trip.
        Defaults to `get_ibkr_account_value(port, tag="AvailableFunds")`.
    """
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
        from ibapi.contract import Contract
        from ibapi.order import Order
    except ImportError:
        logger.error("ibapi not installed. Run: pip install ibapi --break-system-packages")
        return {}

    class IBApp(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)
            self.next_order_id = None
            self.order_status = {}   # orderId -> {'status', 'filled', 'avg_fill_price', 'ticker'}

        def nextValidId(self, orderId: int):
            self.next_order_id = orderId

        def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                         permId=0, parentId=0, lastFillPrice=0, clientId=0, whyHeld="", mktCapPrice=0):
            entry = self.order_status.setdefault(orderId, {})
            entry.update(status=status, filled=float(filled), avg_fill_price=float(avgFillPrice))

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            logger.error("IBKR error %s: %s", errorCode, errorString)
            if reqId in self.order_status:
                self.order_status[reqId]["status"] = f"ERROR: {errorString}"

    import threading
    import time

    # --- Epic 8, Story 8.1: retry the CONNECTION only, never retry order
    #     submission itself. If a disconnect happens AFTER an order was
    #     already sent but before its confirmation arrived, blindly retrying
    #     the whole function could submit a duplicate order -- a much worse
    #     failure mode than just failing this run and alerting. So: connect
    #     with retries; once connected and order submission begins, any
    #     failure from that point fails loudly with no retry. ---
    app = None
    connect_attempts = 3
    for attempt in range(1, connect_attempts + 1):
        app = IBApp()
        try:
            app.connect("127.0.0.1", port, clientId=client_id)
            thread = threading.Thread(target=app.run, daemon=True)
            thread.start()
            time.sleep(1.5)  # allow nextValidId callback to fire
            if app.next_order_id is not None:
                break
            logger.warning("IBKR connect attempt %d/%d: no valid order ID yet.", attempt, connect_attempts)
        except Exception as e:
            logger.warning("IBKR connect attempt %d/%d failed: %s", attempt, connect_attempts, e)
            try:
                app.disconnect()
            except Exception:
                pass
        if attempt < connect_attempts:
            time.sleep(2.0 * attempt)

    if app is None or app.next_order_id is None:
        logger.error("Could not obtain a valid order ID from IBKR after %d attempts -- "
                     "is TWS/Gateway running on port %d? NO ORDERS WERE SUBMITTED.",
                     connect_attempts, port)
        if app is not None:
            app.disconnect()
        return {}

    def _submit_and_wait(order_subset: dict, start_order_id: int) -> tuple[dict, int]:
        """Submits every order in order_subset, polls until each reaches a terminal
        status. Returns ({orderId: ticker}, next_available_order_id)."""
        order_id_to_ticker = {}
        oid = start_order_id
        for ticker, order in order_subset.items():
            contract = Contract()
            contract.symbol = ticker
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"

            ib_order = Order()
            ib_order.action = order["action"]
            # NOTE: ibapi expects totalQuantity as a Decimal for fractional orders in
            # newer versions of the API. If placing fractional shares, use:
            #   from decimal import Decimal
            #   ib_order.totalQuantity = Decimal(str(order["shares"]))
            # Whole-share orders (the default) work fine as plain int/float.
            ib_order.totalQuantity = order["shares"]
            ib_order.orderType = "MKT"
            # Consider "MOC" (market-on-close) or limit orders with a price band
            # instead of raw "MKT" for anything beyond a liquid, tight-spread ETF.

            logger.info("Placing %s %d shares of %s (orderId=%d)", order["action"], order["shares"], ticker, oid)
            app.order_status[oid] = {"status": "SUBMITTED", "filled": 0.0, "avg_fill_price": 0.0}
            order_id_to_ticker[oid] = ticker
            app.placeOrder(oid, contract, ib_order)
            oid += 1
            time.sleep(0.3)  # simple pacing; IBKR rate-limits rapid order submission

        # --- poll for fill confirmation instead of a blind fixed sleep ---
        poll_timeout, waited = 15.0, 0.0
        while waited < poll_timeout:
            statuses = [app.order_status.get(o, {}).get("status") for o in order_id_to_ticker]
            if all(s in ("Filled", "Cancelled", "Inactive") or (s and s.startswith("ERROR")) for s in statuses):
                break
            time.sleep(0.5)
            waited += 0.5

        return order_id_to_ticker, oid

    def _collect_results(order_id_to_ticker: dict) -> dict:
        results = {}
        for oid, ticker in order_id_to_ticker.items():
            info = app.order_status.get(oid, {})
            status = info.get("status", "UNKNOWN")
            results[ticker] = {
                "status": status, "filled": info.get("filled", 0.0), "avg_fill_price": info.get("avg_fill_price", 0.0),
            }
            if status not in ("Filled",):
                logger.warning("Order for %s did not confirm as Filled (status=%s).", ticker, status)
            else:
                logger.info("Order for %s FILLED: %.4f shares @ $%.2f", ticker, info["filled"], info["avg_fill_price"])

                # --- Epic 10, Story 10.2: slippage tolerance check ---
                # Cannot un-fill an order that already executed -- this ALERTS on
                # excess deviation from the expected price rather than attempting
                # any reversal, so a bad fill is at least immediately visible
                # instead of silently accepted.
                if expected_prices and max_slippage_tolerance_pct and ticker in expected_prices:
                    expected = expected_prices[ticker]
                    actual = info["avg_fill_price"]
                    slip_check = check_slippage_tolerance(expected, actual, max_slippage_tolerance_pct)
                    if slip_check["exceeded"]:
                        logger.warning(
                            "SLIPPAGE TOLERANCE EXCEEDED: %s expected $%.2f, filled $%.2f (%.2f%% deviation, "
                            "tolerance %.2f%%). Fill already executed -- review manually.",
                            ticker, expected, actual, slip_check["deviation_pct"] * 100, max_slippage_tolerance_pct * 100,
                        )
                        log_alert(portfolio, "SLIPPAGE_TOLERANCE_EXCEEDED", "WARNING",
                                  f"{ticker} expected ${expected:.2f}, filled ${actual:.2f} "
                                  f"({slip_check['deviation_pct']:.2%} deviation, tolerance {max_slippage_tolerance_pct:.2%})",
                                  log_path=alerts_log_path)
                        results[ticker]["slippage_tolerance_exceeded"] = True
                        results[ticker]["slippage_deviation_pct"] = slip_check["deviation_pct"]
        return results

    # --- Epic 28, Story 28.3: SELLs first, always -- see docstring. ---
    sell_orders = {t: o for t, o in orders.items() if o["action"] == "SELL" and o["shares"] > 0}
    buy_orders = {t: o for t, o in orders.items() if o["action"] == "BUY" and o["shares"] > 0}

    next_oid = app.next_order_id
    sell_ids, next_oid = _submit_and_wait(sell_orders, next_oid)
    results = _collect_results(sell_ids)

    if buy_orders:
        # --- Epic 28, Story 28.3: cash-aware buy sizing, checked AFTER sells have
        #     cleared (so proceeds are reflected in the real available-cash query). ---
        priced_buys = {t: o for t, o in buy_orders.items() if expected_prices and t in expected_prices}
        unpriced = set(buy_orders) - set(priced_buys)
        if unpriced:
            logger.info("Cash-availability check skipped for %s -- no expected price available "
                        "to estimate their dollar value.", sorted(unpriced))

        if priced_buys:
            total_buy_value = sum(o["shares"] * expected_prices[t] for t, o in priced_buys.items())
            cash_fn = available_cash_fn or (lambda: get_ibkr_account_value(port, tag="AvailableFunds"))
            try:
                available_cash = cash_fn()
            except Exception as e:
                logger.warning("Could not fetch available cash for buy-sizing check (non-fatal, "
                               "proceeding with buys as computed): %s", e)
                available_cash = None

            if available_cash is not None and total_buy_value > available_cash:
                shortfall = total_buy_value - available_cash
                logger.warning("INSUFFICIENT CASH: buy orders total $%.2f, available cash $%.2f "
                               "(shortfall $%.2f).", total_buy_value, available_cash, shortfall)
                log_alert(portfolio, "INSUFFICIENT_CASH", "WARNING",
                          f"Buy orders total ${total_buy_value:,.2f}, available cash ${available_cash:,.2f} "
                          f"(shortfall ${shortfall:,.2f}).", log_path=alerts_log_path)
                if auto_reduce_on_insufficient_cash and total_buy_value > 0:
                    scale = available_cash / total_buy_value
                    reduced = {}
                    for t, o in buy_orders.items():
                        if t not in priced_buys:
                            reduced[t] = o  # no price to scale against -- leave as computed
                            continue
                        new_shares = int(o["shares"] * scale)  # floor -- always the safe direction
                        if new_shares <= 0:
                            logger.warning("Dropping BUY %s -- reduced to 0 shares after "
                                           "cash-availability scaling.", t)
                            continue
                        reduced[t] = {**o, "shares": new_shares}
                        logger.info("Reduced BUY %s: %d -> %d shares (cash-availability scaling "
                                   "factor %.3f).", t, o["shares"], new_shares, scale)
                    buy_orders = reduced

        buy_ids, next_oid = _submit_and_wait(buy_orders, next_oid)
        results.update(_collect_results(buy_ids))

    app.disconnect()
    return results


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def run(
    tickers: list[str],
    current_holdings: dict,
    total_value: float,
    cfg: BacktestConfig,
    top_n: int = 10,
    lookback_period: int = 12,
    dry_run: bool = True,
    ibkr_port: int = 7497,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None,
    log_path: str = TRADE_LOG_PATH,
    custom_weights: dict | None = None,
    portfolio: str = "",
    alerts_log_path: str = ALERTS_LOG_PATH,
) -> dict:
    daily_prices = with_retry(fetch_live_prices, 3, 2.0, tickers, fmp_api_key=fmp_api_key, eodhd_api_key=eodhd_api_key)
    if daily_prices.empty:
        logger.error("No price data returned; aborting.")
        return {}

    monthly_prices = daily_prices.resample("ME").last()
    scores = calculate_period_returns(monthly_prices, period=lookback_period).dropna(how="all")
    ranks = assign_ranks(scores)
    picks = get_top_etfs(ranks, top_n=top_n)
    logger.info("Today's signal picks (top %d): %s", top_n, picks)

    # Epic 4, Story 4.2: capture rank/score context so a trade can be reviewed
    # later with "why" (e.g. "XLK was rank 2 of 10"), not just "what" was traded.
    signal_context = {}
    latest_scores = None
    if not ranks.empty:
        latest_ranks = ranks.iloc[-1]
        latest_scores = scores.iloc[-1] if not scores.empty else None
        for t in picks:
            signal_context[t] = {
                "rank": int(latest_ranks[t]) if t in latest_ranks.index and pd.notna(latest_ranks[t]) else None,
                "signal_score": float(latest_scores[t]) if latest_scores is not None and t in latest_scores.index and pd.notna(latest_scores[t]) else None,
            }

    weights, gross_exposure = compute_target_weights(picks, daily_prices, cfg, custom_weights=custom_weights,
                                                       momentum_scores=latest_scores, portfolio=portfolio,
                                                       alerts_log_path=alerts_log_path)
    logger.info("Target weights: %s | Gross exposure: %.1f%%", weights, gross_exposure * 100)

    latest_prices = daily_prices.iloc[-1].to_dict()

    # --- Epic 25, Story 25.3: aggregate-drift skip -- bypass the ENTIRE rebalance if
    #     total portfolio drift is trivial, even if some individual tickers exceed
    #     drift_threshold. Same formula/semantics as the backtest's
    #     run_risk_managed_backtest(); 0 (default) disables this and preserves prior
    #     behavior exactly. ---
    if cfg.aggregate_drift_threshold > 0:
        current_value = {t: s * latest_prices.get(t, 0.0) for t, s in current_holdings.items()}
        target_dollar = {t: total_value * gross_exposure * w for t, w in weights.items()}
        aggregate_drift = compute_aggregate_drift(target_dollar, current_value, total_value)
        if aggregate_drift < cfg.aggregate_drift_threshold:
            logger.info("SKIP REBALANCE: aggregate drift %.2f%% < aggregate_drift_threshold %.2f%%",
                        aggregate_drift * 100, cfg.aggregate_drift_threshold * 100)
            log_alert(portfolio, "AGGREGATE_DRIFT_SKIP", "INFO",
                      f"Aggregate drift {aggregate_drift:.2%} < threshold {cfg.aggregate_drift_threshold:.2%}",
                      log_path=alerts_log_path)
            return {}

    orders = generate_orders(current_holdings, weights, gross_exposure, total_value, latest_prices, cfg,
                              signal_context=signal_context)

    for ticker, order in orders.items():
        logger.info("%-6s %-4s shares=%-4d (%s)", ticker, order["action"], order["shares"], order["reason"])

    # --- Epic 2, Story 2.4: advisory capacity check (best-effort; never blocks trading) ---
    if cfg.max_pct_of_adv > 0:
        try:
            from ..core import functions_quant_extensions as fnx
            volume_frames = {}
            for t in tickers:
                try:
                    raw = fn.get_stock_prices(t, (pd.Timestamp.today() - pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
                                               pd.Timestamp.today().strftime("%Y-%m-%d"),
                                               fmp_api_key=fmp_api_key, eodhd_api_key=eodhd_api_key)
                    vol_col = next((c for c in raw.columns if c.lower() == "volume"), None)
                    if vol_col:
                        volume_frames[t] = raw[vol_col]
                except Exception:
                    continue
            if volume_frames:
                df_volume = pd.DataFrame(volume_frames)
                target_dollars = {t: total_value * gross_exposure * w for t, w in weights.items()}
                capacity = fnx.check_capacity(target_dollars, df_volume, daily_prices, daily_prices.index[-1],
                                               max_pct_of_adv=cfg.max_pct_of_adv)
                for t, c in capacity.items():
                    if c["flagged"]:
                        logger.warning("CAPACITY WARNING: %s target $%.2f is %.1f%% of ADV (limit %.1f%%)",
                                       t, c["target_dollar"], c["pct_of_adv"] * 100, cfg.max_pct_of_adv * 100)
        except Exception as e:
            logger.warning("Capacity check skipped due to error (non-fatal): %s", e)

    log_orders(orders, latest_prices, dry_run, path=log_path, cfg=cfg)

    if not dry_run:
        logger.warning("LIVE MODE: placing real orders via IBKR on port %d", ibkr_port)
        fill_results = place_orders_ibkr(orders, port=ibkr_port, expected_prices=latest_prices,
                                          max_slippage_tolerance_pct=cfg.max_slippage_tolerance_pct,
                                          auto_reduce_on_insufficient_cash=cfg.auto_reduce_buys_on_insufficient_cash,
                                          portfolio=portfolio, alerts_log_path=alerts_log_path)
        for ticker, fill in fill_results.items():
            if ticker in orders:
                orders[ticker]["fill_status"] = fill["status"]
                orders[ticker]["fill_price"] = fill["avg_fill_price"]
    else:
        logger.info("DRY RUN: no orders sent to broker. Use --live to actually trade.")

    return orders


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate and optionally execute live momentum BUY/SELL/HOLD signals.")
    parser.add_argument("--live", action="store_true", help="Actually place orders (default is dry-run).")
    parser.add_argument("--port", type=int, default=7497, help="IBKR port. 7497=paper TWS, 7496=live TWS (verify your setup).")
    parser.add_argument("--confirm-live-trading", action="store_true", help="Required in addition to --port 7496 to trade real money.")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--total-value", type=float, default=1000.0, help="Total account value; pull this from your broker in production.")
    parser.add_argument("--force", action="store_true", help="Run even if today isn't a scheduled rebalance day (NYSE calendar check).")
    args = parser.parse_args()

    if not args.force and not is_rebalance_day(holding_period_months=1):
        logger.info("Today is not a scheduled rebalance day. Exiting without action. Use --force to override.")
        sys.exit(0)

    if args.live and args.port == 7496 and not args.confirm_live_trading:
        logger.error("Refusing to trade on port 7496 (live) without --confirm-live-trading. Aborting.")
        sys.exit(1)

    # --- EXAMPLE universe/config -- replace with your real universe and current broker positions ---
    example_tickers = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLY", "XLP", "XLU", "GLD", "TLT", "BIL"]
    example_holdings = {}  # {} = no positions yet; in production, pull from IBKR reqPositions()

    cfg = BacktestConfig(
        holding_period=1, initial_capital=args.total_value, commission=0.0,
        drift_threshold=0.03, min_trade_size=25.0,
        use_regime_filter=True, regime_benchmark="SPY", regime_sma_window=150,
        max_position_weight=0.35,
    )

    run(
        tickers=example_tickers,
        current_holdings=example_holdings,
        total_value=args.total_value,
        cfg=cfg,
        top_n=args.top_n,
        dry_run=not args.live,
        ibkr_port=args.port,
    )
