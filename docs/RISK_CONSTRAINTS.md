# Risk Constraints: Long-Term vs. Short-Term Momentum

> **New to this project?** Start with `../README.md`. This file covers the momentum-strategy
> risk constraints specifically: what each one checks, why, whether it's a non-blocking WARNING
> or an opt-in config toggle, and its exact default.

## The full risk-strategy tier map

The table below is the complete picture: the seven risk-strategy tiers institutions/hedge funds
commonly layer on a momentum book, cross-referenced against what this project actually
implements. Everything marked Implemented composes as a SEQUENTIAL PIPELINE (signal generation
-> Absolute Momentum pick-list filter -> position sizing -> Position Size Hard-Cap -> Volatility
Scaling exposure throttle -> regime filter/Correlation Monitor further de-risking -> order
generation -> Liquidity/Slippage pre-trade gate -> order submission), plus the circuit breakers
sitting outside that pipeline entirely as an independent backstop, NOT a menu of mutually
exclusive choices, matching both real systematic-fund practice and this codebase's own
pre-existing shared-sizing-pipeline architecture (`resolve_target_weights()`).

| Tier | Strategy | Status |
|---|---|---|
| Mandatory | Volatility Scaling | **Implemented**, position-level (pre-existing) + portfolio-level (live-wired here for the first time), see "Volatility Scaling (Portfolio-Level)" below |
| Mandatory | Absolute Momentum (Macro) | **Implemented**, a benchmark trend filter (pre-existing) + a per-ticker dual-momentum overlay (wired in here for the first time), see "Absolute Momentum (Macro)" below |
| Mandatory | Position Size Hard-Cap | **Implemented** (pre-existing, shared live+backtest), see "Position Size Hard-Cap" below |
| Recommended | Drawdown Circuit Breaker | **Implemented**, per-portfolio (pre-existing) + account-wide (new here), see "Drawdown Circuit Breaker" below |
| Recommended | Correlation Monitor | **Implemented** (pre-existing, live-wired), see "Correlation Monitor" below |
| Nice to Have | Liquidity/Slippage Monitor | **Implemented**, a pre-trade real-time bid-ask spread gate (new here), see "Liquidity/Slippage Monitor" below |
| Nice to Have | Hard-to-Borrow (HTB) Sentinel | **Not Applicable**, this system is strictly long-only, no short legs exist to protect, see "Hard-to-Borrow (HTB) Sentinel" below |

One mechanism below sits OUTSIDE this 7-tier institutional map, not because it's less
important, but because it's not a signal/sizing/exposure constraint at all: **Broker-Side
Protective Stop** (`attach_broker_stop_loss`, LIVE-ONLY, opt-in) attaches a real IBKR bracket
order at BUY time, so a position is protected by the BROKER ITSELF even when this app isn't
running, closing a gap every constraint above shares (all of them, like this whole pipeline,
only ever act while `daily-runner` is actually invoked). See its own section below.

Everything in this table lives entirely in the `daily_runner.py`/`execution/live_signal.py`/
`backtest/momentum_backtest.py` live+backtest path. None of it is visible to
`risk/risk_monitor.py`, that's deliberate, not an oversight, see "Independence from
`risk_monitor.py`" at the bottom of this file.

## Institutional risk-practice audit (24-item review, Epic 1)

A broader, one-time audit against 24 institutional/hedge-fund risk-management practices
(requested directly, not derived from the 7-tier table above, which predates this audit and is
its own narrower, already-implemented curated subset). Confirmed by reading the actual code, not
guessed. Status legend: **Implemented (pre-existing)** = already shipped before this audit, no
work done here; **Not Applicable** = conflicts with a fundamental architecture invariant of this
system (strictly long-only, no shorting, no derivatives, cash-only/unlevered, single broker),
same treatment as the pre-existing HTB Sentinel row above; **Planned** = real gap, scheduled into
one of Epics 1-4 below, each row updated to **Implemented** with real details as its own Epic
lands (not written speculatively in advance).

| # | Practice | Status |
|---|---|---|
| 1 | Volatility Targeting / Scaling | **Implemented (pre-existing)**, see "Volatility Scaling (Portfolio-Level)" above |
| 2 | ATR-Based Trailing Stops | **Planned, Epic 2** — the existing `use_trailing_stop`/`trailing_stop_pct` trail is percentage-based; `core/technical_indicators.py`'s `atr()` exists but was, before this audit, wired into nothing but the email report |
| 3 | Cross-Sectional Beta Neutralization | **Not Applicable** — requires a short leg to neutralize net beta against; this system is strictly long-only |
| 4 | Sector and Industry Neutralization | **Not Applicable (today)** — true benchmark-relative neutralization needs a real constituent-weights feed (e.g. actual S&P 500 sector weights) this project doesn't integrate; the existing `max_sector_weight`/`ticker_sectors` hard cap (row 20 below) is the practical substitute this system ships |
| 5 | Equal Risk Contribution (Risk Parity) | **Planned, Epic 1** — a new `sizing_method: equal_risk_contribution` value, see "Equal Risk Contribution (ERC) Sizing" below |
| 6 | Value-at-Risk (VaR) Budgeting | **Planned, Epic 1** — `core/functions_quant_extensions.py`'s `historical_var_cvar()` existed, fully coded, but before this audit was wired into nothing (its only 2 callers in the whole repo were `tests/test_governance.py`), see "VaR/CVaR Budget (Active Pre-Trade Constraint)" below |
| 7 | Expected Shortfall (Conditional VaR) Optimization | **Planned, Epic 1** — same underlying function/gap as row 6, one combined feature (VaR and CVaR are reported and budgeted together, not two separate mechanisms) |
| 8 | Maximum Drawdown (MDD) Control | **Implemented (pre-existing)**, per-portfolio + account-wide circuit breakers, see "Drawdown Circuit Breaker" above |
| 9 | Dynamic Correlation Risk Overlay | **Implemented (pre-existing)**, `use_correlation_penalty`/`use_correlation_spike_regime`, see "Correlation Monitor" above |
| 10 | Liquidity-Adjusted Position Sizing | **Planned, Epic 1** — the existing `use_liquidity_filter` is a binary in/out rank filter and `max_pct_of_adv`/`check_capacity()` is advisory-only (runs after order sizing, never mutates a size); this adds a genuinely continuous, ADV-scaled, active sizing path, see "Liquidity-Adjusted Position Sizing (Active)" below |
| 11 | Factor Risk Exposure Caps | **Implemented (Epic 3)** — a manually-declared factor-loadings cap (no vendor factor-return data source exists in this project, same honest caveat `ticker_sectors` already carries), see "Factor Risk Exposure Caps" below |
| 12 | Trailing Drawdown High-Water-Mark Limits | **Implemented (pre-existing)** — the circuit breaker's peak-equity tracking (`risk/circuit_breaker.py`'s `_peak_equity_path()`) already measures drawdown from a running high-water mark, portfolio-level and account-wide |
| 13 | Bid-Ask Spread and Transaction Cost Hurdle Filters | **Implemented (Epic 4)** — `max_bid_ask_spread_pct` (row in "Liquidity/Slippage Monitor" above) remains the shipped absolute spread ceiling; `cost_edge_hurdle_multiplier` adds the relative cost-vs-expected-edge hurdle, see "Cost-vs-Edge Hurdle Filter" below |
| 14 | Cross-Asset Hedging with Liquid Proxies | **Not Applicable** — no derivatives/short capability exists to hedge with; this system is long-only |
| 15 | Margin Utilization and Leverage Caps | **Not Applicable** — this system is unlevered and cash-only by design, no margin usage exists to monitor |
| 16 | Shrinkage Estimator Covariance Matrix Regularization | **Planned, Epic 1** — the existing correlation-penalty/spike-detection math uses raw `pandas.DataFrame.corr()`, no regularization; a Ledoit-Wolf-style shrinkage estimator is added as a shared utility, see "Shrinkage Covariance Estimator" below |
| 17 | Turnover and Rebalancing Frequency Constraints | **Implemented (pre-existing)**, `max_turnover_pct`, see "Turnover Limit" above |
| 18 | Stress Testing and Historical Scenario Analysis | **Implemented (pre-existing)**, `core/functions_quant_extensions.py`'s `scenario_shock()` plus the real 2008/2020/2022 crash-replay notebook (Epic 13, see `README.md`'s Known Gaps) |
| 19 | Counterparty Credit Risk Limits | **Not Applicable** — single broker (IBKR), no OTC/multi-counterparty exposure exists to limit |
| 20 | Sector Concentration Hard Caps | **Implemented (pre-existing)**, same mechanism as row 4's practical substitute, `max_sector_weight`/`ticker_sectors`, see "Sector / Asset-Class Concentration Cap" (this file, referenced from `CLAUDE.md`) |
| 21 | Maximum Asset-Level Concentration Limits | **Implemented (pre-existing)**, `max_position_weight`, see "Position Size Hard-Cap" above |
| 22 | Algorithmic Execution Participation Rate Limits | **Implemented (Epic 4), verification partial** — `use_participation_rate_limit`/`max_participation_pct` wire IBKR's native `PctVol` algo order type onto the parent order (`place_orders_ibkr()`), not local TWAP scheduling (this app is a stateless once-daily cron job, no intraday scheduling loop exists to build real slicing against); a real paper-account submission is confirmed ACCEPTED by IBKR (two real bugs found and fixed to get there, see "Execution Participation-Rate Limits" below), but the actual intraday percentage-of-volume fill behavior and composition with broker-side stop/trail brackets were both untested (outside RTH at test time), disclosed as open gaps, not assumed to work |

(This audit lists 22 distinct practices; two pairs from the original 24-item request collapsed
into one row each: rows 6/7 share one mechanism, VaR and CVaR are computed and budgeted
together, and rows 4/20 both point to the same `max_sector_weight`/`ticker_sectors` cap, since
Sector Concentration Hard Caps and the practical substitute for Sector/Industry Neutralization
are, honestly, the same shipped feature under two different names.)

**Epic 4 (2026-08-05) closes this audit**: every row above is now either Implemented,
Implemented with an honestly disclosed partial-verification caveat (row 22), or Not Applicable
by design. No further epics are planned in this program.

## Opt-in overlay strategies (`enabled_risk_strategies`)

Epics 1-4 introduce a second, deliberate config-enabling mechanism alongside this file's many
existing `use_X: true/false` toggles (`use_liquidity_filter`, `use_trailing_stop`, etc., each
still completely untouched). `BacktestConfig.enabled_risk_strategies: list[str]` (default `[]`,
byte-identical to before this field existed) is reserved specifically for features that are
genuinely independent, stackable overlays, not a mutually-exclusive dispatch choice.
Equal Risk Contribution (row 5 above) is deliberately **not** here: `sizing_method` already IS
this codebase's own "pick by name" mechanism for a mutually-exclusive sizing choice (the same way
`"risk_based"` is selected today, with no separate `use_risk_based_sizing: bool`), so ERC becomes
`sizing_method`'s 5th legal value instead of a second, potentially-disagreeing toggle.

`RISK_STRATEGY_ALIASES` (`backtest/momentum_backtest.py`) is the single source of truth mapping
each canonical overlay name to every accepted alias string; `risk_strategy_enabled(cfg,
canonical)` is the one helper every gated call site uses, never an ad-hoc `in` check.
`__post_init__` validates every string in `enabled_risk_strategies` resolves to a known
canonical/alias, failing loud on an unrecognized name (a typo'd alias silently doing nothing
would be a worse failure mode than a config error, matching `sizing_method`'s own unknown-value
handling).

| Canonical name | Aliases | What it does | Config fields |
|---|---|---|---|
| `var_cvar_budget` | `var_budget`, `value_at_risk_budget` | Active VaR/CVaR pre-trade gross-exposure throttle, see "VaR/CVaR Budget (Active Pre-Trade Constraint)" below | `var_budget_pct`, `var_cvar_confidence`, `var_cvar_lookback_days` |
| `liquidity_adjusted_sizing` | `adv_scaled_sizing` | Active, continuous ADV-based position-size scaling, see "Liquidity-Adjusted Position Sizing (Active)" below | `max_pct_of_adv` (reused), `liquidity_lookback_days` (reused) |

```yaml
risk_overrides:
  enabled_risk_strategies:
    - var_cvar_budget
    - liquidity_adjusted_sizing
```

