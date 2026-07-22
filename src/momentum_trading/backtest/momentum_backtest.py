"""
strategy1_reformulated.py

Institutional-style reformulation of the naive equal-weight momentum backtest.

WHAT THIS DOES DIFFERENTLY FROM THE ORIGINAL `run_custom_backtest`
--------------------------------------------------------------------
1. Inverse-volatility position sizing instead of naive equal weight (risk parity–lite).
2. Portfolio-level volatility targeting -> gross exposure is throttled when realized
   vol runs hot (this is the single biggest defense against momentum crashes).
3. Trend/regime filter on the benchmark (SPY > 200D SMA) -> de-risks to cash in
   confirmed downtrends instead of blindly holding through 2008/2020-style routs.
4. Per-position stop-loss checked daily, not just at rebalance.
5. Max single-position weight cap (concentration risk control).
6. Volatility-aware slippage model + explicit cost ledger (commission vs. slippage
   are tracked separately so cost drag is visible, not hidden in daily P&L).
7. Seeded RNG -> reproducible, auditable results (an unseeded backtest is not a backtest).
8. Full tearsheet: CAGR, vol, Sharpe, Sortino, max drawdown, Calmar, win rate,
   beta/alpha vs SPY, turnover, and total cost drag, not just cumulative return.

HONEST CAVEAT
-------------
No parameter set here "guarantees" profit or guaranteed SPY outperformance.
Risk controls reduce tail risk and cost drag; they do not remove market risk.
Always validate with out-of-sample / walk-forward testing before risking capital,
and treat any single backtest run with healthy skepticism (regime dependence,
survivorship bias in the ETF universe, and overfitting are the usual killers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

logger = logging.getLogger("momentum_backtest")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
# See execution/live_signal.py's identical comment, without this, every message here
# would double-print when this module is imported into daily_runner.py's process (which
# also configures the ROOT logger via logging.basicConfig()).
logger.propagate = False


# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
# Every selectable strategy_type value (docs/MOMENTUM_STRATEGIES.md), module-level so both
# BacktestConfig.__post_init__'s validation and daily_runner.py's apply_strategy_type_preset()
# (and core/strategy_signals.py's router) share the exact same list, no risk of the two drifting
# apart. "momentum" and "relative_momentum" are deliberate aliases, identical behavior, the base
# cross-sectional signal every other value either presets fields on top of or replaces entirely.
ALLOWED_STRATEGY_TYPES = (
    "momentum", "relative_momentum", "dual_momentum", "volatility_scaled_momentum",
    "residual_momentum", "absolute_momentum", "rank_sign_momentum", "hybrid_multi_factor",
    "path_dependent_momentum", "correlation_weighted_momentum", "multi_timeframe_composite",
)


@dataclass
class BacktestConfig:
    holding_period: float = 1.0             # months between forced rebalances, accepts
                                             # fractional values that map onto weeks:
                                             # 0.25 = every week, 0.5 = every 2 weeks,
                                             # 0.75 = every 3 weeks, 1.0 = every month (default,
                                             # unchanged behavior), integers >1 = every N months.
                                             # LIVE: see is_rebalance_day()'s weekly branch in
                                             # execution/live_signal.py. Values below 0.25 (faster
                                             # than weekly) are allowed but trigger a non-blocking
                                             # WARNING alert + email every run (see daily_runner.py
                                             # / is_holding_period_too_frequent()), deliberately
                                             # not hard-blocked, since it's a real, well-defined
                                             # schedule, just an economically inadvisable one.
    lookback_period: float = 12.0           # trailing months of returns used to RANK tickers by
                                             # momentum (distinct from holding_period, which
                                             # controls how often you rebalance, not how far back
                                             # the signal looks). LIVE-ONLY, the backtest engine
                                             # operates on monthly_picks, which the caller (e.g. a
                                             # research notebook) already computed upstream via
                                             # calculate_period_returns(..., period=lookback_period)
                                             # before run_custom_backtest() ever runs, so this field
                                             # has no effect on backtest results (mirrors
                                             # commission's BACKTEST-ONLY note, opposite direction).
                                             # Accepts fractional values, but its granularity is
                                             # tied to holding_period's regime, not its own value:
                                             # if holding_period < 1 (weekly cadence), lookback_period
                                             # is ALSO interpreted in week-quarters via the same
                                             # round(x * 4) formula is_rebalance_day() uses for
                                             # holding_period (0.5 = 2 weeks, 0.75 = 3 weeks, 1.0 =
                                             # 4 weeks, 1.5 = 6 weeks), see
                                             # execution/live_signal.py's resolve_momentum_scores().
                                             # If holding_period >= 1, lookback_period stays in
                                             # whole months exactly as before. Values shorter than 2
                                             # weeks in the weekly regime are allowed but trigger a
                                             # non-blocking WARNING (see
                                             # is_lookback_period_too_short()), a momentum signal
                                             # that short is genuinely noisy.
    skip_month_guardrail: bool = False      # LIVE-ONLY, the "Skip-Month" guardrail (classic
                                             # academic "12-1 momentum" construction): when True
                                             # AND lookback_period > 3 (months) AND
                                             # holding_period >= 1 (monthly regime, this is
                                             # inherently a monthly-lookback concept), the most
                                             # recent ~month is excluded from the momentum
                                             # ranking window, to avoid short-term reversal decay.
                                             # Default False, deliberately opt-in: enabling it
                                             # changes what the SAME lookback_period actually
                                             # picks each rebalance, a real signal-construction
                                             # change, not just a new warning. See
                                             # execution/live_signal.py's resolve_momentum_scores().
    use_absolute_momentum: bool = False     # LIVE-ONLY, the "Absolute Momentum (Macro)"
                                             # constraint (Mandatory tier, docs/RISK_CONSTRAINTS.md):
                                             # Antonacci-style dual momentum, when True, any pick
                                             # whose OWN trailing return (not just its rank
                                             # relative to other picks) is negative gets swapped
                                             # for defensive_ticker below instead of being held.
                                             # Distinct from use_regime_filter above, which scales
                                             # the WHOLE book by one benchmark's trend, this swaps
                                             # INDIVIDUAL picks by their own momentum, the two are
                                             # complementary, not redundant, can both be enabled at
                                             # once. Default False, deliberately opt-in like
                                             # skip_month_guardrail above: enabling it changes what
                                             # the SAME picks actually resolve to each rebalance, a
                                             # real signal-construction change, not just a new
                                             # warning. See execution/live_signal.py's
                                             # apply_absolute_momentum_filter(), which reuses
                                             # core/functions_quant_extensions.py's
                                             # absolute_momentum_overlay() directly.
    defensive_ticker: str = "BIL"           # only meaningful when use_absolute_momentum is True,
                                             # the ticker substituted in for a negative-absolute-
                                             # momentum pick (e.g. 'BIL' T-bill ETF, 'SHY' short
                                             # treasuries). Must be priced alongside the portfolio's
                                             # own tickers for this to work (add it to that
                                             # portfolio's own tickers: list in config.yaml).
    persist_dry_run_state: bool = False     # DRY-RUN-ONLY, no effect in --live (the broker is
                                             # always the source of truth there). Default False
                                             # preserves dry-run's existing behavior exactly:
                                             # current_positions = {} on every invocation, a
                                             # stateless signal/sizing preview. Set True to
                                             # instead reconstruct a simulated portfolio from
                                             # the trade log's own dry_run=True rows
                                             # (execution/live_signal.py's
                                             # reconstruct_dry_run_positions()), so dry-run mode
                                             # behaves like a persistent, no-IBKR-required paper
                                             # ledger across separate invocations. For an
                                             # ACTUALLY broker-verified persistent paper
                                             # portfolio, prefer --live --port 7497 against a
                                             # real IBKR paper account instead, that path
                                             # already resumes correctly for the reasons
                                             # documented in docs/RUNNING.md's "Restart and
                                             # Resume Behavior" section.
    initial_capital: float = 100_000.0
    commission: float = 0.0                 # flat $ per trade, BACKTEST-ONLY: only
                                             # run_risk_managed_backtest()'s simulated cash
                                             # ledger reads this. Live trading (daily_runner.py)
                                             # pulls REAL cash/positions from IBKR every run, so
                                             # there's no simulated balance to deduct a commission
                                             # from, real commission is already reflected
                                             # automatically in the broker's real account state.
    exchange: str = "NYSE"

    # --- risk management knobs ---
    vol_lookback_days: int = 63             # ~3 months, used for inverse-vol weights
    target_portfolio_vol: float = 0.15      # annualized vol target for the book
    portfolio_vol_lookback: int = 21        # trailing window to estimate realized vol
    max_gross_exposure: float = 1.0         # never lever above 100% invested
    min_gross_exposure: float = 0.20        # floor so we don't fully flatline
    max_position_weight: float = 0.35       # single-name cap, FLAT, identical for every ticker
                                             # regardless of that ticker's own volatility
    position_vol_budget: float | None = None  # the "Volatility-Adjustment" (Scaling) constraint:
                                             # None (default) = disabled. When set, each
                                             # position is ALSO capped at
                                             # position_vol_budget / asset_vol (that ticker's own
                                             # trailing realized vol, same vol_lookback_days
                                             # window inverse-vol sizing already uses), whichever
                                             # of that or max_position_weight above is more
                                             # restrictive wins. Complementary to, not redundant
                                             # with, max_position_weight: a low-vol name can be
                                             # allowed a larger weight than a high-vol name even
                                             # under the same flat cap. Never allows a single
                                             # position's vol contribution to exceed this budget,
                                             # regardless of how strong the momentum signal is.
                                             # See backtest/momentum_backtest.py's
                                             # _apply_volatility_budget_caps().
    stop_loss_pct: float = 0.12             # per-position stop from entry price
    ticker_risk_overrides: dict = field(default_factory=dict)  # {ticker: {'enabled': bool,
        # 'stop_loss_pct': float}}, per-ticker override of the portfolio-wide stop_loss_pct
        # above. A ticker with no entry here uses stop_loss_pct unchanged (byte-identical
        # default). 'enabled': false disables the stop-loss check entirely for that ticker
        # (never flagged/sold, no broker-side bracket attached even if attach_broker_stop_loss
        # is on for the rest of the portfolio); 'enabled': true (or omitted) with a
        # 'stop_loss_pct' key uses that ticker-specific width instead of the portfolio
        # default. See execution/live_signal.py's resolve_ticker_stop_loss_pct() and
        # docs/RISK_CONSTRAINTS.md's "Per-Ticker Stop-Loss Override".
    use_regime_filter: bool = True
    regime_benchmark: str = "SPY"
    regime_sma_window: int = 200
    # --- second regime dimension, blended into the SAME regime_scalar as the SMA trend
    #     check above (opt-in, None preserves today's SMA-only behavior exactly). When set,
    #     the regime benchmark's own trailing realized volatility (regime_vol_lookback_days
    #     window, annualized) is compared against this threshold; exceeding it pushes
    #     regime_scalar defensive (min_gross_exposure) even when the SMA trend is still
    #     bullish, so a bullish-but-suddenly-volatile market is also throttled, not just a
    #     bearish one. Still a smooth multiplicative throttle composed with vol_scalar
    #     exactly as before, not a new hard binary gate. See
    #     execution/live_signal.py's compute_target_weights() and
    #     run_risk_managed_backtest()'s identical live/backtest-parity implementation, and
    #     docs/RISK_CONSTRAINTS.md's "Regime Filter: Volatility Dimension" section. ---
    regime_vol_threshold: float | None = None
    regime_vol_lookback_days: int = 21

    # --- execution realism ---
    base_slippage_bps: float = 2.0          # baseline slippage in basis points
    vol_slippage_multiplier: float = 0.5    # extra slippage scaled by annualized vol
    random_seed: int = 42

    # --- turnover / cost control ---
    drift_threshold: float = 0.03           # only trade a name if |target_w - current_w| exceeds this
    min_trade_size: float = 50.0            # skip any trade below this $ notional
    max_turnover_pct: float = 0.20          # LIVE-ONLY advisory (non-blocking WARNING) threshold
                                             # for the "Turnover Limit" constraint:
                                             # Total_Positions_Changed / Total_Positions per
                                             # rebalance (execution/live_signal.py's
                                             # compute_turnover()/is_turnover_too_high()), a
                                             # position-COUNT ratio, distinct from
                                             # drift_threshold/min_trade_size above, which are
                                             # dollar-value filters. High turnover is a sign the
                                             # momentum ranking is over-sensitive to noise.
    total_value_drift_warning_pct: float = 0.10  # LIVE-ONLY advisory (non-blocking WARNING),
                                             # only for a FIXED (non-null) total_value
                                             # portfolio, total_value never auto-refreshes from
                                             # real account P&L (a deliberate, documented
                                             # choice, an allocation ceiling, not
                                             # auto-compounding), so this warns when this
                                             # portfolio's own real position value (explicitly
                                             # scoped to its configured tickers, not the whole
                                             # shared-account positions_value computation,
                                             # which double-counts a ticker legitimately shared
                                             # between two portfolios under TICKER OVERLAP)
                                             # exceeds the configured total_value by more than
                                             # this fraction, a sign the static number has gone
                                             # stale. Real per-portfolio cash can't be isolated
                                             # on a shared IBKR account, so only the POSITION
                                             # side is compared, not a full "total value"
                                             # reconstruction.
    low_capital_drop_warning_pct: float = 0.30  # LIVE-ONLY advisory (non-blocking WARNING).
                                             # IBKR's API has no fractional-equity order support
                                             # (see place_orders_ibkr()), so any intended BUY
                                             # whose share count floors to 0 is dropped entirely
                                             # (status DROPPED_FRACTIONAL) rather than ever
                                             # reaching the broker. When the fraction of intended
                                             # BUYs dropped this way on a single rebalance exceeds
                                             # this threshold, daily_runner.py fires a
                                             # LOW_CAPITAL_FRACTIONAL_DROP warning: total_value is
                                             # likely too small relative to top_n and this
                                             # portfolio's ticker prices for real capital to
                                             # actually get deployed. e.g. 0.30 = warn if more
                                             # than 30% of this rebalance's intended BUYs were
                                             # dropped for flooring to 0 shares.

    # --- cash flow simulation ---
    monthly_contribution: float = 0.0       # $ added to cash at each rebalance date (0 = off)

    # --- share granularity ---
    allow_fractional_shares: bool = False    # True = size positions to 4dp fractional shares
                                              # (only if your broker/tickers actually support it)
    redeploy_flooring_remainder: bool = False  # LIVE-facing sizing, but exercised by
        # generate_orders() regardless of --live/dry-run. Opt-in, False (default) is
        # byte-identical to before this field existed. IBKR has no fractional equity order
        # support, so every BUY's target dollar amount is floored to a whole share count,
        # leaving a small leftover per ticker (e.g. a $500 target on a $270 stock floors to 1
        # share = $270, $230 of that ticker's OWN allocation goes unused). When True, this
        # rebalance's pooled flooring remainder across every BUY is redeployed into EXTRA whole
        # shares of the single TOP-RANKED BUY ticker this rebalance (not spread across the
        # basket), only meaningful when allow_fractional_shares is False (nothing to pool
        # otherwise). See docs/RISK_CONSTRAINTS.md's "Flooring Remainder Redeployment".

    # --- Live order execution, cash-aware buy sizing ---
    auto_reduce_buys_on_insufficient_cash: bool = False   # LIVE ONLY. place_orders_ibkr()
        # always submits SELLs first and waits for them to clear before submitting BUYs
        # (unconditional, not configurable here). This flag controls what happens if BUYs
        # would still exceed real available cash after sells clear: False (default) = warn
        # only, submit as computed, let IBKR's own fill/reject be the backstop. True =
        # proportionally scale down BUY share counts (floored to whole shares) to fit.

    # --- live extended-hours (pre-market/after-hours) trading ---
    allow_extended_hours: bool = False   # LIVE ONLY, no effect on the backtest (daily-close
        # based). place_orders_ibkr() submits plain MKT orders by default, which IBKR/exchanges
        # only accept during regular trading hours (9:30am-4:00pm ET), a rebalance running
        # right at or after the close gets "Order rejected - reason:Exchange is closed" (IBKR
        # error 201), same as everyone else's plain MKT order. True switches those orders to
        # LMT with outsideRth=True instead, IBKR does not accept MKT orders outside RTH at
        # all, only LMT (confirmed against IBKR's own TWS API docs), so this is a real order-type
        # change, not just a flag. The limit price is the last known price +/- a small buffer
        # (favors getting filled over exact price). Covers NASDAQ's standard extended sessions:
        # pre-market 4:00-9:30am ET, after-hours 4:00-8:00pm ET. Thinner ETH liquidity means a
        # real chance of no fill, a partial fill, or a worse price than a similar RTH order,
        # this is a real economic trade-off, not just a technical toggle. Ticker/price
        # availability still applies: if no reference price exists for a ticker that run, the
        # order silently falls back to a regular MKT (RTH-only) order instead of failing.

    # --- selection: how many top-momentum-ranked tickers to actually hold ---
    top_n: int = 10   # e.g. 3 = hold only the 3 strongest names this rebalance; clamped to
                       # len(tickers) if the portfolio's universe is smaller than this

    log_file_path: str = "trades_log.txt"

    # --- stop-loss automation (item 2) ---
    auto_execute_stop_loss: bool = False     # False = flag only (live_signal path); True = engine auto-sells (backtest already does this)
    attach_broker_stop_loss: bool = False    # LIVE-ONLY, belt-and-suspenders alongside
                                              # auto_execute_stop_loss above, NOT a replacement
                                              # for it: attaches a REAL IBKR bracket STP order at
                                              # BUY time (reuses stop_loss_pct below), protecting
                                              # the position at the BROKER even when this app
                                              # isn't running, unlike auto_execute_stop_loss's
                                              # Python-side check, which only ever runs when
                                              # daily-runner --live is actually invoked. See
                                              # execution/live_signal.py's place_orders_ibkr().

    # --- correlation-aware sizing (item 10) ---
    use_correlation_penalty: bool = False
    correlation_lookback_days: int = 63
    correlation_penalty_strength: float = 0.5   # 0 = no penalty, 1 = full pairwise-correlation scaling

    # --- aggregate-drift rebalance skip (item 11) ---
    aggregate_drift_threshold: float = 0.0   # 0 = disabled (always rebalance if scheduled); e.g. 0.02 = skip whole rebalance if <2% aggregate drift

    # --- crash protection: portfolio-level circuit breaker ---
    max_portfolio_drawdown_pct: float = 0.0   # 0 = disabled; e.g. 0.20 = halt new entries at -20% from peak equity

    # --- crash protection: correlation spike detection ---
    use_correlation_spike_regime: bool = False
    correlation_spike_short_window: int = 7
    correlation_spike_baseline_window: int = 63
    correlation_spike_threshold: float = 0.3

    # --- crash protection: liquidity-crisis-aware execution ---
    liquidity_stress_multiplier: float = 1.0   # 1.0 = disabled; e.g. 2.0 = double slippage under stress
    liquidity_stress_recent_days: int = 5
    liquidity_stress_vol_ratio: float = 2.0    # recent vol > this multiple of trailing avg -> stress
    liquidity_stress_reduce_only: bool = False  # True = block new BUYs (not SELLs) during detected stress

    # --- capacity / market-impact check ---
    max_pct_of_adv: float = 0.0   # 0 = disabled; e.g. 0.05 = warn if a position exceeds 5% of average daily volume

    # --- liquidity / universe filter, a PRE-selection eligibility filter (distinct from
    #     max_pct_of_adv above, which is a POST-selection advisory warning): zeroes a ticker's
    #     RANK on any date its trailing average dollar volume falls below min_avg_dollar_volume,
    #     so it can never be selected into top_n at all that rebalance, not just flagged after
    #     the fact. core/functions_quant_extensions.py's liquidity_filter(). Opt-in, False
    #     default is byte-identical to before this existed. LIVE + BACKTEST (see
    #     execution/live_signal.py's run() and core/strategy_signals.py's
    #     generate_strategy_monthly_picks()'s daily_volume param). ---
    use_liquidity_filter: bool = False
    min_avg_dollar_volume: float = 1_000_000.0  # trailing avg (price * volume) required to
                                             # remain selection-eligible, liquidity_filter()'s
                                             # own default
    liquidity_lookback_days: int = 63       # trading-day window for the trailing average above
                                             # (~3 months), liquidity_filter()'s own default

    # --- Additional execution safety checks ---
    max_dollar_drawdown: float | None = None       # e.g. 500.0, halt if equity drops this many $ from peak, independent of the % breaker
    max_slippage_tolerance_pct: float | None = None  # e.g. 0.02, alert (not un-fill) if actual fill deviates from expected price by more than this
    max_price_staleness_minutes: int | None = None   # e.g. 30, abort the run rather than trade on a price feed older than this
    max_holding_days: int | None = None              # e.g. 90, force-exit a position after N days regardless of price (time-based stop)
    max_bid_ask_spread_pct: float | None = None       # the "Liquidity/Slippage Monitor" constraint
                                             # (Nice-to-Have tier, docs/RISK_CONSTRAINTS.md).
                                             # None (default) = disabled, LIVE-ONLY, requires a
                                             # real TWS/Gateway connection with a real-time
                                             # market-data subscription (see
                                             # execution/live_signal.py's
                                             # fetch_bid_ask_spread()). e.g. 0.01 = drop an
                                             # order (DROPPED_WIDE_SPREAD) rather than submit it
                                             # into a real-time bid-ask spread wider than 1%.

    # --- Alternative position-sizing method ---
    sizing_method: str = "inverse_vol"   # "inverse_vol" (default), "score_proportional", or
                                          # "equal_weight" (non-parametric, ignores both score
                                          # magnitude and trailing vol, every pick gets 1/N)

    # --- Selectable momentum strategy type (docs/MOMENTUM_STRATEGIES.md), config-driven per
    #     portfolio via default_risk/risk_overrides, same mechanism as every other field here.
    #     "momentum"/"relative_momentum" (identical, the base cross-sectional signal) is the
    #     default and changes NOTHING versus today. Every other value either (a) auto-configures
    #     a bundle of existing fields (dual_momentum, volatility_scaled_momentum,
    #     correlation_weighted_momentum, rank_sign_momentum, see daily_runner.py's
    #     apply_strategy_type_preset(), an explicit field value you set yourself always wins over
    #     the preset's implied one), or (b) dispatches to a genuinely new ranking/selection
    #     function in core/strategy_signals.py's resolve_strategy_scores() router
    #     (multi_timeframe_composite, absolute_momentum, residual_momentum,
    #     path_dependent_momentum, hybrid_multi_factor, the last one LIVE-ONLY, see
    #     docs/MOMENTUM_STRATEGIES.md for why). ---
    strategy_type: str = "momentum"

    # --- Only meaningful when strategy_type == "multi_timeframe_composite" (Epic 2,
    #     docs/MOMENTUM_STRATEGIES.md): blends momentum scores across multiple lookback windows
    #     instead of relying on a single one, via core/functions_quant_extensions.py's
    #     blend_momentum_scores(). Defaults match that function's own defaults exactly. ---
    multi_timeframe_lookbacks: list = field(default_factory=lambda: [3, 6, 12])
    multi_timeframe_weights: list | None = None   # None (default) = equal weight per lookback

    def __post_init__(self):
        """Fail fast on nonsensical config combinations instead of producing silently wrong sizing."""
        errors = []
        if self.min_gross_exposure > self.max_gross_exposure:
            errors.append(f"min_gross_exposure ({self.min_gross_exposure}) > max_gross_exposure ({self.max_gross_exposure})")
        if not (0 < self.max_gross_exposure <= 2.0):
            errors.append(f"max_gross_exposure ({self.max_gross_exposure}) should be in (0, 2.0]")
        if not (0 <= self.min_gross_exposure <= 1.0):
            errors.append(f"min_gross_exposure ({self.min_gross_exposure}) should be in [0, 1.0]")
        if not (0 < self.max_position_weight <= 1.0):
            errors.append(f"max_position_weight ({self.max_position_weight}) should be in (0, 1.0]")
        if not (0 < self.stop_loss_pct < 1.0):
            errors.append(f"stop_loss_pct ({self.stop_loss_pct}) should be in (0, 1.0)")
        if not isinstance(self.ticker_risk_overrides, dict):
            errors.append(f"ticker_risk_overrides ({self.ticker_risk_overrides!r}) must be a dict")
        else:
            for ticker, override in self.ticker_risk_overrides.items():
                if not isinstance(ticker, str) or not isinstance(override, dict):
                    errors.append(
                        f"ticker_risk_overrides[{ticker!r}] must be a {{'enabled': bool, "
                        f"'stop_loss_pct': float}} dict keyed by ticker string"
                    )
                    continue
                extra_keys = set(override) - {"enabled", "stop_loss_pct"}
                if extra_keys:
                    errors.append(
                        f"ticker_risk_overrides[{ticker!r}] has unknown key(s) {sorted(extra_keys)}, "
                        f"only 'enabled'/'stop_loss_pct' are allowed"
                    )
                if "enabled" in override and not isinstance(override["enabled"], bool):
                    errors.append(f"ticker_risk_overrides[{ticker!r}]['enabled'] must be a bool")
                if "stop_loss_pct" in override and not (0 < override["stop_loss_pct"] < 1.0):
                    errors.append(
                        f"ticker_risk_overrides[{ticker!r}]['stop_loss_pct'] "
                        f"({override['stop_loss_pct']}) should be in (0, 1.0)"
                    )
        if self.drift_threshold < 0:
            errors.append(f"drift_threshold ({self.drift_threshold}) must be >= 0")
        if self.min_trade_size < 0:
            errors.append(f"min_trade_size ({self.min_trade_size}) must be >= 0")
        if not (0 < self.max_turnover_pct <= 1.0):
            errors.append(f"max_turnover_pct ({self.max_turnover_pct}) should be in (0, 1.0]")
        if self.total_value_drift_warning_pct <= 0:
            errors.append(
                f"total_value_drift_warning_pct ({self.total_value_drift_warning_pct}) must be > 0"
            )
        if not (0 < self.low_capital_drop_warning_pct <= 1.0):
            errors.append(
                f"low_capital_drop_warning_pct ({self.low_capital_drop_warning_pct}) should be in (0, 1.0]"
            )
        if self.aggregate_drift_threshold < 0:
            errors.append(f"aggregate_drift_threshold ({self.aggregate_drift_threshold}) must be >= 0")
        if not (0 <= self.max_portfolio_drawdown_pct < 1.0):
            errors.append(f"max_portfolio_drawdown_pct ({self.max_portfolio_drawdown_pct}) should be in [0, 1.0)")
        if self.holding_period <= 0:
            errors.append(f"holding_period ({self.holding_period}) must be > 0")
        if self.lookback_period <= 0:
            errors.append(f"lookback_period ({self.lookback_period}) must be > 0")
        if self.top_n < 1:
            errors.append(f"top_n ({self.top_n}) must be >= 1")
        if self.initial_capital <= 0:
            errors.append(f"initial_capital ({self.initial_capital}) must be > 0")
        if self.commission < 0:
            errors.append(f"commission ({self.commission}) must be >= 0")
        if self.target_portfolio_vol <= 0:
            errors.append(f"target_portfolio_vol ({self.target_portfolio_vol}) must be > 0")
        if self.position_vol_budget is not None and self.position_vol_budget <= 0:
            errors.append(f"position_vol_budget ({self.position_vol_budget}) must be > 0 or None")
        if not self.defensive_ticker or not self.defensive_ticker.strip():
            errors.append(f"defensive_ticker ({self.defensive_ticker!r}) must be a non-empty string")
        if not (0 <= self.correlation_penalty_strength <= 1.0):
            errors.append(f"correlation_penalty_strength ({self.correlation_penalty_strength}) should be in [0, 1.0]")
        if self.liquidity_stress_multiplier < 1.0:
            errors.append(f"liquidity_stress_multiplier ({self.liquidity_stress_multiplier}) must be >= 1.0")
        if self.liquidity_stress_recent_days < 1:
            errors.append(f"liquidity_stress_recent_days ({self.liquidity_stress_recent_days}) must be >= 1")
        if self.liquidity_stress_vol_ratio <= 0:
            errors.append(f"liquidity_stress_vol_ratio ({self.liquidity_stress_vol_ratio}) must be > 0")
        if not (0 <= self.max_pct_of_adv <= 1.0):
            errors.append(f"max_pct_of_adv ({self.max_pct_of_adv}) should be in [0, 1.0]")
        if self.min_avg_dollar_volume <= 0:
            errors.append(f"min_avg_dollar_volume ({self.min_avg_dollar_volume}) must be > 0")
        if self.liquidity_lookback_days < 1:
            errors.append(f"liquidity_lookback_days ({self.liquidity_lookback_days}) must be >= 1")
        if self.max_dollar_drawdown is not None and self.max_dollar_drawdown <= 0:
            errors.append(f"max_dollar_drawdown ({self.max_dollar_drawdown}) must be > 0 or None")
        if self.max_slippage_tolerance_pct is not None and not (0 < self.max_slippage_tolerance_pct <= 1.0):
            errors.append(f"max_slippage_tolerance_pct ({self.max_slippage_tolerance_pct}) should be in (0, 1.0] or None")
        if self.max_bid_ask_spread_pct is not None and not (0 < self.max_bid_ask_spread_pct <= 1.0):
            errors.append(f"max_bid_ask_spread_pct ({self.max_bid_ask_spread_pct}) should be in (0, 1.0] or None")
        if self.max_price_staleness_minutes is not None and self.max_price_staleness_minutes <= 0:
            errors.append(f"max_price_staleness_minutes ({self.max_price_staleness_minutes}) must be > 0 or None")
        if self.max_holding_days is not None and self.max_holding_days <= 0:
            errors.append(f"max_holding_days ({self.max_holding_days}) must be > 0 or None")
        if self.sizing_method not in ("inverse_vol", "score_proportional", "equal_weight"):
            errors.append(
                f"sizing_method ({self.sizing_method!r}) must be 'inverse_vol', "
                f"'score_proportional', or 'equal_weight'"
            )
        if self.strategy_type not in ALLOWED_STRATEGY_TYPES:
            errors.append(f"strategy_type ({self.strategy_type!r}) must be one of {ALLOWED_STRATEGY_TYPES}")
        if self.regime_vol_threshold is not None and self.regime_vol_threshold <= 0:
            errors.append(f"regime_vol_threshold ({self.regime_vol_threshold}) must be > 0 or None")
        if self.regime_vol_lookback_days < 2:
            errors.append(f"regime_vol_lookback_days ({self.regime_vol_lookback_days}) must be >= 2")

        if errors:
            raise ValueError("Invalid BacktestConfig:\n  - " + "\n  - ".join(errors))


# --------------------------------------------------------------------------- #
# HELPERS
# --------------------------------------------------------------------------- #
def _split_price_panel(daily_prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Accepts either:
      - a plain DataFrame of close prices (columns = tickers), or
      - a MultiIndex-column DataFrame shaped like daily_prices.xs('open', level=1, axis=1),
        i.e. columns = (ticker, field) with fields such as 'open'/'close'/'high'/'low'/'volume'.

    Returns (close_prices, open_prices). If no 'open' field exists, open_prices falls
    back to close_prices (same-bar execution, matching the original naive behavior).
    """
    if isinstance(daily_prices.columns, pd.MultiIndex):
        level1 = {str(x).lower() for x in daily_prices.columns.get_level_values(1)}
        close_field = "close" if "close" in level1 else ("adj close" if "adj close" in level1 else None)
        if close_field is None:
            raise ValueError(
                "daily_prices has MultiIndex columns but no 'close' field found at level=1."
            )
        cols = daily_prices.columns
        norm = pd.MultiIndex.from_tuples(
            [(t, str(f).lower()) for t, f in cols], names=cols.names
        )
        dp = daily_prices.copy()
        dp.columns = norm
        close_prices = dp.xs(close_field, level=1, axis=1)
        if "open" in level1:
            open_prices = dp.xs("open", level=1, axis=1)
        else:
            logger.info("No 'open' field found in daily_prices; execution will use close prices.")
            open_prices = close_prices
        return close_prices, open_prices
    else:
        return daily_prices, daily_prices


