"""
core/strategy_signals.py

Selectable momentum strategy_type dispatch (docs/MOMENTUM_STRATEGIES.md, config field
BacktestConfig.strategy_type). Two entry points:

- resolve_strategy_scores(): the LIVE call site, execution/live_signal.py's run() calls this
  instead of resolve_momentum_scores() directly, dispatching cfg.strategy_type to the right
  per-strategy ranking function for "today"'s scores.
- generate_strategy_monthly_picks(): the BACKTEST-facing counterpart, produces a full
  HISTORICAL monthly_picks series (same shape research notebooks already hand-build for the
  default strategy via calculate_period_returns()/assign_ranks()/get_top_etfs()), feedable
  UNCHANGED into the existing, untouched run_custom_backtest()/run_risk_managed_backtest(),
  neither of which needs any change, they only ever consume a pre-computed monthly_picks.

Deliberately imports resolve_momentum_scores()/assign_ranks() from execution/live_signal.py, a
one-directional exception to core/'s usual "no dependency on execution/" convention (CLAUDE.md),
rather than reimplementing the same resample/skip-month-guardrail logic a second time here:
this project's core design principle is shared functions so live and backtest paths can't
silently diverge (the same reasoning behind resolve_target_weights()), duplicating that logic
here would reintroduce exactly the divergence risk this whole architecture exists to avoid.
Confirmed safe to import: neither function touches ibapi/IBKR at all, execution/live_signal.py
only imports ibapi lazily inside its own broker-facing functions (get_ibkr_positions(),
place_orders_ibkr(), etc.), never at module level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.momentum_backtest import BacktestConfig
from ..execution.live_signal import resolve_momentum_scores, assign_ranks
from .functions_quant_extensions import blend_momentum_scores

# strategy_type values whose SCORING is identical to the base per-ticker trailing-return score
# resolve_momentum_scores() already computes. Most of these only affect SIZING/EXPOSURE (via
# daily_runner.py's apply_strategy_type_preset() feeding the existing resolve_target_weights()/
# compute_target_weights() machinery), never RANKING; "absolute_momentum" (Epic 3) is the one
# exception, its SCORE is the same base per-ticker score, only SELECTION differs (no
# cross-sectional top_n cutoff, see resolve_strategy_picks() below). "rank_sign_momentum" (Epic
# 4) only changes SIZING (equal_weight preset), ranking/selection are identical to the base
# strategy. Every other strategy_type dispatches to a genuinely new ranking function below, added
# incrementally, one per epic.
_BASE_SCORE_STRATEGY_TYPES = (
    "momentum", "relative_momentum", "dual_momentum", "volatility_scaled_momentum",
    "correlation_weighted_momentum", "absolute_momentum", "rank_sign_momentum",
)


def resolve_strategy_scores(
    daily_prices: pd.DataFrame, tickers: list[str], cfg: BacktestConfig, lookback_period: float,
) -> pd.DataFrame:
    """
    Dispatches on cfg.strategy_type to the right per-strategy scoring function. Scopes
    daily_prices to `tickers` internally (never the wider universe a caller might pass for
    orphaned-ticker pricing purposes, mirrors run()'s existing extra_price_tickers scoping
    guarantee: getting priced must never make a ticker re-selectable as a new pick).

    Returns the same shape resolve_momentum_scores() already does: a DataFrame of trailing
    scores, index=resampled period dates, columns=tickers, drop-in compatible with
    assign_ranks()/get_top_etfs().
    """
    scoped_prices = daily_prices[list(tickers)]
    strategy_type = getattr(cfg, "strategy_type", "momentum")

    if strategy_type in _BASE_SCORE_STRATEGY_TYPES:
        return resolve_momentum_scores(
            scoped_prices, lookback_period, cfg.holding_period, cfg.skip_month_guardrail,
        )

    if strategy_type == "multi_timeframe_composite":
        # Resample to monthly FIRST, matching blend_momentum_scores()'s own documented
        # "resample to monthly first for the conventional N-month momentum meaning" guidance,
        # the same convention resolve_momentum_scores()'s monthly branch already uses, so
        # cfg.multi_timeframe_lookbacks means "months", not raw daily periods.
        monthly_prices = scoped_prices.resample("ME").last()
        return blend_momentum_scores(
            monthly_prices, cfg.multi_timeframe_lookbacks, cfg.multi_timeframe_weights,
        )

    if strategy_type == "residual_momentum":
        # Uses the FULL (unscoped) daily_prices, not scoped_prices: cfg.regime_benchmark is
        # reused as the market-model regressor and is very likely NOT one of `tickers` itself
        # (same "must be priced alongside the portfolio's own tickers" precedent already
        # documented for defensive_ticker).
        return resolve_residual_momentum_scores(
            daily_prices, tickers, cfg.regime_benchmark, lookback_period, cfg.holding_period,
        )

    if strategy_type == "path_dependent_momentum":
        return resolve_path_dependent_momentum_scores(
            scoped_prices, tickers, lookback_period, cfg.holding_period,
        )

    raise ValueError(
        f"resolve_strategy_scores(): unhandled strategy_type {strategy_type!r}, "
        f"this strategy's ranking function hasn't been wired in yet."
    )


def resolve_residual_momentum_scores(
    daily_prices: pd.DataFrame, tickers: list[str], benchmark: str, lookback_period: float,
    holding_period: float,
) -> pd.DataFrame:
    """
    strategy_type == "residual_momentum" (Epic 5): ranks tickers by IDIOSYNCRATIC
    (benchmark-adjusted) trailing return rather than raw total return, a single-factor
    market-model residualization, not a full multi-factor model. Per rebalance date and ticker,
    estimates market-model beta via OLS (np.polyfit, degree 1) on trailing DAILY returns (ticker
    vs `benchmark`, cfg.regime_benchmark reused, no new config field needed) over the same
    lookback window resolve_momentum_scores() already uses, then:

        residual_score = raw_period_return - beta * raw_benchmark_period_return

    the portion of the ticker's trailing return NOT explained by its benchmark exposure. A
    high-beta ticker whose entire move is explained by tracking the benchmark (e.g. a leveraged
    beta=2 ETF in a rising market) scores near zero here even with a LARGE raw return; a
    low-beta ticker with genuine idiosyncratic outperformance scores higher, even with a smaller
    raw return, this is the entire point of residualizing.

    Requires `benchmark` to be priced in daily_prices (same "must be priced alongside the
    portfolio's own tickers" requirement already documented for defensive_ticker), a clear
    ValueError if it isn't, unlike the regime filter's silent no-op (an optional overlay), this
    strategy cannot compute a score AT ALL without its benchmark.
    """
    if benchmark not in daily_prices.columns:
        raise ValueError(
            f"resolve_residual_momentum_scores(): benchmark {benchmark!r} is not priced in "
            f"daily_prices, add it to this portfolio's own tickers: list."
        )

    scoped_tickers = [t for t in tickers if t in daily_prices.columns]
    prices = daily_prices[list(dict.fromkeys(scoped_tickers + [benchmark]))]

    if holding_period < 1:
        resampled = prices.resample("W").last()
        period = max(1, round(lookback_period * 4))
    else:
        resampled = prices.resample("ME").last()
        period = max(1, round(lookback_period))

    raw_returns = resampled.pct_change(periods=period)
    daily_returns = prices.pct_change()

    scores = pd.DataFrame(index=raw_returns.index, columns=scoped_tickers, dtype=float)
    for i, date in enumerate(resampled.index):
        if i < period:
            continue
        window_start = resampled.index[i - period]
        window = daily_returns.loc[window_start:date].iloc[1:]
        bench_window = window[benchmark].dropna()
        bench_raw = raw_returns.loc[date, benchmark]
        if len(bench_window) < 2 or pd.isna(bench_raw):
            continue
        for ticker in scoped_tickers:
            ticker_raw = raw_returns.loc[date, ticker]
            if pd.isna(ticker_raw):
                continue
            ticker_window = window[ticker].dropna()
            common = ticker_window.index.intersection(bench_window.index)
            if len(common) < 2:
                continue
            beta = np.polyfit(bench_window.loc[common], ticker_window.loc[common], 1)[0]
            scores.loc[date, ticker] = ticker_raw - beta * bench_raw

    return scores


def resolve_path_dependent_momentum_scores(
    daily_prices: pd.DataFrame, tickers: list[str], lookback_period: float, holding_period: float,
) -> pd.DataFrame:
    """
    strategy_type == "path_dependent_momentum" (Epic 6): rewards a smooth, consistent trend over
    a choppy/volatile one reaching the same endpoint, the literal "filters for consistent/smooth
    trends" reading. Per rebalance date and ticker, fits a linear trend to log-price
    (np.polyfit, degree 1) over the trailing lookback window (the same window
    resolve_momentum_scores() uses), computes that fit's R^2 ("trend quality"), then:

        path_adjusted_score = raw_period_return * trend_r_squared

    Two tickers with an IDENTICAL raw return over the window but different R^2 (one climbed
    steadily, the other whipsawed to the same endpoint) get different scores here, the smoother
    one ranks higher. Purely price-based, no external benchmark needed (unlike
    resolve_residual_momentum_scores()), so this only ever needs `tickers`' own prices.
    """
    scoped_tickers = [t for t in tickers if t in daily_prices.columns]
    prices = daily_prices[scoped_tickers]

    if holding_period < 1:
        resampled = prices.resample("W").last()
        period = max(1, round(lookback_period * 4))
    else:
        resampled = prices.resample("ME").last()
        period = max(1, round(lookback_period))

    raw_returns = resampled.pct_change(periods=period)

    scores = pd.DataFrame(index=raw_returns.index, columns=scoped_tickers, dtype=float)
    for i, date in enumerate(resampled.index):
        if i < period:
            continue
        window_start = resampled.index[i - period]
        window_prices = prices.loc[window_start:date]
        for ticker in scoped_tickers:
            raw = raw_returns.loc[date, ticker]
            if pd.isna(raw):
                continue
            series = window_prices[ticker].dropna()
            if len(series) < 3 or (series <= 0).any():
                continue
            log_prices = np.log(series.values)
            x = np.arange(len(log_prices))
            slope, intercept = np.polyfit(x, log_prices, 1)
            fitted = slope * x + intercept
            ss_res = np.sum((log_prices - fitted) ** 2)
            ss_tot = np.sum((log_prices - log_prices.mean()) ** 2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
            scores.loc[date, ticker] = raw * r_squared

    return scores


def select_absolute_momentum_picks(
    latest_scores: pd.Series | None, tickers: list[str], defensive_ticker: str,
) -> list[str]:
    """
    strategy_type == "absolute_momentum" (Epic 3): no cross-sectional ranking/top_n cutoff at
    all, every ticker in `tickers` whose OWN trailing score is strictly positive is held,
    [defensive_ticker] alone if latest_scores is None (no score history yet) or nothing in the
    universe has a positive score. A zero score is not positive, a flat trailing return is not
    momentum.
    """
    if latest_scores is None:
        return [defensive_ticker]
    scoped = latest_scores.reindex(tickers).dropna()
    held = scoped[scoped > 0].index.tolist()
    return held if held else [defensive_ticker]


def resolve_strategy_picks(
    scores_row: pd.Series | None, ranks_row: pd.Series | None, tickers: list[str],
    cfg: BacktestConfig, top_n: int,
) -> list[str]:
    """
    Centralizes the "cross-sectional top_n cutoff vs. absolute per-ticker selection" decision,
    shared by run() (live) and generate_strategy_monthly_picks() (backtest), so they can't
    diverge on it. Every strategy_type other than "absolute_momentum" replicates
    get_top_etfs()'s exact behavior (ranks_row.nsmallest(top_n)), just against a single
    already-sliced row instead of a full history DataFrame.
    """
    strategy_type = getattr(cfg, "strategy_type", "momentum")

    if strategy_type == "absolute_momentum":
        return select_absolute_momentum_picks(scores_row, tickers, cfg.defensive_ticker)

    if ranks_row is None:
        return []
    return ranks_row.nsmallest(top_n).index.tolist()


def generate_strategy_monthly_picks(
    daily_prices: pd.DataFrame, tickers: list[str], cfg: BacktestConfig, lookback_period: float,
    top_n: int,
) -> pd.Series:
    """
    Backtest-facing counterpart to resolve_strategy_scores(): produces a FULL HISTORICAL
    monthly_picks series (index=rebalance-period dates, values=list of picked tickers), the same
    shape research notebooks already hand-build for the default strategy, now reusable and
    strategy_type-aware. A date whose scores are all-NaN (e.g. the lookback window isn't
    satisfied yet at the start of the history) is skipped entirely, not included with an empty
    pick list.
    """
    scores = resolve_strategy_scores(daily_prices, tickers, cfg, lookback_period).dropna(how="all")
    ranks = assign_ranks(scores)
    picks = {}
    for date in ranks.index:
        ranks_row = ranks.loc[date].dropna()
        scores_row = scores.loc[date].dropna() if date in scores.index else None
        if ranks_row.empty and (scores_row is None or scores_row.empty):
            continue
        selected = resolve_strategy_picks(scores_row, ranks_row, tickers, cfg, top_n)
        if not selected:
            continue
        picks[date] = selected
    return pd.Series(picks)