**Epic 1 real verification (run 2026-08-05), honestly reported, not just "implemented"**: all
three Epic 1 features (`sizing_method: equal_risk_contribution`, `var_cvar_budget`,
`liquidity_adjusted_sizing`), individually and combined, were verified against a real IBKR paper
account (port 7497), both natively and inside the rebuilt Docker container. Real BUY orders
placed and filled (confirmed via `execDetails`/`commissionReport`, real prices/commissions, not
simulated) for a throwaway 10-mega-cap-ticker test portfolio in each of four configurations. The
VaR/CVaR scalar computed a real CVaR (2.83%-3.07% across runs) against the configured 5% budget
and correctly stayed at `scalar=1.00` (not binding, budget not exceeded at this position size);
ERC sizing produced genuinely non-uniform, risk-balanced weights (e.g. AAPL ~35%, AMD ~8%,
reflecting their real relative volatility) distinct from both equal-weight and inverse-vol
shapes. The pre-existing `OVERLAPPING_TICKER_SCOPED`/ticker-overlap safety mechanisms fired
correctly against the new features with zero interaction bugs. Full pytest suite: 994 passed
(up from 939 before Epic 1, +55 new tests), zero regressions, both natively and confirmed via
the same source tree rebuilt into the Docker image. See `CLAUDE.md`'s `backtest/
momentum_backtest.py` bullet for the full per-story implementation writeup.

## Advisory constraints (non-blocking WARNING, logged and emailed)

These three compare `lookback_period` and `holding_period` directly. Both are normalized to the
SAME unit before comparing, weeks (via `round(x * 4)`) when `holding_period < 1`, months
directly otherwise, exactly matching `execution/live_signal.py`'s `resolve_momentum_scores()`'s
own regime-based interpretation of `lookback_period`. All three fire every run (not just
rebalance days), same as the pre-existing `HOLDING_PERIOD_TOO_FREQUENT`/
`LOOKBACK_PERIOD_TOO_SHORT` checks, so a persistent misconfiguration keeps surfacing until fixed.
None of them block a run, they're advisory, "review this," not "this failed."

| Constraint | Rule | Why | Implemented as |
|---|---|---|---|
| Momentum Persistence | `lookback_period > holding_period` (same unit) | A signal must be "older" than the period you intend to hold the asset. If the holding period is longer than the lookback, you're holding assets based on "stale" signal dynamics. | `is_lookback_shorter_than_holding()`, wired as the `MOMENTUM_PERSISTENCE_VIOLATION` alert |
| Friction | `rebalance_frequency >= holding_period` | Trading more frequently than your holding period is effectively day-trading a strategy with no short-term alpha. | **Not implemented as a runtime check**, see below |
| Lookback-to-Hold Ratio | `lookback_period / holding_period` roughly `3` to `12` | For stable momentum, the signal should have a longer history than the trade duration. A ratio lower than 3 leads to "whipsawing." | `is_lookback_to_holding_ratio_too_low()` (low end only, `< 3`), wired as the `LOOKBACK_TO_HOLD_RATIO_TOO_LOW` alert |

### Why "Friction" has no runtime check

Confirmed by reading `execution/live_signal.py`'s `is_rebalance_day()` in full: it derives its
firing schedule from `holding_period` alone (`weeks_interval`/`months_since_epoch % holding_period`
math), there is no second, independent "rebalance frequency" value anywhere in this codebase that
could ever diverge from `holding_period` itself. `docs/DEPLOYMENT.md`'s "Choosing a rebalance
cadence" section documents this explicitly: `DAILY_RUNNER_CRON` is deliberately decoupled from
cadence (always daily-weekday, regardless of `holding_period`), `is_rebalance_day()`'s own
self-gating is the ONLY thing that determines actual rebalance frequency. Given that, "rebalance
frequency >= holding_period" reduces to "holding_period >= holding_period", tautologically true
by construction. A runtime check that always evaluates `True` isn't a safeguard, it's dead code,
so none was added. If a future change ever introduces a genuinely independent rebalance-frequency
concept, this constraint should be revisited.

### Only the low end of the Ratio constraint is checked

The stated rationale ("a ratio lower than 3 leads to whipsawing") only justifies a lower bound.
No rationale is given here for why a ratio above 12 would itself be a problem, so no upper-bound
warning was added, only `< 3` fires.

## Allow/disallow constraints (config fields in `config.example.yaml`)

| Constraint | Rule | Why | Config field | Default |
|---|---|---|---|---|
| Turnover Limit | `Total_Positions_Changed / Total_Positions` per rebalance, flagged if it exceeds a threshold | High turnover is almost always a sign of an over-sensitive signal. | `max_turnover_pct` | `0.20` |
| Low-Capital Fractional Drop | Fraction of intended BUYs whose computed shares would floor to 0, flagged if it exceeds a threshold | IBKR has no fractional-equity order support, a dropped BUY is capital that silently never got deployed. | `low_capital_drop_warning_pct` | `0.30` |
| Skip-Month Guardrail | For `lookback_period > 3` months, exclude the most recent ~21 trading days from the signal | The classic academic "12-1 momentum" construction, avoids short-term reversal decay. | `skip_month_guardrail` | `false` (opt-in) |
| Volatility-Adjustment (Scaling) | `Pos_Size = Strategy_Weight * (Target_Vol / Asset_Vol)`, never exceed a per-position vol budget | Caps a single position's risk contribution regardless of how strong the momentum signal is. | `position_vol_budget` | `null` (disabled) |

### Turnover Limit

Non-blocking WARNING (like the advisory constraints above), not a hard block, but only computed
on an actual rebalance (turnover is meaningless without executed orders to measure), unlike the
three advisory checks above which fire every run.

`Total_Positions` is the union of currently-held and newly-targeted tickers, exactly what
`execution/live_signal.py`'s `generate_orders()` already produces one decision (`BUY`/`SELL`/
`HOLD`) for. `Total_Positions_Changed` is the count where that decision is `BUY` or `SELL`, a
`HOLD` (for any reason, including "no live price available") doesn't count as a change. See
`compute_turnover()`/`is_turnover_too_high()`, wired as the `TURNOVER_TOO_HIGH` alert.

This is a position-COUNT ratio, distinct from the pre-existing `drift_threshold`/
`aggregate_drift_threshold` fields, which are dollar-value drift fractions (what fraction of
total portfolio value is being traded), not counts of tickers traded. A rebalance can have low
dollar turnover but high position-count turnover (many small trades) or vice versa (one large
trade), the two metrics answer different questions.

### Low-Capital Fractional Drop

Non-blocking WARNING, fires in BOTH dry-run and `--live` (unlike Turnover Limit above, which is
`--live`-meaningful only insofar as it reads the same `orders` dict either mode produces; this
constraint is specifically designed to catch a too-small capital base during a SAFE dry-run test,
before you ever commit real money to it).

IBKR's API has no fractional-equity order support at all (confirmed elsewhere in this project,
not an `ibapi` version issue): `place_orders_ibkr()` floors every BUY to whole shares at
submission time and drops it entirely (`DROPPED_FRACTIONAL`) if it floors to 0. A portfolio with
too little `total_value` spread across too many (`top_n`), too expensive tickers can end up with
most of its intended BUYs silently dropped this way, real capital that never actually gets
deployed, previously visible only by reading individual dropped-order log lines after the fact.

`compute_low_capital_drop_fraction()`/`is_low_capital_drop_too_high()` (`execution/
live_signal.py`) check `orders[ticker]["shares"] < 1` for every intended BUY directly (the raw
value `generate_orders()` computes, identical in dry-run and `--live`), NOT the live-only
`fill_status` field `place_orders_ibkr()` sets, specifically so this fires during a safe
`--force-rebalance` test too. Wired as the `LOW_CAPITAL_FRACTIONAL_DROP` alert, naming the
dropped tickers and suggesting concrete levers: increase `total_value`, reduce `top_n` (fewer,
larger positions), or prefer lower-priced tickers.

### Skip-Month Guardrail

**Opt-in, default `false`**, deliberately not hardcoded despite the "non-negotiable" framing in
the original request: enabling it changes what the SAME `lookback_period` actually picks each
rebalance, a real signal-construction change, not just a new warning. Enabling it on the shipped
default config (`lookback_period: 12`) would silently change the strategy's actual behavior, this
project is careful not to do that without an explicit ask.

Implemented in `execution/live_signal.py`'s `resolve_momentum_scores()`: when
`skip_month_guardrail: true` AND `lookback_period > 3` AND the strategy is in the monthly regime
(`holding_period >= 1`, this guardrail is inherently a monthly-lookback concept, academic "12-1"
momentum specifically), the monthly-resampled price series is shifted back one bar before
computing the trailing return, excluding the most recent month from the ranking window. This is
an **approximation** of a 21-trading-day lag (one monthly-resampled bar, not a literal
daily-granularity 21-day shift), documented honestly rather than overclaiming precision. A no-op
in the weekly regime (`holding_period < 1`) even if set `true`, and a no-op when
`lookback_period <= 3` even if set `true`.

### Volatility-Adjustment (Scaling Constraint)

`null` (disabled) by default. When set, `backtest/momentum_backtest.py`'s
`_apply_volatility_budget_caps()` caps each position at
`min(max_position_weight, position_vol_budget / asset_vol)`, `asset_vol` being that ticker's own
trailing realized volatility (daily, unannualized, the same `window[valid].pct_change().std()`
convention `_inverse_vol_weights()`'s inverse-vol sizing already uses over `vol_lookback_days`,
distinct from `target_portfolio_vol`'s explicitly-annualized convention, worth noting if you're
setting both).

**Complementary to, not redundant with, `max_position_weight`**: that flat cap is identical for
every ticker regardless of its own volatility; `position_vol_budget` varies per ticker, so a
low-vol name can be allowed a larger weight than a high-vol name even under the same flat cap.
Applied AFTER the flat cap in `resolve_target_weights()`'s pipeline, using the same iterative
cap-and-redistribute approximation `_apply_position_caps()` already uses (not a full LP solve),
just with a per-ticker cap dict instead of one global scalar. Also not in tension with
`target_portfolio_vol`'s portfolio-level exposure scaling, that scalar is applied uniformly
across all tickers AFTER weight composition is finalized, a separate axis (overall scale, not
per-position shape).

## Volatility Scaling (Portfolio-Level) [Mandatory tier]

`target_portfolio_vol` (default `0.15`, annualized), distinct from `position_vol_budget` above
(which caps a SINGLE ticker's weight): scales the WHOLE book's gross exposure to hit a target
annualized volatility, shrinking the entire portfolio in a high-vol regime and letting it run up
to `max_gross_exposure` (default `1.0`) in a calm one, clamped at the floor `min_gross_exposure`
(default `0.20`) so it never fully flatlines to 0% invested.

`compute_vol_scalar(realized_vol, target_portfolio_vol, min_gross_exposure, max_gross_exposure)`
(`backtest/momentum_backtest.py`) is the single shared formula: `np.clip(target_portfolio_vol /
realized_vol, min_gross_exposure, max_gross_exposure)`, falling back to `max_gross_exposure` when
`realized_vol` is `None` or `0` (not enough history to scale down safely). Used identically by
both paths:
- **Backtest**: `run_risk_managed_backtest()` measures `realized_vol` from the simulated
  `portfolio_history` equity curve over `portfolio_vol_lookback` trading days (default `21`,
  `_realized_portfolio_vol()`).
- **Live**: `execution/live_signal.py`'s `compute_target_weights()` measures `realized_vol` from
  the trailing `daily_prices` at the just-resolved target weights (`_realized_weighted_portfolio_vol()`,
  no simulated equity curve exists live, this is the honest substitute, the same "trailing data,
  not a simulated ledger" pattern `_inverse_vol_weights()` already uses for position sizing).

**This closes a real gap**: before this, portfolio-level vol targeting existed ONLY in the
backtest engine, `live_signal.py` had no aggregate risk-exposure throttling at all, only
position-level inverse-vol sizing and the regime/correlation-spike gross-exposure scalars.

**Composes multiplicatively, not a replacement for anything else**: `gross_exposure =
min(max_gross_exposure, regime_scalar * vol_scalar)`, exactly matching the backtest's existing
composition order. A bearish regime AND a high-vol realized book can both be active at once,
both scalars apply together.

## Absolute Momentum (Macro) [Mandatory tier]

`use_absolute_momentum` (default `false`, opt-in like `skip_month_guardrail`) + `defensive_ticker`
(default `"BIL"`): the Antonacci-style "dual momentum" fix. Relative momentum (picking the top-N
by rank) says nothing about whether those winners are winning in ABSOLUTE terms, in a broad
drawdown the "top N" can all still have negative trailing returns and the strategy holds them
anyway. When enabled, any pick whose OWN trailing return is negative is swapped for
`defensive_ticker` instead of being held.

Two mechanisms already existed here, worth distinguishing clearly:
- **`use_regime_filter`** (pre-existing): a benchmark SMA trend filter (SPY vs. its 200D SMA by
  default), scales the WHOLE book's gross exposure down to `min_gross_exposure` in a downtrend.
  One signal, applied uniformly to every position.
- **`use_absolute_momentum`** (this constraint, new): swaps INDIVIDUAL picks by their OWN
  trailing momentum, a per-ticker check, not a whole-book scalar.

These are complementary, not redundant, and can both be enabled at once: the regime filter
throttles overall exposure based on the market's trend; the absolute momentum overlay decides
WHICH tickers are even worth holding in the first place. A broad drawdown typically trips both.

Implemented via `execution/live_signal.py`'s `apply_absolute_momentum_filter()`, a thin wrapper
around `core/functions_quant_extensions.py`'s `absolute_momentum_overlay()` (which existed,
fully coded, since before this constraint was wired in, but was never called anywhere until
now), reusing that function directly rather than reimplementing the swap rule so backtest and
live can never silently diverge on it. Wired into `run()` right after picks are selected,
BEFORE sizing/vol-scaling/regime-filtering, so every downstream step (Volatility Scaling above,
Position Size Hard-Cap, the regime filter) all act on the FINAL, post-filter pick list.

**LIVE-ONLY** (same as `skip_month_guardrail`/`lookback_period`): the backtest engine consumes
pre-computed `monthly_picks`, it never ranks tickers itself, so this constraint has no effect on
a backtest run, only on `daily_runner.py`'s live rebalance loop.

`defensive_ticker` must be priced alongside the portfolio's own tickers for this to actually
work (add it to that portfolio's own `tickers:` list in `config.yaml`), there is no automatic
widening of the price fetch for it, unlike the orphaned-ticker reconciliation's
`extra_price_tickers` mechanism (a deliberately different, narrower feature).

## Whole-Book Negative Momentum Cash Filter [New, LIVE + BACKTEST, opt-in]

`use_negative_universe_cash_filter` (default `false`): when EVERY ticker in the eligible
universe has a non-positive trailing score this rebalance, holds literal CASH (0% invested)
instead of picking the "least bad" `top_n` (the default behavior, `nsmallest()`-based selection
picks the strongest of a bad bunch regardless of sign) or swapping to `defensive_ticker`
(`use_absolute_momentum`, above, which still ends up invested).

```yaml
risk_overrides:
  use_negative_universe_cash_filter: true
```

**Distinct from `use_absolute_momentum`, not a replacement for it**: that constraint swaps
INDIVIDUAL negative picks for `defensive_ticker`, a per-ticker decision, the book stays fully
invested (just in a different name). This constraint is a WHOLE-BOOK decision, literal cash,
triggered only when NOTHING in the universe shows positive momentum, a much rarer, more extreme
condition than any single pick being negative. The two are complementary and can both be
enabled at once; **when both trigger simultaneously, this constraint takes precedence**, forcing
literal cash rather than a defensive-ticker swap, see the real interaction bug below.

**Implementation, guaranteeing live/backtest parity by construction**: `is_universe_negative(scores_row,
tickers)` (`core/strategy_signals.py`) is the shared predicate (every valid, non-NaN score `<=
0`, a zero score is not positive, same convention `select_absolute_momentum_picks()` already
uses). `resolve_strategy_picks()`, the SINGLE function both `execution/live_signal.py`'s `run()`
(live) and `generate_strategy_monthly_picks()` (backtest) call for final pick selection, checks
this FIRST, before the `strategy_type` dispatch, forcing an empty pick list immediately when it
triggers, so it correctly overrides `absolute_momentum`'s own `select_absolute_momentum_picks()`
(which never itself returns empty, always falls back to `[defensive_ticker]`). Reuses the
already-confirmed-safe "empty picks -> `generate_orders()` sells any current holdings to cash
and buys nothing, no crash" code path (see `docs/ALERT_LOG.md`'s `NO_ELIGIBLE_TICKERS` row), no
new sizing-path risk introduced.

**A real, confirmed interaction bug, found and fixed while implementing this constraint**:
`core/functions_quant_extensions.py`'s `absolute_momentum_overlay()` (the function backing the
SEPARATE `use_absolute_momentum` overlay toggle, applied AFTER `resolve_strategy_picks()`
returns) falls back to `[defensive_ticker]` whenever handed an ALREADY-empty picks list
(`kept if kept else [defensive_ticker]`), its own designed behavior for "never leave the
portfolio in a naked empty state." With both `use_negative_universe_cash_filter` and
`use_absolute_momentum` enabled at once, this would have silently RE-INJECTED
`defensive_ticker`, completely defeating this constraint's whole point. `execution/
live_signal.py`'s `run()` now recomputes `is_universe_negative()` itself right after
`resolve_strategy_picks()` returns, and skips the `use_absolute_momentum` overlay call entirely
when this constraint (not an unrelated cause like liquidity filtering) is what produced the
empty picks, letting literal cash actually win.

**New alert, `MARKET_WIDE_NEGATIVE_MOMENTUM_CASH`** (WARNING, `log_alert()`): fires specifically
when THIS constraint, not an unrelated cause, emptied `picks`. Distinct from the more generic
`NO_ELIGIBLE_TICKERS` alert (`docs/ALERT_LOG.md`), which also fires in this case (both alerts
fire together, that's fine, they mean different things: one says "nothing was eligible this
rebalance", this one says "specifically, the whole market looked bad").

**Backtest note, a gap that WAS an honest scope caveat here, now closed**: fixing this at the
SELECTION layer (`resolve_strategy_picks()`) also surfaced and fixed a real, confirmed parity gap
in `generate_strategy_monthly_picks()`: a date where a real "hold cash" decision was made (empty
picks, e.g. from this constraint or the liquidity filter) used to be silently SKIPPED from the
returned `monthly_picks` series entirely, exactly like the "no signal at all yet" case (start of
history, lookback not satisfied), even though it's a genuinely different situation. That skip
meant `run_risk_managed_backtest()`'s `monthly_picks.get(date, [])` lookup for the NEXT
rebalance would silently fall through to a STALE prior period's picks instead of correctly
seeing "nothing was eligible then." Fixed: such a date is now included with an explicit `[]`,
so subsequent lookups see the correct, current decision, not stale data.

At the time, this deliberately did NOT also make `run_risk_managed_backtest()` actively LIQUIDATE
existing holdings to cash the moment this constraint triggered, that engine's own rebalance-
trigger condition (`if target_tickers and not circuit_breaker_halted:`) treated an empty
`target_tickers` as "nothing to rebalance this period, hold whatever is currently held" rather
than "force-sell everything," a narrower, EXECUTION-layer gap distinct from the SELECTION layer
this constraint lives in. **This has since been fixed** (a later epic in this same project): the
rebalance-trigger condition is now `if not circuit_breaker_halted:`, with the sizing logic
conditional on `target_tickers` and an explicit `else: target_dollar = {}` branch when it's
empty, flowing through the SAME sell/buy pipeline as any other rebalance and correctly
liquidating every current holding to cash. `circuit_breaker_halted` still overrides both branches
identically (a halted portfolio is neither rebalanced nor force-liquidated, unchanged). Live's
`execution/live_signal.py`'s `run()` never had this gap, `generate_orders()` genuinely sells any
current holdings to cash immediately once `picks` comes back empty; the backtest engine now
matches that behavior exactly.

## Broker-Side Protective Stop [New, LIVE-ONLY, opt-in]

`attach_broker_stop_loss` (default `false`): a REAL IBKR bracket order attached at BUY time,
parent BUY + child `STP` (stop-market) SELL, so the position is protected by the BROKER ITSELF
even when this app isn't running. This closes a real gap surfaced by a confirmed incident
(2026-07-16, see the cross-portfolio-sell fix elsewhere in this project's history): this app is
a scheduled batch job, not a persistent/always-on service, its EXISTING `auto_execute_stop_loss`
check (below) only runs at all when `daily-runner --live` is actually invoked, so a position had
zero downside protection on any day the app wasn't scheduled or the machine/container was off.

**Belt-and-suspenders, deliberately NOT a replacement for `auto_execute_stop_loss`**:
- `attach_broker_stop_loss` is what actually delivers "protection independent of whether this
  app is running." `auto_execute_stop_loss` alone never does, by construction.
- `auto_execute_stop_loss` still has independent value even with a bracket attached: it's the
  ONLY mechanism for `max_holding_days` (a broker `STP` order has no concept of "N days held"),
  it can react to a `stop_loss_pct` adjusted mid-position (e.g. via an `ADJUST_PARAM` email
  command) without needing to cancel/replace a resting order, and it's a fallback for a position
  opened before `attach_broker_stop_loss` was ever turned on.

See "Broker-Native Trailing Stop" below for the trailing counterpart (`attach_broker_trailing_stop`),
which can run alongside this one as an IBKR One-Cancels-All (OCA) pair.

Both reuse the SAME `stop_loss_pct` field, no duplicate config. The child `STP` (not `STP LMT`):
a genuine protective stop must reliably execute during a fast decline, a limit leg can be
skipped over in a gap, defeating the purpose. `outsideRth=True` on both legs when
`allow_extended_hours` is set, otherwise the stop only monitors/triggers during regular hours,
leaving a real gap for a move in the same extended session the entry itself was allowed in.

**TIF, deliberately asymmetric**: the parent BUY carries `tif="DAY"` (matches the account's own
observed default, made explicit rather than implicit, a BUY either fills same-session or the
whole bracket attempt can simply be resubmitted next run). The protective child carries
`tif="GTC"`, NOT `"DAY"`: a `DAY` stop would be cancelled by IBKR at end of day and leave the
position completely unprotected on every subsequent day this app doesn't run, defeating the
entire purpose. IBKR allows a bracket's parent and child to carry different TIF values.

**Cancel-before-sell**: when this app itself later decides to exit a position (a rebalance
rotation, `risk_monitor.py`-triggered action, or the Python-side `auto_execute_stop_loss`
check), any resting protective `STP` for that ticker is cancelled FIRST, via a real,
broker-truth-based `reqAllOpenOrders()` query (not `reqOpenOrders()`, which only returns the
SAME client connection's own orders; not a locally-cached order ID either), since the run that
PLACED the bracket and the run that later decides to EXIT are almost always different process
invocations. This prevents the broker's own triggered stop and this app's rebalance-driven sell
from both trying to sell the same shares. Self-healing even if the placing run crashed before
logging anything, or TWS restarted. Zero extra IBKR round trip when `attach_broker_stop_loss`
is off (the default).

```yaml
risk_overrides:
  attach_broker_stop_loss: true   # opt-in, reuses stop_loss_pct below
  stop_loss_pct: 0.12             # shared with the Python-side auto_execute_stop_loss check
  auto_execute_stop_loss: false   # independent, unaffected by attach_broker_stop_loss's default