def _round_shares(raw_shares: float, allow_fractional: bool) -> float:
    """
    Floor to whole shares by default (matches real market-order fills for most
    US brokers on ETFs). If allow_fractional_shares=True, round DOWN to 4dp
    instead, never round up, since that could overspend available cash.
    Only set allow_fractional=True if your broker/ticker combo actually
    supports fractional share orders (IBKR supports it for many, not all,
    US equities/ETFs, confirm per-ticker before relying on this).
    """
    if allow_fractional:
        return np.floor(raw_shares * 10_000) / 10_000
    return np.floor(raw_shares)


def detect_correlation_spike(
    daily_prices: pd.DataFrame, as_of: pd.Timestamp,
    short_window: int = 7, baseline_window: int = 63, spike_threshold: float = 0.3,
) -> bool:
    """
    Fast-reacting crash indicator, distinct from the
    sizing-time _correlation_penalty_weights() (which uses a single rolling
    window and reacts slowly). This compares a SHORT recent window's average
    pairwise correlation against a longer baseline, in real crashes,
    normally-uncorrelated assets often move together in a matter of days
    ("correlation goes to 1"), which this is built to catch faster than a
    single ~63-day rolling average would.

    Returns True if short-window average correlation exceeds baseline by more
    than spike_threshold (e.g. 0.3 = a 30-percentage-point jump).
    """
    cols = [c for c in daily_prices.columns if c != daily_prices.index.name]
    window_all = daily_prices.loc[:as_of].tail(max(short_window, baseline_window) + 1)
    if len(window_all) < baseline_window + 1:
        return False  # not enough history to compare

    rets = window_all.pct_change().dropna(how="all")
    baseline_corr = rets.tail(baseline_window).corr()
    short_corr = rets.tail(short_window).corr()

    def _avg_offdiag(corr_df):
        n = len(corr_df)
        if n < 2:
            return np.nan
        return (corr_df.sum().sum() - n) / (n * (n - 1))

    baseline_avg = _avg_offdiag(baseline_corr)
    short_avg = _avg_offdiag(short_corr)
    if pd.isna(baseline_avg) or pd.isna(short_avg):
        return False

    return bool((short_avg - baseline_avg) > spike_threshold)


