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

<!-- Epics 2-7 each add their own section below as they land. -->