```

## Stop-Loss Width: Fixed-From-Entry, Not Trailing [`stop_loss_pct`]

**What `stop_loss_pct` actually measures today, confirmed by reading both code paths**:
`(current_price - entry_price) / entry_price`, checked against `-stop_loss_pct`, in BOTH the
backtest (`backtest/momentum_backtest.py`'s stop-loss check, `dd <= -config.stop_loss_pct`
against `entry_prices[ticker]`) and live (`auto_execute_stop_loss`'s Python-side check, and
`attach_broker_stop_loss`'s broker-side `STP` order, `auxPrice = expected_prices[ticker] * (1 -
stop_loss_pct)`, both anchored to the entry fill, never to the position's highest price since
entry). **This is a fixed stop from entry, not a trailing stop.** A true trailing stop ratchets
its exit level up as the position makes new highs, locking in unrealized gains as they accrue;
`stop_loss_pct` never moves once a position is opened (only a mid-position `ADJUST_PARAM` email
command, or a config edit + restart, changes it, and even then it's still measured from the
original entry price, not from any subsequent high).

**Recommended width by momentum regime** (this project's cadence terminology,
`holding_period < 1` = short-term/weekly, `holding_period >= 1` = long-term/monthly, matching
the "Recommended Config Presets" section below):

| Regime | Recommended `stop_loss_pct` | Rationale | What this project delivers today |
|---|---|---|---|
| Short-Term (weekly) | `0.10` | Tighter control suits a short-term/volatile regime, cuts downside rapidly, the position is expected to rotate out within the holding period anyway | Exact match, a fixed 10% stop from entry is the tighter-control behavior described |
| Long-Term (monthly) | `0.15` - `0.20` | Gives winning positions room to breathe through normal pullbacks without an early, premature exit, while still bounding a structural-crash loss | **Partial match only**, see below |

**Why "partial match" for the long-term row**: widening `stop_loss_pct` to `0.15`-`0.20` does
reproduce the "room to breathe, don't get shaken out by a normal pullback" half of the cited
research, a wider fixed stop is genuinely less likely to trigger on routine volatility. It does
**not** reproduce the other half, "lock in gains as the position runs up," a fixed stop measured
from entry offers zero additional protection to an already-profitable position beyond the same
flat percentage every other position gets; a position up 40% still only exits if it round-trips
all the way back down 15-20% from its ORIGINAL entry, not from its peak. If you specifically want
the gain-locking behavior, that's a genuine trailing stop, see "Trailing Stop-Loss" below: a
Python-side daily ratchet (`use_trailing_stop`), or (Epic 9, "Broker-Native Trailing Stop" plan)
a real broker-native IBKR `TRAIL` order (`attach_broker_trailing_stop`), see "Broker-Native
Trailing Stop" below.

## Trailing Stop-Loss [New, LIVE + BACKTEST, opt-in] [`use_trailing_stop`/`trailing_stop_pct`]

Closes the gain-locking gap the section above documents. Distinct from `stop_loss_pct` (measured
from entry, never moves): `trailing_stop_pct` is measured from a position's own highest price
seen since entry, exits once the current price has fallen `trailing_stop_pct` from that high, so
a position up 50% that pulls back 10% from its peak exits with most of the gain locked in, a
fixed from-entry stop would never trigger on that same pullback at all. Independent of and
complementary to `stop_loss_pct` (a portfolio can run both at once, whichever triggers first
wins; `stop_loss_pct` still bounds the worst case on a position that never rallies).

**Implementation: a Python-side daily ratchet here, PLUS (Epic 9, "Broker-Native Trailing Stop"
plan) a real broker-native IBKR `TRAIL` order, see "Broker-Native Trailing Stop" below**. The
Python-side check below mirrors this project's own established `auto_execute_stop_loss` pattern:
checked once per invocation (not intraday), fully testable in dry-run, no new broker order type.
It remains useful even now that a broker-native option exists, same "belt-and-suspenders, not a
replacement" relationship `auto_execute_stop_loss` already has with `attach_broker_stop_loss`
(the Python-side check is the only mechanism that works in dry-run, and reacts to a
`trailing_stop_pct` changed mid-position without needing to cancel/replace a resting order).

- LIVE (`daily_runner.py`'s `check_and_handle_trailing_stops()`, in the same "ALWAYS runs"
  per-portfolio block as `check_and_handle_stop_losses()`/`check_and_handle_time_stops()`):
  persists each ticker's high-water-mark to `data/trailing_stop_hwm_<portfolio>.json`, since
  `daily-runner` is a stateless CLI process re-invoked by cron, this file is what lets the trail
  survive across separate invocations. A ticker no longer held is pruned from this file, so a
  later re-entry always starts a fresh trail rather than inheriting a stale high. Honors
  `ticker_risk_overrides[ticker]['enabled']`, the same kill-switch `stop_loss_pct` already uses
  (a disabled ticker is skipped by both checks); flag-vs-auto-execute follows the same
  `auto_execute_stop_loss` toggle as every other exit check here.
- BACKTEST (`backtest/momentum_backtest.py`'s `run_risk_managed_backtest()`): a `running_high`
  dict tracked alongside the existing `entry_prices` dict in the day loop, same lifecycle (both
  cleared together on exit or re-entry). Same daily-bar gap-risk limitation as the fixed
  stop-loss check documented above, a real overnight gap will not be caught at its true level.

```yaml
risk_overrides:
  use_trailing_stop: true         # opt-in, false (default) is byte-identical to before this
                                   # existed
  trailing_stop_pct: 0.10         # exit once price has fallen 10% from its post-entry high
```

## Broker-Native Trailing Stop [New, LIVE-ONLY, opt-in] [`attach_broker_trailing_stop`]

The broker-native counterpart to `use_trailing_stop` above, closing the gap that section
previously documented as "a considered, explicit choice, not an oversight" but left open: a REAL
IBKR `TRAIL` order attached at BUY time (parent BUY + child `TRAIL` SELL), protecting the position
continuously AT THE BROKER, intraday, even while this app isn't running, the same
"protection independent of whether this app is running" property `attach_broker_stop_loss`
already delivers for the fixed stop. Reuses `trailing_stop_pct` for the trail width, no duplicate
field. Deliberately independent of `use_trailing_stop` (you can run the broker bracket without
the Python-side daily check, or vice versa, or both), same independence
`attach_broker_stop_loss` already has from `auto_execute_stop_loss`.

**Mechanics**: `orderType="TRAIL"`, `trailingPercent = trailing_stop_pct * 100` (a percent
number, e.g. `10.0` for 10%, not a dollar amount), `trailStopPrice` computed and logged
explicitly at submission time (`reference_price * (1 - trailing_stop_pct)`, the same
transparency precedent `attach_broker_stop_loss`'s `auxPrice` already sets, rather than left to
IBKR's own auto-calculation), `tif="GTC"` (same "must survive across days/restarts" rationale as
the fixed bracket's own `GTC` child). IBKR itself then ratchets the stop price up as the position
makes new highs, no daily Python check involved.

**A real, confirmed IBKR platform constraint, found via this feature's own real paper-account
verification, not assumed**: submitting a TRAIL child attached to a plain `MKT` parent (the same
shape the STP bracket already uses successfully) failed with **IBKR error 328, "Trailing stop
orders can be attached to limit or stop-limit orders only."** Unlike the STP child, which attaches
fine to a `MKT` parent, a `TRAIL` child specifically requires its parent to be `LMT` or `STP LMT`.
Fixed: the parent is now forced to `LMT` whenever a TRAIL child will attach and it isn't already
(`allow_extended_hours` may have already converted it), using the exact same buffer-based limit
price computation (`reference_price * 1.005`) `allow_extended_hours`'s own LMT conversion already
uses, for the identical reason (favor an actual fill over an exact price). This does NOT affect
the STP-only case (`attach_broker_stop_loss` without `attach_broker_trailing_stop`), which keeps
its plain `MKT` parent exactly as before.

**Combining with the fixed bracket (One-Cancels-All)**: when a portfolio enables BOTH
`attach_broker_stop_loss` and `attach_broker_trailing_stop` together, confirmed with the project
owner, the two children attach as an IBKR **One-Cancels-All (OCA) group** (`ocaGroup` set to the
same string on both, `ocaType=1`, "cancel all remaining orders in the group on any fill"), so
whichever one triggers first automatically cancels the other AT THE BROKER. This matches
`trailing_stop_pct`'s own documented "whichever triggers first wins" semantics (see "Trailing
Stop-Loss" above) extended correctly to the broker level, instead of leaving a naked resting
order behind once one side fills. Only ONE bracket type attaching (either flag alone) gets no OCA
fields at all, unchanged single-child behavior.

**Cancel-before-sell, widened**: the SAME broker-truth-based `reqAllOpenOrders()` mechanism
described under "Broker-Side Protective Stop" above now also matches a resting `TRAIL` order
(previously `STP`-only), so this app's own SELL (rebalance rotation, or any of the three
Python-side auto-exit checks) correctly cancels a resting protective TRAIL first, the same way it
already did for a resting STP. **A real, confirmed pre-existing gap found and fixed while wiring
this in, unrelated to the TRAIL order type itself**: `daily_runner.py`'s three auto-exit call
sites (`check_and_handle_stop_losses()`, `check_and_handle_time_stops()`,
`check_and_handle_trailing_stops()`) were never passing `attach_broker_stop_loss`/`stop_loss_pct`
to `place_orders_ibkr()` at all, confirmed by reading all three, meaning a resting broker STP was
NEVER cancelled before one of these three checks' own auto-exit SELL, only the rebalance path had
this protection. Fixed: all three now pass the same four kwargs
(`attach_broker_stop_loss`/`stop_loss_pct`/`attach_broker_trailing_stop`/`trailing_stop_pct`) the
rebalance path already did, closing this for both bracket types at once.

**Confirmed live, a real IBKR behavior worth knowing**: when both children of an OCA pair are
resting and this app cancels one (say the TRAIL child), IBKR itself immediately cancels the
OTHER child (the STP) automatically, before this app's own code ever issues a second
`cancelOrder()` call for it. The code still attempts to cancel both (it doesn't know in advance
that IBKR will do this), the second attempt simply gets a harmless "already cancelled" response
(IBKR error 10148), not a bug or a race condition, confirmed directly against a real paper
account.

```yaml
risk_overrides:
  attach_broker_trailing_stop: true  # opt-in, reuses trailing_stop_pct below
  trailing_stop_pct: 0.10            # required (0, 1) when this OR use_trailing_stop is true
  attach_broker_stop_loss: true      # optional: combine for an OCA-paired fixed + trailing stop
  stop_loss_pct: 0.12                # the fixed bracket's own width, if attach_broker_stop_loss is on
```

**A real, confirmed bug found while paper-verifying this feature, unrelated to the trailing-stop
logic itself, fixed alongside it**: `execution/live_signal.py`'s `generate_orders()` crashed with
`ValueError: cannot convert float NaN to integer` when a ticker's latest fetched price was `NaN`
(confirmed directly against a real paper-account run: yfinance's most recent trading-day row for
XLRE was `NaN`, a vendor data-lag case, not synthetic). The price-validity guard,
`if price is None or price <= 0:`, does not catch `NaN` (`NaN <= 0` is `False` in Python, so a
`NaN` price silently fell through as if valid, reaching `int(shares)` further down). Fixed with
an explicit `pd.isna(price)` check alongside the existing `None`/`<= 0` checks, now correctly
resolves to the same safe `"no live price available"` HOLD every other missing-price case already
gets. See `README.md`'s Known Gaps for the full incident writeup.

**Configuring the recommended widths** (`config.yaml`, per-portfolio):

```yaml
portfolios:
  long_term_portfolio:
    risk_overrides:
      stop_loss_pct: 0.18           # room to breathe for a monthly-cadence position; still a
                                     # FIXED stop from entry, not trailing, see docs/RISK_CONSTRAINTS.md
  short_term_portfolio:
    risk_overrides:
      stop_loss_pct: 0.10           # tighter control for a weekly-cadence, noisier signal