def _correlation_penalty_weights(
    weights: dict, daily_prices: pd.DataFrame, as_of: pd.Timestamp,
    lookback_days: int, strength: float,
) -> dict:
    """
    Downweights tickers that are highly correlated with the rest of the current
    picks, so two near-duplicate exposures (e.g. XLK and QQQ both selected)
    don't get sized as if they were independent risk sources. strength=0 is a
    no-op; strength=1 applies the full penalty.
    """
    tickers = [t for t in weights if t in daily_prices.columns]
    if len(tickers) < 2 or strength <= 0:
        return weights

    window = daily_prices[tickers].loc[:as_of].pct_change().tail(lookback_days)
    corr = window.corr()
    if corr.isna().all().all():
        return weights

    avg_corr = (corr.sum(axis=1) - 1) / max(len(tickers) - 1, 1)  # exclude self-correlation
    avg_corr = avg_corr.clip(lower=0, upper=1).fillna(0)  # only penalize positive co-movement

    penalized = {t: weights[t] * (1 - strength * avg_corr.get(t, 0.0)) for t in weights}
    total = sum(penalized.values())
    if total <= 0:
        return weights
    return {t: w / total for t, w in penalized.items()}


def _score_proportional_weights(picks: list[str], momentum_scores: pd.Series | None) -> dict:
    """
    Weights proportional to each pick's momentum score
    (higher trailing return -> larger weight), instead of inverse-vol sizing.
    The theory: if the signal genuinely ranks conviction, the strongest
    momentum names arguably deserve more capital, not less, inverse-vol
    sizing is agnostic to signal STRENGTH, only to volatility.

    Falls back to equal weight if momentum_scores isn't provided or all
    scores are non-positive (can't meaningfully weight by a negative or zero
    "strength").
    """
    if momentum_scores is None:
        return {t: 1.0 / len(picks) for t in picks}

    scores = {t: momentum_scores.get(t) for t in picks if t in momentum_scores.index}
    positive_scores = {t: s for t, s in scores.items() if pd.notna(s) and s > 0}

    if not positive_scores:
        return {t: 1.0 / len(picks) for t in picks}

    total = sum(positive_scores.values())
    weights = {t: s / total for t, s in positive_scores.items()}
    # any pick missing a usable score gets excluded from this method's output;
    # resolve_target_weights' caller still holds the full picks list, so a
    # zero-weight here just means "not sized," not "silently dropped from picks"
    return weights


