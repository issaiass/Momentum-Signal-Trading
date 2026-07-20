# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cross-sectional momentum ETF rotation strategy: research/backtest engine plus a live trading
system (single or multi-portfolio, paper or real IBKR accounts via the TWS API). Installable
package at `src/momentum_trading/`, console script `daily-runner`. See `README.md` for the full
file inventory and folder structure, don't re-derive it, it's kept current there.

**This is unvalidated-strategy software with well-tested infrastructure**, the code mechanics
(circuit breakers, idempotency, audit logging) are solid, and a real IBKR paper connection has
now confirmed the execution mechanics work end-to-end (real BUY and SELL fills, verified
directly in TWS, see `README.md`'s "Project Maturity & Safety" section for exactly what has
and hasn't been exercised, including the live/real-money port). But the momentum *strategy*
itself (whether it has real economic edge) has never been run against real historical
out-of-sample data. Keep that distinction in mind: a passing test suite, or even a confirmed
real fill, says nothing about strategy edge.

## Commands

```bash
# Install (editable, with dev deps for pytest)
uv sync                                    # if using uv (uv.lock present)
pip install -e ".[dev]"                    # or plain pip

# Tests, no network/broker required, all synthetic/mocked data
pytest tests/ -v                           # full suite
pytest tests/test_daily_runner.py -v       # one file
pytest tests/ -k "circuit_breaker" -v      # name pattern
pytest tests/path::TestClass::test_name -v # single test
pytest tests/ -x --tb=short                # stop at first failure, short tracebacks

# Run (config.yaml required, cp config.example.yaml config.yaml first)
daily-runner --test-email                  # live SMTP/IMAP check, no config.yaml needed, run
                                            # this once after editing .env on any machine
daily-runner --force-rebalance             # safe, no broker connection, test signal/order output
daily-runner                               # dry-run default (no --live = never places orders)
daily-runner --live --port 7497            # paper trading
daily-runner --live --port 7496 --confirm-live-trading   # real money, both flags required together
daily-runner --resume-trading <portfolio_name>            # clear a circuit-breaker halt
python -m momentum_trading.risk.risk_monitor --portfolio <name> --max-loss-pct 0.25
    # --initial-capital optional, defaults to portfolios.<name>.total_value in config.yaml

# Docker
docker compose up -d --build
docker exec -it momentum-signal crontab -l              # verify cron schedule
docker exec -it momentum-signal daily-runner --force-rebalance
```

There is no configured linter/formatter (no ruff/black/flake8 config in this repo), don't
assume one.

## Architecture

Domain-separated sub-packages under `src/momentum_trading/`, each with a specific coupling rule
that tests enforce, don't casually violate these when editing:

- **`core/`**, pure data/signal logic, no execution or I/O side effects. `core/paths.py` is the
  single source of truth for where `config.yaml`/`data/`/`logs/` live (env var override →
  walk up for `pyproject.toml` → CWD fallback). Any new module needing the data or logs dir
  should use `data_dir()`/`logs_dir()` from here, not a bare `"data"` string,
  `tests/test_architecture.py::TestPathResolutionAcrossWorkingDirectories` guards this.
  `core/technical_indicators.py` (SMA/EMA/RSI/MACD/ATR/Bollinger/ADX/VWAP/OBV) is hand-rolled,
  not `pandas-ta`, that package hard-pins `numba==0.61.2`, incompatible with this project's
  `pandas>=3.0.3` under `uv sync`'s full dependency resolution (confirmed by direct attempt:
  installs fine standalone, breaks the project lockfile). `core/functions.py`'s
  `trailing_returns()`/`return_period_dates()` (used by the backtest's `tear_sheet()`) raise a
  `KeyError` against a short, live daily-snapshot history, their `"Since Inception"` window's
  lookback routinely falls outside the fetched market-calendar schedule, and the `"M"`-frequency
  branch skips holiday/weekend snapping entirely. Confirmed only ever exercised against full
  multi-year backtest histories before now, `functions_quant_extensions.py`'s
  `since_inception_performance()`/`monthly_window_comparison()`/`daily_window_comparison()`
  (used by the live monthly/daily email reports) deliberately do NOT call `trailing_returns()`
  for this reason; they call the individual `annualize_returns()`/`annualize_vol()`/
  `max_drawdown()`/`sharpe_ratio()`/`sortino_ratio()` functions directly (still reusing them, so
  live and backtested stats can't diverge) or a small dedicated cumulative-growth-index lookback,
  never the monolithic aggregator. Don't route new live-reporting code through `tear_sheet()`
  itself without re-confirming it handles short histories first.
  `core/fundamentals.py` (P/E, PEG, ROE, Debt-to-Equity, Current Ratio) and `core/macro_data.py`
  (Fed Funds Rate, CPI) feed the email reports' Fundamental/Macro sections. Confirmed by live
  testing, not guessed: FMP's `/api/v3/` endpoints are dead (shut down 2025-08-31, return 403
  regardless of subscription), `core/fundamentals.py` uses FMP's `/stable/` endpoints instead
  (`/stable/ratios` + `/stable/key-metrics` for ROE). `core/functions.py`'s `_fetch_fmp()` price
  fetch has the same migration: `/stable/historical-price-eod/full` for raw OHLCV (what
  `execution/live_signal.py`'s `fetch_ohlcv_for_tickers()` needs) plus a second call to
  `/stable/historical-price-eod/dividend-adjusted` merged in for `adjClose` (what
  `get_bulk_prices()`'s momentum-ranking price series needs, unadjusted close would distort
  rankings around ex-dividend dates). The `/stable/` response is a flat list, unlike
  `/api/v3/`'s `{"historical": [...]}` wrapper, don't reintroduce that key lookup. EODHD's
  fundamentals endpoint returns `403 Only EOD data allowed for free users` on
  a free-tier key, implemented as a fallback per EODHD's documented response shape but
  unverified against a real paid response. Both cache to `data/fundamentals_cache.json` (7-day
  TTL) / `data/macro_cache.json` (30-day TTL) since neither data source changes daily; a failed
  fetch is never cached, so a transient outage or a since-added API key doesn't block retrying.
  `core/macro_data.py` needs its own `FRED_API_KEY` (free, `fred.stlouisfed.org`), unset means
  the whole macro section is silently omitted, not an error.
- **`core/strategy_signals.py`** (NEW module, selectable-momentum-strategy plan), dispatches on
  `BacktestConfig.strategy_type` (`config.yaml`'s per-portfolio `default_risk`/`risk_overrides`,
  one of 11 allowed values, `ALLOWED_STRATEGY_TYPES` in `backtest/momentum_backtest.py`, see
  `docs/MOMENTUM_STRATEGIES.md`), the single shared router BOTH `execution/live_signal.py`'s
  `run()` (LIVE) and this file's own `generate_strategy_monthly_picks()` (BACKTEST) call, so live
  and backtest can never diverge on which tickers get selected for a given strategy. Deliberately
  imports `resolve_momentum_scores()`/`assign_ranks()` from `execution/live_signal.py`, a
  documented one-directional exception to `core/`'s usual "no dependency on `execution/`" rule
  (this module's own docstring explains why: reusing the shared resample/skip-month-guardrail
  logic rather than reimplementing it a second time, avoiding exactly the live/backtest
  divergence risk this whole architecture exists to prevent).
  `resolve_strategy_scores(daily_prices, tickers, cfg, lookback_period, fmp_api_key=None,
  eodhd_api_key=None)` is the LIVE-facing router (scores for "today", scopes `daily_prices` to
  `tickers` internally EXCEPT for `residual_momentum`, which needs the wider unscoped
  `daily_prices` since its benchmark is very likely not one of `tickers` itself);
  `generate_strategy_monthly_picks(daily_prices, tickers, cfg, lookback_period, top_n)` is the
  BACKTEST-facing counterpart (a full historical `monthly_picks` series feedable UNCHANGED into
  `run_custom_backtest()`/`run_risk_managed_backtest()`, neither of which needed any change).
  `_BASE_SCORE_STRATEGY_TYPES` (`momentum`, `relative_momentum`, `dual_momentum`,
  `volatility_scaled_momentum`, `correlation_weighted_momentum`, `absolute_momentum`,
  `rank_sign_momentum`) all fall through to the EXISTING `resolve_momentum_scores()` unchanged
  for SCORING, they only affect sizing/exposure (via `daily_runner.py`'s
  `apply_strategy_type_preset()`) or selection (`absolute_momentum`), never ranking.
  `resolve_strategy_picks(scores_row, ranks_row, tickers, cfg, top_n)` centralizes the
  "cross-sectional `top_n` cutoff vs. absolute per-ticker selection" decision, shared by `run()`
  and `generate_strategy_monthly_picks()`: every `strategy_type` except `absolute_momentum`
  replicates `get_top_etfs()`'s exact behavior, `absolute_momentum` delegates to
  `select_absolute_momentum_picks(latest_scores, tickers, defensive_ticker)` (no `top_n` cutoff
  at all, every ticker with a positive OWN trailing score is held, `defensive_ticker` alone
  otherwise, `defensive_ticker` must be priced alongside the portfolio's own `tickers:`, the same
  "must be priced" requirement `dual_momentum`'s `use_absolute_momentum` already documents).
  Four genuinely new ranking functions, one per strategy: `blend_momentum_scores()` (reused
  UNCHANGED from `core/functions_quant_extensions.py`, previously fully coded but dead code, zero
  production call sites before this, for `multi_timeframe_composite`, resamples to monthly FIRST
  then blends across `cfg.multi_timeframe_lookbacks`/`multi_timeframe_weights`);
  `resolve_residual_momentum_scores(daily_prices, tickers, benchmark, lookback_period,
  holding_period)` (`residual_momentum`, market-model OLS beta via `np.polyfit` on trailing DAILY
  returns against `cfg.regime_benchmark`, reused, no new field,
  `residual_score = raw_period_return - beta * raw_benchmark_period_return`, requires the
  benchmark priced in `daily_prices` or raises `ValueError`, unlike the regime filter's silent
  no-op); `resolve_path_dependent_momentum_scores(daily_prices, tickers, lookback_period,
  holding_period)` (`path_dependent_momentum`, linear-trend R² on log-price via `np.polyfit`,
  `path_adjusted_score = raw_period_return * trend_r_squared`, purely price-based, no benchmark
  needed); `resolve_hybrid_multi_factor_scores(daily_prices, tickers, lookback_period,
  holding_period, fundamentals_by_ticker)` (`hybrid_multi_factor`, LIVE-ONLY, blends a momentum
  percentile rank with a Quality/Value composite percentile built from `core/fundamentals.py`'s
  EXISTING P/E, PEG, ROE, Debt-to-Equity, Current Ratio fields via
  `_quality_value_percentile_scores()`, `get_cached_or_fetch_fundamentals()` fetched per ticker
  inside `resolve_strategy_scores()`'s `hybrid_multi_factor` branch, reusing `core/fundamentals.py`
  UNCHANGED). `generate_strategy_monthly_picks()` RAISES `NotImplementedError` for
  `hybrid_multi_factor` (not a silent wrong number), no point-in-time historical fundamentals
  data source exists anywhere in this project or its free-tier vendors, applying today's
  fundamentals across historical dates would silently look-ahead bias a backtest.
- **`backtest/momentum_backtest.py`**, `BacktestConfig` (validated on construction) and
  `resolve_target_weights()`, the sizing logic shared by *both* the backtest engine and live
  execution, specifically so the two paths can't silently diverge. `lookback_period` is LIVE-ONLY
  (mirrors `commission`'s existing BACKTEST-ONLY note, opposite direction), the engine consumes
  pre-computed `monthly_picks`, so this field only affects `daily_runner.py`'s live rebalance loop.
  `holding_period` is a `float`, not just an `int`, values below `1` map onto weeks (`0.25` =
  weekly) via `execution/live_signal.py`'s `is_rebalance_day()`; only `holding_period <= 0` is a
  hard validation error, sub-weekly values (`< 0.25`) are allowed but flagged (see below).
  `lookback_period` is also a `float` now, not an `int`, only `lookback_period <= 0` is a hard
  error. Its granularity is tied to `holding_period`'s regime, not its own value:
  `execution/live_signal.py`'s `resolve_momentum_scores()` interprets `lookback_period` in
  week-quarters (`round(x * 4)`, same formula `is_rebalance_day()` uses for `holding_period`)
  when `holding_period < 1`, or whole months otherwise, this is deliberate, a short-term
  (weekly) strategy's lookback window is expressed on the SAME week-scale as its rebalance
  cadence, not mixed months/weeks, `lookback_period: 1.0` under a weekly `holding_period` means
  "4 weeks", not "1 month". `run()` calls `resolve_momentum_scores()` instead of resampling
  inline, don't reintroduce a hardcoded `resample("ME")` there. `is_lookback_period_too_short()`
  is the sub-2-week advisory warning, mirrors `is_holding_period_too_frequent()`'s non-blocking
  pattern, only meaningful in the weekly regime.
  `is_rebalance_day()` targets the first REAL trading day of the period (monthly or weekly), not
  a fixed calendar date: `mcal.get_calendar(exchange)` (default `"NYSE"`) +
  `cal.schedule(start_date, end_date)` builds the exchange's actual trading-session list for the
  month/week, and the target is whichever date is that schedule's first entry. A weekend/holiday
  is never IN that schedule, so the roll-forward past it happens by construction, there's no
  explicit `if holiday: shift` branch to break. Confirmed test-proven for BOTH branches, not just
  one: `test_default_fires_on_first_trading_day_of_month` (Jan 1 2026 = New Year's Day, resolves
  to Jan 2) and `test_holiday_shifts_the_weekly_target_day` (a Presidents'-Day Monday resolves to
  the following Tuesday). Don't add a separate holiday-check step if editing this, the
  `cal.schedule()` call already IS the holiday check.
  Three new risk-constraint fields, all detailed in `docs/RISK_CONSTRAINTS.md`, don't duplicate
  the full rationale here: `max_turnover_pct` (default `0.20`, the "Turnover Limit"
  position-COUNT ratio, distinct from `drift_threshold`/`aggregate_drift_threshold`'s
  dollar-value drift), `skip_month_guardrail` (default `False`, opt-in, changes
  `resolve_momentum_scores()`'s actual signal when enabled, don't ever default this on without
  an explicit ask), `position_vol_budget` (default `None`, the per-ticker vol-budget cap applied
  in `resolve_target_weights()` via `_apply_volatility_budget_caps()`, AFTER the flat
  `max_position_weight` cap, complementary not redundant with it).
  `compute_vol_scalar(realized_vol, target_portfolio_vol, min_gross_exposure,
  max_gross_exposure)` is the "Volatility Scaling" (Mandatory tier) portfolio-level formula,
  extracted from `run_risk_managed_backtest()`'s previously-inline logic specifically so
  `execution/live_signal.py`'s `compute_target_weights()` can share the identical formula
  (`_realized_weighted_portfolio_vol()` there is the live substitute for this file's
  `_realized_portfolio_vol()`, which needs a simulated `portfolio_history` equity curve that
  doesn't exist live). Before this, portfolio-level vol targeting existed ONLY in the backtest,
  live trading had no aggregate exposure throttling at all. `use_absolute_momentum` (default
  `False`, opt-in, same "changes the actual signal when enabled" caution as
  `skip_month_guardrail`) + `defensive_ticker` (default `"BIL"`) back the "Absolute Momentum
  (Macro)" constraint (Mandatory tier): `core/functions_quant_extensions.py`'s
  `absolute_momentum_overlay()` existed, fully coded, since before this was wired in, but was
  called NOWHERE until `execution/live_signal.py`'s `apply_absolute_momentum_filter()` (a thin
  wrapper reusing it directly) was added; don't reimplement its swap rule a second time.
  `max_bid_ask_spread_pct` (default `None`) backs the "Liquidity/Slippage Monitor" (Nice-to-Have
  tier), threaded through to `execution/live_signal.py`'s `place_orders_ibkr()`, LIVE-ONLY,
  requires a real-time IBKR market-data subscription, see that file's bullet below.
  `strategy_type: str = "momentum"` (`ALLOWED_STRATEGY_TYPES`, 11 values, validated in
  `__post_init__` via the exact `sizing_method` `not in (...)` precedent) selects among the
  momentum strategies documented in `docs/MOMENTUM_STRATEGIES.md`, per-portfolio via
  `config.yaml`'s `default_risk`/`risk_overrides`, dispatched by `core/strategy_signals.py`'s
  `resolve_strategy_scores()`/`generate_strategy_monthly_picks()` (see that file's own bullet
  above). `daily_runner.py`'s `apply_strategy_type_preset()` auto-configures a bundle of EXISTING
  fields for the 4 preset-only `strategy_type`s (`dual_momentum`, `volatility_scaled_momentum`,
  `correlation_weighted_momentum`, `rank_sign_momentum`) BEFORE `BacktestConfig` construction, an
  explicit field value in the portfolio's own config always wins over the preset.
  `multi_timeframe_lookbacks: list = field(default_factory=lambda: [3, 6, 12])` (needed
  `field(default_factory=...)`, a bare mutable list default raises a dataclass error) and
  `multi_timeframe_weights: list | None = None` back `multi_timeframe_composite`.
  `sizing_method` gained a third value, `"equal_weight"` (`_equal_weight_weights(picks)`, every
  pick gets an identical `1/N` weight, ignoring both score magnitude and trailing vol), the
  `rank_sign_momentum` preset's field, independently usable without selecting that
  `strategy_type` too, wired into `resolve_target_weights()` alongside the existing
  `inverse_vol`/`score_proportional` branches, same position-cap/correlation-penalty pipeline
  applied afterward regardless of which of the three is chosen.
- **`execution/live_signal.py`**, live signal/order generation, IBKR integration (`ibapi`
  `EClient`/`EWrapper`, not a third-party wrapper), multi-portfolio orchestration, FIFO P&L,
  hash-chained audit log. `fetch_ohlcv_for_tickers()` is distinct from `fetch_live_prices()`,
  the latter returns close-only prices across many tickers at once (for momentum ranking), the
  former returns per-ticker full OHLCV (for `core/technical_indicators.py`), one
  `get_stock_prices()` call per ticker since `get_bulk_prices()` collapses to close-only.
  IBKR routes informational notices (data-farm status, an auto-set TIF,
  etc.) through the *same* `EWrapper.error()` callback as real errors, `IBKR_INFORMATIONAL_CODES`
  is the single source of truth for which codes are safe to log at `INFO` and, critically, must
  never be allowed to overwrite a tracked order's status to `"ERROR: ..."` (that mistake once
  made a real, filled order get reported as rejected, see `DEPLOYMENT.md`'s IBKR troubleshooting
  sections before adding a new code here or touching `place_orders_ibkr()`'s `error()` callback).
  Also: IBKR's API has no fractional equity/ETF order support at all, ever (not an `ibapi`
  version issue), `place_orders_ibkr()` floors to whole shares at submission time; don't
  reintroduce `cashQty` for `STK` contracts, it doesn't work (confirmed empirically). Orders
  dropped before ever reaching IBKR (flooring to 0 shares, or cash-scaling to 0 shares) never
  get a real orderId, so `_collect_results()` alone would silently omit them, they're tracked
  separately in a `dropped_orders` dict (`DROPPED_FRACTIONAL`/`DROPPED_INSUFFICIENT_CASH`) and
  merged into the returned results, since `interfaces/notifications.py`'s rebalance summary
  email's "What Actually Happened" column depends on every ticker having *some* recorded
  outcome. Any new drop path added to `place_orders_ibkr()` should record into `dropped_orders`
  the same way, not just `continue`.
  `build_position_performance()` feeds the reports' "Position Performance (since entry)" section,
  reuses `avg_entry_price` (already tracked in `current_positions` for
  `check_and_handle_stop_losses()`'s gating) and `derive_entry_date()` (already used by
  `check_and_handle_time_stops()`), both previously computed live and discarded after the
  stop-loss/time-stop check, never surfaced anywhere before this. It's unrealized/mark-to-market
  return on the *currently open* position, distinct from `measure_live_performance()`'s
  aggregate/`per_ticker_realized` P&L (realized+unrealized across the *whole* trade history,
  including closed lots). Only populated in `--live` mode: `current_positions` is `{}` in
  dry-run (`daily_runner.py` never calls `get_ibkr_positions()` without a real connection), so
  this section is empty there, same as Technical/Fundamental Indicators, not a new gap.
  `measure_live_performance()`'s returned dict also includes `open_position_avg_cost` (`{ticker:
  weighted-average cost basis of the currently open lots}`), a 3-line additive read of the same
  `open_lots` FIFO structure the function already builds for realized P&L, added specifically so
  a `current_positions`-shaped dict for `build_position_performance()` can be reconstructed from
  the trade log alone, without a live broker connection. This is what lets
  `notebooks/operational/portfolio_snapshot_report.ipynb` demonstrate real Position Performance
  data safely (dry-run only, no IBKR needed), see that notebook's section 5a. Don't remove this
  key without checking that notebook first.
  Four new pure functions back the `docs/RISK_CONSTRAINTS.md` advisory constraints, all wired
  into `daily_runner.py`'s per-portfolio WARNING checks (same `logger.warning` → `log_alert` →
  `send_action_email(NotificationCategory.WARNING, ...)` triple-step every existing WARNING
  site already uses): `is_lookback_shorter_than_holding()`/`is_lookback_to_holding_ratio_too_low()`
  (Momentum Persistence/Ratio, compare `lookback_period` and `holding_period` in the same
  regime-appropriate unit via the shared `_lookback_and_holding_in_common_unit()` helper, don't
  introduce a second unit-conversion convention if extending these), and
  `compute_turnover()`/`is_turnover_too_high()` (Turnover Limit, computed directly from `run()`'s
  returned `orders` dict, `action` is never overwritten by the `--live` fill-status merge so
  this is always reliable regardless of dry-run/live/dropped/filled state). None of these four,
  or the two config-toggle constraints in `backtest/momentum_backtest.py`, are visible to
  `risk/risk_monitor.py`, that independence is deliberate, see `docs/RISK_CONSTRAINTS.md`'s
  closing section before wiring any of this into the monitor.
  `run()` gained an optional `extra_price_tickers` param (backs `daily_runner.py`'s orphaned-
  ticker reconciliation): widens the internal `fetch_live_prices()` call so `generate_orders()`
  can price/exit a currently-held-but-no-longer-configured ticker, WITHOUT widening the
  momentum ranking/selection universe, `resolve_momentum_scores()` still only ever sees
  `daily_prices[tickers]`, never the extra ones, getting priced must never make a ticker
  re-selectable as a NEW pick. `None` (default) is byte-identical to this function's behavior
  before this param existed.
  `run()` also gained an optional `daily_prices` param, fixing a real, confirmed redundant
  network round-trip: `daily_runner.py`'s "ALWAYS runs" block (stop-loss check + portfolio
  snapshot, runs every day regardless of rebalance schedule) already fetches prices for
  `tickers + confirmed_orphaned` BEFORE deciding whether today is a rebalance day; when it is,
  `run()` was fetching that SAME data a second time internally (identical ticker set once
  `extra_price_tickers=confirmed_orphaned` is passed, both call sites use
  `fetch_live_prices()`'s only default `lookback_days=400`), a second multi-minute
  multi-vendor-fallback fetch every single rebalance day. `daily_runner.py`'s call to `run()`
  now passes its own already-fetched `daily_prices` straight through; `run()` reuses it only
  when `set(price_tickers).issubset(daily_prices.columns)` (covers every ticker it would
  otherwise fetch), falling back to fetching internally exactly as before if not (a narrower
  or absent `daily_prices`, e.g. every pre-existing test/notebook call site that doesn't pass
  it). `None` (the default) is byte-identical to this function's behavior before this param
  existed.
  `reconstruct_dry_run_positions(log_path)` reuses `measure_live_performance()`'s EXISTING FIFO
  `open_positions`/`open_position_avg_cost` computation (filtered to `dry_run=True` rows), not a
  second, separately-maintained FIFO implementation, reshaped into the same
  `{ticker: {'shares', 'avg_entry_price'}}` shape `get_ibkr_positions()` returns. Backs
  `daily_runner.py`'s opt-in `persist_dry_run_state` (default `False`), never called in `--live`
  mode.
  `_realized_weighted_portfolio_vol(weights, daily_prices, as_of, lookback_days)` is the live
  substitute for `momentum_backtest.py`'s `_realized_portfolio_vol()`: estimates realized vol
  directly from trailing `daily_prices` at the JUST-resolved target weights (no simulated
  equity curve exists live), the same "trailing data, not a simulated ledger" pattern
  `_inverse_vol_weights()` already uses for position sizing. `compute_target_weights()`'s
  `gross_exposure` now composes `regime_scalar * vol_scalar` (via
  `momentum_backtest.compute_vol_scalar()`), matching the backtest's exact composition order,
  don't compute one scalar without the other, they're independent and multiplicative.
  `apply_absolute_momentum_filter(picks, latest_scores, defensive_ticker)` wraps
  `core/functions_quant_extensions.py`'s `absolute_momentum_overlay()` (wraps the single live
  `picks` list in a length-1 `pd.Series`, calls the shared function, unwraps the result), wired
  into `run()` right after `picks = get_top_etfs(...)`, BEFORE `signal_context`/
  `compute_target_weights()` are built, so a substituted defensive ticker flows through sizing/
  vol-scaling/regime-filtering like any other pick. `defensive_ticker` needs its own live price,
  add it to that portfolio's own `tickers:` list in `config.yaml`, there's no automatic
  `extra_price_tickers`-style widening for it.
  `fetch_bid_ask_spread(ticker, port, client_id, host, timeout)` opens its own real-time
  `reqMktData()` subscription (a separate minimal `EWrapper`/`EClient` app, mirrors
  `PositionsApp`/`AccountApp`/`IBApp`'s existing pattern), requires a live TWS/Gateway
  connection AND, per IBKR's own rules, typically a PAID real-time market-data subscription
  (confirmed against IBKR's docs, not assumed), a `None` return (timeout/no usable quote) is
  treated as "couldn't check," never as "spread is fine." `compute_spread_pct(bid, ask)` is the
  pure math half, factored out so it's unit-testable without a connection, same precedent as
  `check_slippage_tolerance()`. `place_orders_ibkr()`'s new `max_bid_ask_spread_pct` param
  (`None` default, zero new IBKR calls) gates each ticker right before submission, a too-wide
  spread drops into the EXISTING `dropped_orders` mechanism (`DROPPED_WIDE_SPREAD`, same merge
  pattern as `DROPPED_FRACTIONAL`/`DROPPED_INSUFFICIENT_CASH`), don't just `continue` without
  recording it there.
  `run()`'s picks-selection call was rerouted through `core/strategy_signals.py`'s
  `resolve_strategy_scores()`/`resolve_strategy_picks()` (a LAZY, function-local import inside
  `run()`'s body, breaking an otherwise-circular import since `core/strategy_signals.py` itself
  imports `resolve_momentum_scores()`/`assign_ranks()` from THIS file, the same lazy-import
  pattern this file already uses for `ibapi`), the single live call site every `strategy_type`
  (see `docs/MOMENTUM_STRATEGIES.md`) now flows through. For
  `strategy_type == "hybrid_multi_factor"`, `resolve_strategy_scores()` needs
  `FMP_API_KEY`/`EODHD_API_KEY` to fetch real fundamentals; `run()` reads them directly via
  `os.environ.get(...)` at that one call site, DELIBERATELY NOT reusing this function's own
  `fmp_api_key`/`eodhd_api_key` params (those remain scoped to `fetch_live_prices()`'s
  PRICE-vendor selection only, and `daily_runner.py` deliberately never populates them, confirmed
  by every strategy-plan epic's live validation, production price data comes from `yfinance`).
  Reusing the price-fetch keys for fundamentals too would have silently switched the real
  production price vendor for EVERY portfolio the first time `daily_runner.py` started passing
  real keys through, an unrelated, unbudgeted side effect discovered and avoided while wiring up
  `hybrid_multi_factor`.
  `place_orders_ibkr()`'s `attach_broker_stop_loss`/`stop_loss_pct` params (from
  `BacktestConfig`, belt-and-suspenders alongside `auto_execute_stop_loss`, see
  `docs/RISK_CONSTRAINTS.md`'s "Broker-Side Protective Stop") attach a real IBKR bracket at BUY
  time when set: parent BUY (`transmit=False`) + child `STP` SELL (`parentId` linked,
  `transmit=True`, `auxPrice = expected_prices[ticker] * (1 - stop_loss_pct)`), inside the
  existing per-order loop, no reference price -> falls back to a plain, unprotected BUY (same
  fallback shape as `allow_extended_hours`'s "no reference price" case). Only the PARENT oid
  goes into `order_id_to_ticker` (the fill-poll wait set), the child's oid is tracked separately
  (`stop_order_ids[ticker]`) and surfaced via `results[ticker]["stop_order_id"]` ->
  `orders[ticker]["broker_stop_order_id"]` in `run()`, purely in-memory for the rebalance
  summary email's "What Actually Happened" column (`interfaces/notifications.py` already reads
  `fill_status`/`fill_price` from that same dict), deliberately NOT added to `log_orders()`'s
  hash-chained CSV schema, that log is append-only with a fixed header and is written BEFORE
  `place_orders_ibkr()` even runs, the stop orderId isn't known yet at that point, and a schema
  change there would misalign columns for any pre-existing log file (see that function's own
  "NOTE on schema evolution"). TIF is deliberately asymmetric: the parent explicitly carries
  `tif="DAY"` (Fix 3, matches the account's own previously-implicit default, now made explicit
  for EVERY order, bracket or not), the child protective STP explicitly carries `tif="GTC"`, a
  `DAY` stop would be cancelled by IBKR at end of day and leave the position unprotected on
  every subsequent day this app doesn't run, defeating the entire point. Cancel-before-sell (any
  SELL this app itself generates, whether from a rebalance, `check_and_handle_stop_losses()`, or
  `check_and_handle_time_stops()`, all funnel through this one function) is centralized here via
  a new `IBApp.openOrder()`/`openOrderEnd()` pair and `reqAllOpenOrders()` (NOT
  `reqOpenOrders()`, which only returns the SAME client connection's own orders; the run that
  PLACED a bracket and the run that later decides to EXIT are almost always different
  connections), cancelling any resting `(symbol, SELL, STP)` order matching this run's SELL
  batch via `cancelOrder(orderId)` before that SELL is submitted, broker-truth-based, not
  dependent on any locally-cached order ID, zero extra IBKR round trip when
  `attach_broker_stop_loss` is off (the default).
  `is_outside_all_trading_windows(exchange, allow_extended_hours, now)` (pure, `now` injectable
  for testing, defaults to real `pd.Timestamp.now(tz="America/New_York")`) backs a proactive
  `WARNING` logged at the very top of `place_orders_ibkr()`, before ever connecting, when the
  current time is outside both RTH (9:30am-4:00pm ET) and, if `allow_extended_hours` is set,
  the pre-market/after-hours window too (4:00-9:30am ET / 4:00-8:00pm ET, exactly
  `allow_extended_hours`' own documented coverage). Compares plain ET time-of-day boundaries,
  not `mcal`'s `market_open`/`market_close` (a deliberate, documented simplification, doesn't
  special-case early/late half-days, this is advisory visibility, not a hard submission gate);
  still uses `mcal` to confirm today has a session at all (weekend/holiday -> always "outside").
  Motivated by a real observed gap: a late-night manual `--force-rebalance --live` test run
  only surfaced this via IBKR's own `error 399` ("will not be placed until <next session>")
  after submission, buried among other informational codes, not proactively.
  `generate_orders()` now sets `money_invested`/`pct_money_invested` on EVERY returned order
  (BUY/SELL/HOLD, every HOLD reason including "no live price available"), via the same
  `_with_context()` helper that already injects `rank`/`signal_score` uniformly.
  `money_invested` is `target_dollar[t] = total_value * gross_exposure * weight[t]`, each
  ticker's TARGET dollar allocation this rebalance, deliberately NOT `drift_dollar` (the
  incremental change the BUY/SELL/HOLD decision itself is based on, a few lines below in the
  same function), so a currently-held, not-traded HOLD still reports a real, non-zero target
  allocation. Summed across every order this function returns, `money_invested` totals exactly
  `total_value * gross_exposure` by construction (a ticker being sold out of the target universe
  entirely correctly contributes `0`). `log_orders()` gained matching `money_invested`/
  `pct_money_invested` CSV columns (same schema-evolution caveat as the pre-existing `rank`/
  `signal_score` addition, archive old `live_trades_log_*.csv` files first), and
  `notifications.py`'s `build_rebalance_summary_html()` gained "Money Invest"/"% Money Invest"
  columns plus a "Capital allocated this rebalance" line above the table (the same sum,
  recomputed from the enriched `orders` dict, no new function parameters needed anywhere
  upstream). Reporting-only: IBKR has no dollar-denominated order type for equities/ETFs
  (`cashQty` only works for forex/CASH pairs, confirmed empirically, see `README.md`'s Known
  Gaps), the actual order submitted to `place_orders_ibkr()` is still sized in whole shares
  regardless of this.
- **`risk/circuit_breaker.py`**, extracted from `daily_runner.py` with alerting
  dependency-injected (`alert_fn` param) specifically so `risk/` has zero import dependency on
  `interfaces/`, enforced by an AST-based test
  (`test_risk_module_has_no_dependency_on_interfaces_module`), not just a convention.
  `check_circuit_breaker()`'s `halt_path.exists()` check MUST run first, unconditionally,
  before the "both config breakers disabled" early return, confirmed by a real bug found (and
  fixed) while building the account-wide breaker below: the old order let that early return
  skip the halt-flag check entirely whenever the CALLING portfolio's own
  `max_portfolio_drawdown_pct`/`max_dollar_drawdown` were at their shipped defaults (the common
  case), silently ignoring a halt flag written by ANY external source
  (`risk/risk_monitor.py`'s `write_halt_flag()`, an email-commanded PAUSE, the account-wide
  breaker), making `risk_monitor.py`'s entire documented purpose ineffective for any portfolio
  that hadn't separately opted into its own breaker. Don't reintroduce that ordering.
  `compute_account_wide_drawdown()` (pure, no I/O) plus `ACCOUNT_WIDE_PEAK_NAME` back
  `daily_runner.py`'s `check_account_wide_drawdown_breaker()` (Recommended tier,
  docs/RISK_CONSTRAINTS.md): ONE peak tracked for the SUM of every portfolio's resolved
  capital (`account_wide_max_drawdown_pct`, top-level config field, not per-portfolio), when
  tripped it writes EVERY portfolio's own `circuit_breaker_halted_<name>.flag` (reusing the
  exact mechanism above, no new gating code path), distinct from the per-portfolio breaker
  which only halts that one portfolio. Its peak-equity file
  (`data/peak_equity___account__.txt`) is deliberately separate from any portfolio's own, so
  resuming one portfolio via `resume_trading()` does NOT reset the account-wide peak, an
  unrecovered account will re-trip and re-halt everyone again on the next run, a deliberate
  kill-switch property, not a bug.
- **`risk/risk_monitor.py`**, an intentionally *independent* read-only oversight process. It
  must not import `daily_runner.load_config()`/`BacktestConfig` or share P&L-computation code
  with `execution/live_signal.py`, the whole point is that a bug in the trading logic can't
  also blind the thing watching for it. It has its own minimal FIFO P&L re-derivation and its
  own YAML read for `total_value`. Preserve this segregation in any future edit here.
  `--log-dir`'s default is `logs_dir()`, matching where `daily_runner.py`/`live_signal.py`
  actually write `live_trades_log_<portfolio>.csv`, confirmed (not assumed) by finding it
  previously defaulted to `data_dir()`, a genuinely DIFFERENT directory, meaning this process's
  default Docker cron invocation (`docker-entrypoint.sh`, no `--log-dir` override) could never
  find any trades and silently reported "within risk limits" forever, its hourly halt check was
  never actually reachable. If editing this default again, add a test exercising `main()`'s own
  default (not just `compute_realized_and_open_pnl()` with an explicit `log_path`, which alone
  would not have caught this), see `tests/test_governance.py::TestRiskMonitor`'s two regression
  tests for the pattern.
- **`interfaces/`**, email notifications (categorized CRITICAL/STANDARD/PERIODIC/DAILY/WARNING,
  CRITICAL can never be filtered, DAILY uniquely defaults to OFF when unconfigured, every other
  filterable category defaults to ON) and pydantic-validated email-commanded remote actions.
  `email_commands.py`'s `poll_and_process_commands()` guards against a same-inbox reply
  cascade with two checks together, not one: the `X-Momentum-Trading-Bot` header catches the
  bot's own generated replies, and `BOT_SUBJECT_MARKER`/`_is_bot_thread()` catches a *human's*
  reply to those replies (which never carries the header), don't remove either one without
  re-reading why both exist. `email_diagnostics.py`'s `run_email_diagnostics()` backs
  `daily-runner --test-email`, a live SMTP+IMAP check independent of `config.yaml`.
  `notifications.py`'s `build_monthly_report_html()`/`build_daily_report_html()` are both thin
  wrappers over a shared `_build_report_html()`, the two reports differ only in cadence/window
  scale, not structure, so keep it that way rather than letting them diverge into two copies.
  `email_commands.py`'s command-outcome model is three-way, not two: `ACCEPTED`/`REJECTED`
  (decided at parse time by `parse_command()`) plus `ERROR` (decided AFTER parsing, either
  `poll_and_process_commands()`'s own top-level except catching an IMAP/connection failure
  before any message was fetched, or `daily_runner.py`'s per-command apply loop catching a
  failure while APPLYING an already-`ACCEPTED` command). `log_command_attempt()` and
  `build_reply_body()` both take optional `outcome`/`reason` overrides for this, backward
  compatible, every pre-existing call site (no `outcome=` passed) still derives
  `ACCEPTED`/`REJECTED` from `result.success` exactly as before. `daily_runner.py`'s
  `check_and_apply_email_commands()` wraps EACH command's apply block in its own
  `try/except`, deliberately, so one command failing to apply does not abort the rest of
  the batch, don't reintroduce one shared `try/except` around the whole per-command loop.
  `notifications.send_email_command_feedback` (default `true`) gates the ACCEPTED/REJECTED/
  ERROR reply EMAIL only, same pattern as `send_warning`, `log_command_attempt()`'s audit
  write is never gated by it or anything else.
  `core/smtp_auth.py`'s `connect(host, port, timeout=None)` is the single shared connection
  helper for every SMTP call site in the project (`daily_runner.py`'s `send_alert_email()`,
  every category email in `notifications.py`, `email_diagnostics.py`'s `--test-email` check),
  replacing what used to be five separate, duplicated `smtplib.SMTP(...) + starttls()` blocks.
  Picks the connection type from the port: `smtplib.SMTP_SSL` (implicit TLS) for `465`,
  `smtplib.SMTP` + `starttls()` for everything else (`587`, the common default). This exists
  because of a real, confirmed incident: every SMTP send inside the Docker container was
  timing out (100% failure rate, `Failed to send notification: timed out` on every category),
  and a raw socket probe from inside that same container proved port 587 hangs the full
  timeout while port 465 connects in under a second, against the identical Gmail host, IMAP
  (993) also connected instantly, ruling out a general network-egress problem. `SMTP_PORT=465`
  in `.env` is the fix, not a longer timeout, a genuinely blocked port times out identically
  regardless of how long you wait. `send_with_retry(send_fn, max_attempts=2,
  backoff_seconds=3.0)` (also `core/smtp_auth.py`) wraps the actual send in a bounded retry,
  mirrors `execution/live_signal.py`'s `with_retry()` pattern but kept local to avoid a new
  cross-domain import from `interfaces/` into `execution/`; still fully non-fatal on final
  failure, every call site's own `except Exception -> logger.error(...); return False` (or
  equivalent) is unchanged. `smtp_timeout()` reads `SMTP_TIMEOUT_SECONDS` (default `30`, up
  from the old hardcoded `15`).
  `notifications.py`'s `build_no_action_summary_html(portfolio_name)` backs a new always-sent
  STANDARD notice (`daily_runner.py`, the `else` branch alongside the existing
  `if orders_result: send_standard_action(...)`): a rebalance that ran to completion (this
  branch is only reached on a rebalance day or `--force-rebalance`) but produced zero orders
  (e.g. `AGGREGATE_DRIFT_SKIP`) previously sent NO summary at all, indistinguishable from a
  failed/skipped run purely from your inbox. Reuses the same rich-HTML look as
  `build_rebalance_summary_html()`, not a plain-text fallback, deliberately, to stay visually
  consistent with every other portfolio email. Does NOT fire on a non-rebalance day (the daily
  snapshot/stop-loss block runs regardless but was never gated by `orders_result` and stays
  silent by design, this isn't a new daily-cron email).
- **`daily_runner.py`**, the actual scheduled entry point (`daily-runner` console script).
  Loads and schema-validates `config.yaml`, loops over every portfolio defined under
  `portfolios:`, idempotent per day, refuses `--live` unless `config.yaml`'s
  `metadata.approved_by`/`approved_date` are set. `--port`'s default reads the `IBKR_PORT` env
  var (falling back to `7497`), mirrors `execution/live_signal.py`'s existing `IBKR_HOST` env
  var pattern; an explicit `--port` on the command line always overrides it.
  Restart/resume safety is intentional and already correct in `--live` mode BY CONSTRUCTION,
  confirmed by reading the code, not assumed: `is_rebalance_day()` recomputes purely from
  today's real date every run (no stored "days since last rebalance" counter to desync), and
  `get_ibkr_positions()` is a real broker query every run (never local memory), so a restart
  changes nothing about actual holdings. Don't "fix" this by adding new persisted local
  position state, that would introduce exactly the drift-from-the-broker risk this
  architecture deliberately avoids. `has_run_on_or_after(tag, since_date)` (a range check over
  `data/last_run_{tag}_*.lock` files, distinct from `already_ran_today(tag, as_of=...)`'s
  exact-date check) backs the one confirmed gap that WAS worth closing: `MISSED_REBALANCE_DAY`,
  a non-blocking WARNING (same triple-step pattern as this file's other advisory checks) when
  a scheduled rebalance date passed with no run recorded since. Deliberately a range check, not
  an exact-date match, so a manual `--force-rebalance` catch-up (which marks TODAY's date, never
  the missed period's original target date) correctly clears the warning on the next run
  instead of nagging forever. `execution/live_signal.py`'s
  `most_recent_rebalance_target_date()` is the pure calendar half of this (finds the most
  recent date STRICTLY BEFORE today that was itself a rebalance day, built directly on
  `is_rebalance_day()`), `daily_runner.py`'s wiring adds the file-existence half and the
  "portfolio has run at least once before" guard (skips a brand-new portfolio's very first
  run, nothing to have missed yet). See `docs/RUNNING.md`'s "4.11c. Restart and Resume
  Behavior" for the full user-facing explanation, including dry-run mode's deliberate lack of
  persisted simulated-portfolio state by default (unrelated to this gap, a separate, intentional
  design choice, though `BacktestConfig.persist_dry_run_state`, default `False`, opts a
  portfolio into `execution/live_signal.py`'s `reconstruct_dry_run_positions()` instead, see
  below).
  A "rebalance in progress" marker (`LOCK_DIR / f"rebalance_in_progress_{name}.marker"`,
  `_write_rebalance_in_progress_marker()`/`_clear_rebalance_in_progress_marker()`, written
  atomically via temp-file + `os.replace()`) brackets the `run(...)` call, a stale one found on
  a LATER run fires a one-time `STALE_REBALANCE_MARKER` WARNING (consumed after firing, not
  persistent) flagging that a previous process crashed mid-rebalance, this is visibility only
  for the one narrow gap the diff-based retry mechanism above can't fully close (a crash exactly
  during in-flight order submission), it does NOT block the current run. `risk_monitor.py` is
  deliberately never made aware of this marker, same independence principle as the six existing
  risk constraints.
  `_classify_orphaned_tickers(current_holdings, tickers, trade_log_path)` partitions a
  held-but-not-configured ticker into `confirmed_orphaned` (this portfolio's OWN trade log,
  via `derive_entry_date()`, confirms it was legitimately held here) or `unrecognized` (not
  confirmed, could belong to a SIBLING portfolio sharing the same real IBKR account, the
  documented multi-portfolio ticker-leakage scenario, `get_ibkr_positions()` returns the WHOLE
  account unfiltered to every portfolio). Only `confirmed_orphaned` gets priced (via `run()`'s
  new `extra_price_tickers` param, see below) and fires `ORPHANED_POSITION`; `unrecognized`
  stays exactly as untouched as before this feature existed and fires `UNRECOGNIZED_POSITION`
  instead. Don't ever widen pricing/trading to `unrecognized` tickers, that reintroduces the
  cross-portfolio-sell risk this classification exists to prevent.
  `_compute_scoped_positions_value()` backs the `TOTAL_VALUE_DRIFT` WARNING (fixed/non-null
  `total_value` portfolios, `--live` only, `cfg.total_value_drift_warning_pct`, default `0.10`):
  an EXPLICIT `set(tickers) | set(confirmed_orphaned)` intersection, deliberately NOT reusing
  the pre-existing `positions_value`/`write_portfolio_snapshot()` computation, which was found
  (during this work, not previously known) to double-count a ticker legitimately shared between
  two portfolios under the documented `TICKER OVERLAP` warning, since it only scopes implicitly
  via `latest_prices` price-availability, not an explicit ticker-membership check. That
  pre-existing double-counting bug itself is NOT fixed here, out of scope, flag it before
  touching `positions_value` or `write_portfolio_snapshot()` again. Only the anomalous-high side
  is checked (real positions exceeding the whole configured capital base), real per-portfolio
  cash can't be isolated on a shared IBKR account, so no attempt is made to reconstruct a full
  "total value," only the position side.
  `scope_overlapping_holdings(current_positions, tickers, overlap, trade_log_path, portfolio)`
  fixes a real, confirmed incident (2026-07-16, not a theoretical risk): `get_ibkr_positions()`'s
  whole-account result was flowing straight into `current_holdings` with zero per-portfolio
  scoping, so for a ticker configured in more than one portfolio sharing this real account, one
  portfolio's rebalance saw a SIBLING portfolio's legitimately-held shares as its own
  over-allocation and generated a real SELL against them, confirmed directly against real trade
  log timestamps and share counts matching the sibling's own buy sizes. Caps
  `current_positions[ticker].shares` at `min(broker_reported_shares, this portfolio's own
  execution/live_signal.py's derive_own_live_positions() shares)` for every ticker BOTH
  configured in this portfolio's own `tickers:` list AND present in `check_ticker_overlap()`'s
  overlap map, and substitutes this portfolio's own FIFO `avg_entry_price` for that ticker too
  (the broker's `avgCost` is blended across ALL shares including a sibling's, which would also
  corrupt stop-loss threshold accuracy). A ticker with zero FIFO history for this portfolio
  scopes to `0` even if the broker shows a large combined position, the safe failure direction,
  same "unrecognized -> untouched" philosophy as `_classify_orphaned_tickers()` above (a
  different, narrower scenario: that one only covers a ticker no longer in a portfolio's CURRENT
  config, never a ticker actively configured in two portfolios at once, which is what actually
  caused this). Called immediately after `get_ibkr_positions()`, before `current_holdings` is
  built or used anywhere, so every downstream consumer (orphaned-ticker classification, `run()`/
  `generate_orders()`, stop-loss checks, snapshot writing) gets the corrected numbers for free.
  `check_ticker_overlap()`'s call is HOISTED above the per-portfolio loop (previously only
  computed inside the warning-email gate) so `overlap` is unconditionally available every
  iteration. Fires a new `OVERLAPPING_TICKER_SCOPED` WARNING alert only on a run where capping
  actually happened (naming the ticker, both share counts, and the sibling portfolio), distinct
  from the pre-existing static `TICKER_OVERLAP` warning (still fires whenever configs share a
  ticker regardless of whether capping ever triggers, now reworded to reflect that destructive
  sells are prevented, not just flagged). No new config field, this corrects unintended
  behavior, always-on, not an opt-in toggle like `skip_month_guardrail`/`use_absolute_momentum`.

**Config flow**: `config.yaml` (gitignored; copy from `config.example.yaml`) →
`daily_runner.load_config()` builds one `BacktestConfig` per portfolio from
`default_risk` + that portfolio's `risk_overrides`, and any `BacktestConfig` field is accepted
via `**kwargs` even if `config.example.yaml` doesn't mention it. `config.example.yaml`
documents every `BacktestConfig` field, both LIVE-relevant and BACKTEST-ONLY (confirmed by
enumerating the dataclass directly against the file, not guessed). None of `default_risk`/
`risk_overrides` apply to `risk_monitor.py`, see its own bullet above.
A `total_value: <number>` means a fixed
capital baseline, used as-is every run, never auto-refreshed against real account P&L
(intentional, an explicit allocation ceiling, not auto-compounding, see the `total_value` drift
warning above). `total_value: null` does NOT mean "pull the full account value", it means "a
share of the real IBKR account's NetLiquidation, after every fixed (non-null) portfolio's
`total_value` is reserved first." `resolve_total_values()` (`daily_runner.py`, called once
before the per-portfolio loop, independent of any portfolio's momentum regime) computes this:
`validate_config_schema()` no longer restricts how many portfolios may be `null` (zero, one, or
several), and if MORE than one portfolio is null, the remainder is split EQUALLY across all of
them, e.g. a $10,000 account with one $2,500 fixed portfolio and two null portfolios gives each
null portfolio $3,750, not $7,500 each (which would double-count the same real capital). This
guarantees `sum(resolved.values()) <= account_value` by construction (fixed portfolios' sum plus
equal shares of a bounded remainder can never exceed it), and `resolve_total_values()` hard-fails
(`raise ValueError`, naming every affected null portfolio) if the fixed portfolios already
consume the whole account before any null portfolio gets a share. In dry-run mode, EACH null
portfolio independently gets a flat $1000 placeholder (not divided among them, not reduced by
other portfolios' `total_value`), dry-run tests signal/order-generation LOGIC, not real capital
math, don't route dry-run through the real-remainder calculation. Each portfolio's resolved
capital is logged once at startup (`Portfolio '<name>' resolved total_value: $<amount>`), this
equal-split math is DELIBERATELY invisible to `risk_monitor.py` (same independence principle as
the six risk constraints below), so a null portfolio's `risk_monitor.py` cron entry needs its
resolved share passed explicitly via `--initial-capital`, read off that startup log line, not
hand-computed from `config.yaml` alone, see `docs/DEPLOYMENT.md`'s "Independent risk oversight"
section.

**Safety defaults that are load-bearing, not incidental**, never change these without an
explicit user ask: dry-run is the *unflagged default* (`--live` is opt-in, and there is no
`--dry-run` flag, passing one is an argparse error, since `parse_args()` is strict); real-money
trading requires `--port 7496` **and** `--confirm-live-trading` together; circuit-breaker halts
require explicit `--resume-trading`, never auto-clear; `docker-entrypoint.sh`'s `--live`/
`--confirm-live-trading` are manual-edit-and-rebuild-only, deliberately NOT env-var-driven like
every other setting in that file (`DAILY_RUNNER_CRON`, `IBKR_HOST`/`IBKR_PORT`), considered and
explicitly rejected, since an env var toggle would let real-money trading get enabled by a plain
`.env` edit alone, no code change or rebuild required.

## Testing conventions

- Entire suite runs on synthetic/seeded data or mocked IBKR calls, no network or broker needed.
  See `docs/TESTING.md` for fixture details and how to interpret a failure (most post-change
  failures are either a real regression or a dependency-version mismatch, not a strategy issue).
- `tests/test_architecture.py` specifically protects the package restructure (import boundaries,
  cross-directory path resolution via subprocess, the circuit-breaker extraction's decoupling),
  distinct from the rest of the suite, which tests strategy/execution logic.
- When adding a `BacktestConfig` field, a new `config.yaml` schema field, or changing the trade
  log CSV schema, add both a validation test and a run-succeeds test, see `docs/TESTING.md`
  "When to add a new test" for the exact existing patterns to follow.

## Deeper docs (read before touching related code, don't duplicate here)

- `docs/RUNNING.md`, day-to-day run commands, staged rollout (paper → small live → full live)
- `docs/DEPLOYMENT.md`, one-time setup, SMTP/OAuth, Docker/Task Scheduler/systemd specifics
- `docs/TESTING.md`, test organization and fixtures
- `docs/STRATEGY_THEORY.md`, momentum theory, worked example
- `docs/EMAIL_REPORTING.md` / `docs/EMAIL_COMMANDS.md`, notification and remote-command setup
- `docs/RISK_CONSTRAINTS.md`, long-term vs. short-term momentum risk constraints (advisory
  warnings and opt-in config toggles), and why they're deliberately invisible to `risk_monitor.py`
- `docs/MOMENTUM_STRATEGIES.md`, the selectable `strategy_type` field, all 11 momentum variants,
  how presets compose with explicit config values, and per-strategy best-parameter tables

## Constraints for documentation
- Do not use "—" to comment, document the code or add this marks on files.