```

Both regimes can layer `auto_execute_stop_loss: true` (Python-side auto-sell on trigger,
checked every day this app runs) and/or `attach_broker_stop_loss: true` (a real IBKR bracket at
BUY time, protects the position even when this app isn't running), independent of which width you
pick, and/or (Epic 9) `attach_broker_trailing_stop: true` (a real IBKR TRAIL bracket, OCA-paired
with the fixed bracket above when both are on), see "Broker-Side Protective Stop" and
"Broker-Native Trailing Stop" above.

**A per-ticker "Stop-Loss Price" figure is also visible**, in the rebalance email's second "Full
Signal Universe" table and its sibling `logs/signal_rankings_log_<portfolio>.csv`. As of Epic 8
("Stop-Loss Price Reporting Fix" plan), this IS a real per-share price, matching the mechanism
described above: `close_price * (1 - stop_loss_pct)` for a `BUY` (no entry exists yet), or
`avg_entry_price * (1 - stop_loss_pct)` for a `HOLD` when the real entry price is known, falling
back to `close_price` when it isn't (e.g. dry-run without `persist_dry_run_state`), see
`docs/SIGNAL_RANKINGS_LOG.md`. This reverses an earlier reporting-layer decision (a dollar-
amount-at-risk figure, `money_invested * stop_loss_pct`), reversed on an explicit, informed
instruction because the dollar figure didn't match what's actually enforced. Still
reporting-only: neither `check_and_handle_stop_losses()`'s daily check nor
`place_orders_ibkr()`'s broker-side bracket read this reported value, both independently compute
their own real per-share threshold directly from `avg_entry_price`.

## ATR-Based Trailing Stop [New, Epic 2, LIVE + BACKTEST, opt-in] [`use_atr_trailing_stop`]

`use_atr_trailing_stop` + `atr_trailing_stop_multiplier` (required when enabled) +
`atr_period` (default `14`): a volatility-ADAPTIVE trailing-stop distance using Wilder's ATR
(`core/technical_indicators.py`'s `atr()`, already hand-rolled in this codebase for the email
report's technical-indicator section, now wired into a real exit decision for the first time),
instead of "Trailing Stop-Loss" above's FIXED percentage. A tight percentage stop whipsaws a
volatile ticker and a loose one gives back too much gain on a calm one; ATR sizes the distance
per-ticker automatically from that ticker's own recent volatility.

**Comparison shape is deliberately different from the percentage trail, not an inconsistency**:
`trailing_stop_pct` compares a PERCENTAGE fraction, `(price - high) / high <= -trailing_stop_pct`.
The ATR trail instead compares an ABSOLUTE PRICE/DOLLAR distance, `(high - price) >=
atr_trailing_stop_multiplier * ATR`, since ATR itself is quoted in the ticker's own price units
(dollars), not as a percentage of price. Forcing ATR into a percentage frame would need an extra,
lossy `/ high` conversion for no real benefit.

**Shares state with the percentage trail, does not duplicate it**: "highest price since entry" is
the identical quantity regardless of which distance formula reads it. Backtest: the SAME
`running_high` dict the percentage-trail block already maintains. Live: the SAME
`trailing_stop_hwm_<portfolio>.json` file/schema `check_and_handle_trailing_stops()` already
persists for the percentage trail, no migration needed. Both trail types may run simultaneously,
**whichever triggers first wins**, the same precedent `stop_loss_pct` + `trailing_stop_pct`
already establish; a ticker breaching both in the same run is flagged by whichever check runs
first (percentage trail checked first) and correctly skipped by the other, generating exactly one
exit order, not two.

```yaml
risk_overrides:
  use_atr_trailing_stop: true
  atr_trailing_stop_multiplier: 3.0   # exit once price has fallen 3x ATR(14) from its post-entry high
  atr_period: 14                      # default, matches core/technical_indicators.py's atr()
  ticker_risk_overrides:
    AMD:
      atr_trailing_stop_multiplier: 2.0   # a tighter multiplier for AMD alone
```

**A real, confirmed OHLC-plumbing gap, the largest real cost of this feature**: `daily_prices`
throughout `backtest/momentum_backtest.py`'s `run_risk_managed_backtest()` is close-only,
confirmed by reading `_split_price_panel()` directly, it extracts only `close`/`open` from a
MultiIndex panel and silently discards `high`/`low` even when present. ATR needs high/low/close.
`run_risk_managed_backtest()`/`run_custom_backtest()` therefore gained new optional
`daily_highs`/`daily_lows` DataFrame parameters (same column shape as `daily_prices`), **REQUIRED**
(a loud `ValueError`, not a silent skip) when `use_atr_trailing_stop` is `True`, matching the same
"opt-in extra data the caller must supply" precedent `use_liquidity_filter`'s `daily_volume`
requirement already established. ATR is precomputed once per ticker before the day loop (a pure
function of the already-available OHLC panels), sourced from the FULL pre-simulation-mask history
(`close_full`/`daily_highs`/`daily_lows`), not the already-window-masked `prices`, the same
"bound only at the end, not the start" lesson Epic 14's `momentum_crash_lookback_days` fix already
applied, so a short simulation window doesn't starve ATR's own lookback. Live-side, the cost is
much smaller: `check_and_handle_trailing_stops()` fetches OHLC for CURRENTLY-HELD tickers only
(a handful, not the full universe) via the EXISTING `fetch_ohlcv_for_tickers()` (already used
elsewhere for the email report's technical indicators), no new fetch mechanism, a vendor gap for
one ticker skips the ATR check for that ticker only, not the whole run.

**Per-ticker override**: `ticker_risk_overrides[ticker]['atr_trailing_stop_multiplier']`, a 4th
allowed key alongside `enabled`/`stop_loss_pct`/`max_pct_of_adv`, resolved via
`resolve_ticker_atr_multiplier(ticker, cfg)` (same shape/placement as
`resolve_ticker_stop_loss_pct()`). Unlike `resolve_ticker_max_pct_of_adv()`, this DOES honor
`'enabled': false` (returns `None`, disabling the ATR check for that ticker): ATR trailing stop
is a stop-loss-family exit mechanism, the same `'enabled'` kill-switch semantics the fixed and
percentage trailing stops already use, one consistent "enabled" meaning per ticker across every
exit check, not a second, divergent one (liquidity-adjusted sizing's `max_pct_of_adv` is a
genuinely different, non-exit concern, which is why that resolver ignores `'enabled'`).

**Deliberately NOT paired with a broker-native IBKR `TRAIL` order, unlike `use_trailing_stop`'s own
`attach_broker_trailing_stop` counterpart**: IBKR's `TRAIL` order type only fixes its trail
distance ONCE, at submission time (confirmed: IBKR `TRAIL` supports either a percent
(`trailingPercent`) or a fixed dollar (`auxPrice`) trail amount, never a value that dynamically
recomputes). A broker-native ATR-distance version would therefore only ever be "ATR distance as of
order placement, held fixed thereafter," a materially weaker and potentially misleading claim of
"ATR-based" protection given ATR itself changes day to day. Given the OHLC-plumbing cost above
already makes this the largest single feature in this project's institutional risk-management
program, the broker-native variant is a deliberate, explicit scope decision, not an oversight; a
future epic could revisit it if genuinely wanted, but the Python-side daily ratchet documented
here (works in dry-run, recomputes ATR fresh every real invocation) is the complete, real
deliverable.

**Epic 2 real verification (run 2026-08-05), honestly reported**: full pytest suite 1014 passed
(up from 994 pre-Epic-2, +20 new tests), zero regressions. Real end-to-end paper-account
confirmation (port 7497), both natively and inside a Docker container rebuilt with this epic's
code: a real BUY into a throwaway single-ticker test position, followed by a second invocation
with a deliberately tiny `atr_trailing_stop_multiplier` (`0.01`) to force a near-certain trigger
from ordinary bid/ask tick noise, confirmed a REAL computed ATR (e.g. `$8.17` for INTC, from real
fetched OHLCV, not simulated), a real `ATR_TRAILING_STOP_TRIGGERED` alert, and a real
auto-executed SELL closing the position (`execDetails`/`commissionReport` confirmed, real fill
prices). Repeated twice with two different tickers (JPM, then INTC) for extra confidence, then
confirmed identically inside the rebuilt Docker container (same real ATR value, same trigger
mechanism, alert email sent successfully there). Both trading-hours (order queues correctly for
next session, informational `error 399`, not a crash) and after-hours (`allow_extended_hours`,
real fill) submission paths were exercised for real, not just the common case.

**A real, confirmed cross-CONFIG-FILE ticker-overlap incident, found during this epic's own live
verification, distinct from the already-documented cross-PORTFOLIO overlap
`scope_overlapping_holdings()` protects against**: the first live verification attempt used a
throwaway `--config <file>.yaml` containing a single-ticker test portfolio for a ticker (`NVDA`)
that also happens to be configured in this project's own REAL `config.yaml`'s `portfolio1`. Since
`get_ibkr_positions()` returns the WHOLE real account's positions regardless of which config file
is currently loaded, and `scope_overlapping_holdings()`'s overlap detection is built from
`check_ticker_overlap()`, which only inspects the portfolios dict of the SINGLE config file
actually loaded in the current process, the throwaway portfolio saw the REAL, whole-account NVDA
position (66 shares, accumulated from real `portfolio1` activity) as entirely "its own," and its
small target weight drove a real (paper, not financial-loss) full-liquidation SELL of all 66
shares. This is a genuine, previously-undocumented gap: the existing ticker-overlap protection
only covers overlap BETWEEN PORTFOLIOS WITHIN ONE LOADED `config.yaml`, not overlap between the
currently-loaded config and ANY OTHER config file/process that may have traded the same real
account historically, since there is no cross-invocation, cross-file registry of "which config
last touched this ticker." Confirmed harmless here only because it was a paper account and the
position was later understood to be intentionally cleared by the account owner; the SAME
mechanism would be genuinely dangerous against a real-money account if a throwaway/test config
file ever shared a ticker with a real portfolio's own config. **Practical mitigation until a real
fix exists**: never point `--config` at a file containing a ticker also configured in any other
config file/portfolio trading the same real IBKR account; this project's own throwaway
verification configs for Epic 1/Epic 2 were built with this rule in mind after this incident
(deliberately ticker-disjoint from `config.yaml`'s real portfolios). A genuine fix (e.g. a
process-external, persisted "which config/portfolio owns this ticker" registry, or extending
`scope_overlapping_holdings()` to consult more than just the currently-loaded file) is out of
scope for this epic and not yet built.

## Per-Ticker Stop-Loss Override

`stop_loss_pct` above is the portfolio-wide default, applied to every ticker equally. Some
tickers may genuinely warrant a different treatment, a defensive/hedge position you never want
auto-exited on a routine pullback, or a single-name position you want protected more tightly
than the rest of the portfolio. `ticker_risk_overrides` (`BacktestConfig`, `{}` default, zero
behavior change for any ticker without an entry) lets you set this per ticker, per portfolio:

```yaml
risk_overrides:
  stop_loss_pct: 0.12             # portfolio-wide default, unchanged
  ticker_risk_overrides:
    AAPL:
      enabled: false               # AAPL is never stop-loss-checked, held through any drawdown
    AMD:
      stop_loss_pct: 0.08          # tighter than the portfolio default, AMD alone
```

| Key | Type | Effect |
|---|---|---|
| `enabled: false` | bool | Disables the stop-loss check ENTIRELY for this ticker: never flagged, never auto-sold, no broker-side bracket attached even if `attach_broker_stop_loss: true` for the rest of the portfolio. |
| `stop_loss_pct: <float>` | float in `(0, 1.0)` | This ticker uses its OWN width instead of the portfolio's `stop_loss_pct`. Can be combined with `enabled: true` (or omitted, defaults to enabled) to make the intent explicit. |

A ticker with **no entry** in `ticker_risk_overrides` behaves exactly as before this feature
existed, using the portfolio's own `stop_loss_pct`. This applies uniformly across every place
`stop_loss_pct` is consulted: `check_and_handle_stop_losses()`'s daily drawdown check (the
"ALWAYS runs" block, before any rebalance-day logic), `compute_stop_loss_price()`'s reporting
(the Full Signal Universe table/log's `Stop-Loss Price` column, a dollar-at-risk figure, see
above), and `place_orders_ibkr()`'s `attach_broker_stop_loss` bracket, resolved once via
`execution/live_signal.py`'s
`resolve_ticker_stop_loss_pct(ticker, cfg)`, the single source of truth for "what stop-loss
width, if any, applies to this ticker right now." (the `Stop-Loss Price` column/log figure
above is a real per-share price as of Epic 8, no longer a dollar-at-risk figure)

## Flooring Remainder Redeployment

IBKR has no fractional equity/ETF order support at all (see `README.md`'s Known Gaps), so
`generate_orders()` floors every BUY's target dollar amount to a whole share count. That
flooring always leaves a small leftover per ticker unused, e.g. a $500 target on a $270 stock
floors to 1 share (`$270`), leaving `$230` of that ticker's own allocation never deployed.
`redeploy_flooring_remainder` (`BacktestConfig`, `false` default, zero behavior change when
off) closes this: when `true`, this rebalance's leftover is pooled across EVERY BUY and
redeployed as extra whole shares of the single TOP-RANKED BUY ticker (the strongest signal this
rebalance), not spread thinly across the basket.

```yaml
risk_overrides:
  redeploy_flooring_remainder: true
```

Worked example, two BUYs this rebalance, `A` ranked #1, `B` ranked #2:

| Ticker | Target | Price | Floored shares | Spent | Leftover |
|---|---|---|---|---|---|
| A (rank 1) | $500 | $270 | 1 | $270 | $230 |
| B (rank 2) | $500 | $130 | 3 | $390 | $110 |

Pooled leftover: `$230 + $110 = $340`. Redeployed into `A` (top-ranked): `floor($340 / $270) =
1` extra share, `A` ends up with 2 shares total, `B` unchanged at 3. If the pooled leftover
can't afford even one more share of the top pick, or there are no BUYs at all this rebalance, this
is a safe no-op, no different from today. Only meaningful when `allow_fractional_shares` is
`false` (there's nothing to pool when shares are never floored to whole numbers in the first
place). This changes the SHARE COUNT actually submitted, not `money_invested`/
`pct_money_invested`/`rank`/`signal_score`/`stop_loss_price` on the affected order, which
continue to describe the TARGET allocation model, not the final adjusted share count.

## Liquidity / Universe Filter

`core/functions_quant_extensions.py`'s `liquidity_filter()` existed, fully coded, since before
this was wired in, but had zero production call sites, only a research-notebook reference. It
zeroes a ticker's RANK (not its score) on any date its trailing average dollar volume falls
below `min_avg_dollar_volume`, so `nsmallest()`-based selection naturally skips it, the ticker
can never be picked into `top_n` at all that rebalance. This is a PRE-selection eligibility
filter, distinct from `max_pct_of_adv` (a POST-selection advisory warning that never blocks a
pick, just flags it after the fact).

```yaml
risk_overrides:
  use_liquidity_filter: true
  min_avg_dollar_volume: 1000000.0   # default
  liquidity_lookback_days: 63        # default, ~3 months
```

**LIVE + BACKTEST parity**: wired into both `execution/live_signal.py`'s `run()` (volume
fetched via the existing `fetch_ohlcv_for_tickers()`, one call per ticker) and
`core/strategy_signals.py`'s `generate_strategy_monthly_picks()` (a new `daily_volume` param,
historical volume you supply, since a backtest has no live fetch to call). Unlike the
fundamentals point-in-time-bias case documented elsewhere in this project, historical volume
genuinely exists and using it here is NOT a look-ahead risk, so enabling `use_liquidity_filter`
in a backtest WITHOUT passing `daily_volume` raises a loud `ValueError` naming the missing
requirement, rather than silently skipping the constraint.

**A real, confirmed caveat, not glossed over**: this filter operates on RANKS. Every
`strategy_type` selects via the shared cross-sectional `nsmallest()`-equivalent
(`resolve_strategy_picks()`) EXCEPT `absolute_momentum`, whose
`select_absolute_momentum_picks()` selects by each ticker's OWN trailing score directly, never
consulting rank at all. An illiquid ticker with positive absolute momentum is **not** excluded
under that one `strategy_type` today. If you run `absolute_momentum` and need a liquidity
constraint too, that combination isn't covered by this feature yet.

A ticker excluded by this filter appears in the rebalance email's "Full Signal Universe" table
and `logs/signal_rankings_log_<portfolio>.csv` as `"Excluded (Illiquid)"` (`action = "EXCLUDED"`,
distinct from `"Watchlist / Reserve"`, see Epic 2 of the "Rebalance Reporting Clarity &
Selection-Logic Fixes" plan) with a blank `Momentum Rank`, an accurate reflection of "excluded
for illiquidity," not silently invisible.

**A second real, confirmed bug, found and fixed via that same epic's real-deployed-code
verification**: `resolve_strategy_picks()`/`get_top_etfs()` previously called `nsmallest(top_n)`
directly on the (possibly NaN-containing) ranks, and `pandas.Series.nsmallest(n)` backfills with
NaN rows when fewer than `n` non-null values exist, so a liquidity-filtered ticker could still
get selected into `top_n` whenever fewer than `top_n` tickers had a valid rank, e.g. every
ticker in a small portfolio getting filtered at once. Both functions now call `.dropna()` before
`.nsmallest()`, guaranteeing a filtered ticker can never be selected, correctly returning FEWER
than `top_n` picks (down to zero, holding cash) rather than padding with invalid ones.

## Technical-Indicator Entry Confirmation [New] [`use_technical_confirmation`]

Motivated by a real gap found reviewing IBKR's Quant "Momentum Trading: Types, Strategies, and
More" Parts I & II against this codebase: `core/technical_indicators.py` computes
SMA/EMA/RSI/MACD, but before this feature existed they were used ONLY for the email report's
indicator section, never to gate or time a selection decision, entirely disconnected from the
signal-generation/ranking pipeline. This closes that gap: an opt-in **hard gate** (excludes a
ticker from selection, same convention as `skip_month_guardrail`/`use_absolute_momentum`/
`use_negative_universe_cash_filter`, default off, changes what gets traded only when explicitly
enabled), not an advisory-only annotation.

Three independently-optional sub-checks, close-price-only (no volume/high/low needed, so this
works identically in LIVE and BACKTEST off the same `daily_prices` panel every scorer already
uses, full parity by construction, unlike `hybrid_multi_factor`'s fundamentals, which has no
point-in-time historical source and is LIVE-ONLY as a result). A ticker must pass EVERY enabled
sub-check to remain eligible on a given date:

| Sub-check | Rule | Rationale |
|---|---|---|
| `technical_confirmation_min_sma_window` | close > `SMA(window)` | Trend confirmation, Part II's SMA-crossover framing: only enter a name whose price is above its own trailing trend line. |
| `technical_confirmation_max_rsi` | `RSI(14) <= max_rsi` | Excludes an already-overbought/extended ticker AT ENTRY (Part I/II's overbought definition, `RSI > 70`), a momentum winner that has run too far too fast is a real short-term-reversal risk this filter lets you avoid buying into. |
| `technical_confirmation_require_macd_bullish` | MACD line > signal line | A second, independent trend-confirmation signal. |

```yaml
risk_overrides:
  use_technical_confirmation: true
  technical_confirmation_max_rsi: 70.0        # avoid buying an already-overbought winner
  # technical_confirmation_min_sma_window: 50   # optional, additive
  # technical_confirmation_require_macd_bullish: true   # optional, additive