def _equal_weight_weights(picks: list[str]) -> dict:
    """
    Non-parametric sizing: every pick gets an identical 1/N weight, ignoring both raw score
    magnitude (unlike score_proportional) and trailing volatility (unlike inverse_vol). Backs
    strategy_type "rank_sign_momentum" (docs/MOMENTUM_STRATEGIES.md), the literal "sign-only,
    reduces outlier impact" reading, a large momentum reading and a barely-positive one get the
    same capital.
    """
    return {t: 1.0 / len(picks) for t in picks}


def resolve_target_weights(
    picks: list[str], daily_prices: pd.DataFrame, as_of: pd.Timestamp, cfg: "BacktestConfig",
    custom_weights: dict | None = None, momentum_scores: pd.Series | None = None,
) -> dict:
    """
    Single source of truth for turning a pick list into position weights,
    used by BOTH the backtest engine and live_signal.py, so live sizing can
    never silently diverge from what was backtested.

    If custom_weights is provided, it's used directly (renormalized to sum to
    1.0 across the intersection with `picks`) and inverse-vol sizing /
    correlation penalty are skipped entirely. Position caps still apply.

    If custom_weights is NOT provided, cfg.sizing_method selects between
    "inverse_vol" (default, weight inversely to trailing volatility),
    "score_proportional" (weight proportional to each
    pick's momentum score, requires momentum_scores to be passed in; falls
    back to equal-weight if scores aren't available), and "equal_weight"
    (non-parametric, every pick gets an identical 1/N weight, see
    _equal_weight_weights()).
    """
    if custom_weights is not None:
        provided = {t: w for t, w in custom_weights.items() if t in picks and w > 0}
        if not provided:
            raise ValueError("custom_weights provided but none of its tickers are in `picks`.")
        total = sum(provided.values())
        if total > 1.0 + 1e-6:
            logger.warning("custom_weights sum to %.4f (>1.0); renormalizing.", total)
        weights = {t: w / total for t, w in provided.items()}
    elif cfg.sizing_method == "score_proportional":
        weights = _score_proportional_weights(picks, momentum_scores)
        if cfg.use_correlation_penalty:
            weights = _correlation_penalty_weights(
                weights, daily_prices, as_of, cfg.correlation_lookback_days, cfg.correlation_penalty_strength
            )
    elif cfg.sizing_method == "equal_weight":
        weights = _equal_weight_weights(picks)
        if cfg.use_correlation_penalty:
            weights = _correlation_penalty_weights(
                weights, daily_prices, as_of, cfg.correlation_lookback_days, cfg.correlation_penalty_strength
            )
    else:
        weights = _inverse_vol_weights(picks, daily_prices, as_of, cfg.vol_lookback_days)
        if cfg.use_correlation_penalty:
            weights = _correlation_penalty_weights(
                weights, daily_prices, as_of, cfg.correlation_lookback_days, cfg.correlation_penalty_strength
            )

    weights = _apply_position_caps(weights, cfg.max_position_weight)
    if cfg.position_vol_budget is not None:
        weights = _apply_volatility_budget_caps(
            weights, daily_prices, as_of, cfg.vol_lookback_days,
            cfg.position_vol_budget, cfg.max_position_weight,
        )
    return weights


