# Momentum Strategies: What's Selectable, and What Each One Actually Does

> **New to this project?** Start with `../README.md`. This file covers the selectable
> `strategy_type` field specifically: what each named strategy does, exactly which existing
> fields it maps to (or what new logic it dispatches to), and its live/backtest support status.

## Which momentum strategy is implemented by default? Basic signal, advanced risk wrapper

The default (`strategy_type: momentum`, unset also means this) is a textbook, single-factor
**cross-sectional relative momentum** strategy: rank every ticker in the portfolio's universe by
trailing total return over a configurable lookback, hold the top N, rotate periodically. This is
the basic Jegadeesh & Titman (1993) construction, see `docs/STRATEGY_THEORY.md` for the full
theory and a worked example. Everything else in this project (volatility targeting, the regime
filter, circuit breakers, correlation defenses, etc., see `docs/RISK_CONSTRAINTS.md`) is an
ADVANCED risk-management layer wrapped AROUND that basic signal, none of it changes *which*
tickers get picked by default, only *how much* capital is deployed and when.

## Selecting a strategy

Set `strategy_type` under a portfolio's own `risk_overrides` (or `default_risk` for every
portfolio that doesn't override it), same mechanism as every other field in `config.yaml`.
Different portfolios sharing one account can run different strategies independently, this
requires zero new plumbing, `daily_runner.py`'s existing per-portfolio `BacktestConfig`
construction already supports it:

```yaml
portfolios:
  portfolio1:
    tickers: [SPY, QQQ, XLK, XLF]
    risk_overrides:
      strategy_type: dual_momentum
  portfolio2:
    tickers: [XLE, GLD, TLT]
    risk_overrides:
      strategy_type: volatility_scaled_momentum
```

## How presets compose with your own explicit field values