```

`__post_init__` requires at least one sub-check set when the toggle is `true` (fails loud, same
precedent as `hybrid_multi_factor`'s `NotImplementedError`/`use_liquidity_filter`'s missing-
`daily_volume` `ValueError`), an enabled-but-empty gate would be a silent no-op otherwise.

**Wiring, same rank-NaN'ing point as the Liquidity / Universe Filter above** (applied
immediately after it, in `execution/live_signal.py`'s `run()` and `core/strategy_signals.py`'s
`generate_strategy_monthly_picks()`): `core/functions_quant_extensions.py`'s
`technical_confirmation_filter()` zeroes a failing ticker's RANK (not its score), so
`nsmallest()`-based selection naturally skips it, the same "excluded from selection, not from the
universe" mechanism `liquidity_filter()` uses. A ticker without enough trailing history yet to
compute an enabled indicator is treated as not-yet-eligible (a `NaN` comparison is `False`), same
safe-default precedent as the liquidity filter's own early-history handling.

A ticker excluded by this filter appears in the rebalance email's "Full Signal Universe" table
and `logs/signal_rankings_log_<portfolio>.csv` as `"Excluded (Technical)"` (`action =
"EXCLUDED"`), distinguishable from `"Excluded (Illiquid)"`/`"Excluded (Negative Momentum)"`/
`"Watchlist / Reserve"`, via the same pre/post-rank snapshot technique
`pre_liquidity_ranks_row` already uses (a new `pre_technical_ranks_row`, captured after the
liquidity filter but before this one, so the two exclusion reasons are never conflated when both
are enabled at once).

## Volume-Confirmed Signal Quality [New] [`use_volume_confirmation`]

The last of the four gaps found reviewing IBKR's Quant "Momentum Trading" Parts I & II against
this codebase: Part I states plainly that rising volume confirms a momentum move. The Liquidity
/ Universe Filter above already exists, but it checks something different: an ABSOLUTE
dollar-volume THRESHOLD (tradability, "can I actually execute this size"). Nothing previously
checked the RELATIVE volume TREND, whether a ticker's own participation is confirming its price
move or merely coasting on stale, declining interest. A ticker can easily pass the liquidity
filter (plenty of absolute dollar volume) while failing this one (that volume is flat or
declining, not rising to confirm the move).

**Formula**: the trailing `volume_confirmation_lookback_days` window (default `20`) splits into
two EQUAL halves, the RECENT half (closest to the rebalance date) and the EARLIER half
(immediately before it). A ticker remains eligible only if
`recent_half_avg_volume / earlier_half_avg_volume >= volume_confirmation_min_ratio`.
`min_ratio: 1.0` (the default) requires at least flat-or-rising participation; a higher value
demands an accelerating volume trend.

```yaml
risk_overrides:
  use_volume_confirmation: true
  volume_confirmation_lookback_days: 20   # default, split into two 10-day halves
  volume_confirmation_min_ratio: 1.0      # default, at least flat-or-rising participation
```

**LIVE + BACKTEST parity, and zero new plumbing on the live side**: `core/functions_quant_
extensions.py`'s `volume_confirmation_filter()` (a new function, same "zero out ineligible
(ticker, date) ranks" shape as `liquidity_filter()`) is applied at the same rank-NaN'ing point,
right after the Technical-Indicator Entry Confirmation filter. On the LIVE side
(`execution/live_signal.py`'s `run()`), it reuses the EXACT SAME `fetch_ohlcv_for_tickers()`
volume fetch already gated for `use_liquidity_filter`, now gated on `use_liquidity_filter OR
use_volume_confirmation`, so enabling this feature costs no additional API call when the
liquidity filter is also on, and only one extra fetch (same call, same shape) when it's the only
one enabled. On the BACKTEST side (`core/strategy_signals.py`'s
`generate_strategy_monthly_picks()`), it reuses the SAME `daily_volume` parameter already
threaded through for the liquidity filter; enabling `use_volume_confirmation` WITHOUT supplying
`daily_volume` raises a loud `ValueError`, same "historical volume genuinely exists, fail loud
rather than silently skip" precedent the liquidity filter and `hybrid_multi_factor` both
established.

A ticker excluded by this filter appears in the rebalance email's "Full Signal Universe" table
and `logs/signal_rankings_log_<portfolio>.csv` as `"Excluded (Low Volume Confirmation)"`
(`action = "EXCLUDED"`), distinguishable from every other exclusion reason via a third
pre/post-rank snapshot (`pre_volume_ranks_row`, captured after the technical-confirmation filter
but before this one), the same chained-snapshot technique now used for all three opt-in
selection filters, so a ticker excluded by more than one enabled filter is always attributed to
whichever ran FIRST in the pipeline (liquidity, then technical, then volume), never conflated.

## Regime Filter: Volatility Dimension

The pre-existing regime filter (see "Absolute Momentum (Macro)" above) only ever looked at ONE
dimension: `regime_benchmark`'s price relative to its own `regime_sma_window`-day SMA, a pure
trend check. A market can be bullish by that measure and still be violently, dangerously
volatile (a classic "melt-up before the crash" pattern), and the SMA-only filter would never
throttle exposure for that. `regime_vol_threshold` (default `None`, opt-in) blends in a second,
genuinely different dimension: the benchmark's own trailing realized volatility.

```yaml
risk_overrides:
  regime_vol_threshold: 0.25         # e.g. throttle if SPY's realized vol exceeds 25% annualized
  regime_vol_lookback_days: 21       # default, ~1 month
```

`None` (the default) is byte-identical to the pre-existing SMA-only behavior, this is purely
additive, not a replacement.

**Blended formula, identical in both live and backtest** (same "live and backtest must not
diverge" principle every other regime/vol mechanism in this codebase already follows):

```
bearish_by_sma = benchmark below its regime_sma_window-day SMA
high_vol        = benchmark's trailing realized_vol (regime_vol_lookback_days window,
                   annualized) exceeds regime_vol_threshold
regime_scalar   = min_gross_exposure if (bearish_by_sma or high_vol) else 1.0
```

Still ONE scalar, composed multiplicatively with `vol_scalar` exactly as before
(`gross_exposure = min(max_gross_exposure, regime_scalar * vol_scalar)`), a smooth exposure
throttle, not a new hard binary gate. The two dimensions are OR'd together: either one alone is
enough to push the book defensive, neither silently overrides the other.

- **Backtest**: `run_risk_managed_backtest()` (`backtest/momentum_backtest.py`) precomputes a
  `regime_high_vol` boolean series (the benchmark's rolling realized vol vs. the threshold,
  reindexed to the price panel) alongside the pre-existing `regime_bullish` series, evaluated
  per rebalance date inside the same loop that already reads `regime_bullish`.
- **Live**: `execution/live_signal.py`'s `compute_target_weights()` computes the benchmark's
  trailing realized vol directly from `daily_prices` (same "trailing data, not a simulated
  ledger" pattern the portfolio-level Volatility Scaling section above already uses).

**`MARKET_VOLATILITY_REGIME_DEFENSIVE`** (WARNING, logged via `log_alert()` live /
`log_file.write()` in the backtest) fires only when volatility ALONE, not the SMA trend, is what
pushed `regime_scalar` defensive, i.e. `high_vol` is true while `bearish_by_sma` is false. A
purely trend-driven defensive scalar (the pre-existing case) does not get this alert, it's
already visible via the existing "Regime filter: ... below its SMA" log line, this alert exists
specifically to surface the NEW case that previously had no signal at all.

**Real verification example** (live paper-account run, 2026-07-21, deliberately using an
unrealistically low `regime_vol_threshold: 0.001` to force the condition): SPY was confirmed
above its 150D SMA (bullish by trend) with a trailing realized vol of `11.95%` over the default
21-day lookback, far exceeding the `0.10%` threshold. The regime filter logged `Regime filter:
SPY is above its 150D SMA, realized_vol=11.95% (threshold=0.10%) -> scalar=0.20`, the
`MARKET_VOLATILITY_REGIME_DEFENSIVE` alert fired (`SPY realized vol 11.95% exceeds threshold
0.10%; reducing exposure to 20%`), `Gross exposure: 20.0%` propagated all the way through to
real order sizing, and real IBKR paper orders were placed at that throttled size (`BUY 5 EFA`,
`BUY 7 EEM`), not the 100% exposure an SMA-only check would have allowed.

## Momentum-Crash-Specific Dynamic Scaling [New, LIVE + BACKTEST, opt-in] [`momentum_crash_lookback_days`/`momentum_crash_derate`]

Daniel & Moskowitz's 2016 paper "Momentum Crashes" identifies a narrower, more dangerous regime
than "elevated volatility alone" (the section above): momentum's real crash risk is a market that
has been in a **sustained prior downturn AND is volatile at the same time** (past losers,
excluded from a long-only momentum book, violently rebound during exactly this joint regime, the
worst case for a momentum strategy). The volatility-dimension regime filter above already floors
exposure whenever `high_vol` is true, alone, regardless of the trend, so an AND-condition
requiring the SAME `high_vol` flag and clamping to the SAME `min_gross_exposure` floor would add
literally nothing, a real design trap found and avoided while building this (see `CLAUDE.md`'s
`backtest/momentum_backtest.py` bullet for the full trace). Instead, this is an ADDITIONAL
multiplicative derate stacked on top of `regime_scalar * vol_scalar`, able to push exposure
BELOW `min_gross_exposure` specifically during this one empirically-worse joint regime, which
nothing else in this codebase can do.

```yaml
risk_overrides:
  regime_vol_threshold: 0.25              # required prerequisite, reused not duplicated
  momentum_crash_lookback_days: 504       # ~24 months, Daniel & Moskowitz's own empirical choice
  momentum_crash_derate: 0.5              # an EXTRA 50% cut on top of regime_scalar * vol_scalar
```

`None` (the default for `momentum_crash_lookback_days`) is byte-identical to before this existed.
Requires `regime_vol_threshold` to also be set (validated at load time, fail loud): the vol half
of the joint condition reuses that same signal rather than a duplicate threshold field.

**Formula, identical in both live and backtest**:

```
bear_now     = benchmark's trailing return over momentum_crash_lookback_days is negative
high_vol_now = regime_vol_threshold's own elevated-vol signal (same one, not duplicated)
momentum_crash_scalar = momentum_crash_derate if (bear_now and high_vol_now) else 1.0
gross_exposure = min(max_gross_exposure, regime_scalar * vol_scalar * momentum_crash_scalar)
```

**`MOMENTUM_CRASH_PROTECTION_ACTIVE`** (WARNING, `log_alert()` live / `log_file.write()` in the
backtest) fires only when this joint condition specifically is active, distinct from
`MARKET_VOLATILITY_REGIME_DEFENSIVE` (which can fire from `high_vol` alone, an unrelated cause).

**Two real, confirmed gaps found and fixed while validating this against real 2008/2020/2022
historical data**, not synthetic:
1. `compute_required_lookback_days()` (`execution/live_signal.py`) didn't include
   `momentum_crash_lookback_days` in its candidates, so a live fetch could be far too narrow
   (e.g. the default 400 days against a 504-day lookback need), silently meaning this feature
   could never fire live, the identical silent-NaN failure mode that function exists to prevent
   for every other consumer. Fixed.
2. A deeper, previously-latent issue in `run_risk_managed_backtest()`: the regime/vol precompute
   (`regime_bullish`/`regime_high_vol`, and now `momentum_crash_bear`) sourced from the
   already-simulation-window-masked price panel, not the caller's full `daily_prices`, so a long
   lookback could be entirely `NaN` even with plenty more real history available. Fixed by
   sourcing from the unmasked panel instead, purely additive (byte-identical whenever the caller
   already provided enough buffer), also benefits the SMA/vol dimensions above, just less
   visibly since their windows are smaller.

**Real validation results** (`notebooks/research/crash_period_stress_test.ipynb`, 12 backtests,
both a monthly and weekly regime, `momentum_crash_lookback_days=504`/`derate=0.5`/
`regime_vol_threshold=0.25`, run 2026-08-04 after both fixes above): the condition fired 9 times
(2008 GFC, monthly), 39 times (2008 GFC, weekly), 1 time (2020 COVID, monthly), and 2 times (2020
COVID, weekly); zero times in the slower, non-crash-shaped 2022 decline. When it fired, it
modestly improved max drawdown at a modest cost to total return, an honest, mixed result, not a
clean win. This specific 3-way comparison also can't fully isolate the marginal effect of
`momentum_crash_derate` alone (the "full + protection" variant necessarily also turns on
`regime_vol_threshold` as a prerequisite, a confound the deterministic unit tests
(`TestMomentumCrashDynamicScaling`, `tests/execution/test_live_signal.py`) don't have, those
isolate the joint condition against an identical `regime_vol_threshold` baseline). See
`README.md`'s Known Gaps for the full results table.

**Real paper-account regression test** (2026-08-04, `portfolio2`, port 7497): confirmed
end-to-end order generation/execution with these fields active, no crashes, orders correctly
computed and submitted. Surfaced a new, previously-uncatalogued IBKR informational code (`2109`,
"Outside Regular Trading Hours is ignored... PlaceOrder is now being processed"), confirmed
non-fatal (an order carrying it filled normally with a real `execDetails`/`commissionReport` in
that same run) and added to `IBKR_INFORMATIONAL_CODES`.

## Position Size Hard-Cap [Mandatory tier]

`max_position_weight` (default `0.35`): a flat, single-name cap, identical for every ticker
regardless of its own volatility, "no position may exceed this fraction of the book." Was
already fully implemented before this plan, this section just gives it the explicit, named
documentation entry it hadn't had (it previously only appeared as a `config.example.yaml`
comment, not a documented constraint in its own right).

Implemented by `_apply_position_caps()` (`backtest/momentum_backtest.py`), an iterative
cap-and-redistribute pass (not a full LP solve): any ticker over the cap is clamped to it, the
excess is redistributed proportionally across every under-cap ticker, repeated up to 10 passes,
then renormalized to sum to `1.0` **only if that redistribution fully succeeded**. Applied
unconditionally inside `resolve_target_weights()` (the single shared sizing function both the
backtest engine and `execution/live_signal.py`'s `compute_target_weights()` call), so the cap is
genuinely identical live and backtested, not a parallel reimplementation. Applies even when
`custom_weights` is supplied (a hand-specified allocation can still get capped, see
`TestResolveTargetWeights::test_custom_weights_capped_when_infeasible`'s documented edge case:
when the cap makes the requested split mathematically infeasible, e.g. 2 assets and a 0.35 cap
can sum to at most 0.70).

**A real, confirmed bug, fixed via Epic 4 of the "Rebalance Reporting Clarity &
Selection-Logic Fixes" plan**: when there's genuinely no ticker under the cap left to absorb the
excess (a single-ticker portfolio hitting the cap, or every picked ticker simultaneously over
cap, like the infeasible-split example above), the OLD code renormalized anyway, silently
rescaling the just-capped ticker(s) back up past the cap (a single ticker capped to `0.35`
ended up back at `1.0`, and the two-asset infeasible-split example above ended up at an equal
`0.5`/`0.5` split, both defeating the cap this constraint exists to enforce). Fixed: when
redistribution can't fully complete, the weights are returned AS CAPPED, summing to LESS than
`1.0` (`0.70` for the two examples above), the shortfall is left as genuinely unallocated
capital/cash for that rebalance, not silently invested anyway. One real downstream consequence,
worth knowing: `generate_orders()`'s documented invariant ("`money_invested` totals exactly
`total_value * gross_exposure`") now only holds when the cap never has to leave a shortfall;
when it does, the sum is correspondingly smaller, by design, not a bug.

Your own tier description's 5-10% example is achievable by simply setting a tighter
`max_position_weight`, this isn't a missing feature, the default (`0.35`) is just a looser
starting point. `position_vol_budget` (Allow/disallow constraints above) is a complementary,
NOT redundant, per-ticker cap applied AFTER this flat one, varying by each ticker's own
volatility rather than being identical for every ticker.

**A second real, confirmed bug in this same function, found and fixed while adding Risk-Based
Position Sizing below**: the final renormalize targeted a hardcoded `1.0`, not the input's own
pre-cap total. Every PRE-EXISTING `sizing_method` already sums to `~1.0` before this function
ever runs, so that was invisible until `"risk_based"` sizing's DELIBERATELY-often-under-`1.0`
input exposed it: even when NO ticker was anywhere near the cap (nothing to cap at all), the old
code still silently rescaled the whole book back up to `1.0`, defeating `risk_based` sizing's
entire point. Fixed: the renormalize now targets `original_total` (the input's own sum before
this function ran), byte-identical to the old hardcoded `1.0` for every sizing method whose
input already summed to `1.0`, and correctly a no-op for `risk_based`'s genuinely-under-invested
input.

## Risk-Based ("Fixed-Fractional") Position Sizing [New] [`sizing_method: risk_based`]

Motivated by the same IBKR Quant "Momentum Trading" review that motivated Technical-Indicator
Entry Confirmation above: Part I's explicit sizing rule, "risk only 1-2% of capital per trade,"
standard CTA/Van Tharp fixed-fractional practice. Confirmed by reading `resolve_target_weights()`
before this existed: the three pre-existing `sizing_method`s (`inverse_vol`, `score_proportional`,
`equal_weight`) size by trailing volatility, score strength, or count, never by a position's own
stop-loss distance, so this genuinely closes a gap, not a re-implementation of something already
possible.

**Formula**: `weight = risk_per_trade_pct / that ticker's own resolved stop-loss width`
(`resolve_ticker_stop_loss_pct()`, honoring `ticker_risk_overrides` the same way
`check_and_handle_stop_losses()` already does). Sizes the position so a full stop-out loses
exactly `risk_per_trade_pct` of total capital: a TIGHTER stop gets a LARGER weight (less room to
the stop, so more shares needed to risk the same dollar amount), a WIDER stop gets a smaller
weight. A pick whose stop-loss is disabled for that ticker (`ticker_risk_overrides[t]['enabled']
= false`) has no stop distance to size against, falls back to an equal-weight `1/N` slice for
just that one ticker, same per-ticker-fallback precedent `score_proportional`'s missing-score
case already uses.