def _trading_days(prices: pd.DataFrame, exchange: str) -> pd.DatetimeIndex:
    cal = mcal.get_calendar(exchange)
    return cal.schedule(start_date=prices.index.min(), end_date=prices.index.max()).index


def _rebalance_dates(monthly_picks: pd.Series, trading_days: pd.DatetimeIndex) -> set:
    dates = []
    for signal_date in monthly_picks.index:
        idx = trading_days.searchsorted(signal_date, side="right")
        if idx < len(trading_days):
            dates.append(trading_days[idx])
    return set(dates)


def _inverse_vol_weights(
    tickers: list[str], daily_prices: pd.DataFrame, as_of: pd.Timestamp, lookback: int
) -> dict:
    """Risk-parity-lite: weight inversely proportional to trailing realized vol."""
    window = daily_prices.loc[:as_of].iloc[-(lookback + 1):]
    valid = [t for t in tickers if t in window.columns]
    if not valid:
        return {}
    rets = window[valid].pct_change().dropna(how="all")
    vol = rets.std().replace(0, np.nan)
    inv_vol = (1.0 / vol).replace([np.inf, -np.inf], np.nan)
    inv_vol = inv_vol.dropna()
    if inv_vol.empty:
        # fallback to equal weight if vol is undefined (e.g. brand-new listing)
        return {t: 1.0 / len(valid) for t in valid}
    weights = inv_vol / inv_vol.sum()
    # fill any dropped tickers with a small equal-weight residual so nothing is orphaned
    missing = [t for t in valid if t not in weights.index]
    if missing:
        residual = max(0.0, 1.0 - weights.sum())
        for t in missing:
            weights[t] = residual / len(missing)
    return weights.to_dict()


def _apply_position_caps(weights: dict, max_weight: float) -> dict:
    """
    Cap any single position and redistribute the excess proportionally.

    A real, confirmed bug (found via Epic 4 of the "Rebalance Reporting Clarity &
    Selection-Logic Fixes" plan, exposed by a single-ticker portfolio): when EVERY ticker over
    max_weight has no ticker under the cap to redistribute the excess into (a single-ticker
    portfolio, or every picked ticker simultaneously over cap), this loop correctly `break`s
    without redistributing, but the OLD code then unconditionally renormalized every weight
    back to sum to 1.0, silently rescaling the just-capped ticker(s) back up past the cap (a
    single ticker capped to 0.35 ended up back at 1.0, defeating the cap entirely).
    `redistribution_incomplete` tracks exactly this case (the loop broke at `under_sum <= 0`
    while `over` was still non-empty, as opposed to the normal case where `over` becomes empty
    because capping+redistribution fully succeeded) and skips the final renormalize, correctly
    leaving the undistributable excess as unallocated capital/cash, the whole point of a
    position cap. The normal multi-ticker successful-redistribution path is unchanged.
    """
    weights = dict(weights)
    redistribution_incomplete = False
    for _ in range(10):  # a few passes converges caps without a full LP solve
        over = {t: w for t, w in weights.items() if w > max_weight}
        if not over:
            break
        excess = sum(w - max_weight for w in over.values())
        for t in over:
            weights[t] = max_weight
        under = {t: w for t, w in weights.items() if w < max_weight}
        under_sum = sum(under.values())
        if under_sum <= 0:
            redistribution_incomplete = True
            break
        for t in under:
            weights[t] += excess * (weights[t] / under_sum)
    if redistribution_incomplete:
        return weights
    total = sum(weights.values())
    if total > 0:
        weights = {t: w / total for t, w in weights.items()}
    return weights


def _apply_volatility_budget_caps(
    weights: dict, daily_prices: pd.DataFrame, as_of: pd.Timestamp, lookback: int,
    position_vol_budget: float, max_position_weight: float,
) -> dict:
    """
    The "Volatility-Adjustment" (Scaling) constraint: caps each position at
    min(max_position_weight, position_vol_budget / asset_vol), never allowing a single
    position to exceed its own volatility budget regardless of how strong the momentum signal
    is. Complementary to, not redundant with, _apply_position_caps()'s flat max_position_weight
    cap, that one is identical for every ticker regardless of its own volatility, this one
    varies per ticker, a low-vol name can be allowed a larger weight than a high-vol name even
    under the same flat cap. Reuses the same trailing-vol window _inverse_vol_weights() computes
    (window[valid].pct_change().std() over `lookback` trading days), so "asset volatility" means
    the same thing throughout this module. Same iterative cap-and-redistribute approximation
    _apply_position_caps() already uses (not a full LP solve), per-ticker caps instead of one
    global scalar. Only called when cfg.position_vol_budget is not None.
    """
    tickers = list(weights.keys())
    window = daily_prices.loc[:as_of].iloc[-(lookback + 1):]
    valid = [t for t in tickers if t in window.columns]
    vol = window[valid].pct_change().dropna(how="all").std() if valid else pd.Series(dtype=float)

    caps = {}
    for t in tickers:
        asset_vol = vol.get(t) if t in vol.index else None
        if asset_vol is None or pd.isna(asset_vol) or asset_vol <= 0:
            # undefined vol (e.g. brand-new listing), fall back to the flat cap
            caps[t] = max_position_weight
        else:
            caps[t] = min(max_position_weight, position_vol_budget / asset_vol)

    weights = dict(weights)
    for _ in range(10):
        over = {t: w for t, w in weights.items() if w > caps.get(t, max_position_weight)}
        if not over:
            break
        excess = sum(w - caps[t] for t, w in over.items())
        for t in over:
            weights[t] = caps[t]
        under = {t: w for t, w in weights.items() if w < caps.get(t, max_position_weight)}
        under_sum = sum(under.values())
        if under_sum <= 0:
            break
        for t in under:
            weights[t] += excess * (weights[t] / under_sum)
    total = sum(weights.values())
    if total > 0:
        weights = {t: w / total for t, w in weights.items()}
    return weights


def _realized_portfolio_vol(portfolio_history: list, lookback: int) -> Optional[float]:
    if len(portfolio_history) < lookback + 1:
        return None
    vals = pd.Series([v for _, v in portfolio_history[-(lookback + 1):]])
    rets = vals.pct_change().dropna()
    if rets.empty:
        return None
    return float(rets.std() * np.sqrt(252))


def compute_vol_scalar(
    realized_vol: Optional[float], target_portfolio_vol: float,
    min_gross_exposure: float, max_gross_exposure: float,
) -> float:
    """
    Shared by both the backtest engine and live trading (execution/live_signal.py) so
    portfolio-level vol targeting can't silently diverge between the two paths, the same
    "one shared function" principle resolve_target_weights() already establishes for sizing.

    realized_vol is None (no history yet) or 0 (a degenerate flat series) both fall back to
    max_gross_exposure, the same "not enough information to scale down" behavior
    run_risk_managed_backtest()'s original inline logic used before this was extracted.
    """
    if not realized_vol:
        return max_gross_exposure
    return float(np.clip(target_portfolio_vol / realized_vol, min_gross_exposure, max_gross_exposure))


