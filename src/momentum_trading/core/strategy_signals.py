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

import pandas as pd

from ..backtest.momentum_backtest import BacktestConfig
from ..execution.live_signal import resolve_momentum_scores, assign_ranks
from .functions_quant_extensions import blend_momentum_scores

# strategy_type values that only affect SIZING/EXPOSURE (via daily_runner.py's
# apply_strategy_type_preset() feeding the existing resolve_target_weights()/
# compute_target_weights() machinery), never RANKING. All fall through to the existing
# resolve_momentum_scores() unchanged. Every other strategy_type dispatches to a genuinely new
# ranking function below, added incrementally, one per epic.
_SIZING_ONLY_STRATEGY_TYPES = (
    "momentum", "relative_momentum", "dual_momentum", "volatility_scaled_momentum",
    "correlation_weighted_momentum",
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

    if strategy_type in _SIZING_ONLY_STRATEGY_TYPES:
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

    raise ValueError(
        f"resolve_strategy_scores(): unhandled strategy_type {strategy_type!r}, "
        f"this strategy's ranking function hasn't been wired in yet."
    )


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
        row = ranks.loc[date].dropna()
        if row.empty:
            continue
        picks[date] = row.nsmallest(top_n).index.tolist()
    return pd.Series(picks)