```yaml
risk_overrides:
  sizing_method: risk_based
  risk_per_trade_pct: 0.02        # risk 2% of capital per trade (Part I's "1-2%" rule)
  stop_loss_pct: 0.10             # this position's own stop-loss distance (or a per-ticker
                                   # ticker_risk_overrides entry), what the formula sizes against
```

**Deliberately does NOT normalize to sum to `1.0`**, unlike the other three `sizing_method`s: real
fixed-fractional sizing lets aggregate exposure emerge from the risk budget, not force full
investment. Several tight-stop picks can push the raw sum ABOVE `1.0` (scaled down proportionally,
preserving each position's RELATIVE risk allocation, so the portfolio never exceeds 100%
invested); several wide-stop picks can leave it genuinely BELOW `1.0` (left as real, unallocated
cash for that rebalance, same "leave undistributable weight as cash" precedent
`_apply_position_caps()`'s `redistribution_incomplete` and `_apply_sector_caps()` already
establish). `resolve_target_weights()`'s shared `_apply_position_caps()`/`position_vol_budget`/
`max_sector_weight` pipeline still applies afterward exactly as it does for every other
`sizing_method`.

**LIVE + BACKTEST parity by construction, zero extra work**: `_risk_based_weights()`
(`backtest/momentum_backtest.py`) flows through the SAME `resolve_target_weights()` both
`execution/live_signal.py`'s `compute_target_weights()` and the backtest engine already share,
unlike several other constraints in this document that needed separate live/backtest wiring.
`resolve_ticker_stop_loss_pct()` itself now also lives in `backtest/momentum_backtest.py` (moved
from `execution/live_signal.py`, which imports and re-exports it for its own call sites and
`daily_runner.py`'s import), a pure function of `BacktestConfig` needed here without introducing
a `backtest/` -> `execution/` import, which would be circular (`execution/live_signal.py` already
imports `BacktestConfig`/`resolve_target_weights`/etc. FROM `backtest/momentum_backtest.py` at
module load time).

## Sector / Asset-Class Concentration Cap [New, Nice-to-Have tier, LIVE + BACKTEST, opt-in]

`max_position_weight` and `position_vol_budget` above only ever constrain a SINGLE ticker's
weight. Nothing previously stopped several picks that all happen to sit in the SAME sector (e.g.
`XLK` + `QQQ`, both effectively "Technology") from combining into an outsized aggregate exposure
even when each individually respects its own per-ticker cap, standard hedge-fund/prop-desk
practice caps this dimension separately from single-name risk, and this project didn't.

`ticker_sectors` (`{}` default, e.g. `{"XLK": "Technology", "QQQ": "Technology", "XLE":
"Energy"}`) + `max_sector_weight` (`None` default) close this gap. This is a **manual mapping**,
deliberately: this project has no vendor integration for ETF sector/asset-class data today
(`core/fundamentals.py` covers P/E, PEG, ROE, Debt-to-Equity, Current Ratio, not sector), and
adding one is out of scope here. A ticker absent from `ticker_sectors` is never grouped or capped
by this constraint at all, an incomplete mapping is a safe no-op for the unclassified tickers,
not a silent error, you can classify only the tickers you actually care about capping.

Implemented by `_apply_sector_caps()` (`backtest/momentum_backtest.py`), wired into
`resolve_target_weights()` (the single shared sizing function both the backtest engine and
`execution/live_signal.py`'s `compute_target_weights()` call, so live and backtest can't
diverge) as the LAST step, after `_apply_position_caps()` and the optional
`_apply_volatility_budget_caps()`, so it constrains the FINAL, fully-capped weights, not an
intermediate state. For any sector whose summed weight exceeds `max_sector_weight`, every
ticker in that sector is scaled down proportionally so the sector sums to exactly the cap.

**Deliberately simpler than `_apply_position_caps()`'s redistribute-to-others logic**: the freed
weight from capping an over-limit sector is left as unallocated gross exposure (cash), NOT
redistributed into other tickers or sectors. This is a conscious, conservative design choice, not
an oversight: redistributing that excess could push a DIFFERENT ticker over ITS OWN
`max_position_weight`, or push a DIFFERENT sector over ITS OWN cap, a multi-constraint
interaction `_apply_position_caps()`'s own single-dimension redistribution never has to reason
about. Same "reduce exposure rather than silently violate a cap" precedent that function's own
`redistribution_incomplete` bug fix established.

Worked example, `max_sector_weight: 0.30`, two Technology picks each independently sized to
`0.35` by inverse-vol weighting (summing to `0.70`, well over the cap):

| Ticker | Sector | Pre-cap weight | Post-cap weight |
|---|---|---|---|
| XLK | Technology | 0.35 | 0.15 |
| QQQ | Technology | 0.35 | 0.15 |
| GLD | (unmapped) | 0.30 | 0.30 (untouched) |

Technology's combined weight is scaled from `0.70` down to exactly `0.30` (each ticker halved,
since they started equal), `GLD` (no `ticker_sectors` entry) is completely unaffected, and the
freed `0.40` is left unallocated, the portfolio's total invested weight drops from `1.00` to
`0.60` for this rebalance, by design.

## Factor Risk Exposure Caps [New, Epic 3, LIVE + BACKTEST, opt-in]

`ticker_factor_loadings` (`{}` default, e.g. `{"XLK": {"tech_beta": 1.2, "growth": 0.8}}`) +
`max_factor_exposure` (`None` default, e.g. `{"tech_beta": 0.5}`) cap the SUMMED EXPOSURE (not
just summed weight, see below) of every ticker with a declared loading for the same factor. The
Sector / Asset-Class Concentration Cap above only ever groups tickers by binary membership (a
ticker either IS in a sector or isn't); factor caps generalize this to a continuous, per-ticker
WEIGHTED contribution, `exposure = sum(weight[t] * loading[t][factor])` across every ticker with
a declared loading for that factor, so two tickers with different degrees of exposure to the same
factor (e.g. `1.2` vs. `0.5` "tech_beta") contribute proportionally, not as an all-or-nothing
group membership.

**Manually-declared loadings only, same honest precedent as `ticker_sectors`**: no factor model,
no vendor beta/style-factor return data source exists anywhere in this project
(`core/fundamentals.py` covers P/E, PEG, ROE, Debt-to-Equity, Current Ratio, not factor
loadings), and adding a regression-based factor model is out of scope here. A ticker absent from
`ticker_factor_loadings`, or one without a loading for a particular factor, is never
grouped/capped for that factor at all, an incomplete mapping is a safe no-op, not a silent error,
matching `ticker_sectors`'s own documented convention exactly.

Implemented by `_apply_factor_caps()` (`backtest/momentum_backtest.py`, structurally identical to
`_apply_sector_caps()`), wired into `resolve_target_weights()` as the NEW LAST step, after
`_apply_sector_caps()`: the most aggregate/composite constraint runs last, constraining the
FINAL, fully-capped weights (position caps → vol-budget caps → sector caps → factor caps). Same
"reduce exposure rather than silently violate a cap" precedent: the freed weight from capping an
over-limit factor is left as unallocated gross exposure (cash), NOT redistributed.

```yaml
risk_overrides:
  ticker_factor_loadings:
    XLK: {tech_beta: 1.2}
    QQQ: {tech_beta: 1.0}
    XLE: {energy_beta: 1.0}
  max_factor_exposure:
    tech_beta: 0.5    # e.g. throttle combined tech-factor exposure to 50% of the book
```

**No upper bound of `1.0` on `max_factor_exposure`'s cap values, unlike `max_sector_weight`'s
`(0, 1.0]` range**: a sector weight is a portfolio-weight SUM, naturally bounded by `1.0`; factor
EXPOSURE is `sum(weight * loading)`, and a loading itself can exceed `1.0` (e.g. a leveraged-style
factor proxy), so exposure has no natural weight-sum ceiling the way a sector-membership sum does.
Validated `> 0` only.

**A real, honestly documented simplification, found and decided during implementation, not
glossed over**: the proportional-scaling algorithm treats every ticker with a NONZERO loading for
an over-cap factor uniformly, INCLUDING one with a NEGATIVE loading (i.e. already reducing net
exposure to that factor, e.g. a hedge/offsetting proxy). A more sophisticated constrained solve
could leave a negative-loading position untouched and only trim positive contributors, but
`_apply_factor_caps()` deliberately keeps the exact same simple, predictable algorithm
`_apply_sector_caps()` already uses (and this codebase's users already understand from that
feature) rather than introducing a second, more complex capping algorithm for one feature. Pinned
explicitly by a dedicated test
(`TestApplyFactorCaps::test_negative_loading_ticker_is_also_scaled_documented_simplification`) so
a future change to this behavior is a deliberate decision, not an accidental regression.

A separate, real edge case confirmed during implementation (caught by manual testing before the
formal test suite was even written): a ticker with an EXPLICIT `{"factor": 0.0}` entry must be
treated identically to one absent from the mapping entirely, not scaled just because the factor
KEY is present in its loadings dict. The implementation checks the loading VALUE is nonzero, not
just key membership (`ticker_factor_loadings.get(t, {}).get(factor, 0.0) != 0`), pinned by
`TestApplyFactorCaps::test_explicit_zero_loading_is_never_touched`.

**Epic 3 real verification (run 2026-08-05), honestly reported**: full pytest suite 1032 passed
(up from 1014 pre-Epic-3, +18 new tests), zero regressions. Real end-to-end paper-account
confirmation (port 7497), both natively and inside a Docker container rebuilt with this epic's
code: a throwaway 3-ticker test portfolio (`JPM`/`BAC`/`WFC`, deliberately disjoint from this
project's real `config.yaml` portfolios, per the discipline established after Epic 2's real
cross-config ticker-overlap incident) with all three tickers loaded `1.0` on a `bank_beta`
factor capped at `0.3`. Confirmed real: the natural inverse-vol target weights summed well above
`0.3`, and the ACTUAL post-cap weights summed to exactly `0.30` (`BAC 0.1134 + JPM 0.0985 + WFC
0.0882`), then real BUY fills confirmed via `execDetails` (`5 BAC @ $63.35`, `2 WFC @ $89.50`,
`JPM` correctly held at 0 shares, its capped allocation floored below 1 share). Docker reproduced
the identical capped weights (`0.1133/0.0985/0.0882`, matching to within real-time price-fetch
noise) and, since the container run happened after the native run had already established the
real position, correctly read back real broker state and HELD rather than duplicate-buying
(`WFC HOLD ... within drift_threshold (2.9%)`), a real, unplanned bonus confirmation that
position-aware idempotency works correctly end-to-end, not just in the common "fresh portfolio"
case Epic 1/2's own verifications exercised.

## Drawdown Circuit Breaker [Recommended tier]

Three now-distinct loss-protection layers exist, worth telling apart clearly:

| Layer | Scope | Halts | Config |
|---|---|---|---|
| `risk/circuit_breaker.py`'s `check_circuit_breaker()` (pre-existing) | PER-PORTFOLIO, that portfolio's own peak equity | Only that one portfolio's new entries (does NOT force-liquidate existing positions) | `default_risk.max_portfolio_drawdown_pct` / `max_dollar_drawdown` (per-portfolio, `risk_overrides` can differ per portfolio) |
| `check_account_wide_drawdown_breaker()` (this constraint, new) | ACCOUNT-WIDE, one peak for the SUM of every portfolio's resolved capital | EVERY portfolio sharing the real IBKR account at once | top-level `account_wide_max_drawdown_pct` (account-scoped, not per-portfolio) |
| `risk/risk_monitor.py` (pre-existing, independent process) | PER-PORTFOLIO, realized loss only (not peak-relative drawdown) | That one portfolio | `--max-loss-pct` CLI flag, separately scheduled |

The account-wide breaker reuses the EXACT SAME halt-flag mechanism the other two already use
(`circuit_breaker_halted_<name>.flag`, one file per portfolio), writing it for every portfolio
in the account when tripped, so `daily_runner.py`'s existing per-portfolio rebalance gate needs
no new code path to respect it, and resuming still uses the existing
`daily-runner --resume-trading <name>`, called once per affected portfolio, per the "no new
resume mechanism" design goal.

**A real, pre-existing bug was found and fixed while building this**: `check_circuit_breaker()`
used to skip its own `halt_path.exists()` check ENTIRELY whenever the CALLING portfolio's own
`max_portfolio_drawdown_pct`/`max_dollar_drawdown` were both at their shipped defaults
(`0.0`/`null`, the common case), an early-return optimization that predates this account-wide
feature. This meant a halt flag written by ANY external source, `risk_monitor.py`'s
`write_halt_flag()` (its entire documented purpose), an email-commanded PAUSE, or now this new
account-wide breaker, was SILENTLY IGNORED by the rebalance gate for any portfolio that hadn't
separately opted into its own per-portfolio drawdown breaker. Confirmed by direct reproduction
before the fix (`check_circuit_breaker()` returned `False` despite the flag file existing on
disk), fixed by checking `halt_path.exists()` FIRST, unconditionally, before the "both breakers
disabled" early return, see `check_circuit_breaker()`'s own updated docstring/comments and
`TestCircuitBreaker::test_externally_written_halt_flag_is_respected_even_with_breaker_disabled`.
This was a genuine safety gap in already-shipped functionality (`risk_monitor.py`'s halting,
email PAUSE), not something newly introduced by this constraint, it was only DISCOVERED while
adding this constraint's own halt-flag reuse.

**Independent peak tracking, deliberately**: the account-wide peak (`data/peak_equity___account__.txt`)
is a SEPARATE file from any portfolio's own `peak_equity_<name>.txt`, so resuming one portfolio
via `resume_trading(name)` does NOT reset the account-wide peak. If the account's real capital
hasn't actually recovered above the tripped threshold, this breaker will re-trip and re-halt
every portfolio again on the next run, even ones just individually resumed, a genuine
capital-preservation kill-switch property, not a bug. Delete
`data/peak_equity___account__.txt` manually (no code path does this automatically) to force a
fresh account-wide peak baseline despite an unrecovered loss, only as a deliberate, reviewed
decision.

**Real-crash validation (Epic 13, `notebooks/research/crash_period_stress_test.ipynb`)**: run
against a real 2008 GFC / 2020 COVID / 2022 Bear replay (long-history ETF proxy universe, since
most currently-configured tickers didn't exist in 2008), `max_portfolio_drawdown_pct: 0.20` never
tripped in any of 12 real backtest runs. Not a bug: the regime filter + volatility targeting
combination alone kept every real-crash drawdown well inside that threshold (worst case -26.0%
only in the naive baseline with the breaker deliberately disabled; the risk-managed variant's
worst real drawdown was -9.2%), so in every scenario tested here the circuit breaker functioned
correctly as an untriggered last-resort backstop, not the primary defense. See `README.md`'s
Known Gaps entry for the full 6-scenario results table.

## Correlation Monitor [Recommended tier]

Already fully implemented and live-wired before this plan, this section just gives it the
explicit, named documentation entry it hadn't had (`use_correlation_spike_regime`, default
`false`, previously only described in a `config.example.yaml` comment).

`detect_correlation_spike()` (`backtest/momentum_backtest.py`) compares a SHORT recent window's
average pairwise correlation across the priced ticker universe against a longer baseline
window (`correlation_spike_short_window`/`correlation_spike_baseline_window`, defaults `7`/`63`
trading days), the classic "in a real crash, normally-uncorrelated assets suddenly move
together" signature, built to react faster than a single long rolling-average window would.
Returns `True` when the short-window average exceeds the baseline by more than
`correlation_spike_threshold` (default `0.3`, a 30-percentage-point jump).

When triggered: logs a WARNING, writes a `CORRELATION_SPIKE_DETECTED` alert, and automatically
clamps gross exposure down to `min_gross_exposure`, the SAME defensive de-risking action
`use_regime_filter`'s bearish-trend case takes, composing with it via `min()` (whichever signal
is more defensive wins). Implemented identically in the backtest (`run_risk_managed_backtest()`)
and live (`execution/live_signal.py`'s `compute_target_weights()`), reusing the exact same
`detect_correlation_spike()` function, live and backtest can't diverge on the detection logic.

**Honest scope, worth understanding before relying on it**: this fires only on an actual
scheduled rebalance (once per cycle, via `compute_target_weights()`), not continuously between
rebalances, and it's scoped to the portfolio's whole CONFIGURED ticker universe (whatever
`daily_prices` covers that rebalance), not narrowly to just currently-held positions. A spike
among tickers you're not currently holding, but are still ranked/priced for the next pick
cycle, can still trigger it, that's intentional (a genuinely diversifying-in-name-only universe
is worth flagging even before you hold the correlated names), but distinct from a literal
"only my open positions" reading of the tier description.