def _slippage_bps(ticker_returns_window: pd.Series, cfg: BacktestConfig) -> float:
    """
    Base slippage scaled by trailing (vol_lookback_days) annualized vol. If
    liquidity_stress_multiplier > 1.0, additionally checks
    whether the MOST RECENT few days' vol is spiking well above that trailing
    average, a proxy for liquidity deteriorating faster than a long rolling
    window would show, which is exactly when execution quality matters most
    and behaves least like historical averages.
    """
    if ticker_returns_window is None or ticker_returns_window.empty:
        return cfg.base_slippage_bps
    ann_vol = ticker_returns_window.std() * np.sqrt(252)
    if np.isnan(ann_vol):
        ann_vol = 0.0
    slippage = cfg.base_slippage_bps + cfg.vol_slippage_multiplier * ann_vol * 10_000 / 100

    if cfg.liquidity_stress_multiplier > 1.0 and len(ticker_returns_window) >= cfg.liquidity_stress_recent_days:
        recent = ticker_returns_window.tail(cfg.liquidity_stress_recent_days)
        recent_vol = recent.std() * np.sqrt(252)
        if pd.notna(recent_vol) and ann_vol > 0 and recent_vol / ann_vol > cfg.liquidity_stress_vol_ratio:
            slippage *= cfg.liquidity_stress_multiplier

    return slippage


