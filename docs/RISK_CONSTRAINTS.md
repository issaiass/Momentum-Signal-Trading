# Risk Constraints: Long-Term vs. Short-Term Momentum

> **New to this project?** Start with `../README.md`. This file covers the momentum-strategy
> risk constraints specifically: what each one checks, why, whether it's a non-blocking WARNING
> or an opt-in config toggle, and its exact default.

All six constraints below live entirely in the `daily_runner.py`/`execution/live_signal.py` live
path. None of them are visible to `risk/risk_monitor.py`, that's deliberate, not an oversight,
see "Independence from `risk_monitor.py`" at the bottom of this file.

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

## Recommended Config Presets

These are two starting-point `default_risk` presets, one long-term (monthly), one short-term
(weekly), each cross-checked against every rule above and confirmed warning-free at its own
values. They tune `daily_runner.py`'s LIVE signal generation only, `lookback_period`,
`holding_period`, and `skip_month_guardrail` are all confirmed no-ops in the backtest engine
(`run_custom_backtest()` consumes pre-computed `monthly_picks`, it never ranks tickers itself),
so neither preset explains or predicts any backtest chart's result, that's a separate question,
governed entirely by `top_n`/`holding_period` at picks-generation time, not by these fields.

### Long-Term Momentum (Monthly)

```yaml
holding_period: 1               # monthly rebalance
lookback_period: 12             # classic 12-month trailing momentum window
skip_month_guardrail: false     # matches config.example.yaml's shipped default, tune
                                 # lookback_period/holding_period directly instead of
                                 # relying on this guardrail
max_turnover_pct: 0.20          # default
position_vol_budget: null       # default, optional
top_n: 10
sizing_method: inverse_vol
max_position_weight: 0.35
```

| Field | Value | Why |
|---|---|---|
| `holding_period` | `1` | Monthly rebalance, the academically-studied cadence |
| `lookback_period` | `12` | Classic 3-12 month momentum window, `Jegadeesh and Titman (1993)` |
| Momentum Persistence | `12 > 1` | Passes, the signal is far older than the holding window |
| Lookback-to-Hold Ratio | `12 / 1 = 12` | At the top of the roughly 3-12 recommended band, not below 3, no warning |
| `skip_month_guardrail` | `false` | Shipped default, an opt-in change to signal construction, not enabled by default here either |

### Short-Term Momentum (Weekly)

```yaml
holding_period: 0.25            # weekly rebalance
lookback_period: 1.0            # 4-week momentum window (round(1.0 * 4) weeks)
skip_month_guardrail: false     # confirmed no-op in the weekly regime regardless of value
max_turnover_pct: 0.20          # default, more likely to be visited under weekly cadence,
                                 # that's expected/informational, not a sign of misconfiguration
position_vol_budget: null       # default, optional
top_n: 5                        # more concentrated, a shorter lookback carries a noisier signal
sizing_method: inverse_vol      # kept as the safer default under short-term noise
max_position_weight: 0.35
```

| Field | Value | Why |
|---|---|---|
| `holding_period` | `0.25` | Weekly rebalance |
| `lookback_period` | `1.0` | 4 weeks, chosen above the `2`-week minimum-lookback warning and above the Ratio warning's `3`-week-equivalent floor, unlike a `0.5` (2-week) window, which would trip the Ratio warning |
| Momentum Persistence | `4wk > 1wk` | Passes |
| Lookback-to-Hold Ratio | `4 / 1 = 4` | Above `3`, no warning |
| `top_n` | `5` | A shorter, noisier signal window argues for fewer, higher-conviction picks |

**Treat the short-term preset as unvalidated**, same caveat `docs/STRATEGY_THEORY.md` already
states for weekly-scale momentum in general, this is a genuine departure from the 3-12 month
range the academic literature actually studied, warning-free is not the same as
performance-validated for either preset, it only means the values respect this file's own
documented advisory thresholds.

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
