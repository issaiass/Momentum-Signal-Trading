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

**Backtest note, an honest scope caveat, not overclaiming**: fixing this at the SELECTION layer
(`resolve_strategy_picks()`) also surfaced and fixed a real, confirmed parity gap in
`generate_strategy_monthly_picks()`: a date where a real "hold cash" decision was made (empty
picks, e.g. from this constraint or the liquidity filter) used to be silently SKIPPED from the
returned `monthly_picks` series entirely, exactly like the "no signal at all yet" case (start of
history, lookback not satisfied), even though it's a genuinely different situation. That skip
meant `run_risk_managed_backtest()`'s `monthly_picks.get(date, [])` lookup for the NEXT
rebalance would silently fall through to a STALE prior period's picks instead of correctly
seeing "nothing was eligible then." Fixed: such a date is now included with an explicit `[]`,
so subsequent lookups see the correct, current decision, not stale data. This does NOT, by
itself, make `run_risk_managed_backtest()` actively LIQUIDATE existing holdings to cash the
moment this constraint triggers, that engine's own rebalance-trigger condition
(`if target_tickers and not circuit_breaker_halted:`) currently treats an empty `target_tickers`
as "nothing to rebalance this period, hold whatever is currently held" rather than "force-sell
everything," a narrower, pre-existing characteristic of the backtest EXECUTION engine itself,
distinct from the SELECTION layer this constraint lives in, deliberately left alone here rather
than risking a larger, unrequested change to a heavily-tested, financially-significant
simulation loop. Live's `execution/live_signal.py`'s `run()` has no such gap, `generate_orders()`
genuinely sells any current holdings to cash immediately once `picks` comes back empty.

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
the gain-locking behavior, that's a genuine trailing stop, which does not exist anywhere in this
codebase today, neither the Python-side `auto_execute_stop_loss` check nor the broker-side
`attach_broker_stop_loss` bracket (IBKR's native `TRAIL` order type exists and would implement
this, but `attach_broker_stop_loss` submits a plain `STP` at a fixed `auxPrice`, not a `TRAIL`
order that IBKR itself would ratchet). Tracked as a real, documented gap, not implemented, see
`README.md`'s Known Gaps.

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
pick, see "Broker-Side Protective Stop" above.

**A per-ticker "Stop-Loss Price" figure is also visible**, in the rebalance email's second "Full
Signal Universe" table and its sibling `logs/signal_rankings_log_<portfolio>.csv`, but despite
the column name, it does NOT report the per-share price described above: it's `Money Invest *
stop_loss_pct`, a DOLLAR AMOUNT AT RISK on the position, for a `BUY` or `HOLD`, see
`docs/SIGNAL_RANKINGS_LOG.md`. This is a deliberate, explicit reporting-layer decision, entirely
separate from the fixed-from-entry mechanism described above: neither `check_and_handle_stop_
losses()`'s daily check nor `place_orders_ibkr()`'s broker-side bracket read this reported value,
both still compute their own real per-share threshold directly from `avg_entry_price`.

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
width, if any, applies to this ticker right now."

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