# --------------------------------------------------------------------------- #
# MAIN BACKTEST
# --------------------------------------------------------------------------- #
def run_risk_managed_backtest(
    monthly_picks: pd.Series,
    daily_prices: pd.DataFrame,
    config: BacktestConfig = BacktestConfig(),
    custom_weights_by_date: dict | None = None,
) -> pd.DataFrame:
    """
    Risk-managed momentum backtest with volatility targeting, regime filtering,
    stop losses, and realistic execution costs.

    Parameters
    ----------
    monthly_picks : pd.Series
        Index = month-end signal dates, values = list of tickers selected that month.
    daily_prices : pd.DataFrame
        Daily price panel, columns = tickers (must include the regime benchmark,
        e.g. 'SPY', if use_regime_filter=True).
    config : BacktestConfig
    custom_weights_by_date : dict, optional
        {signal_date: {ticker: weight}}, if present for a given rebalance's
        signal date, those weights are used directly instead of inverse-vol
        sizing (still subject to max_position_weight capping).

    Returns
    -------
    pd.DataFrame
        Monthly report with portfolio & benchmark returns, cumulative returns,
        and exposure/cost diagnostics.
    """
    if monthly_picks.empty or daily_prices.empty:
        logger.warning("Empty picks or price data supplied; aborting backtest.")
        return pd.DataFrame()

    rng = np.random.default_rng(config.random_seed)

    # --- Normalize input: supports plain close-price DataFrames AND MultiIndex
    #     (ticker, field) panels like daily_prices.xs('open', level=1, axis=1). ---
    close_full, open_full = _split_price_panel(daily_prices)

    # --- SETUP: align simulation start to the first tradable month after signal ---
    first_signal_date = monthly_picks.index[0]
    start_of_first_trade_month = (first_signal_date + pd.DateOffset(months=1)).to_period("M").start_time
    candidates = close_full.index[close_full.index >= start_of_first_trade_month]
    if candidates.empty:
        logger.warning("No trading days found after the first signal date.")
        return pd.DataFrame()
    sim_start_date = candidates[0]

    mask = close_full.index >= (sim_start_date - pd.DateOffset(days=1))
    prices = close_full[mask].copy()          # used for signals, valuation, regime, vol
    exec_prices = open_full[mask].copy()      # used for trade fills (next-open execution)
    exec_prices = exec_prices.reindex(columns=prices.columns)
    exec_prices = exec_prices.fillna(prices)  # fall back to close if open missing

    if config.use_regime_filter and config.regime_benchmark not in prices.columns:
        logger.warning(
            "Regime benchmark '%s' not in price panel; disabling regime filter.",
            config.regime_benchmark,
        )
        config.use_regime_filter = False

    # Precompute regime signal (benchmark above its long SMA), on close prices
    regime_bullish = None
    regime_high_vol = None
    if config.use_regime_filter:
        bench = prices[config.regime_benchmark]
        sma = bench.rolling(config.regime_sma_window, min_periods=config.regime_sma_window // 2).mean()
        regime_bullish = (bench >= sma).reindex(prices.index).fillna(False)

        # Second regime dimension (opt-in): benchmark's own trailing realized volatility.
        # See BacktestConfig.regime_vol_threshold's docstring for why this is blended into
        # the SAME regime_scalar rather than a separate gate.
        if config.regime_vol_threshold is not None:
            bench_returns = bench.pct_change()
            realized_bench_vol = (
                bench_returns.rolling(config.regime_vol_lookback_days,
                                       min_periods=config.regime_vol_lookback_days).std()
                * np.sqrt(252)
            )
            regime_high_vol = (realized_bench_vol > config.regime_vol_threshold).reindex(prices.index).fillna(False)

    trading_days = _trading_days(prices, config.exchange)
    rebalance_dates = _rebalance_dates(monthly_picks, trading_days)

    logger.info(
        "Backtest period: %s to %s | %d rebalance dates.",
        prices.index.min().strftime("%Y-%m-%d"),
        prices.index.max().strftime("%Y-%m-%d"),
        len(rebalance_dates),
    )

    cash = config.initial_capital
    holdings: dict[str, float] = {}
    entry_prices: dict[str, float] = {}
    entry_dates: dict[str, pd.Timestamp] = {}  # time-based stops
    portfolio_history = [(prices.index[0], config.initial_capital)]
    months_held = 0
    total_commission_paid = 0.0
    total_slippage_cost = 0.0
    turnover_log = []
    peak_equity = config.initial_capital
    circuit_breaker_halted = False  # once tripped, only allow risk-reducing trades

    with open(config.log_file_path, "w") as log_file:
        log_file.write(
            f"Backtest Start: {sim_start_date.strftime('%Y-%m-%d')}, "
            f"Initial Capital: ${config.initial_capital:,.2f}\n"
            f"Config: vol_target={config.target_portfolio_vol}, "
            f"max_position={config.max_position_weight}, "
            f"stop_loss={config.stop_loss_pct}, regime_filter={config.use_regime_filter}\n\n"
        )

        for today in prices.index[1:]:
            today_prices = prices.loc[today]        # close, for valuation/signals
            today_exec = exec_prices.loc[today]      # open, for fills

            # ---------------- STOP-LOSS CHECK (every day, not just rebalance) -------
            # GAP-RISK LIMITATION: this checks the drawdown from
            # entry using DAILY close prices, and fills at that day's open/close
            # (via today_exec) with the standard slippage model. On a genuine
            # overnight gap-down (common in real crashes, e.g. several March 2020
            # sessions opened well below the prior close), this will UNDERSTATE the
            # actual loss and OVERSTATE the achievable exit price: the stop "sees"
            # the drop only after it has already happened, and fills near that
            # already-dropped price, not at the pre-gap stop level. This is a
            # structural limitation of daily-bar backtesting and cannot be fully
            # solved without intraday data. Do not assume stop_loss_pct is a hard
            # ceiling on realized loss per position during fast, gapping markets.
            for ticker in list(holdings.keys()):
                if ticker not in prices.columns or pd.isna(today_prices.get(ticker)):
                    continue
                entry = entry_prices.get(ticker)
                if entry is None or entry <= 0:
                    continue
                dd = (today_prices[ticker] - entry) / entry
                if dd <= -config.stop_loss_pct:
                    shares = holdings[ticker]
                    fill_ref = today_exec.get(ticker, today_prices[ticker])
                    exec_price = fill_ref * (1 - config.base_slippage_bps / 10_000)
                    proceeds = shares * exec_price - config.commission
                    cash += proceeds
                    total_commission_paid += config.commission
                    del holdings[ticker]
                    del entry_prices[ticker]
                    log_file.write(
                        f"{today.strftime('%Y-%m-%d')} STOP-LOSS: sold {shares:,.0f} {ticker} "
                        f"@ ${exec_price:,.2f} (drawdown {dd:.1%})\n"
                    )
                    entry_dates.pop(ticker, None)

            # ---------------- TIME-BASED STOP -----------------
            # Force-exits a position after max_holding_days regardless of price,
            # independent of and in addition to the price-based stop-loss above.
            # Exists to bound exposure duration even when a position is neither
            # winning nor losing enough to trigger the price stop, but has simply
            # been held longer than intended (e.g. the signal moved on but the
            # position never technically breached stop_loss_pct).
            if config.max_holding_days is not None:
                for ticker in list(holdings.keys()):
                    if ticker not in prices.columns or pd.isna(today_prices.get(ticker)):
                        continue
                    entry_date = entry_dates.get(ticker)
                    if entry_date is None:
                        continue
                    days_held = (today - entry_date).days
                    if days_held >= config.max_holding_days:
                        shares = holdings[ticker]
                        fill_ref = today_exec.get(ticker, today_prices[ticker])
                        exec_price = fill_ref * (1 - config.base_slippage_bps / 10_000)
                        proceeds = shares * exec_price - config.commission
                        cash += proceeds
                        total_commission_paid += config.commission
                        del holdings[ticker]
                        entry_prices.pop(ticker, None)
                        entry_dates.pop(ticker, None)
                        log_file.write(
                            f"{today.strftime('%Y-%m-%d')} TIME-STOP: sold {shares:,.0f} {ticker} "
                            f"@ ${exec_price:,.2f} (held {days_held} days >= max_holding_days={config.max_holding_days})\n"
                        )

            # ---------------------------- REBALANCE ----------------------------------
            if today in rebalance_dates and (months_held >= config.holding_period or months_held == 0):
                months_held = 1

                # --- optional periodic cash injection (DCA-style contributions) ---
                if config.monthly_contribution > 0:
                    cash += config.monthly_contribution
                    log_file.write(
                        f"{today.strftime('%Y-%m-%d')} CONTRIBUTION: +${config.monthly_contribution:,.2f} "
                        f"(cash now ${cash:,.2f})\n"
                    )

                market_value = sum(
                    shares * today_prices[t] for t, shares in holdings.items()
                    if t in prices.columns and pd.notna(today_prices.get(t))
                )
                total_value = cash + market_value

                # --- circuit breaker check ---
                peak_equity = max(peak_equity, total_value)
                current_drawdown = (total_value - peak_equity) / peak_equity if peak_equity > 0 else 0.0
                if config.max_portfolio_drawdown_pct > 0:
                    was_halted = circuit_breaker_halted
                    circuit_breaker_halted = current_drawdown <= -config.max_portfolio_drawdown_pct
                    if circuit_breaker_halted and not was_halted:
                        log_file.write(
                            f"{today.strftime('%Y-%m-%d')} CIRCUIT BREAKER TRIPPED: drawdown "
                            f"{current_drawdown:.1%} <= -{config.max_portfolio_drawdown_pct:.1%}. "
                            f"Halting new entries; existing positions still subject to stop-loss.\n\n"
                        )
                    elif was_halted and not circuit_breaker_halted:
                        log_file.write(
                            f"{today.strftime('%Y-%m-%d')} CIRCUIT BREAKER CLEARED: drawdown "
                            f"recovered to {current_drawdown:.1%}. Resuming normal rebalancing.\n\n"
                        )

                signal_lookup = today - pd.Timedelta(days=1)
                eligible_signals = monthly_picks.index[monthly_picks.index <= signal_lookup]
                if len(eligible_signals) == 0:
                    portfolio_history.append((today, cash + market_value))
                    continue
                latest_signal_date = eligible_signals[-1]
                target_tickers = monthly_picks.get(latest_signal_date, [])

                if target_tickers and not circuit_breaker_halted:
                    # --- regime filter: scale down gross exposure in a downtrend ---
                    if config.use_regime_filter and regime_bullish is not None:
                        bullish = bool(regime_bullish.loc[today]) if today in regime_bullish.index else True
                        high_vol = bool(regime_high_vol.loc[today]) if (
                            regime_high_vol is not None and today in regime_high_vol.index
                        ) else False
                        regime_scalar = config.min_gross_exposure if (not bullish or high_vol) else 1.0
                        if high_vol and bullish:
                            log_file.write(
                                f"{today.strftime('%Y-%m-%d')} MARKET VOLATILITY REGIME DEFENSIVE: "
                                f"{config.regime_benchmark} realized vol exceeds "
                                f"{config.regime_vol_threshold:.0%} threshold, reducing exposure to "
                                f"{config.min_gross_exposure:.0%} despite bullish trend\n"
                            )
                    else:
                        regime_scalar = 1.0

                    # --- correlation spike: additional fast-reacting risk-off signal ---
                    if config.use_correlation_spike_regime:
                        spike = detect_correlation_spike(
                            prices, today,
                            config.correlation_spike_short_window,
                            config.correlation_spike_baseline_window,
                            config.correlation_spike_threshold,
                        )
                        if spike:
                            regime_scalar = min(regime_scalar, config.min_gross_exposure)
                            log_file.write(
                                f"{today.strftime('%Y-%m-%d')} CORRELATION SPIKE DETECTED: "
                                f"reducing exposure to {config.min_gross_exposure:.0%}\n"
                            )

                    # --- volatility targeting: scale gross exposure to hit target vol ---
                    realized_vol = _realized_portfolio_vol(portfolio_history, config.portfolio_vol_lookback)
                    vol_scalar = compute_vol_scalar(
                        realized_vol, config.target_portfolio_vol,
                        config.min_gross_exposure, config.max_gross_exposure,
                    )

                    gross_exposure = min(config.max_gross_exposure, regime_scalar * vol_scalar)

                    # --- position sizing: custom weights (if provided for this date) or
                    #     inverse-vol + optional correlation penalty, via the SAME resolver
                    #     live_signal.py uses, single source of truth for sizing logic ---
                    custom_w = None
                    if custom_weights_by_date is not None:
                        custom_w = custom_weights_by_date.get(latest_signal_date) or custom_weights_by_date.get(today)
                    weights = resolve_target_weights(target_tickers, prices, today, config, custom_weights=custom_w)

                    log_file.write(
                        f"--- Rebalance {today.strftime('%Y-%m-%d')} | Total Value ${total_value:,.2f} "
                        f"| Gross Exposure {gross_exposure:.1%} (regime={regime_scalar:.2f}, "
                        f"vol_scalar={vol_scalar:.2f}) ---\n"
                    )

                    target_dollar = {
                        t: total_value * gross_exposure * w for t, w in weights.items()
                    }

                    current_value = {
                        t: shares * today_prices[t]
                        for t, shares in holdings.items()
                        if t in prices.columns and pd.notna(today_prices.get(t))
                    }
                    all_tickers = set(holdings.keys()) | set(target_dollar.keys())
                    raw_trades = {
                        t: target_dollar.get(t, 0.0) - current_value.get(t, 0.0) for t in all_tickers
                    }

                    # --- aggregate-drift skip: bypass the ENTIRE rebalance if total portfolio
                    #     drift is trivial, even if some individual tickers exceed drift_threshold.
                    #     0 (default) disables this and preserves prior behavior exactly. ---
                    if config.aggregate_drift_threshold > 0 and total_value > 0:
                        aggregate_drift = sum(abs(v) for v in raw_trades.values()) / total_value
                        if aggregate_drift < config.aggregate_drift_threshold:
                            log_file.write(
                                f"{today.strftime('%Y-%m-%d')} SKIP REBALANCE: aggregate drift "
                                f"{aggregate_drift:.2%} < aggregate_drift_threshold "
                                f"{config.aggregate_drift_threshold:.2%}\n\n"
                            )
                            daily_mv = sum(
                                shares * today_prices[t] for t, shares in holdings.items()
                                if pd.notna(today_prices.get(t))
                            )
                            portfolio_history.append((today, cash + daily_mv))
                            continue

                    # --- drift threshold + min trade size filtering (turnover/cost control) ---
                    # A "rebalance-only" trade on an existing position is skipped if the
                    # drift from target is too small to be worth the cost. New entries and
                    # full exits still go through (only gated by min_trade_size), since
                    # those aren't just noise, they're genuine allocation changes.
                    trades = {}
                    for t, trade_value in raw_trades.items():
                        is_continuing_position = (t in current_value) and (t in target_dollar)
                        if abs(trade_value) < config.min_trade_size:
                            continue
                        if is_continuing_position and total_value > 0:
                            drift = abs(trade_value) / total_value
                            if drift < config.drift_threshold:
                                continue
                        trades[t] = trade_value

                    period_turnover = sum(abs(v) for v in trades.values())
                    turnover_log.append((today, period_turnover))

                    # --- sells first ---
                    for ticker, trade_value in trades.items():
                        if trade_value < 0 and ticker in prices.columns and pd.notna(today_prices.get(ticker)):
                            price = today_exec.get(ticker, today_prices[ticker])
                            window = prices[ticker].loc[:today].pct_change().tail(config.vol_lookback_days)
                            slip_bps = _slippage_bps(window, config)
                            jitter = rng.uniform(-0.25, 0.25) * slip_bps  # +/-25% noise on the cost estimate
                            exec_price = price * (1 - (slip_bps + jitter) / 10_000)
                            shares_to_sell = min(
                                _round_shares(abs(trade_value) / exec_price, config.allow_fractional_shares),
                                holdings.get(ticker, 0)
                            )
                            if shares_to_sell > 0:
                                holdings[ticker] -= shares_to_sell
                                if holdings[ticker] < 1e-6:
                                    del holdings[ticker]
                                    entry_prices.pop(ticker, None)
                                    entry_dates.pop(ticker, None)
                                proceeds = shares_to_sell * exec_price - config.commission
                                cash += proceeds
                                total_commission_paid += config.commission
                                total_slippage_cost += shares_to_sell * price * (slip_bps / 10_000)
                                log_file.write(
                                    f"SELL: {shares_to_sell:,.4f} {ticker} @ ${exec_price:,.2f} "
                                    f"(slip {slip_bps:.1f}bps)\n"
                                )

                    # --- buys second, capped by available cash ---
                    for ticker, trade_value in sorted(trades.items()):
                        if trade_value > 0 and ticker in prices.columns and pd.notna(today_prices.get(ticker)):
                            price = today_exec.get(ticker, today_prices[ticker])
                            window = prices[ticker].loc[:today].pct_change().tail(config.vol_lookback_days)

                            # --- reduce-only: block new BUYs during detected per-ticker liquidity stress ---
                            if config.liquidity_stress_reduce_only and len(window) >= config.liquidity_stress_recent_days:
                                recent_vol = window.tail(config.liquidity_stress_recent_days).std() * np.sqrt(252)
                                baseline_vol = window.std() * np.sqrt(252)
                                if pd.notna(recent_vol) and baseline_vol > 0 and recent_vol / baseline_vol > config.liquidity_stress_vol_ratio:
                                    log_file.write(
                                        f"{today.strftime('%Y-%m-%d')} REDUCE-ONLY: skipping BUY of {ticker} "
                                        f"(recent/baseline vol ratio {recent_vol/baseline_vol:.2f} > "
                                        f"{config.liquidity_stress_vol_ratio:.2f})\n"
                                    )
                                    continue

                            slip_bps = _slippage_bps(window, config)
                            jitter = rng.uniform(-0.25, 0.25) * slip_bps
                            exec_price = price * (1 + (slip_bps + jitter) / 10_000)

                            affordable = _round_shares((cash - config.commission) / exec_price, config.allow_fractional_shares) if cash > config.commission else 0
                            target_shares = _round_shares(trade_value / exec_price, config.allow_fractional_shares)
                            shares_to_buy = min(target_shares, affordable)

                            if shares_to_buy > 0:
                                prev_shares = holdings.get(ticker, 0)
                                new_shares = prev_shares + shares_to_buy
                                # volume-weighted entry price for stop-loss reference
                                entry_prices[ticker] = (
                                    (entry_prices.get(ticker, price) * prev_shares + exec_price * shares_to_buy)
                                    / new_shares
                                )
                                if ticker not in entry_dates:
                                    # only set on a genuinely NEW position, a top-up to an
                                    # existing holding shouldn't reset the time-based stop clock
                                    entry_dates[ticker] = today
                                holdings[ticker] = new_shares
                                cash -= (shares_to_buy * exec_price) + config.commission
                                total_commission_paid += config.commission
                                total_slippage_cost += shares_to_buy * price * (slip_bps / 10_000)
                                log_file.write(
                                    f"BUY:  {shares_to_buy:,.4f} {ticker} @ ${exec_price:,.2f} "
                                    f"(slip {slip_bps:.1f}bps)\n"
                                )
                    log_file.write("\n")
            elif today in rebalance_dates:
                months_held += 1

            # ---------------------------- DAILY VALUATION -----------------------------
            daily_mv = sum(
                shares * today_prices[t] for t, shares in holdings.items()
                if pd.notna(today_prices.get(t))
            )
            portfolio_history.append((today, cash + daily_mv))

    logger.info(
        "Backtest complete. Total commission: $%.2f | Total slippage cost (est.): $%.2f",
        total_commission_paid, total_slippage_cost,
    )

    return _build_report(portfolio_history, prices, config, total_commission_paid, total_slippage_cost, turnover_log)


# --------------------------------------------------------------------------- #
# REPORTING / TEARSHEET
# --------------------------------------------------------------------------- #
def _build_report(
    portfolio_history: list,
    prices: pd.DataFrame,
    config: BacktestConfig,
    total_commission: float,
    total_slippage: float,
    turnover_log: list,
) -> pd.DataFrame:
    daily = pd.DataFrame(portfolio_history, columns=["Date", "Portfolio_Value"]).set_index("Date")
    daily = daily[~daily.index.duplicated(keep="last")]
    monthly = daily.resample("ME").last()

    report = pd.DataFrame(index=monthly.index)
    report["Month End Portfolio Value"] = monthly["Portfolio_Value"]
    report["Month Beginning Portfolio Value"] = report["Month End Portfolio Value"].shift(1).fillna(
        config.initial_capital
    )
    report["Portfolio Monthly Return"] = report["Month End Portfolio Value"].pct_change()

    bench = config.regime_benchmark if config.regime_benchmark in prices.columns else None
    if bench:
        bench_monthly = prices[bench].resample("ME").last()
        report[f"{bench} Monthly Return"] = bench_monthly.pct_change()

    report["Portfolio Cumulative Return"] = (1 + report["Portfolio Monthly Return"]).cumprod()
    if bench:
        report[f"{bench} Cumulative Return"] = (1 + report[f"{bench} Monthly Return"]).cumprod()

    report = report.dropna(subset=["Portfolio Monthly Return"])

    # --- risk/return diagnostics (printed, not just returned) ---
    monthly_ret = report["Portfolio Monthly Return"].dropna()
    if not monthly_ret.empty:
        n_months = len(monthly_ret)
        cagr = (1 + monthly_ret).prod() ** (12 / n_months) - 1
        ann_vol = monthly_ret.std() * np.sqrt(12)
        sharpe = (monthly_ret.mean() * 12) / ann_vol if ann_vol > 0 else np.nan
        downside = monthly_ret[monthly_ret < 0]
        sortino = (monthly_ret.mean() * 12) / (downside.std() * np.sqrt(12)) if len(downside) > 1 else np.nan
        cum = (1 + monthly_ret).cumprod()
        running_max = cum.cummax()
        max_dd = (cum / running_max - 1).min()
        calmar = cagr / abs(max_dd) if max_dd != 0 else np.nan
        win_rate = (monthly_ret > 0).mean()

        beta = alpha = np.nan
        if bench and f"{bench} Monthly Return" in report.columns:
            bret = report[f"{bench} Monthly Return"].reindex(monthly_ret.index).dropna()
            aligned = monthly_ret.reindex(bret.index)
            if len(bret) > 2 and bret.var() > 0:
                cov = np.cov(aligned, bret)[0, 1]
                beta = cov / bret.var()
                alpha = (aligned.mean() - beta * bret.mean()) * 12

        total_turnover = sum(v for _, v in turnover_log)

        logger.info("=" * 60)
        logger.info("TEARSHEET")
        logger.info("CAGR:                 %.2f%%", cagr * 100)
        logger.info("Annualized Vol:       %.2f%%", ann_vol * 100)
        logger.info("Sharpe Ratio:         %.2f", sharpe)
        logger.info("Sortino Ratio:        %.2f", sortino)
        logger.info("Max Drawdown:         %.2f%%", max_dd * 100)
        logger.info("Calmar Ratio:         %.2f", calmar)
        logger.info("Monthly Win Rate:     %.1f%%", win_rate * 100)
        if bench:
            logger.info("Beta vs %s:          %.2f", bench, beta)
            logger.info("Annualized Alpha:     %.2f%%", alpha * 100)
        logger.info("Total Commission:     $%.2f", total_commission)
        logger.info("Total Est. Slippage:  $%.2f", total_slippage)
        logger.info("Total Turnover ($):   $%.2f", total_turnover)
        logger.info("=" * 60)

        report.attrs["tearsheet"] = {
            "CAGR": cagr, "AnnVol": ann_vol, "Sharpe": sharpe, "Sortino": sortino,
            "MaxDrawdown": max_dd, "Calmar": calmar, "WinRate": win_rate,
            "Beta": beta, "Alpha": alpha, "TotalCommission": total_commission,
            "TotalSlippage": total_slippage, "TotalTurnover": total_turnover,
        }

    return report


# --------------------------------------------------------------------------- #
# DROP-IN COMPATIBILITY WRAPPER
# --------------------------------------------------------------------------- #
def run_custom_backtest(
    monthly_picks: pd.Series,
    daily_prices: pd.DataFrame,
    holding_period: int = 1,
    initial_capital: float = 100_000.0,
    commission: float = 0.0,
    exchange: str = "NYSE",
    **risk_overrides,
) -> pd.DataFrame:
    """
    Drop-in replacement for the original run_custom_backtest(...) signature, so
    existing notebook cells keep working unchanged:

        backtest_df = run_custom_backtest(top_etfs_monthly, daily_prices,
                                           holding_period=1, commission=0,
                                           initial_capital=1000.00)
        display(backtest_df.head())

    `daily_prices` may be either:
      - a plain DataFrame of close prices (ticker columns), or
      - a MultiIndex-column panel with (ticker, field) columns, e.g. the object
        you'd call daily_prices.xs('open', level=1, axis=1) on. In that case this
        function automatically uses 'close' for signals/valuation and 'open' for
        trade execution (fills happen the trading day after the signal, at that
        day's open, avoids same-bar look-ahead on execution price).

    Any BacktestConfig field (e.g. target_portfolio_vol=0.20, stop_loss_pct=0.10,
    use_regime_filter=False) can be passed as an extra keyword to override the
    default risk-management settings without touching the config class directly.
    """
    custom_weights_by_date = risk_overrides.pop("custom_weights_by_date", None)

    cfg_kwargs = dict(
        holding_period=holding_period,
        initial_capital=initial_capital,
        commission=commission,
        exchange=exchange,
    )
    for key, val in risk_overrides.items():
        if key in BacktestConfig.__dataclass_fields__:
            cfg_kwargs[key] = val
        else:
            logger.warning("Ignoring unknown BacktestConfig override: %s=%r", key, val)

    # Construct once (not via post-hoc setattr) so __post_init__ validation actually runs
    # against the final, complete set of values, setattr after construction would bypass it.
    cfg = BacktestConfig(**cfg_kwargs)

    return run_risk_managed_backtest(monthly_picks, daily_prices, cfg, custom_weights_by_date=custom_weights_by_date)


if __name__ == "__main__":
    logger.info(
        "This module exposes run_risk_managed_backtest(monthly_picks, daily_prices, config). "
        "Import it and call it with your ETF momentum signal series and daily price panel."
    )