Several strategies below map onto EXISTING `BacktestConfig` fields (`use_absolute_momentum`,
`use_regime_filter`, `sizing_method`, `use_correlation_penalty`). Selecting one of these
`strategy_type` values automatically sets the mapped field(s) for you, via
`daily_runner.py`'s `apply_strategy_type_preset()`, called once per portfolio inside
`load_config()`, BEFORE the portfolio's `BacktestConfig` is built. **If you explicitly set that
specific field yourself** (in the same portfolio's `default_risk`/`risk_overrides`), your value
always wins, the preset only fills in fields you haven't already set. For example:

```yaml
risk_overrides:
  strategy_type: dual_momentum       # implies use_absolute_momentum: true, use_regime_filter: true
  use_absolute_momentum: false       # your explicit value wins, stays false
```

resolves to `use_absolute_momentum: false` (your explicit value) and `use_regime_filter: true`
(the preset, since you didn't set that field yourself).

## The full strategy list

| `strategy_type` value | Table name | Status |
|---|---|---|
| `momentum` (default) | Momentum | The base cross-sectional signal, no preset, changes nothing |
| `relative_momentum` | Relative (Cross-Sectional) | Explicit alias for `momentum`, byte-identical behavior |
| `dual_momentum` | Dual Momentum | Preset: `use_absolute_momentum: true`, `use_regime_filter: true` |
| `volatility_scaled_momentum` | Volatility-Scaled Momentum | Preset: `sizing_method: inverse_vol` (already the default, made explicit) |
| `correlation_weighted_momentum` | Correlation-Weighted | Preset: `use_correlation_penalty: true` |
| `rank_sign_momentum` | Rank & Sign Momentum | Preset: `sizing_method: equal_weight` (Epic 4) |
| `multi_timeframe_composite` | Multi-Timeframe Composite | New ranking function, `blend_momentum_scores()` (Epic 2) |
| `absolute_momentum` | Absolute (Time-Series) | New selection mode, no cross-sectional ranking at all (Epic 3) |
| `residual_momentum` | Residual Momentum | New ranking function, market-model regression (Epic 5) |
| `path_dependent_momentum` | Path-Dependent Momentum | New ranking function, trend-smoothness filter (Epic 6) |
| `hybrid_multi_factor` | Hybrid Multi-Factor | New ranking function, momentum + fundamentals blend, **LIVE-ONLY** (Epic 7) |

## Momentum, Relative (Cross-Sectional) [`momentum` / `relative_momentum`]

The base signal: `execution/live_signal.py`'s `resolve_momentum_scores()` computes trailing
total return per ticker over `lookback_period`, `assign_ranks()` ranks the whole universe by
that return, `get_top_etfs()` takes the top `top_n`. No preset, `relative_momentum` is a purely
cosmetic alias, both resolve identically. Fully implemented, live AND backtest (the backtest
engine's `monthly_picks` input is normally built with this exact same signal, upstream, by a
research notebook or `core/strategy_signals.py`'s `generate_strategy_monthly_picks()`).

## Dual Momentum [`dual_momentum`]

Antonacci-style: blends the base RELATIVE ranking above with an ABSOLUTE (per-ticker,
own-trend) filter and a market-wide trend overlay. Preset sets `use_absolute_momentum: true`
(swaps any pick with a negative own trailing return for `defensive_ticker`, via
`core/functions_quant_extensions.py`'s `absolute_momentum_overlay()`) and `use_regime_filter:
true` (scales the whole book's exposure down when the benchmark, default SPY, is below its own
moving average). Both mechanisms pre-date this plan and are already live-wired; `dual_momentum`
just gives them one named, self-documenting selector. `use_absolute_momentum` is LIVE-ONLY (no
effect in the backtest engine), `use_regime_filter` works in both.

## Volatility-Scaled Momentum [`volatility_scaled_momentum`]

Normalizes risk via position sizing, at BOTH levels this project already implements: per-ticker
inverse-volatility weighting (`sizing_method: inverse_vol`, the preset here, already the
default) and portfolio-level gross-exposure targeting (`target_portfolio_vol`,
`compute_vol_scalar()`, always active regardless of `strategy_type`, not gated by this preset).
Fully implemented, live AND backtest, see `docs/RISK_CONSTRAINTS.md`'s "Volatility Scaling
(Portfolio-Level)" section for the exact formula.

## Correlation-Weighted [`correlation_weighted_momentum`]

Scales exposure based on asset correlation, via a sizing-time correlation PENALTY on
already-selected picks (`_correlation_penalty_weights()`, preset: `use_correlation_penalty:
true`), downweighting picks that are mutually highly correlated with each other this rebalance.
Distinct from the separate, always-available `use_correlation_spike_regime` market-wide
crash-detection overlay (see `docs/RISK_CONSTRAINTS.md`'s "Correlation Monitor"), which is not
part of this preset and can be enabled independently. Fully implemented, live AND backtest, both
via the shared `resolve_target_weights()`.

## Rank & Sign Momentum [`rank_sign_momentum`]

Non-parametric sizing: every held pick gets an identical `1/N` weight, ignoring both raw
momentum score MAGNITUDE (unlike the default `inverse_vol` and `score_proportional`
`sizing_method`s) and trailing volatility. The literal "rank/sign-only" reading, a pick that
barely cleared the ranking cutoff gets the same capital as the strongest-momentum pick, reducing
the influence of any single outlier score. Preset sets `sizing_method: equal_weight`, a new
third `sizing_method` value (`backtest/momentum_backtest.py`'s `_equal_weight_weights()`),
independently usable without selecting this whole `strategy_type` too. Ranking/selection itself
is completely unchanged from the base `momentum` strategy, only sizing differs, so this reuses
`resolve_momentum_scores()` for scoring like every other sizing-only `strategy_type`. Position
caps and the correlation penalty (if enabled) still apply afterward, same as the other two
`sizing_method` values. Fully implemented, live AND backtest (sizing is already the one shared
function both engines call via `resolve_target_weights()`).

## Multi-Timeframe Composite [`multi_timeframe_composite`]

Aligns signals across multiple horizons: blends momentum scores across several lookback windows
(default 3/6/12-month, `multi_timeframe_lookbacks`, equal-weighted unless
`multi_timeframe_weights` is set) into ONE ranking signal, instead of relying on a single
lookback. Rationale (from `blend_momentum_scores()`'s own docstring): shorter windows react
faster to regime changes but are noisier, longer windows are the classic academic momentum
window but react slowly to reversals, a blend is a reasonable middle ground, not a guaranteed
improvement, validate with a real backtest comparison before trusting it over a single lookback.

**This closes a real, previously-undiscovered gap**: `core/functions_quant_extensions.py`'s
`blend_momentum_scores()` was already fully coded, explicitly documented as "drop-in compatible
with `assign_ranks()`/`get_top_etfs()`", but had ZERO production call sites, only exercised by
its own unit test, and the README/`docs/RUNNING.md`/its own docstring all falsely claimed a
"Notebook 2 demo" existed for it (confirmed via repo-wide grep, no such demo ever existed). Now
wired in via `core/strategy_signals.py`'s `resolve_strategy_scores()` router, resamples to
monthly first (the conventional "N-month momentum" meaning `blend_momentum_scores()`'s own
docstring recommends), then blends, feeding the exact same `assign_ranks()`/`get_top_etfs()`
pipeline every other strategy uses. Fully implemented, LIVE and BACKTEST (via
`generate_strategy_monthly_picks()`, the first strategy this plan gave real historical backtest
parity to, not just a live-only preview).

## Absolute (Time-Series) Momentum [`absolute_momentum`]

A genuinely different SELECTION mode, not just a sizing/exposure change: no cross-sectional
ranking and no `top_n` cutoff at all. `core/strategy_signals.py`'s
`select_absolute_momentum_picks()` checks EACH ticker in the portfolio's universe against its
OWN trailing score (the same per-ticker score `resolve_momentum_scores()` already computes,
scoring is unchanged, only selection differs): a strictly positive score holds the ticker, a
zero or negative score does not. If nothing in the universe has a positive score, the whole book
falls back to `defensive_ticker` (already an existing field, reused as-is, no new config needed)
alone. Depending on how many tickers currently have positive momentum, the resulting pick count
can be smaller OR larger than `top_n`, that cutoff simply does not apply to this strategy_type.

This is distinct from two pre-existing, easily-confused mechanisms:
- `use_regime_filter`: ONE benchmark (default SPY) scaling the WHOLE book's exposure, not a
  per-ticker decision.
- `use_absolute_momentum` (the `dual_momentum` preset field): a POST-relative-ranking swap, only
  ever applied to tickers that already survived the cross-sectional `top_n` cutoff, still
  fundamentally a relative-momentum variant underneath.

`resolve_strategy_picks()` (`core/strategy_signals.py`) is the shared centralizing dispatcher
`execution/live_signal.py`'s `run()` and `generate_strategy_monthly_picks()` (backtest) both call,
so live and backtest can never diverge on this "top_n cutoff vs. absolute per-ticker selection"
decision. Fully implemented, LIVE and BACKTEST.

```yaml
risk_overrides:
  strategy_type: absolute_momentum
  defensive_ticker: BIL       # already existed, reused here, holds this alone if nothing is trending up
```

## Residual Momentum [`residual_momentum`]

Ranks tickers by IDIOSYNCRATIC (benchmark-adjusted) trailing return rather than raw total
return, a single-factor market-model residualization (not a full multi-factor model). Per
rebalance date and ticker, `core/strategy_signals.py`'s `resolve_residual_momentum_scores()`
estimates market-model beta via OLS (`np.polyfit`, degree 1) on trailing DAILY returns (ticker
vs `regime_benchmark`, an EXISTING field reused here, no new config needed) over the same
lookback window `resolve_momentum_scores()` already uses, then:

```
residual_score = raw_period_return - beta * raw_benchmark_period_return
```

the portion of the ticker's trailing return NOT explained by its benchmark exposure. A
high-beta ticker whose entire move is explained by tracking the benchmark (e.g. a leveraged
beta=2 ETF in a rising market) scores near zero here even with a LARGE raw return; a low-beta
ticker with genuine idiosyncratic outperformance scores higher, even with a smaller raw return,
that's the entire point of residualizing.

`regime_benchmark` must be priced alongside the portfolio's own tickers for this to work, same
"must be priced" requirement already documented for `defensive_ticker`: either add it to that
portfolio's own `tickers:` list, or (live only) it flows through via `run()`'s
`extra_price_tickers` mechanism if the caller supplies it. A missing benchmark price raises a
clear `ValueError` naming the ticker, rather than silently falling back to raw momentum, unlike
the regime filter's optional no-op, this strategy cannot compute a score AT ALL without its
benchmark.

```yaml
risk_overrides:
  strategy_type: residual_momentum
  regime_benchmark: SPY       # already existed, reused here as the market-model regressor,
                               # must be priced (add to this portfolio's own tickers: list)
```

Fully implemented, LIVE and BACKTEST (`generate_strategy_monthly_picks()`).

## Path-Dependent Momentum [`path_dependent_momentum`]

Rewards a smooth, consistent trend over a choppy/volatile one reaching the same endpoint, the
literal "filters for consistent/smooth trends" reading. Per rebalance date and ticker,
`core/strategy_signals.py`'s `resolve_path_dependent_momentum_scores()` fits a linear trend to
log-price (`np.polyfit`, degree 1) over the same trailing lookback window
`resolve_momentum_scores()` uses, computes that fit's R^2 (a standard "trend quality" measure,
1.0 = perfectly smooth, lower = choppier), then:

```
path_adjusted_score = raw_period_return * trend_r_squared
```

Two tickers with an IDENTICAL raw return over the window but different paths (one climbed
steadily, the other whipsawed to the same endpoint) get different scores here, the smoother one
ranks higher. Purely price-based, no external benchmark needed (unlike `residual_momentum`
above), so this only ever needs the portfolio's own configured tickers.

```yaml
risk_overrides:
  strategy_type: path_dependent_momentum
```

Fully implemented, LIVE and BACKTEST (`generate_strategy_monthly_picks()`).

<!-- Epic 7 adds its own section below as it lands. -->