## Shrinkage Covariance Estimator [New, Epic 1, LIVE + BACKTEST, opt-in]

`use_shrinkage_covariance` (default `false`, only meaningful when `use_correlation_penalty` is
also `true`): the Correlation Monitor above and `_correlation_penalty_weights()`'s own sizing-time
correlation penalty both, confirmed by reading the code directly, compute their correlation
matrix via raw `pandas.DataFrame.corr()`, with no regularization at all. A raw sample covariance
(and the correlation matrix implied by it) is poorly conditioned exactly when the number of
return observations (`correlation_lookback_days`, default `63`) is small relative to the number
of tickers being compared, the textbook motivating case for shrinkage estimation, and exactly the
condition Equal Risk Contribution sizing (below) needs a well-behaved matrix for.

`core/covariance.py`'s `shrinkage_covariance(returns, shrinkage=None)` shrinks the raw sample
covariance toward a constant-correlation target (Ledoit & Wolf 2004's target choice); an explicit
`shrinkage=0.0` degenerates to the raw sample covariance exactly (the regression anchor proving
this can reduce to today's pre-existing behavior). `shrinkage=None` (the default the correlation
penalty uses) computes a practical analytic shrinkage intensity, a simplified variant of Ledoit &
Wolf's original formula, honestly documented as such in the function's own docstring, not
presented as a research-grade replication of the paper, sufficient for conditioning a covariance
matrix for portfolio construction. `_correlation_penalty_weights()` converts the shrunk covariance
back to a correlation matrix before applying the exact same downweighting formula it already
used, this feature changes HOW the correlation matrix is estimated, not WHAT the penalty does
with it.

```yaml
risk_overrides:
  use_correlation_penalty: true
  use_shrinkage_covariance: true    # opt-in, requires use_correlation_penalty above
```

New, focused module (`core/covariance.py`), not folded into `core/functions_quant_extensions.py`,
matching this project's existing precedent of giving a new pure-numerical domain its own file
(`core/technical_indicators.py`). Reused directly by Equal Risk Contribution sizing below (the
covariance input a risk-parity solve needs), not duplicated.

## Equal Risk Contribution (ERC) Sizing [New, Epic 1, LIVE + BACKTEST]

`sizing_method: "equal_risk_contribution"`, a 5th legal value alongside the pre-existing
`inverse_vol`/`score_proportional`/`equal_weight`/`risk_based`: sizes each position so every
pick contributes an EQUAL share of total portfolio risk (the standard risk-parity construction),
rather than an equal dollar amount (`equal_weight`) or a size inversely proportional to a
position's OWN volatility alone (`inverse_vol`, which ignores cross-asset correlation entirely).
Uses `shrinkage_covariance()` above as its covariance input directly (not raw sample covariance),
the textbook motivating case for shrinkage: a risk-parity solve is sensitive to a poorly
conditioned covariance matrix in exactly the few-observations/many-tickers regime this project's
typical `correlation_lookback_days`/portfolio-size combination produces.

Selected purely by `sizing_method`'s own existing "pick by name" mechanism, the same way
`"risk_based"` needs no separate `use_risk_based_sizing: bool` today, deliberately NOT gated via
`enabled_risk_strategies` (see that section above for why). When `use_correlation_penalty` is
also enabled, the correlation penalty still applies on top of ERC's own risk-balanced weights,
same as every other `sizing_method`, consistent rather than special-cased. A degenerate case (1-2
picks, an ill-posed covariance solve) falls back to `inverse_vol` sizing for that rebalance, same
"graceful fallback for an ill-posed case" precedent `score_proportional`'s own missing-scores
fallback already establishes.

```yaml
risk_overrides:
  sizing_method: equal_risk_contribution
```

Shared by both engines through `resolve_target_weights()`, the same single-source-of-truth
function every other `sizing_method` already goes through, live and backtest cannot diverge on
ERC's weights by construction.

## VaR/CVaR Budget (Active Pre-Trade Constraint) [New, Epic 1, LIVE + BACKTEST, opt-in]

`enabled_risk_strategies: [var_cvar_budget]` (see "Opt-in overlay strategies" above) plus
`var_budget_pct` (required when enabled): closes a real, confirmed gap found during this epic's
own audit, `core/functions_quant_extensions.py`'s `historical_var_cvar()` (historical/
non-parametric Value-at-Risk and Conditional VaR / Expected Shortfall) existed, fully coded,
before this epic, but was wired into nothing, its only 2 callers in the entire repository were
`tests/test_governance.py`, confirmed by grep, zero references in `execution/live_signal.py`,
`backtest/momentum_backtest.py`, or `daily_runner.py`. This is the exact "scaffold exists, never
wired in" pattern this project has closed before (Epic 15's `run_walk_forward_lookback_search()`,
Epic 6's `absolute_momentum_overlay()`).

Implemented as a new multiplicative gross-exposure scalar, `compute_var_cvar_scalar(cvar_pct,
var_budget_pct, min_gross_exposure, max_gross_exposure)` (`backtest/momentum_backtest.py`,
co-located with `compute_vol_scalar()`, exact same shape: `clip(var_budget_pct / cvar_pct, min,
max)`, falling back to `max_gross_exposure` when there isn't enough return history yet to compute
a real CVaR), composed alongside `vol_scalar`/`momentum_crash_scalar`, NOT as a step inside
`resolve_target_weights()`. That function operates purely in weight-space and has no concept of a
realized portfolio-returns series to compute VaR/CVaR from, unlike `target_portfolio_vol`'s
`vol_scalar`, which each engine already sources independently:

- **Backtest**: sourced from the simulated `portfolio_history` equity curve's trailing
  `var_cvar_lookback_days` (default `252`, ~1 year) returns, the same source
  `_realized_portfolio_vol()` already uses for `vol_scalar` -- the real thing that happened in
  the simulation.
- **Live**: sourced from a weighted-returns series built from trailing `daily_prices` at the
  just-resolved target weights (no simulated equity curve exists live), via a new shared
  `_weighted_portfolio_returns_series()` helper extracted from `_realized_weighted_portfolio_vol()`'s
  existing body, so the two live-side scalars can never silently disagree on "what counts as the
  portfolio's returns."

This preserves the existing, deliberate backtest/live realized-vol sourcing asymmetry
`target_portfolio_vol` already documents, rather than forcing a false equivalence between a real
simulated ledger and a live proxy. `gross_exposure = min(max_gross_exposure, regime_scalar *
vol_scalar * momentum_crash_scalar * var_cvar_scalar)` in each engine's OWN existing composition
order (backtest and live compute their scalars in a different literal sequence today, a real
parity trap this project has hit before with other scalars, see `CLAUDE.md`'s
`compute_target_weights()` bullet). A new `VAR_CVAR_BUDGET_EXCEEDED` `WARNING` (`log_alert()`)
fires when the scalar meaningfully throttles exposure, same pattern as
`MARKET_VOLATILITY_REGIME_DEFENSIVE`.

```yaml
risk_overrides:
  enabled_risk_strategies:
    - var_cvar_budget
  var_budget_pct: 0.05          # required when enabled, e.g. 0.05 = throttle exposure once
                                 # 95%-confidence CVaR would risk more than 5% of capital
  var_cvar_confidence: 0.95     # default
  var_cvar_lookback_days: 252   # default, ~1 year
```

## Liquidity-Adjusted Position Sizing (Active) [New, Epic 1, LIVE + BACKTEST, opt-in]

`enabled_risk_strategies: [liquidity_adjusted_sizing]` (see "Opt-in overlay strategies" above):
distinct from, and additive to, TWO pre-existing liquidity mechanisms, neither of which this
feature replaces or changes:
- `use_liquidity_filter` is a binary in/out RANK filter (a ticker below
  `min_avg_dollar_volume` can't be selected into `top_n` at all).
- `max_pct_of_adv`/`check_capacity()` is purely ADVISORY, confirmed by reading the code: it runs
  strictly AFTER `generate_orders()` has already computed final share counts, and only logs a
  `CAPACITY WARNING`, it never mutates an order's size. This advisory check is left completely
  unchanged; a portfolio not opting into this new feature sees zero behavior change.

This feature adds a genuinely ACTIVE, continuous scaling path: `core/functions_quant_extensions.py`'s
new `scale_dollar_targets_for_capacity()` reuses `check_capacity()`'s own ADV-dollar-volume
formula internally (not a second, divergent formula), and scales down any ticker's target dollar
allocation that would exceed `max_pct_of_adv * adv_dollar` to exactly that ceiling; a ticker under
the cap is unchanged. Freed capacity is left as unallocated cash, not redistributed to other
picks, the same "reduce exposure rather than silently violate a cap" precedent
`_apply_sector_caps()` already established.

Applied downstream of `resolve_target_weights()`, in each caller's own dollar-space code (right
where `target_dollar = total_value * gross_exposure * weight` is already computed), not inside
the shared weight-space sizing function, which has no concept of total deployable capital by
design. A future maintainer should NOT "simplify" this by pushing it into
`resolve_target_weights()`, doing so would require widening that single-source-of-truth
function's signature for every existing caller across both engines.

```yaml
risk_overrides:
  enabled_risk_strategies:
    - liquidity_adjusted_sizing
  max_pct_of_adv: 0.05             # reused from the pre-existing advisory check, now ALSO the
                                    # active scaling ceiling once this overlay is enabled
  liquidity_lookback_days: 63      # reused, ~3 months
  ticker_risk_overrides:
    XLE:
      max_pct_of_adv: 0.10         # per-ticker override, same dict-of-optional-keys mechanism
                                    # as stop_loss_pct/enabled above, a ticker absent from this
                                    # dict uses the portfolio-wide max_pct_of_adv unchanged
```

## Liquidity/Slippage Monitor [Nice-to-Have tier]

`max_bid_ask_spread_pct` (default `None`, disabled): a PRE-trade real-time bid-ask spread
check, distinct from two pre-existing execution-safety checks this project already had,
neither of which uses a real-time quote:
- `check_slippage_tolerance()` (POST-trade): compares the actual IBKR fill price against the
  last daily close, after the order already executed, an alert-only check, it can never un-fill.
- `check_capacity()` (`core/functions_quant_extensions.py`, pre-trade): flags an order size
  exceeding `max_pct_of_adv` of a ticker's HISTORICAL average daily dollar volume, a
  market-impact proxy, not a live spread.

