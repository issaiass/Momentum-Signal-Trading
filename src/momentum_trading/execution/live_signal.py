"""
live_signal.py

Turns the backtested momentum strategy into a real BUY/SELL/HOLD decision on
live data. Reuses the SAME signal functions (calculate_period_returns,
assign_ranks, get_top_etfs from Notebook 2 / functions.py) and the SAME
risk-management internals (inverse-vol sizing, position caps, regime filter,
vol targeting from momentum_backtest.py) as the backtest, so the live logic
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
  configuration, port numbers are configurable and vary by setup.
- Every run appends a full audit row (ticker, signal rank, target weight,
  drift, action, shares, price, timestamp, dry_run flag) to live_trades_log.csv
  BEFORE attempting any broker call, so you have a record even if the broker
  call fails.

USAGE
-----
    python -m momentum_trading.execution.live_signal                  # safe default (dry-run), just prints/logs orders
    python -m momentum_trading.execution.live_signal --live --port 7497          # places real paper orders
    python -m momentum_trading.execution.live_signal --live --port 7496 --confirm-live-trading   # REAL MONEY

There is no --dry-run flag, dry-run is the default when --live is omitted (passing --dry-run
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
from ..core.paths import data_dir, logs_dir
from ..backtest.momentum_backtest import (
    BacktestConfig,
    resolve_target_weights,
    detect_correlation_spike,
    compute_vol_scalar,
)
from ..core.functions_quant_extensions import absolute_momentum_overlay

logger = logging.getLogger("live_signal")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
# daily_runner.py's logging.basicConfig() attaches its own handler to the ROOT logger,
# without this, every message here would print twice: once via this logger's own handler
# (needed so this module still prints when run standalone, e.g. its own __main__ block),
# once via propagation up to root's handler when imported into daily_runner.py's process.
logger.propagate = False

TRADE_LOG_PATH = str(logs_dir() / "live_trades_log.csv")

# IBKR_HOST: docker-compose.yml already sets this to host.docker.internal (Mac/Windows Docker
# Desktop) so the container can reach TWS/Gateway running on the host machine. Inside a container,
# "127.0.0.1" is the container itself (never where TWS/Gateway listens) so that can't be the
# unconditional default. Falls back to "127.0.0.1" for non-Docker (bare-metal/venv) usage, where
# TWS/Gateway genuinely does run on localhost relative to the script.
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")

# IBKR routes purely informational/status notices through the SAME EWrapper.error() callback
# as real errors (a quirk of the TWS API, not this codebase), these specific codes are
# IBKR's own "System"/"Warning" classification (see their API docs), not failures: 2104/2106/2108
# fire on every successful connect ("data farm connection is OK"), 2107/2158 are similar
# farm-status notices, 2119 is a market-data-type notice, 2137 is a warning, not an error.
# Logging these at ERROR severity on every single successful run (as this used to do) buries
# real errors (like a 502 "couldn't connect") in noise and can trip naive log/alert greps.
# 10349 is the same pattern for a per-ORDER notice: "Order TIF was set to DAY based on order
# preset" fires because place_orders_ibkr() never sets order.tif explicitly, so IBKR fills in
# its own account-level default and tells you, confirmed non-fatal empirically (orders
# carrying this exact code have gone on to fill seconds later with a real execDetails/
# commissionReport). Distinct from the codes above in one way: it DOES carry a real orderId
# (reqId), so place_orders_ibkr()'s error() callback below must not let it overwrite that
# order's tracked status to "ERROR: ...", doing so falsely marks a still-pending (or already
# filled) order as terminally failed and makes the poll loop stop watching it too early.
IBKR_INFORMATIONAL_CODES = {2104, 2106, 2107, 2108, 2119, 2137, 2158, 10349}


def _log_ibkr_message(reqId: int, errorCode: int, errorString: str) -> None:
    if errorCode in IBKR_INFORMATIONAL_CODES:
        logger.info("IBKR status %s: %s", errorCode, errorString)
    else:
        logger.error("IBKR error %s: %s", errorCode, errorString)


def is_holding_period_too_frequent(holding_period: float) -> bool:
    """
    True for any positive holding_period faster than weekly (< 0.25, i.e. more often than
    every 4 weeks' worth of months, see is_rebalance_day()'s weekly mapping). Single source
    of truth for this threshold so daily_runner.py's warning and its test don't each hardcode
    0.25 separately. Deliberately not a hard validation error in BacktestConfig, this is a
    real, well-defined schedule, just an economically inadvisable one (see daily_runner.py's
    per-portfolio WARNING check).
    """
    return 0 < holding_period < 0.25


def is_lookback_period_too_short(lookback_period: float, holding_period: float) -> bool:
    """
    True when the momentum-ranking window is short enough to be dominated by noise rather than
    real trend, a well-known problem with sub-2-week momentum signals, single-day price moves
    can flip the ranking. Only meaningful in the weekly regime (holding_period < 1), where
    lookback_period is interpreted in week-quarters via the same round(x * 4) formula
    resolve_momentum_scores() uses; always False in the monthly regime (holding_period >= 1),
    since a whole-month lookback is never "too short" the way a few days is, and
    BacktestConfig's own > 0 validation already rules out zero/negative values there.
    """
    if holding_period >= 1:
        return False
    weeks_lookback = max(1, round(lookback_period * 4))
    return weeks_lookback < 2


def _lookback_and_holding_in_common_unit(lookback_period: float, holding_period: float) -> tuple[float, float]:
    """
    Expresses lookback_period and holding_period in the SAME unit, weeks (via round(x * 4)) in
    the weekly regime (holding_period < 1), months directly otherwise, exactly matching
    resolve_momentum_scores()'s own regime-based interpretation of lookback_period. Shared by
    is_lookback_shorter_than_holding() and is_lookback_to_holding_ratio_too_low() so both
    constraints compare the same two numbers the signal calculation itself actually uses,
    never a separate unit-conversion convention that could drift out of sync with it.
    """
    if holding_period < 1:
        return max(1, round(lookback_period * 4)), max(1, round(holding_period * 4))
    return lookback_period, holding_period


def is_lookback_shorter_than_holding(lookback_period: float, holding_period: float) -> bool:
    """
    The "Momentum Persistence" constraint: a signal must be older than the period you intend to
    hold the asset, if lookback_period is not strictly greater than holding_period (in the same
    regime-appropriate unit), you're holding a position based on signal dynamics that are
    already stale by the time you exit. Equality counts as a violation ("older than", not "at
    least as old as").
    """
    lookback, holding = _lookback_and_holding_in_common_unit(lookback_period, holding_period)
    return lookback <= holding


def is_lookback_to_holding_ratio_too_low(lookback_period: float, holding_period: float) -> bool:
    """
    The "Lookback-to-Hold Ratio" constraint: for stable momentum, the signal's history should be
    meaningfully longer than the trade duration, academic convention is roughly 3-12x. A ratio
    below 3 risks "whipsawing", the position gets exited/re-entered based on noise within a
    lookback window barely longer than the holding period itself, rather than a persistent
    trend. Deliberately independent of is_lookback_shorter_than_holding(), not suppressed when
    that one already fired (a ratio < 1 trips both), matching this module's existing precedent
    of non-deduplicated advisory checks (e.g. is_holding_period_too_frequent() and
    is_lookback_period_too_short() can also both fire for the same misconfiguration). Only the
    low end is checked, warranted by the whipsaw rationale, there's no stated reason a high
    ratio is itself a problem.
    """
    lookback, holding = _lookback_and_holding_in_common_unit(lookback_period, holding_period)
    return (lookback / holding) < 3


def is_rebalance_day(holding_period_months: float = 1, exchange: str = "NYSE",
                      reference_day_of_month: int = 1,
                      today: pd.Timestamp | None = None) -> bool:
    """
    True only on the Nth trading day of a rebalance period. holding_period_months >= 1
    (the default, holding_period_months=1): the FIRST trading day of every month, or every
    Nth month if holding_period_months > 1, unchanged from this function's original behavior.
    holding_period_months < 1: weekly granularity instead, 0.25 = every week, 0.5 = every 2
    weeks, 0.75 = every 3 weeks (weeks_interval = round(holding_period_months * 4)), firing on
    the first trading day of the qualifying week. Lets you schedule this script to run EVERY
    day via cron/Task Scheduler and have it self-gate, instead of hand-calculating which
    calendar dates are holidays/weekends each year.

    today : pd.Timestamp | None
        Injectable for testing (defaults to the real current date), same dependency-injection
        pattern used elsewhere in this project (resolve_total_values()'s account_value_fn,
        circuit_breaker.py's alert_fn) so this function's date logic can be tested against a
        fixed date instead of the real calendar.
    """
    cal = mcal.get_calendar(exchange)
    today = (today if today is not None else pd.Timestamp.today()).normalize()

    if holding_period_months < 1:
        weeks_interval = max(1, round(holding_period_months * 4))
        week_start = today - pd.Timedelta(days=today.dayofweek)  # Monday of this week
        week_end = week_start + pd.Timedelta(days=6)
        schedule = cal.schedule(start_date=week_start, end_date=week_end)
        trading_days_this_week = schedule.index

        if len(trading_days_this_week) == 0:
            return False

        target_day = trading_days_this_week[0]
        is_target = today.date() == target_day.date()

        if is_target and weeks_interval > 1:
            # Weeks since a fixed Monday epoch, stable across year boundaries, unlike ISO
            # week numbers (which reset near Dec/Jan and some years have a 53rd week).
            weeks_since_epoch = (week_start - pd.Timestamp("1970-01-05")).days // 7
            return (weeks_since_epoch % weeks_interval) == 0
        return is_target

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


def most_recent_rebalance_target_date(holding_period_months: float = 1, exchange: str = "NYSE",
                                        reference_day_of_month: int = 1,
                                        today: pd.Timestamp | None = None) -> pd.Timestamp | None:
    """
    The most recent date STRICTLY BEFORE today that was itself a rebalance day
    (is_rebalance_day() would have returned True had it been asked about that date), or None if
    none falls within the lookback window. Distinct from is_rebalance_day() itself, which only
    answers "is TODAY the day", this answers "when was the last day I should have rebalanced",
    used to detect a rebalance day that was missed entirely (the process/container wasn't
    running that day), not just to gate today's own run.

    Pure, no file I/O, same injectable `today` pattern as is_rebalance_day() for testing.

    lookback_days=40 is a fixed margin, comfortably covering the monthly regime (at most ~31
    days between target days) and every weekly/multi-week holding_period variant this project
    supports, without needing to know holding_period's own cadence length in advance.
    """
    today = (today if today is not None else pd.Timestamp.today()).normalize()
    lookback_days = 40
    for offset in range(1, lookback_days + 1):
        candidate = today - pd.Timedelta(days=offset)
        if is_rebalance_day(holding_period_months, exchange, reference_day_of_month, today=candidate):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 1. SIGNAL GENERATION, identical logic to Notebook 2, run on live data
# --------------------------------------------------------------------------- #
def calculate_period_returns(df_prices: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    return df_prices.ffill().pct_change(periods=period)


def resolve_momentum_scores(
    daily_prices: pd.DataFrame, lookback_period: float, holding_period: float,
    skip_month_guardrail: bool = False,
) -> pd.DataFrame:
    """
    Resamples daily_prices to the granularity matching the strategy's cadence and computes
    trailing-period returns for momentum ranking, the single place run() decides monthly vs.
    weekly momentum. Deliberately a separate, directly-testable pure function (no IBKR/network
    dependency), matching this file's existing pattern (calculate_period_returns()/
    assign_ranks()/get_top_etfs() are all already small composable pure functions).

    holding_period < 1 (weekly rebalance cadence, see is_rebalance_day()'s identical weekly
    branch) resamples to WEEKLY and interprets lookback_period in week-quarters via the same
    round(x * 4) formula: 0.5 = 2 weeks, 0.75 = 3 weeks, 1.0 = 4 weeks, 1.5 = 6 weeks. This ties
    lookback_period's granularity to holding_period's regime rather than lookback_period's own
    value, so a short-term (weekly) strategy's lookback window is expressed on the SAME
    week-scale as its rebalance cadence, not mixed months/weeks, lookback_period=1.0 under a
    weekly holding_period means "4 weeks", not "1 month". skip_month_guardrail is ignored in
    this branch, it's inherently a monthly-lookback concept (see below).

    holding_period >= 1 (monthly+ cadence) resamples to MONTHLY exactly as before,
    lookback_period stays in whole months, this branch is byte-for-byte the existing behavior
    UNLESS skip_month_guardrail is True and lookback_period > 3: then the monthly-resampled
    series is shifted back one bar (excluding the most recent ~month) before computing the
    trailing return, the classic academic "12-1 momentum" construction, avoiding short-term
    reversal decay. This is an approximation of a 21-trading-day lag (one monthly-resampled
    bar), not a literal daily-granularity shift, documented explicitly rather than overclaiming
    precision. Default False, a no-op when lookback_period <= 3 even if True.
    """
    if holding_period < 1:
        weeks_lookback = max(1, round(lookback_period * 4))
        resampled = daily_prices.resample("W").last()
        return calculate_period_returns(resampled, period=weeks_lookback)
    monthly_prices = daily_prices.resample("ME").last()
    if skip_month_guardrail and lookback_period > 3:
        monthly_prices = monthly_prices.shift(1)
    return calculate_period_returns(monthly_prices, period=max(1, round(lookback_period)))


def assign_ranks(df_returns: pd.DataFrame) -> pd.DataFrame:
    return df_returns.rank(axis=1, ascending=False)


def get_top_etfs(df_ranks: pd.DataFrame, top_n: int = 10) -> list[str]:
    """Live version returns a plain list for "today", not a Series over history."""
    latest = df_ranks.iloc[-1]
    return latest.nsmallest(top_n).index.tolist()


def apply_absolute_momentum_filter(
    picks: list[str], latest_scores: pd.Series | None, defensive_ticker: str,
) -> list[str]:
    """
    Live-trading wrapper around core/functions_quant_extensions.py's
    absolute_momentum_overlay() (Antonacci-style dual momentum): reuses that function directly
    rather than reimplementing its swap rule, so backtest and live can never silently diverge
    on it, the same "one shared function" principle resolve_target_weights() already
    establishes for sizing.

    absolute_momentum_overlay() operates on a pd.Series of {date: picks} against a
    pd.DataFrame of {date: {ticker: score}}, both keyed by a date index (its natural shape when
    driven by a backtest's monthly_picks history). Live trading only ever has ONE "today", so
    this wraps `picks` in a length-1 Series under a placeholder date, calls the shared function,
    and unwraps the single result.

    latest_scores=None (no momentum-score history available yet) degrades conservatively to a
    no-op, returning `picks` unchanged, rather than raising or dropping everything to
    defensive_ticker on missing data.
    """
    if latest_scores is None:
        return picks
    placeholder_date = pd.Timestamp("2000-01-01")
    picks_series = pd.Series({placeholder_date: list(picks)})
    scores_df = pd.DataFrame([latest_scores], index=[placeholder_date])
    filtered = absolute_momentum_overlay(picks_series, scores_df, defensive_ticker=defensive_ticker)
    return list(filtered.loc[placeholder_date])


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


_OHLCV_COLUMN_ALIASES = {
    # FMP / EODHD / yfinance each name columns differently, normalize to lowercase
    # open/high/low/close/volume so core/technical_indicators.py has one consistent schema
    # to work with regardless of which vendor answered for a given ticker.
    "open": "open", "Open": "open",
    "high": "high", "High": "high",
    "low": "low", "Low": "low",
    "close": "close", "Close": "close",
    "volume": "volume", "Volume": "volume",
}


def fetch_ohlcv_for_tickers(
    tickers: list[str], lookback_days: int = 60,
    fmp_api_key: str | None = None, eodhd_api_key: str | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Per-ticker OHLCV (not just close), for technical indicator computation
    (core/technical_indicators.py), distinct from fetch_live_prices() above, which returns
    only close prices across many tickers at once (all that's needed for momentum ranking).
    Uses core/functions.py's single-symbol get_stock_prices() (same FMP -> EODHD -> yfinance
    auto-fallback chain), one call per ticker, since get_bulk_prices() collapses to close-only.

    lookback_days default (60) covers MACD's 26-period EMA plus buffer for the other
    indicators, without fetching as much history as fetch_live_prices()'s 400-day window
    (technical indicators don't need a 12-month lookback the way the momentum signal does).

    Returns {ticker: ohlcv_df} for tickers that fetched successfully, a ticker that fails
    (vendor outage, delisted symbol, etc.) is simply omitted rather than failing the whole
    batch, so one bad ticker can't block indicators for the rest of the portfolio.
    """
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    results = {}
    for ticker in tickers:
        try:
            df = fn.get_stock_prices(
                ticker, start_date, end_date,
                fmp_api_key=fmp_api_key, eodhd_api_key=eodhd_api_key,
            )
            df = df.rename(columns=_OHLCV_COLUMN_ALIASES)
            missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
            if missing:
                logger.warning("OHLCV fetch for %s missing columns %s, skipping indicators for it.",
                                ticker, missing)
                continue
            results[ticker] = df[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.warning("OHLCV fetch failed for %s, skipping indicators for it: %s", ticker, e)
    return results


def check_price_staleness(daily_prices: pd.DataFrame, max_staleness_minutes: int, exchange: str = "NYSE") -> dict:
    """
    Guards against trading on a frozen/stale price feed
    (e.g. a data vendor outage that returns yesterday's data without erroring).

    NOTE on granularity: this system fetches DAILY bars, not intraday ticks, so
    a literal minute-level staleness check doesn't map cleanly onto the data,
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


def _realized_weighted_portfolio_vol(
    weights: dict, daily_prices: pd.DataFrame, as_of: pd.Timestamp, lookback_days: int,
) -> float | None:
    """
    Live substitute for momentum_backtest.py's _realized_portfolio_vol(), which measures
    realized vol from a simulated portfolio_history equity curve that doesn't exist in live
    trading (no local ledger is ever trusted as portfolio truth, see get_ibkr_positions()'s own
    docstring). Instead, estimates forward risk directly from trailing daily_prices at the
    given target weights, the same "trailing data, not a simulated ledger" pattern
    _inverse_vol_weights() already uses for position-level sizing: combine each ticker's daily
    return by its target weight into one weighted daily-return series, then annualize its std
    dev over lookback_days.

    Returns None if there isn't enough trailing history for ANY weighted ticker, or if none of
    `weights`' tickers are even present in daily_prices, mirroring _realized_portfolio_vol()'s
    own None-when-insufficient-data behavior; compute_vol_scalar() falls back to
    max_gross_exposure in that case, the same "not enough information to scale down" behavior
    the backtest already has.
    """
    tickers = [t for t in weights if t in daily_prices.columns]
    if not tickers:
        return None
    window = daily_prices[tickers].loc[:as_of].tail(lookback_days + 1)
    if len(window) < lookback_days + 1:
        return None
    rets = window.pct_change().dropna(how="all")
    if rets.empty:
        return None
    w = pd.Series({t: weights[t] for t in tickers})
    weighted_rets = (rets[tickers].fillna(0.0) * w).sum(axis=1)
    if weighted_rets.empty:
        return None
    return float(weighted_rets.std() * np.sqrt(252))


# --------------------------------------------------------------------------- #
# 2. RISK-MANAGED TARGET WEIGHTS, same internals as momentum_backtest.py
# --------------------------------------------------------------------------- #
def compute_target_weights(
    picks: list[str], daily_prices: pd.DataFrame, cfg: BacktestConfig,
    custom_weights: dict | None = None, momentum_scores: pd.Series | None = None,
    portfolio: str = "", alerts_log_path: str = ALERTS_LOG_PATH,
) -> tuple[dict, float]:
    """
    Returns (weights, gross_exposure) via resolve_target_weights(), the SAME
    shared sizing function the backtest engine calls, so live sizing decisions
    are provably the same code as what was validated historically, not a
    parallel reimplementation that could silently drift.

    custom_weights : dict, optional
        {ticker: weight} to use directly instead of algorithmic inverse-vol
        sizing (still subject to position caps). See resolve_target_weights()
        in momentum_backtest.py for details.
    momentum_scores : pd.Series, optional
        Required for cfg.sizing_method == "score_proportional", ignored
        otherwise.
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

    # --- Correlation-spike defensive scaling, live-trading
    #     equivalent of the backtest's use_correlation_spike_regime (same placement,
    #     same defensive action, momentum_backtest.py's run_risk_managed_backtest). ---
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

    # --- Portfolio-level volatility targeting, live-trading equivalent of the backtest's
    #     target_portfolio_vol scaling (momentum_backtest.py's run_risk_managed_backtest,
    #     regime_scalar * vol_scalar). Previously ONLY existed in the backtest, live trading
    #     had no aggregate exposure throttling at all. Composes multiplicatively with
    #     regime_scalar exactly like the backtest, not a replacement for it. ---
    realized_vol = _realized_weighted_portfolio_vol(weights, daily_prices, as_of, cfg.portfolio_vol_lookback)
    vol_scalar = compute_vol_scalar(realized_vol, cfg.target_portfolio_vol,
                                     cfg.min_gross_exposure, cfg.max_gross_exposure)
    logger.info("Volatility targeting: realized_vol=%s target=%.2f -> scalar=%.2f",
                f"{realized_vol:.2%}" if realized_vol is not None else "n/a",
                cfg.target_portfolio_vol, vol_scalar)

    gross_exposure = min(cfg.max_gross_exposure, regime_scalar * vol_scalar)
    return weights, gross_exposure


# --------------------------------------------------------------------------- #
# 3. ORDER GENERATION, target weights + real broker positions -> BUY/SELL/HOLD
# --------------------------------------------------------------------------- #
def compute_aggregate_drift(target_dollar: dict, current_value: dict, total_value: float) -> float:
    """
    Same formula as the backtest's aggregate-drift skip
    (run_risk_managed_backtest() in momentum_backtest.py), sum of absolute dollar
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

    signal_context, if provided, is carried through into
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


def compute_turnover(orders: dict) -> float:
    """
    The "Turnover Limit" constraint: Total_Positions_Changed / Total_Positions for this
    rebalance. Total_Positions is every ticker generate_orders() produced a decision for (the
    union of currently-held and newly-targeted tickers, exactly orders.keys());
    Total_Positions_Changed is the count where action is BUY or SELL, HOLD (including "no live
    price available"/"below min_trade_size"/"within drift_threshold" holds) doesn't count as a
    change. High turnover is a sign the momentum ranking is over-sensitive to noise rather than
    tracking a persistent trend. Returns 0.0 for an empty dict (e.g. run()'s
    AGGREGATE_DRIFT_SKIP early-return), correctly "no turnover" when nothing traded.
    """
    if not orders:
        return 0.0
    changed = sum(1 for o in orders.values() if o.get("action") in ("BUY", "SELL"))
    return changed / len(orders)


def is_turnover_too_high(turnover_pct: float, max_turnover_pct: float) -> bool:
    """
    True when a rebalance's turnover (see compute_turnover()) exceeds the configured
    max_turnover_pct (BacktestConfig, default 0.20). A named function rather than an inline
    comparison in daily_runner.py, for consistency with this module's other is_*_too_*
    advisory-check functions.
    """
    return turnover_pct > max_turnover_pct


def compute_low_capital_drop_fraction(orders: dict) -> tuple[float, list[str]]:
    """
    Fraction of intended BUYs whose computed share count would floor to 0 (IBKR has no
    fractional-equity order support at all, place_orders_ibkr() floors and drops any BUY
    where int(shares) <= 0, status DROPPED_FRACTIONAL). Deliberately checks orders[t]["shares"]
    directly (generate_orders()'s raw computed value, set identically in dry-run and --live)
    rather than the fill_status field place_orders_ibkr() sets, since fill_status is LIVE-ONLY
    (place_orders_ibkr() never runs in dry-run) and this warning should catch a too-small
    capital base during a SAFE dry-run test too, not just discover it after already going
    live. shares < 1 for a BUY always predicts the same drop place_orders_ibkr() would make
    (int(shares) <= 0 iff shares < 1 for a positive share count). A high fraction here means
    total_value is too small relative to top_n and this portfolio's ticker prices for real
    capital to actually get deployed, distinct from turnover (compute_turnover() above), which
    measures signal noise, not capital sizing. Returns (0.0, []) for an empty dict or a
    rebalance with no intended BUYs (nothing to divide by, not "all dropped").
    """
    buys = {t: o for t, o in orders.items() if o.get("action") == "BUY"}
    if not buys:
        return 0.0, []
    dropped = [t for t, o in buys.items() if o.get("shares", 0) < 1]
    return len(dropped) / len(buys), dropped


def is_low_capital_drop_too_high(drop_fraction: float, low_capital_drop_warning_pct: float) -> bool:
    """
    True when compute_low_capital_drop_fraction()'s fraction exceeds the configured
    low_capital_drop_warning_pct (BacktestConfig, default 0.30). Named for consistency with
    this module's other is_*_too_* advisory-check functions.
    """
    return drop_fraction > low_capital_drop_warning_pct


# --------------------------------------------------------------------------- #
# 4. AUDIT LOG, written BEFORE any broker call, regardless of outcome
# --------------------------------------------------------------------------- #
def _config_hash(cfg) -> str:
    """
    Short hash identifying the exact BacktestConfig used, so every trade in
    the audit log can be tied back to the risk settings that produced it.
    Order-independent (sorted) so field-addition order
    doesn't change the hash unnecessarily.
    """
    import hashlib
    from dataclasses import asdict
    payload = str(sorted(asdict(cfg).items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _last_row_hash(path: str) -> str:
    """Reads the hash of the last row written, for hash-chaining."""
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
    Verification utility: re-walks the hash chain and
    confirms no row was altered or removed after the fact. A plain CSV a
    script can also freely rewrite is not tamper-evident on its own, this
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
    NOTE on schema evolution: this adds 'rank' and
    'signal_score' columns. If you have an existing log file from before this
    change, its header won't have these columns, appending new-schema rows
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
# 4c. PORTFOLIO SNAPSHOT, one row per run, not per trade
# --------------------------------------------------------------------------- #
def write_portfolio_snapshot(
    name: str, current_positions: dict, latest_prices: dict, total_value: float, cash: float,
    benchmark_ticker: str | None = None, snapshot_dir: str = str(data_dir()),
) -> str:
    """
    Writes a single summary row capturing "where things stand today", distinct
    from the trade log, which only has rows on days something was bought/sold.
    Without this, answering "what's my portfolio worth right now" requires
    replaying the entire trade log through measure_live_performance(); this is
    the fast path, meant to be called once per run regardless of whether it's
    a rebalance day.

    Also stores the benchmark's current price (if benchmark_ticker + its price
    are available in latest_prices) so the NEXT call can compute a real
    period-over-period return for both portfolio and benchmark by comparing
    against the previous row, without needing a separate
    price history lookup.

    Parameters
    ----------
    current_positions : dict {ticker: {'shares': float, 'avg_entry_price': float}}
    latest_prices : dict {ticker: float}
    total_value, cash : float
    benchmark_ticker : str, optional, e.g. cfg.regime_benchmark ("SPY")

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


def get_latest_snapshot(name: str, snapshot_dir: str = str(data_dir())) -> dict | None:
    """Fast 'where do things stand today' read, last row only, no full-log replay."""
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
    dry_run: bool | None = None,
) -> dict:
    """
    Computes REAL realized + unrealized P&L from the actual order log written
    by log_orders(), this reads what genuinely happened (or would have, in
    dry-run), not a backtest re-simulation. Uses FIFO lot matching for realized
    gains, same convention as most brokers' tax reporting.

    Parameters
    ----------
    start_date, end_date : str, 'YYYY-MM-DD'
        Window to measure. Only rows with dry_run matching your actual live
        mode are meaningful for a real account, pass `dry_run` (below) to
        filter, since log_orders() writes both modes to the same file.
    latest_prices : dict, optional
        {ticker: price} for marking open positions to market as of `end_date`.
        If omitted, unrealized P&L on still-open positions is left as NaN.
    initial_capital : float, optional
        If provided, also returns total_return_pct.
    dry_run : bool, optional
        If set, only rows whose logged `dry_run` column matches are used,
        a dry-run test run and a real --live run share the same log file, so
        without this a report could silently mix simulated and real fills.
        None (default) uses every row regardless of mode.

    Returns
    -------
    dict: realized_pnl, unrealized_pnl, total_pnl, open_positions (dict),
          open_position_avg_cost (dict: ticker -> weighted-average cost basis of the
          currently open lots, lets a caller reconstruct a current_positions-shaped dict
          for build_position_performance() from the trade log alone, without a live broker
          connection; see notebooks/operational/portfolio_snapshot_report.ipynb),
          trade_count, per_ticker (DataFrame breakdown).
    """
    if not os.path.isfile(log_path):
        raise FileNotFoundError(f"No trade log found at {log_path}, nothing has been logged yet.")

    log = pd.read_csv(log_path, parse_dates=["timestamp"])
    log = log[(log["timestamp"] >= start_date) & (log["timestamp"] <= end_date)]
    if dry_run is not None:
        log = log[log["dry_run"] == dry_run]
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
                logger.warning("SELL of %.4f %s exceeds logged open lots by %.4f shares, "
                                "log may not cover full history; realized P&L may be understated.",
                                shares, ticker, remaining)

    open_positions = {t: sum(s for s, _ in lots) for t, lots in open_lots.items() if sum(s for s, _ in lots) > 1e-9}
    open_position_avg_cost = {
        t: sum(s * p for s, p in open_lots[t]) / open_positions[t] for t in open_positions
    }
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
        "open_position_avg_cost": open_position_avg_cost,
        "trade_count": len(log),
        "per_ticker_realized": per_ticker_realized,
    }
    if initial_capital:
        result["total_return_pct"] = result["total_pnl"] / initial_capital
    return result


def _positions_from_trade_log(trade_log_path: str, dry_run: bool) -> dict:
    """
    Shared FIFO reconstruction: a current_positions-shaped dict
    ({ticker: {'shares', 'avg_entry_price'}}, the SAME shape get_ibkr_positions() returns) from
    a trade log, filtered to rows whose `dry_run` column matches, via
    measure_live_performance()'s EXISTING FIFO open_positions/open_position_avg_cost
    computation, not a new, separately-maintained FIFO implementation. Reused by
    reconstruct_dry_run_positions() (dry_run=True) and derive_own_live_positions()
    (dry_run=False), so the two can never drift onto two different FIFO algorithms.
    """
    if not os.path.isfile(trade_log_path):
        return {}
    result = measure_live_performance(
        "1970-01-01", pd.Timestamp.today().strftime("%Y-%m-%d"),
        log_path=trade_log_path, dry_run=dry_run,
    )
    return {
        t: {"shares": shares, "avg_entry_price": result["open_position_avg_cost"].get(t, 0.0)}
        for t, shares in result["open_positions"].items()
    }


def reconstruct_dry_run_positions(log_path: str = TRADE_LOG_PATH) -> dict:
    """
    Reconstructs a current_positions-shaped dict from the trade log's dry_run=True rows only.

    Backs an OPT-IN dry-run persistence feature (daily_runner.py's
    persist_dry_run_state config flag, default off): lets dry-run mode OPTIONALLY behave like
    a persistent, no-IBKR-required paper-trading ledger across separate invocations, instead
    of always starting from {} (dry-run's default, and this function's own return value when
    the log doesn't exist yet or has no open dry-run positions, matching that default exactly).
    """
    return _positions_from_trade_log(log_path, dry_run=True)


def derive_own_live_positions(trade_log_path: str = TRADE_LOG_PATH) -> dict:
    """
    Live counterpart to reconstruct_dry_run_positions(): reconstructs a current_positions-shaped
    dict from the trade log's dry_run=False rows only, "what does THIS portfolio's own log show
    it holds," independent of what the shared broker account shows for a ticker overall.

    Backs Epic 1 of the cross-portfolio-sell-prevention plan (see
    daily_runner.py's scope_overlapping_holdings()): each portfolio writes to its OWN
    live_trades_log_<portfolio>.csv, so calling this with that path gives a portfolio-scoped
    real-share count for a ticker even when the broker's whole-account reqPositions() result
    combines it with a sibling portfolio's shares of the same ticker.
    """
    return _positions_from_trade_log(trade_log_path, dry_run=False)


def derive_entry_date(ticker: str, trade_log_path: str = TRADE_LOG_PATH) -> pd.Timestamp | None:
    """
    Live-side equivalent of the backtest's entry_dates tracking
    (momentum_backtest.py's run_risk_managed_backtest), so max_holding_days means the
    same thing in live trading as it did when the strategy was backtested with the same
    setting. There's no in-memory day-by-day loop in live trading to track this the way
    the backtest does, so it's derived from the trade log instead.

    Walks the log chronologically for `ticker` and returns the timestamp of the BUY
    that started the CURRENTLY open position's unbroken holding streak, persists
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


def build_position_performance(
    current_positions: dict, latest_prices: dict, trade_log_path: str = TRADE_LOG_PATH,
) -> dict[str, dict]:
    """
    Per-ticker return-since-entry for the reports' "Position Performance" section, distinct
    from measure_live_performance()'s aggregate/per_ticker_realized P&L (closed-lot gains):
    this is unrealized return on the CURRENTLY open position, from its entry date to now.
    Reuses avg_entry_price already tracked in current_positions (the same field
    check_and_handle_stop_losses() compares against) and derive_entry_date() (the same
    FIFO entry-date derivation check_and_handle_time_stops() uses), both already computed
    live for stop-loss/time-stop gating today, just not previously surfaced in a report.

    Returns {ticker: {"entry_date", "entry_price", "current_price", "shares", "return_pct",
    "market_value"}}. A ticker missing a valid avg_entry_price, with non-positive shares, or
    without a known current price is omitted entirely, same graceful-degradation contract
    used throughout this module. "entry_date" may be None (trade log doesn't cover this
    position's full history) even when the other fields are present, the row still renders,
    just with entry date shown as unknown rather than being dropped.
    """
    result = {}
    for ticker, pos in current_positions.items():
        entry_price = pos.get("avg_entry_price")
        shares = pos.get("shares", 0)
        if not entry_price or shares <= 0 or ticker not in latest_prices:
            continue
        current_price = latest_prices[ticker]
        result[ticker] = {
            "entry_date": derive_entry_date(ticker, trade_log_path),
            "entry_price": entry_price,
            "current_price": current_price,
            "shares": shares,
            "return_pct": (current_price - entry_price) / entry_price,
            "market_value": shares * current_price,
        }
    return result


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
    Runs the SAME strategy independently across multiple named portfolios,
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
# 5. BROKER EXECUTION, IBKR, gated behind explicit --live flag
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


def get_ibkr_positions(port: int, client_id: int = 8, timeout: float = 5.0, host: str = IBKR_HOST) -> dict:
    """
    Real broker positions ({ticker: {'shares': float, 'avg_entry_price': float}})
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
            _log_ibkr_message(reqId, errorCode, errorString)

    import threading, time
    app = PositionsApp()
    app.connect(host, port, clientId=client_id)
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
        raise TimeoutError(f"reqPositions() did not complete within {timeout}s, is TWS/Gateway running on port {port}?")
    return app.positions


def get_ibkr_account_value(port: int, client_id: int = 9, timeout: float = 5.0,
                            tag: str = "NetLiquidation", host: str = IBKR_HOST) -> float:
    """
    Real account value via reqAccountSummary(), replaces hardcoded total_value.

    tag : which IBKR account summary tag to fetch. Default "NetLiquidation" (total account
    equity) preserves every existing call site's behavior unchanged. Also used with
    "AvailableFunds", real spendable cash, checked by
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
            _log_ibkr_message(reqId, errorCode, errorString)

    import threading, time
    app = AccountApp()
    app.connect(host, port, clientId=client_id)
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
        raise TimeoutError(f"reqAccountSummary({tag}) did not complete within {timeout}s, is TWS/Gateway running on port {port}?")
    return app.value


def check_slippage_tolerance(expected_price: float, actual_price: float, tolerance_pct: float) -> dict:
    """
    Pure function for the slippage-tolerance comparison,
    factored out of place_orders_ibkr() so the math is unit-testable without
    a real (or mocked) broker connection.
    """
    if expected_price <= 0 or actual_price <= 0:
        return {"exceeded": False, "deviation_pct": None}
    deviation = abs(actual_price - expected_price) / expected_price
    return {"exceeded": deviation > tolerance_pct, "deviation_pct": deviation}


def compute_spread_pct(bid: float, ask: float) -> float | None:
    """
    Pure bid-ask spread math, factored out of fetch_bid_ask_spread() below so it's
    unit-testable without a real (or mocked) IBKR connection, the same "pure math separated
    from I/O" precedent check_slippage_tolerance() above already established.

    Returns None for an invalid or crossed market (bid/ask <= 0, or ask <= bid), a genuine
    real-time quote should never look like that, treat it as "no usable quote" rather than a
    nonsensical zero/negative spread.
    """
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    midpoint = (bid + ask) / 2
    return (ask - bid) / midpoint


def fetch_bid_ask_spread(ticker: str, port: int, client_id: int = 10,
                          host: str = IBKR_HOST, timeout: float = 5.0) -> dict | None:
    """
    Real-time NBBO bid/ask via IBKR's reqMktData(), the PRE-trade counterpart to
    check_slippage_tolerance()'s POST-trade fill-deviation check and
    core/functions_quant_extensions.py's check_capacity() (a historical-ADV-based pre-trade
    sizing check, neither of which uses a real-time quote).

    Requires a live TWS/IB Gateway connection AND, per IBKR's own documented data-subscription
    requirements, typically a paid real-time market-data subscription for the ticker's
    exchange, real-time NBBO for US stocks/ETFs is NOT included on IBKR's free/delayed tier.
    Confirmed against IBKR's own documentation, not assumed, same "state the real operational
    dependency plainly" discipline this project applies to the fractional-share limitation
    elsewhere in this file. On a free/delayed-data account this will time out (or return
    delayed, frozen ticks that never populate both BID and ASK before the timeout); treat a
    None return as "couldn't get a usable real-time quote," never as "the spread is fine."

    Returns {'bid', 'ask', 'spread_pct'} once both BID and ASK ticks have arrived, or None on
    timeout / no usable quote (see compute_spread_pct()).
    """
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
        from ibapi.contract import Contract
    except ImportError:
        logger.error("ibapi not installed. Run: pip install ibapi --break-system-packages")
        return None

    class QuoteApp(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)
            self.bid = None
            self.ask = None

        def tickPrice(self, reqId, tickType, price, attrib):
            if price is None or price <= 0:
                return
            if tickType == 1:      # BID
                self.bid = float(price)
            elif tickType == 2:    # ASK
                self.ask = float(price)

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            _log_ibkr_message(reqId, errorCode, errorString)

    import threading, time
    contract = Contract()
    contract.symbol = ticker
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    app = QuoteApp()
    app.connect(host, port, clientId=client_id)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    app.reqMktData(1, contract, "", False, False, [])

    waited = 0.0
    while (app.bid is None or app.ask is None) and waited < timeout:
        time.sleep(0.2)
        waited += 0.2

    try:
        app.cancelMktData(1)
    except Exception:
        pass
    app.disconnect()

    if app.bid is None or app.ask is None:
        logger.warning(
            "%s: no real-time bid/ask received within %.1fs (requires a live TWS/Gateway "
            "connection and typically a paid real-time market-data subscription), skipping "
            "the spread check for this order.", ticker, timeout)
        return None

    spread_pct = compute_spread_pct(app.bid, app.ask)
    if spread_pct is None:
        return None
    return {"bid": app.bid, "ask": app.ask, "spread_pct": spread_pct}


def place_orders_ibkr(orders: dict, port: int, client_id: int = 7,
                       expected_prices: dict | None = None,
                       max_slippage_tolerance_pct: float | None = None,
                       auto_reduce_on_insufficient_cash: bool = False,
                       available_cash_fn=None, portfolio: str = "",
                       alerts_log_path: str = ALERTS_LOG_PATH,
                       host: str = IBKR_HOST, fill_poll_timeout: float = 60.0,
                       allow_extended_hours: bool = False,
                       max_bid_ask_spread_pct: float | None = None,
                       attach_broker_stop_loss: bool = False,
                       stop_loss_pct: float | None = None) -> dict:
    """
    Requires `ibapi` (pip install ibapi --break-system-packages) and a running
    TWS or IB Gateway instance listening on `port`. Only called when --live
    is passed; dry-run mode never reaches this function.

    Returns {ticker: {'status': str, 'filled': float, 'avg_fill_price': float}}
    by polling orderStatus/execDetails after submission instead of firing
    orders and disconnecting blind after a fixed sleep.

    SELLs are always submitted first and confirmed (terminal status) before any
    BUY is submitted, a BUY submitted before its funding SELL clears can be rejected on a
    cash account, or silently rely on margin buying power this code never used to check.
    Mirrors the backtest engine's explicit sells-first/buys-second structure.

    auto_reduce_on_insufficient_cash : bool
        After sells clear, if the real available cash (queried fresh from IBKR) can't cover
        every BUY at its computed size: False (default) logs a warning and submits BUYs as
        computed anyway, letting IBKR's own fill/reject be the backstop. True proportionally
        scales down BUY share counts (floored to whole shares) to fit. Either way, the
        shortfall is always logged, this flag only controls whether anything is done about
        it here, never whether it's visible.
    available_cash_fn : callable() -> float, optional
        Injected so this is unit-testable without a real IBKR account-summary round trip.
        Defaults to `get_ibkr_account_value(port, tag="AvailableFunds")`.
    fill_poll_timeout : float
        Seconds to wait for each batch (sells, then buys) to reach a terminal status before
        giving up and logging "did not confirm as Filled". Real paper-account fills have been
        observed taking longer than a short window, a too-short timeout doesn't mean the
        order failed, just that this function stopped watching before the fill callback
        arrived (confirm in TWS's own execution log before assuming an order didn't fill).
    allow_extended_hours : bool
        IBKR/exchanges reject plain MKT orders outside regular trading hours (error 201,
        "Exchange is closed"), and only accept LMT orders with outsideRth=True instead (MKT
        does not work outside RTH at all, confirmed against IBKR's own docs). True switches
        every order in this call to LMT (limit price = expected_prices[ticker] +/- a small
        buffer favoring fill likelihood) with outsideRth=True; a ticker with no entry in
        expected_prices falls back to a regular MKT order (RTH-only) instead of failing.
    max_bid_ask_spread_pct : float, optional
        The "Liquidity/Slippage Monitor" pre-trade gate (Nice-to-Have tier,
        docs/RISK_CONSTRAINTS.md). None (default) makes zero new IBKR calls, byte-identical to
        before this feature existed. When set, fetch_bid_ask_spread() is called for each ticker
        right before it would be submitted; a spread wider than this drops the order into
        dropped_orders (status DROPPED_WIDE_SPREAD, same merge pattern as DROPPED_FRACTIONAL/
        DROPPED_INSUFFICIENT_CASH) instead of submitting it. A None quote (timeout / no usable
        real-time data, see fetch_bid_ask_spread()'s own docstring) does NOT block the order,
        "couldn't check" is treated as "proceed," not as "spread is wide."
    attach_broker_stop_loss : bool
        BacktestConfig.attach_broker_stop_loss, LIVE-ONLY, belt-and-suspenders alongside the
        Python-side auto_execute_stop_loss check (NOT a replacement for it). When True, each
        BUY with a usable reference price in expected_prices submits a real IBKR bracket:
        parent BUY (transmit=False) + child STP SELL (parentId linked, transmit=True, stop
        price = reference_price * (1 - stop_loss_pct)), so the position is protected by the
        BROKER ITSELF even when this app isn't running, unlike the Python-side check, which
        only ever runs when daily-runner --live is actually invoked. A ticker with no
        reference price falls back to a plain, unprotected BUY (same fallback shape as
        allow_extended_hours' "no reference price" case), never blocks the BUY entirely. Only
        the parent's orderId is tracked in the fill-confirmation poll/wait set, a resting
        protective STP correctly stays non-terminal indefinitely, that's the whole point; its
        orderId is surfaced back via results[ticker]["stop_order_id"] instead (audit-only, see
        log_orders()'s broker_stop_order_id column; the actual cancel-before-sell mechanism,
        see below, is broker-truth-based via reqAllOpenOrders(), not dependent on this).
    stop_loss_pct : float, optional
        BacktestConfig.stop_loss_pct, reused as-is for the bracket's stop offset, no duplicate
        field. Required (non-None) for attach_broker_stop_loss to do anything.
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
            self.open_orders = []    # [{'orderId', 'symbol', 'action', 'orderType'}], see
                                      # reqAllOpenOrders() usage below (attach_broker_stop_loss's
                                      # cancel-before-sell mechanism)
            self.open_orders_done = False

        def nextValidId(self, orderId: int):
            self.next_order_id = orderId

        def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                         permId=0, parentId=0, lastFillPrice=0, clientId=0, whyHeld="", mktCapPrice=0):
            entry = self.order_status.setdefault(orderId, {})
            entry.update(status=status, filled=float(filled), avg_fill_price=float(avgFillPrice))

        def openOrder(self, orderId, contract, order, orderState):
            # reqAllOpenOrders() (unlike reqOpenOrders(), which only returns THIS clientId's own
            # orders) returns every API-submitted open order account-wide, required for
            # cancel-before-sell to correctly find a resting protective STP that a DIFFERENT
            # process invocation/client connection placed (the run that placed a bracket and
            # the run that later decides to exit are almost always different connections).
            self.open_orders.append({
                "orderId": orderId, "symbol": contract.symbol,
                "action": order.action, "orderType": order.orderType,
            })

        def openOrderEnd(self):
            self.open_orders_done = True

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            _log_ibkr_message(reqId, errorCode, errorString)
            # Informational codes (e.g. 10349, "TIF was set to DAY based on order preset")
            # carry a real orderId but are not failures, only a genuine error should mark
            # this order's tracked status as terminal, or the poll loop stops watching an
            # order that's still pending (or already filled) as if it had been rejected.
            if reqId in self.order_status and errorCode not in IBKR_INFORMATIONAL_CODES:
                self.order_status[reqId]["status"] = f"ERROR: {errorString}"

    import threading
    import time

    # --- Retry the CONNECTION only, never retry order
    #     submission itself. If a disconnect happens AFTER an order was
    #     already sent but before its confirmation arrived, blindly retrying
    #     the whole function could submit a duplicate order, a much worse
    #     failure mode than just failing this run and alerting. So: connect
    #     with retries; once connected and order submission begins, any
    #     failure from that point fails loudly with no retry. ---
    app = None
    connect_attempts = 3
    for attempt in range(1, connect_attempts + 1):
        app = IBApp()
        try:
            app.connect(host, port, clientId=client_id)
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
        logger.error("Could not obtain a valid order ID from IBKR after %d attempts, "
                     "is TWS/Gateway running on port %d? NO ORDERS WERE SUBMITTED.",
                     connect_attempts, port)
        if app is not None:
            app.disconnect()
        return {}

    # Orders dropped before ever reaching IBKR (floors to 0 whole shares, or scaled to 0 by
    # cash-availability reduction) never get a real orderId, so they'd otherwise vanish from
    # the returned results entirely. Tracked separately and merged in at the end so callers
    # (the rebalance email, in particular) can show what actually happened for every order,
    # not just the ones that made it to a real IBKR order.
    dropped_orders: dict = {}

    # {ticker: child STP orderId} for a bracket's resting protective stop
    # (attach_broker_stop_loss), surfaced into results[ticker]["stop_order_id"] by
    # _collect_results() below, audit-only, NOT the cancel-before-sell source of truth (that's
    # broker-truth-based via reqAllOpenOrders(), see the SELLs-first block further down).
    stop_order_ids: dict = {}

    def _submit_and_wait(order_subset: dict, start_order_id: int) -> tuple[dict, int]:
        """Submits every order in order_subset, polls until each reaches a terminal
        status. Returns ({orderId: ticker}, next_available_order_id)."""
        order_id_to_ticker = {}
        oid = start_order_id
        for ticker, order in order_subset.items():
            if max_bid_ask_spread_pct is not None:
                quote = fetch_bid_ask_spread(ticker, port, host=host)
                if quote is not None and quote["spread_pct"] > max_bid_ask_spread_pct:
                    logger.warning(
                        "%s: dropping %s order, bid-ask spread %.2f%% exceeds "
                        "max_bid_ask_spread_pct %.2f%%.",
                        ticker, order["action"], quote["spread_pct"] * 100, max_bid_ask_spread_pct * 100)
                    dropped_orders[ticker] = {
                        "status": "DROPPED_WIDE_SPREAD",
                        "filled": 0.0,
                        "avg_fill_price": 0.0,
                    }
                    log_alert(portfolio, "WIDE_BID_ASK_SPREAD", "WARNING",
                              f"{ticker}: spread {quote['spread_pct']:.2%} exceeds "
                              f"max_bid_ask_spread_pct {max_bid_ask_spread_pct:.2%}, order dropped.",
                              log_path=alerts_log_path)
                    continue

            contract = Contract()
            contract.symbol = ticker
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"

            ib_order = Order()
            # ibapi's Order() defaults eTradeOnly/firmQuoteOnly to True (legacy fields from
            # older ibapi releases), current TWS/Gateway versions have dropped server-side
            # support for them entirely and reject ANY order carrying them as True with error
            # 10268 ("attribute is not supported"), regardless of account/order type. Every
            # order is rejected until these are explicitly cleared, this is IBKR's own
            # documented fix for this exact ibapi/TWS version combination, not optional.
            ib_order.eTradeOnly = False
            ib_order.firmQuoteOnly = False
            ib_order.action = order["action"]
            ib_order.orderType = "MKT"
            # Consider "MOC" (market-on-close) or limit orders with a price band
            # instead of raw "MKT" for anything beyond a liquid, tight-spread ETF.

            # IBKR does not support fractional EQUITY/ETF share orders via the API, under any
            # circumstances, confirmed empirically (error 10243, "Fractional-sized order
            # cannot be placed via API. Please use desktop version") even after correctly
            # setting cashQty per IBKR's own official sample code (LimitOrderWithCashQty):
            # cashQty only authorizes fractional fills for forex/CASH-pair orders, NOT STK
            # contracts like these. There is no code-level workaround for STK, the desktop/
            # web UI is the only way to place a genuinely fractional equity order. So: floor to
            # whole shares here, at the submission boundary only. allow_fractional_shares still
            # fully applies everywhere else (sizing math, drift calc, the backtest engine),
            # only the final live order quantity is forced whole, because that's a hard broker
            # constraint no config setting can change.
            shares = order["shares"]
            if shares != int(shares):
                floored = int(shares)
                if floored <= 0:
                    logger.warning(
                        "%s: dropping %s order, %.4f shares floors to 0 whole shares "
                        "(IBKR does not support fractional equity orders via API).",
                        ticker, order["action"], shares)
                    dropped_orders[ticker] = {
                        "status": "DROPPED_FRACTIONAL",
                        "filled": 0.0,
                        "avg_fill_price": 0.0,
                    }
                    continue
                logger.info(
                    "%s: flooring fractional order %.4f -> %d whole shares (IBKR does not "
                    "support fractional equity orders via API).", ticker, shares, floored)
                shares = floored
            ib_order.totalQuantity = shares

            # Explicit TIF for every order (bracket or not), no longer relying on the account's
            # own implicit preset (previously observed defaulting to DAY via IBKR's own
            # informational code 10349, see module docstring). A bracket's child protective
            # STP overrides this to "GTC" below, deliberately, see that code for why.
            ib_order.tif = "DAY"

            # IBKR/exchanges reject plain MKT orders outside regular trading hours (error 201,
            # "Exchange is closed") and only accept LMT orders with outsideRth=True instead,
            # MKT does not work outside RTH at all (confirmed against IBKR's own TWS API docs),
            # so this is a real order-type change, not just a flag. A 0.5% buffer favors
            # actually getting filled over exact price, extended-hours liquidity is thinner,
            # so a tight limit risks no fill at all. No reference price -> fall back to a
            # regular MKT (RTH-only) order rather than submitting an unpriced limit order.
            extended_hours_note = ""
            if allow_extended_hours:
                ref_price = (expected_prices or {}).get(ticker)
                if ref_price and ref_price > 0:
                    buffer = 0.005
                    ib_order.outsideRth = True
                    ib_order.orderType = "LMT"
                    ib_order.lmtPrice = round(
                        ref_price * (1 + buffer if order["action"] == "BUY" else 1 - buffer), 2)
                    extended_hours_note = f" [extended hours: LMT @ {ib_order.lmtPrice}]"
                else:
                    logger.warning(
                        "%s: allow_extended_hours is set but no reference price is available, "
                        "submitting as a regular MKT order (RTH only) instead.", ticker)

            # Broker-side protective bracket (BacktestConfig.attach_broker_stop_loss,
            # belt-and-suspenders alongside the Python-side auto_execute_stop_loss check, see
            # this function's own docstring). Only ever attaches to a BUY, needs a usable
            # reference price to compute the stop's absolute trigger price (an STP's auxPrice
            # is an absolute price, not a percentage, so it can't be computed post-fill without
            # knowing the fill price in advance).
            attach_stop = (
                attach_broker_stop_loss and order["action"] == "BUY" and stop_loss_pct is not None
            )
            stop_ref_price = (expected_prices or {}).get(ticker)
            if attach_stop and not (stop_ref_price and stop_ref_price > 0):
                logger.warning(
                    "%s: attach_broker_stop_loss is set but no reference price is available, "
                    "submitting a plain, unprotected BUY instead.", ticker)
                attach_stop = False
            if attach_stop:
                ib_order.transmit = False  # holds the parent until the child below transmits

            logger.info("Placing %s %s shares of %s (orderId=%d)%s",
                        order["action"], shares, ticker, oid, extended_hours_note)
            app.order_status[oid] = {"status": "SUBMITTED", "filled": 0.0, "avg_fill_price": 0.0}
            order_id_to_ticker[oid] = ticker
            app.placeOrder(oid, contract, ib_order)
            parent_oid = oid
            oid += 1
            time.sleep(0.3)  # simple pacing; IBKR rate-limits rapid order submission

            if attach_stop:
                stop_contract = Contract()
                stop_contract.symbol = ticker
                stop_contract.secType = "STK"
                stop_contract.exchange = "SMART"
                stop_contract.currency = "USD"

                stop_order = Order()
                stop_order.eTradeOnly = False
                stop_order.firmQuoteOnly = False
                stop_order.action = "SELL"
                stop_order.orderType = "STP"  # plain stop-market, not STP LMT: a genuine
                                               # protective stop must reliably execute during a
                                               # fast decline, a limit leg can be skipped over
                                               # in a gap, defeating the purpose.
                stop_order.auxPrice = round(stop_ref_price * (1 - stop_loss_pct), 2)
                stop_order.totalQuantity = shares
                stop_order.parentId = parent_oid
                stop_order.transmit = True  # transmits the whole bracket atomically
                # GTC, deliberately NOT "DAY": a DAY stop would be cancelled by IBKR at end of
                # day and leave the position completely unprotected on every subsequent day
                # this app doesn't run, defeating the entire purpose of this feature (broker-
                # side protection independent of whether the app is running). Parent and child
                # are allowed to carry different tif values in a bracket, IBKR supports this.
                stop_order.tif = "GTC"
                if allow_extended_hours:
                    # A plain STP only monitors/triggers during RTH otherwise, leaving a real
                    # gap for a move in the same extended session the entry itself was allowed
                    # in.
                    stop_order.outsideRth = True

                logger.info(
                    "Attaching broker-side protective STP SELL for %s: %d shares @ stop %.2f "
                    "(orderId=%d, parentId=%d)", ticker, shares, stop_order.auxPrice, oid, parent_oid)
                app.placeOrder(oid, stop_contract, stop_order)
                stop_order_ids[ticker] = oid
                oid += 1
                time.sleep(0.3)

        # --- poll for fill confirmation instead of a blind fixed sleep ---
        waited = 0.0
        while waited < fill_poll_timeout:
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
            if ticker in stop_order_ids:
                results[ticker]["stop_order_id"] = stop_order_ids[ticker]
            if status not in ("Filled",):
                logger.warning("Order for %s did not confirm as Filled (status=%s).", ticker, status)
            else:
                logger.info("Order for %s FILLED: %.4f shares @ $%.2f", ticker, info["filled"], info["avg_fill_price"])

                # --- Slippage tolerance check ---
                # Cannot un-fill an order that already executed, this ALERTS on
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
                            "tolerance %.2f%%). Fill already executed, review manually.",
                            ticker, expected, actual, slip_check["deviation_pct"] * 100, max_slippage_tolerance_pct * 100,
                        )
                        log_alert(portfolio, "SLIPPAGE_TOLERANCE_EXCEEDED", "WARNING",
                                  f"{ticker} expected ${expected:.2f}, filled ${actual:.2f} "
                                  f"({slip_check['deviation_pct']:.2%} deviation, tolerance {max_slippage_tolerance_pct:.2%})",
                                  log_path=alerts_log_path)
                        results[ticker]["slippage_tolerance_exceeded"] = True
                        results[ticker]["slippage_deviation_pct"] = slip_check["deviation_pct"]
        return results

    # --- SELLs first, always, see docstring. ---
    sell_orders = {t: o for t, o in orders.items() if o["action"] == "SELL" and o["shares"] > 0}
    buy_orders = {t: o for t, o in orders.items() if o["action"] == "BUY" and o["shares"] > 0}

    # Cancel any resting broker-side protective STP (attach_broker_stop_loss) for a ticker THIS
    # run is about to SELL, BEFORE submitting that SELL, otherwise the broker's own triggered
    # stop and this app's rebalance-driven sell could both try to sell the same shares.
    # Deliberately broker-truth-based (reqAllOpenOrders(), NOT reqOpenOrders(), which only
    # returns THIS clientId's own orders, and not a locally-cached order ID either): the run
    # that PLACED the bracket and the run that later decides to EXIT are almost always
    # different process invocations/client connections, self-healing even if the placing run
    # crashed before logging anything, or TWS restarted. Only performed when
    # attach_broker_stop_loss is truthy, so accounts that never opt in pay no extra IBKR round
    # trip.
    if attach_broker_stop_loss and sell_orders:
        app.reqAllOpenOrders()
        waited = 0.0
        while not app.open_orders_done and waited < 10.0:
            time.sleep(0.2)
            waited += 0.2
        for resting in app.open_orders:
            if (resting["symbol"] in sell_orders and resting["action"] == "SELL"
                    and resting["orderType"] == "STP"):
                logger.info(
                    "Cancelling resting protective STP orderId=%d for %s before submitting "
                    "this run's SELL.", resting["orderId"], resting["symbol"])
                app.cancelOrder(resting["orderId"])
                time.sleep(0.3)

    next_oid = app.next_order_id
    sell_ids, next_oid = _submit_and_wait(sell_orders, next_oid)
    results = _collect_results(sell_ids)

    if buy_orders:
        # --- Cash-aware buy sizing, checked AFTER sells have
        #     cleared (so proceeds are reflected in the real available-cash query). ---
        priced_buys = {t: o for t, o in buy_orders.items() if expected_prices and t in expected_prices}
        unpriced = set(buy_orders) - set(priced_buys)
        if unpriced:
            logger.info("Cash-availability check skipped for %s, no expected price available "
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
                            reduced[t] = o  # no price to scale against, leave as computed
                            continue
                        new_shares = int(o["shares"] * scale)  # floor, always the safe direction
                        if new_shares <= 0:
                            logger.warning("Dropping BUY %s, reduced to 0 shares after "
                                           "cash-availability scaling.", t)
                            dropped_orders[t] = {
                                "status": "DROPPED_INSUFFICIENT_CASH",
                                "filled": 0.0,
                                "avg_fill_price": 0.0,
                            }
                            continue
                        reduced[t] = {**o, "shares": new_shares}
                        logger.info("Reduced BUY %s: %d -> %d shares (cash-availability scaling "
                                   "factor %.3f).", t, o["shares"], new_shares, scale)
                    buy_orders = reduced

        buy_ids, next_oid = _submit_and_wait(buy_orders, next_oid)
        results.update(_collect_results(buy_ids))

    app.disconnect()
    for ticker, dropped in dropped_orders.items():
        results.setdefault(ticker, dropped)
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
    lookback_period: float = 12.0,
    dry_run: bool = True,
    ibkr_port: int = 7497,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None,
    log_path: str = TRADE_LOG_PATH,
    custom_weights: dict | None = None,
    portfolio: str = "",
    alerts_log_path: str = ALERTS_LOG_PATH,
    extra_price_tickers: list[str] | None = None,
) -> dict:
    """
    extra_price_tickers : additional tickers to fetch a price for (so generate_orders() can
    evaluate them for exit) WITHOUT adding them to the momentum ranking/selection universe.
    Backs the orphaned-ticker reconciliation in daily_runner.py, a ticker currently held but no
    longer in the portfolio's configured `tickers:` list, confirmed (via that portfolio's own
    trade log) to have been legitimately held there before. Getting priced must never make a
    ticker re-selectable as a NEW pick, that's why this widens `daily_prices` (used for pricing)
    but NOT the DataFrame passed into resolve_momentum_scores()/ranking (still `tickers` only).
    None (default) preserves this function's exact pre-existing behavior.
    """
    # Lazy import: core/strategy_signals.py imports resolve_momentum_scores()/assign_ranks()
    # FROM this module (core/'s deliberate one-directional exception, see that module's own
    # docstring), so a top-level import here would be circular. Importing inside the function
    # body breaks the cycle, the same established pattern this file already uses for ibapi.
    from ..core.strategy_signals import resolve_strategy_scores, resolve_strategy_picks

    price_tickers = tickers if not extra_price_tickers else list(dict.fromkeys(list(tickers) + list(extra_price_tickers)))
    daily_prices = with_retry(fetch_live_prices, 3, 2.0, price_tickers, fmp_api_key=fmp_api_key, eodhd_api_key=eodhd_api_key)
    if daily_prices.empty:
        logger.error("No price data returned; aborting.")
        return {}

    # resolve_strategy_scores() (core/strategy_signals.py) scopes to `tickers` internally,
    # NEVER the wider extra_price_tickers-fetched universe, getting priced must never make a
    # ticker re-selectable as a new pick, same guarantee the old inline ranking_prices
    # conditional enforced, now centralized in one place shared with the backtest-facing
    # generate_strategy_monthly_picks(). Dispatches on cfg.strategy_type; "momentum" (the
    # default) and every strategy_type that only affects sizing/exposure are byte-identical to
    # calling resolve_momentum_scores() directly, per its own regression test.
    #
    # Deliberately reads FMP_API_KEY/EODHD_API_KEY directly from the environment here, NOT this
    # function's own fmp_api_key/eodhd_api_key params (those are scoped to fetch_live_prices()'s
    # PRICE vendor selection above, and daily_runner.py deliberately never populates them, real
    # production price data comes from yfinance, confirmed by every prior epic's live
    # validation). Reusing them here would have silently switched the real production price
    # vendor for EVERY portfolio/strategy_type the first time daily_runner.py started passing
    # real keys through, an unrelated, unbudgeted side effect of wiring up ONLY
    # hybrid_multi_factor's fundamentals fetch. Same os.environ.get() pattern daily_runner.py's
    # OTHER existing fundamentals call sites already use, is a no-op (get_cached_or_fetch_
    # fundamentals() returns {} gracefully) for every strategy_type except hybrid_multi_factor,
    # which is the only branch that actually calls it.
    scores = resolve_strategy_scores(
        daily_prices, tickers, cfg, lookback_period,
        os.environ.get("FMP_API_KEY"), os.environ.get("EODHD_API_KEY"),
    ).dropna(how="all")
    ranks = assign_ranks(scores)
    latest_scores = scores.iloc[-1] if not scores.empty else None
    latest_ranks_row = ranks.iloc[-1] if not ranks.empty else None
    # resolve_strategy_picks() (core/strategy_signals.py) dispatches on cfg.strategy_type:
    # "absolute_momentum" bypasses the cross-sectional top_n cutoff entirely (every ticker with
    # a positive own trailing score is held, defensive_ticker alone otherwise), every other
    # strategy_type is byte-identical to the existing get_top_etfs(ranks, top_n=top_n) call.
    picks = resolve_strategy_picks(latest_scores, latest_ranks_row, tickers, cfg, top_n)
    logger.info("Today's signal picks (top %d, strategy_type=%s): %s", top_n, cfg.strategy_type, picks)

    # --- Absolute Momentum (Macro) overlay: any pick with negative OWN trailing momentum
    #     (not just its rank relative to other picks) gets swapped for cfg.defensive_ticker,
    #     BEFORE signal_context/sizing/vol-scaling/regime-filtering, so every downstream step
    #     acts on the FINAL pick list. Opt-in, byte-identical to before this feature when
    #     disabled (default). See apply_absolute_momentum_filter(). ---
    if cfg.use_absolute_momentum:
        filtered_picks = apply_absolute_momentum_filter(picks, latest_scores, cfg.defensive_ticker)
        if filtered_picks != picks:
            logger.info("Absolute momentum filter: %s -> %s", picks, filtered_picks)
        picks = filtered_picks

    # Capture rank/score context so a trade can be reviewed
    # later with "why" (e.g. "XLK was rank 2 of 10"), not just "what" was traded.
    signal_context = {}
    if not ranks.empty:
        latest_ranks = ranks.iloc[-1]
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

    # --- Aggregate-drift skip, bypass the ENTIRE rebalance if
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
        logger.info("%-6s %-4s shares=%-8.4f (%s)", ticker, order["action"], order["shares"], order["reason"])

    # --- Advisory capacity check (best-effort; never blocks trading) ---
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
                                          portfolio=portfolio, alerts_log_path=alerts_log_path,
                                          allow_extended_hours=cfg.allow_extended_hours,
                                          max_bid_ask_spread_pct=cfg.max_bid_ask_spread_pct,
                                          attach_broker_stop_loss=cfg.attach_broker_stop_loss,
                                          stop_loss_pct=cfg.stop_loss_pct)
        for ticker, fill in fill_results.items():
            if ticker in orders:
                orders[ticker]["fill_status"] = fill["status"]
                orders[ticker]["fill_price"] = fill["avg_fill_price"]
                if "stop_order_id" in fill:
                    orders[ticker]["broker_stop_order_id"] = fill["stop_order_id"]
                orders[ticker]["fill_shares"] = fill["filled"]
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

    # --- EXAMPLE universe/config, replace with your real universe and current broker positions ---
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