`fetch_bid_ask_spread()` (`execution/live_signal.py`) opens a real-time IBKR `reqMktData()`
subscription for BID(1)/ASK(2) tick types, timeout-bounded (default `5.0`s). `compute_spread_pct()`
is the pure math half (`(ask - bid) / midpoint`), factored out so it's unit-testable without a
real connection, the same "pure math separated from I/O" precedent `check_slippage_tolerance()`
already established. Wired into `place_orders_ibkr()`: when `max_bid_ask_spread_pct` is set,
called once per ticker right before submission; a spread wider than the threshold drops the
order (`DROPPED_WIDE_SPREAD`, the same `dropped_orders` merge pattern as `DROPPED_FRACTIONAL`/
`DROPPED_INSUFFICIENT_CASH`, so it still shows up in the rebalance summary email's "What
Actually Happened" column) instead of submitting it.

**Real operational dependency, stated plainly, the same honesty this project applies to the
fractional-share IBKR limitation**: real-time NBBO for US stocks/ETFs is NOT included on IBKR's
free/delayed-data tier, confirmed against IBKR's own documentation. Without a live, paid
real-time market-data subscription for the relevant exchange, `fetch_bid_ask_spread()` will
time out or receive stale/frozen ticks and return `None`. A `None` quote is deliberately treated
as "couldn't check," NOT as "spread is wide," so the order still proceeds rather than being
silently blocked by an unrelated data-feed gap, see `docs/DEPLOYMENT.md`'s IBKR troubleshooting
section. `None` (the default) makes ZERO new IBKR calls, byte-identical to before this feature
existed. LIVE-ONLY, dry-run never opens an IBKR connection at all, consistent with every other
IBKR-dependent check in this codebase.

## Cost-vs-Edge Hurdle Filter [Nice-to-Have tier]

`cost_edge_hurdle_multiplier` (default `None`, disabled, Epic 4 of the "Institutional
Risk-Management Features" plan) is a SEPARATE, ADDITIVE relative check, gated by plain
field-presence exactly like `max_bid_ask_spread_pct` (not `enabled_risk_strategies`, the two
gating conventions this project uses are documented under "Opt-in overlay strategies" below).
`max_bid_ask_spread_pct` remains an unchanged, independent ABSOLUTE spread ceiling; this field
instead asks a RELATIVE question: is the estimated round-trip transaction cost worth paying for
the signal strength behind this specific trade?

`should_drop_for_cost_edge_hurdle(spread_pct, signal_score, multiplier)` (`execution/live_signal.py`,
pure, unit-tested without a mocked IBKR connection) estimates round-trip cost as `2 * spread_pct`
(pay the spread once entering, once exiting) and drops the order when that exceeds
`multiplier * abs(signal_score)`. `signal_score` is a momentum-strength PROXY, not a guaranteed
forward return, the same honest caveat this project already applies to momentum scores
elsewhere (see `docs/STRATEGY_THEORY.md`); a stronger score tolerates a wider spread before the
trade is rejected as not worth its transaction cost. `signal_score is None` (a hand-built
exit order from one of `daily_runner.py`'s three auto-exit checks, none of which construct that
key, an existing, pre-Epic-4 scope boundary this epic deliberately does not change, see Design
Decision 5 in the epic's own plan) means "can't evaluate the hurdle," never an automatic drop,
the same "couldn't check, treat as fine" precedent `fetch_bid_ask_spread()`'s own `None`-quote
handling already establishes.

Wired into `place_orders_ibkr()`'s SAME per-ticker real-time quote fetch the spread gate already
uses (`fetch_bid_ask_spread()`, restructured to fire once whenever EITHER
`max_bid_ask_spread_pct is not None` OR `cost_edge_hurdle_multiplier is not None`, one quote
fetch per ticker regardless of how many of the two checks are enabled): the spread gate runs
first (unchanged behavior/log lines when only it is set), the hurdle check second, only reached
if the spread gate didn't already drop the order. A hurdle-triggered drop
(`DROPPED_COST_EXCEEDS_EDGE`, the same `dropped_orders` merge pattern as `DROPPED_WIDE_SPREAD`)
fires a matching `COST_EXCEEDS_EDGE` `WARNING` alert. Only wired at the rebalance call site
(`run()`), matching `max_bid_ask_spread_pct`'s own existing real scope, and a real necessity, not
just consistency: the three `daily_runner.py` auto-exit paths' hand-built order dicts have no
`signal_score` at all, so the hurdle would always no-op there regardless. `None` (the default)
is byte-identical to before this feature existed, zero new IBKR calls, same real operational
dependency (a live, paid real-time market-data subscription) as `max_bid_ask_spread_pct` above.

**Real verification, run 2026-08-05**: a throwaway single-ticker paper portfolio (`JPM`,
ticker-disjoint from every real `config.yaml` portfolio) with `cost_edge_hurdle_multiplier: 1000`
(deliberately extreme, to force a drop against even a tiny real spread) generated a real BUY
signal and reached the quote-fetch gate as designed. The real outcome was honest, not a clean
"drop confirmed": this paper account has no real-time market-data subscription (confirmed via a
real IBKR error `10168`, "Requested market data is not subscribed"), so `fetch_bid_ask_spread()`
correctly returned `None`, both the spread gate and the hurdle check correctly treated that as
"couldn't check," and the order submitted and filled normally (`execDetails` confirmed, `1 JPM @
$360.80`). The DROP path itself is verified by the mocked-IBKR test suite
(`TestCostEdgeHurdleGate`), not by this live run, the same real, pre-existing operational
dependency `max_bid_ask_spread_pct` above has always had, not a new gap introduced by this
feature.

## Execution Participation-Rate Limits [Nice-to-Have tier]

`use_participation_rate_limit` (default `False`) + `max_participation_pct` (required, `(0,
1.0]`, when the toggle is `True`), Epic 4 of the "Institutional Risk-Management Features" plan,
LIVE-ONLY, plain-bool-gated (same convention as `attach_broker_stop_loss`/
`attach_broker_trailing_stop`, deliberately NOT `enabled_risk_strategies`).

Delegates entirely to IBKR's own native `PctVol` execution algo, set directly on the PARENT
order object in `place_orders_ibkr()` (`ib_order.algoStrategy = "PctVol"`, `ib_order.algoParams
= [TagValue("pctVol", str(max_participation_pct))]`), not a bracket child, no
`parentId`/`transmit` linkage needed, it's an execution-style attribute on the order itself.
Minimal viable parameter set (`pctVol` only); IBKR's own `AvailableAlgoParams.
FillPctVolParams()` sample helper also supports optional `startTime`/`endTime`/`noTakeLiq` tags,
deliberately not included in this first version, out of scope.

**Deliberately NOT local TWAP/multi-run slicing**: this app is a stateless once-daily cron job
(`daily_runner.py`, `docker-entrypoint.sh`'s `DAILY_RUNNER_CRON`) with no intraday scheduling
loop to build real slicing against, IBKR's own algo handles the real-time in-session execution
instead, a considered choice, not a limitation stumbled into.

**Real verification, run 2026-08-05, TWO real bugs found and fixed via live paper-account
testing, neither caught by the mocked-IBKR test suite alone**:

1. **Wrong algo param tag name.** The first implementation used `TagValue("maxPctVol", ...)`
   (matching this project's own field name, `max_participation_pct`), a real, confirmed guess
   that turned out wrong: a real submission to a real paper account was rejected outright
   (`IBKR error 443: Order processing failed. Unknown algo attribute:maxPctVol`). IBKR's actual
   documented tag for this algo is `pctVol`, not `maxPctVol`. Fixed by changing the `TagValue`'s
   tag string; the project's own field name (`max_participation_pct`) is unchanged, only the
   internal IBKR wire-protocol tag was wrong.
2. **RTH/algo-order incompatibility.** After fixing (1), a second real submission (this time with
   `allow_extended_hours: true` also set, since the test ran after market close) was again
   rejected (`IBKR error 201: Order rejected - reason:Only RTH orders are allowed for IB
   algorithmic orders`), a genuine IBKR constraint not documented in this project's own plan in
   advance: an `algoStrategy`-bearing order cannot be combined with `outsideRth=True`. Fixed in
   `place_orders_ibkr()`: the extended-hours LMT/`outsideRth` conversion block is now skipped
   entirely whenever `use_participation_rate_limit` is set, falling through to a plain RTH-only
   `MKT` order (still carrying the `PctVol` algo), which correctly queues at the broker until the
   next session opens, the exact same "no reference price" fallback shape this function already
   had, not a new code path. Pinned by
   `TestParticipationRateLimit::test_combined_with_extended_hours_falls_back_to_rth_only_order`.

After both fixes, a real resubmission (native AND inside Docker, both port 7497) was **accepted**
by IBKR with no error (informational code `399`, "will not be placed until the next session,"
the same pre-existing, already-documented queuing behavior any RTH-only order gets after hours),
confirming the order object itself (`algoStrategy="PctVol"`, `algoParams=[pctVol=0.1]`) is
well-formed and accepted by a real broker connection. **Disclosed limitation, stated honestly,
not glossed over**: both live tests ran after regular trading hours, so the actual INTRADAY
percentage-of-volume fill behavior (whether IBKR's algo genuinely paces the fill against
real-time volume as configured) was never observed, only that submission itself succeeds. This
remains unconfirmed until exercised during a live RTH session, a real, open verification gap for
future work, not something this epic's testing window could close. Whether this composes cleanly
with `attach_broker_stop_loss`/`attach_broker_trailing_stop` brackets on the same parent order
was also not exercised in this test round (only a plain, non-bracket order), a second real,
disclosed gap, not assumed to work.

## Hard-to-Borrow (HTB) Sentinel [Nice-to-Have tier]: Not Applicable

Confirmed by an exhaustive full-codebase search, not assumed: this system is strictly
long-only. The only `action` values ever produced anywhere are `"BUY"`, `"SELL"`, and `"HOLD"`
(`execution/live_signal.py`'s `generate_orders()`, and every downstream consumer: order
placement, the order-log CSV schema, FIFO P&L parsing); `SELL` always means closing or reducing
an existing LONG position back toward flat, never opening a short. Every position weight
computed anywhere (`resolve_target_weights()`'s sizing, `_apply_position_caps()`, gross-exposure
scaling) is non-negative by construction, `min_gross_exposure`'s defensive de-risking reduces
exposure toward cash, it never flips to a negative/short weight. No config field, CLI flag, or
IBKR margin/borrow API call (`whatIfOrder`, a shortable-shares check, anything) exists anywhere
in this codebase for opening a short position.

"Ensuring a stock is borrowable before submitting orders" therefore doesn't apply: there is no
short leg for it to protect. This isn't a partially-implemented feature missing a piece, it's a
tier item this system's design doesn't need. If short-selling were ever added to this project
(a much larger undertaking than any other constraint in this document, out of scope here), HTB
checking would need to be built from scratch, no scaffolding for it exists today.

## Recommended Config Presets

These are two starting-point `default_risk` presets, one long-term (monthly), one short-term
(weekly), each cross-checked against every rule above and confirmed warning-free at its own
values. They tune `daily_runner.py`'s LIVE signal generation only, `lookback_period`,
`holding_period`, and `skip_month_guardrail` are all confirmed no-ops in the backtest engine
(`run_custom_backtest()` consumes pre-computed `monthly_picks`, it never ranks tickers itself),
so neither preset explains or predicts any backtest chart's result, that's a separate question,
governed entirely by `top_n`/`holding_period` at picks-generation time, not by these fields.

Both presets below also cover the newer Mandatory/Nice-to-Have tier fields from "The full
risk-strategy tier map" above (`target_portfolio_vol`, `portfolio_vol_lookback`,
`use_absolute_momentum`, `defensive_ticker`, `max_bid_ask_spread_pct`), all `default_risk`-scoped
like every other field here. `account_wide_max_drawdown_pct` is deliberately NOT in either preset
block: it's a TOP-LEVEL, account-scoped field, not per-portfolio/per-regime, see the note after
both presets below. `attach_broker_stop_loss` is likewise NOT in either preset block below,
deliberately: unlike `holding_period`/`lookback_period`/`target_portfolio_vol`, its
recommendation doesn't vary by cadence, it's `false` (default, no bracket order) unless you
specifically want IBKR-native, broker-side stop-loss protection regardless of whether this app
is running, set `attach_broker_stop_loss: true` in EITHER regime's preset the same way, reusing
whichever `stop_loss_pct` that preset already specifies, see "Broker-Side Protective Stop" above
for the full rationale (including why it's belt-and-suspenders alongside
`auto_execute_stop_loss`, not a replacement for it).

### Long-Term Momentum (Monthly)

```yaml
holding_period: 1               # monthly rebalance
lookback_period: 12             # classic 12-month trailing momentum window
skip_month_guardrail: false     # matches config.example.yaml's shipped default, tune
                                 # lookback_period/holding_period directly instead of
                                 # relying on this guardrail
max_turnover_pct: 0.20          # default
position_vol_budget: null       # default, optional
target_portfolio_vol: 0.15      # default, the standard portfolio-level vol target
portfolio_vol_lookback: 21      # default, ~1 month of trading days
use_absolute_momentum: false    # shipped default, opt-in, a real signal-construction change
defensive_ticker: BIL           # only relevant if use_absolute_momentum is enabled above
max_bid_ask_spread_pct: null    # default, disabled (requires a live, paid real-time
                                 # market-data subscription, see docs/DEPLOYMENT.md)
top_n: 10
sizing_method: inverse_vol
max_position_weight: 0.35
stop_loss_pct: 0.18             # room to breathe for a monthly-cadence position, wider than
                                 # the shipped 0.12 default; still fixed-from-entry, not
                                 # trailing, see "Stop-Loss Width" above
auto_execute_stop_loss: false   # opt-in, see "Stop-Loss Width" above for why this remains a
                                 # per-portfolio choice regardless of regime
```

| Field | Value | Why |
|---|---|---|
| `holding_period` | `1` | Monthly rebalance, the academically-studied cadence |
| `lookback_period` | `12` | Classic 3-12 month momentum window, `Jegadeesh and Titman (1993)` |
| Momentum Persistence | `12 > 1` | Passes, the signal is far older than the holding window |
| Lookback-to-Hold Ratio | `12 / 1 = 12` | At the top of the roughly 3-12 recommended band, not below 3, no warning |
| `skip_month_guardrail` | `false` | Shipped default, an opt-in change to signal construction, not enabled by default here either |
| `target_portfolio_vol` | `0.15` | The standard, unmodified default, no reason specific to the monthly regime to tighten it |
| `portfolio_vol_lookback` | `21` | ~1 month, matches the monthly rebalance cadence's own natural timescale |
| `use_absolute_momentum` | `false` | Same opt-in precedent as `skip_month_guardrail`, a real signal-construction change, not enabled by default here either |
| `max_bid_ask_spread_pct` | `null` | Disabled by default, real-time market data is a real operational dependency (paid subscription), not something to silently assume is available |
| `stop_loss_pct` | `0.18` | Wider fixed stop for the long-term regime, room to breathe through normal pullbacks, see "Stop-Loss Width" above for why this is not a true trailing stop |

`momentum_crash_lookback_days`/`regime_vol_threshold` deliberately left at their `null`
(disabled) defaults in both presets below: the real 2008/2020/2022 validation (see "Momentum-
Crash-Specific Dynamic Scaling" above) showed a modest, honest, mixed result (better max
drawdown, worse total return, when it fires), not a clean win to recommend by default the way
the other fields on this page are. Enable it deliberately, not as a preset default.

### Short-Term Momentum (Weekly)

```yaml
holding_period: 0.25            # weekly rebalance
lookback_period: 1.0            # 4-week momentum window (round(1.0 * 4) weeks)
skip_month_guardrail: false     # confirmed no-op in the weekly regime regardless of value
max_turnover_pct: 0.20          # default, more likely to be visited under weekly cadence,
                                 # that's expected/informational, not a sign of misconfiguration
position_vol_budget: null       # default, optional
target_portfolio_vol: 0.12      # tighter than the monthly preset's 0.15, the weekly regime's
                                 # signal is noisier and unvalidated (see below), a smaller
                                 # aggregate risk budget is the more conservative starting point
portfolio_vol_lookback: 10      # ~2 weeks, shorter than the monthly preset's 21, more
                                 # responsive to fast-changing conditions under a weekly
                                 # rebalance cadence, matching its own faster timescale
use_absolute_momentum: false    # same opt-in precedent as the monthly preset
defensive_ticker: BIL           # only relevant if use_absolute_momentum is enabled above
max_bid_ask_spread_pct: null    # same reasoning as the monthly preset
top_n: 5                        # more concentrated, a shorter lookback carries a noisier signal
sizing_method: inverse_vol      # kept as the safer default under short-term noise
max_position_weight: 0.35
stop_loss_pct: 0.10             # tighter control for the weekly regime's noisier signal, see
                                 # "Stop-Loss Width" above
auto_execute_stop_loss: false   # opt-in, same reasoning as the monthly preset
```

| Field | Value | Why |
|---|---|---|
| `holding_period` | `0.25` | Weekly rebalance |
| `lookback_period` | `1.0` | 4 weeks, chosen above the `2`-week minimum-lookback warning and above the Ratio warning's `3`-week-equivalent floor, unlike a `0.5` (2-week) window, which would trip the Ratio warning |
| Momentum Persistence | `4wk > 1wk` | Passes |
| Lookback-to-Hold Ratio | `4 / 1 = 4` | Above `3`, no warning |
| `top_n` | `5` | A shorter, noisier signal window argues for fewer, higher-conviction picks |
| `target_portfolio_vol` | `0.12` | Tighter than the monthly preset, a more conservative aggregate risk budget given the weekly regime's noisier, unvalidated signal |
| `portfolio_vol_lookback` | `10` | ~2 weeks, more responsive than the monthly preset's 21, matching the weekly cadence's own faster timescale |
| `use_absolute_momentum` | `false` | Same opt-in precedent as the monthly preset |
| `max_bid_ask_spread_pct` | `null` | Same reasoning as the monthly preset |
| `stop_loss_pct` | `0.10` | Tighter control suits the short-term/noisier regime, cuts downside rapidly, see "Stop-Loss Width" above |

**Treat the short-term preset as unvalidated**, same caveat `docs/STRATEGY_THEORY.md` already
states for weekly-scale momentum in general, this is a genuine departure from the 3-12 month
range the academic literature actually studied, warning-free is not the same as
performance-validated for either preset, it only means the values respect this file's own
documented advisory thresholds.

### Account-wide breaker: applies once per account, not per regime

`account_wide_max_drawdown_pct` (top-level, `0.0` = disabled by default) is orthogonal to which
momentum regime any given portfolio in the account uses, one real IBKR account can hold a mix of
long-term and short-term portfolios under a SINGLE account-wide value. There is no "long-term"
vs. "short-term" recommended value for this field the way there is for the regime-scoped fields
above, set it once, based on your own real capital-preservation tolerance for the WHOLE account,
independent of any individual portfolio's cadence. See "Drawdown Circuit Breaker" above.

## Independence from `risk_monitor.py`

Confirmed by reading `risk/risk_monitor.py`'s full contents: it has zero visibility into any of
this. Its only inputs are the trade-log CSV and `portfolios.<name>.total_value` read directly
from `config.yaml`, it never imports `BacktestConfig`, never reads `default_risk`/
`risk_overrides`, and never reads the alerts log these six constraints write to. This is
deliberate, the same "a bug in the trading logic can't also blind the thing watching for it"
segregation principle `CLAUDE.md` already documents for P&L computation, `risk_monitor.py`'s
only job is independently re-derived realized-loss monitoring against `total_value`, not
strategy-configuration review. No conflict is possible today because there's no shared surface
between them.

**Still true after Epics 1-4's institutional risk-practice audit above**: none of the new
`enabled_risk_strategies` overlays, the new `sizing_method: equal_risk_contribution`, the
shrinkage covariance estimator, or any other feature from that audit adds any dependency into
`risk/risk_monitor.py`. Every one of them lives entirely in the `daily_runner.py`/
`execution/live_signal.py`/`backtest/momentum_backtest.py` path, same as every constraint above.
