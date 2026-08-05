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
  `core/functions.py`'s `fetch_with_retry(fetch_fn, max_attempts=3, backoff_seconds=1.0)` (Epic
  10, "API Resilience for Price-Vendor Fetches" plan) is a bounded retry-with-backoff wrapper
  for a zero-arg vendor-fetch callable, confirmed by direct code reading (not assumed) to be a
  real, previously-missing gap: every vendor call in this file was a single, unretried attempt
  that fell straight through to the next vendor in `get_stock_prices()`'s FMP -> EODHD -> yf
  cascade on ANY exception, and `get_bulk_prices()`'s per-ticker loop fired requests back-to-back
  with zero pacing, a real risk against a free-tier vendor's rate limit given a real portfolio in
  this project's own `config.yaml` has 58 tickers (`portfolio2`). Mirrors
  `execution/live_signal.py`'s `with_retry()` and `core/smtp_auth.py`'s `send_with_retry()`
  pattern exactly, but kept LOCAL to `core/functions.py` (not imported from `execution/`), the
  same "avoid a new cross-domain dependency" precedent `send_with_retry()`'s own docstring
  already established for the identical reason (`core/` must not depend on `execution/`,
  `tests/test_architecture.py`'s AST-based import check enforces this). Only retries genuinely
  transient failures: `HTTPError` with code `429` (rate limit) or `>= 500` (server-side);
  `URLError` (network-level failure via `urllib`, e.g. a DNS failure, timeout, or connection
  reset); and bare `OSError` (the same class of failure from a non-`urllib` HTTP client,
  `_fetch_yf()`'s `yf.download()` uses `requests`/`curl_cffi` underneath, not `urllib`, so its
  connection/timeout failures surface as `ConnectionError`/`TimeoutError`, both `OSError`
  subclasses in Python 3, not `URLError`). Every other exception (a `4xx` other than `429`, e.g.
  a bad API key or "no access"; the existing "no data returned" `ValueError`; a JSON parse error)
  is NOT retried, raised immediately exactly as before, since retrying those wastes time on a
  failure a retry can't fix and delays falling through to the next vendor in the existing
  fallback cascade, which this change does not otherwise alter. Wired into each of
  `get_stock_prices()`'s three vendor closures (`_fetch_fmp()`, `_fetch_eodhd()`, `_fetch_yf()`),
  wrapping only the primary network call (FMP's best-effort secondary dividend-adjusted-close
  call keeps its own existing silent-`except Exception: pass`, unchanged, retrying a call that's
  already tolerant of failure isn't needed), so this benefits both real callers automatically
  without duplicated logic: `execution/live_signal.py`'s `fetch_ohlcv_for_tickers()` (direct
  single-ticker calls) and `fetch_live_prices()` (via `get_bulk_prices()`'s own internal
  per-ticker `get_stock_prices()` calls). `get_bulk_prices()` also gained a
  `request_pacing_seconds: float = 0.25` param, a small `time.sleep()` between successive
  tickers in its per-ticker loop (not before the first request or during the earlier
  vendor-auto-detection probe), default nonzero as a real robustness fix, not purely opt-in;
  `execution/live_signal.py`'s `fetch_live_prices()` gained a matching pass-through param
  (default unchanged), deliberately NOT exposed in `config.yaml`, a fixed internal constant
  rather than a per-portfolio tuning knob. Pagination was investigated and found not applicable:
  none of the three vendors this project uses paginate results for the date ranges actually
  requested here (daily history over months to ~1-2 years, each vendor returns the full range in
  one call), not implemented since there's nothing to paginate.
  `_fetch_yf()`'s `yf.download(..., end=end_date)` call (Epic 12, "Live-vs-Backtest Divergence
  Reconciliation" plan) had a real, confirmed bug found via real-data verification, not
  synthetic: yfinance's own `end` param is EXCLUSIVE (confirmed directly against a real call,
  `end="2026-08-03"` never returns that day's own row), unlike FMP's/EODHD's `to` REST params in
  this same file, both confirmed inclusive. Every real caller computes `end_date` as literally
  "today" (`execution/live_signal.py`'s `fetch_live_prices()`), so this silently meant the
  yfinance vendor path never included today's own close, even fetched well after market close.
  Fixed: `_fetch_yf()` now requests `end_date + 1 day` internally (`yf_end`, a local variable,
  the outer `end_date` closure var used everywhere else in `get_stock_prices()` is untouched),
  making the yfinance call effectively inclusive, matching FMP/EODHD. See
  `tests/core/test_functions.py`'s `TestFetchYfEndDateInclusive` and `README.md`'s Known Gaps
  entry for the reconciliation context that surfaced this.
  `core/functions_quant_extensions.py`'s `compute_drawdown_episodes(cumulative_returns)` (Epic
  13, "Real Historical Crash-Period Stress Test" plan) is a pure function, one row per
  peak-to-new-high drawdown episode (`peak_date`/`trough_date`/`trough_pct`/`recovery_date`/
  `recovery_days`, `recovery_date` is `pd.NaT` for an episode still open at the end of the
  series), the recovery-time counterpart to `backtest/momentum_backtest.py`'s `_build_report()`,
  which only ever returned a single scalar max drawdown
  (`report.attrs["tearsheet"]["MaxDrawdown"]"`), no episode/recovery detail. Built specifically
  so `notebooks/research/crash_period_stress_test.ipynb` could show not just "max drawdown was
  X%" but "and it took N days to recover," a real gap found while designing that epic, not
  previously present anywhere in this codebase. Takes a cumulative growth-of-$1 index (e.g.
  `(1 + returns).cumprod()`, the same convention `_build_report()`'s own "Portfolio Cumulative
  Return" column already uses), single forward pass, O(n), no post-hoc lookup ambiguity. See
  `tests/core/test_functions_quant_extensions.py`'s `TestComputeDrawdownEpisodes` and `README.md`'s
  Known Gaps entry for the real crash-period results this was built to produce.
  `core/audit_log.py`'s `log_alert()` gained an optional `sender` param (default `None`, resolves
  internally to `os.environ.get("SMTP_USER", "")`), recording which outbound email account this
  alert would/did notify from, self-configuring so none of its ~27 existing call sites needed to
  change. `ALERTS_LOG_HEADER` gained a matching `sender` column (appended before `row_hash`,
  same "grow at the end" schema-evolution precedent as the trade log's own additions), see
  `docs/ALERT_LOG.md`'s schema section for the full caveat (records the configured account, not
  proof of actual delivery).
  `acquire_log_lock(log_path, timeout=15.0, stale_after=10.0)`/`release_log_lock(lock_path)`
  (also here) fix a real, confirmed incident (2026-07-21): two `daily-runner
  --force-rebalance` invocations run seconds apart broke a real trade log's hash chain, both
  processes read the same "last row hash" before either had written (`append_hash_chained_row()`,
  `execution/live_signal.py`'s `log_orders()`, and `interfaces/email_commands.py`'s
  `log_command_attempt()` all had this exact read-then-write race, confirmed by reading all
  three, not assumed identical). Implemented as a portable exclusive-create sentinel file
  (`log_path + ".lock"`, `os.open(..., O_CREAT | O_EXCL)`), no new dependency, same philosophy
  as `daily_runner.py`'s rebalance-in-progress marker; a lock older than `stale_after` is
  force-reclaimed (a crashed holder must not deadlock every future run). Windows raises
  `PermissionError` under contention here, not `FileExistsError` like POSIX, confirmed directly
  by a real concurrency test failure on this project's own Windows dev environment, both are
  handled identically, don't narrow that except clause back to one exception type. All three
  call sites (`append_hash_chained_row()`'s own body, plus `log_orders()` and
  `log_command_attempt()`'s bespoke read-then-write blocks, which don't go through
  `append_hash_chained_row()` and so each needed their own explicit acquire/release) now share
  this one locking primitive rather than each risking reinventing (or omitting) it.
  `compute_retention_window_days(lookback_period, holding_period)` and
  `rotate_hash_chained_log(log_path, cutoff_date)`/`rotate_plain_log(log_path, cutoff_date,
  timestamp_col)` (also here) back `BacktestConfig.enable_log_retention` (opt-in, default
  `False`, byte-identical behavior when off): every one of the five time-series CSVs this project
  writes (trade log, signal rankings log, portfolio snapshot, plus the two shared logs) grew
  unbounded forever until this existed, confirmed by search, not assumed. `compute_retention_
  window_days()` implements `3 * (lookback_period + holding_period)` converted to calendar days
  via the SAME month/week-quarter convention `execution/live_signal.py`'s
  `resolve_momentum_scores()`/`compute_required_lookback_days()` already established (regime
  decided ONCE from `holding_period < 1`, applied to BOTH fields, not independently per field).
  `rotate_hash_chained_log()`/`rotate_plain_log()` (a shared private `_rotate_log()` underneath,
  `rechain` bool selects whether output files get a freshly recomputed row_hash chain) ARCHIVE,
  never delete: old rows move to a sibling `<log_path>.archive_<run_timestamp>.csv`, both output
  files written atomically (temp file + `os.replace()`), guarded by the same
  `acquire_log_lock()`/`release_log_lock()` critical section as every append, and (for
  hash-chained files) independently re-seeded from `"GENESIS"` so `verify_log_integrity()`
  (unchanged) keeps working on both resulting files with zero changes to that function. A serious
  correctness risk was found and designed around while building this, not previously known:
  `execution/live_signal.py`'s FIFO cost-basis functions (`measure_live_performance()`, and
  transitively `reconstruct_dry_run_positions()`/`derive_own_live_positions()`, plus
  `derive_entry_date()` separately) used to read the trade log via a single `pd.read_csv()` call;
  archiving away a STILL-OPEN position's BUY row would have silently corrupted its cost basis
  forever. `read_trade_log_with_archives(trade_log_path)` (`execution/live_signal.py`) fixes this:
  concatenates the active file with every sibling `<trade_log_path>.archive_*.csv`, sorted by
  timestamp, byte-identical in content to a plain read when no archives exist; all four
  FIFO-dependent readers go through it now. `daily_runner.py`'s `apply_portfolio_log_retention()`
  (per-portfolio: trade/signal-rankings/snapshot logs, using that portfolio's own resolved
  window) and `apply_shared_log_retention()` (the two shared logs, using the LARGEST resolved
  window across every opted-in portfolio, since a shared file can't have two different windows)
  wire this in, once per run, firing a new `LOG_ROTATED` INFO alert whenever a rotation actually
  moves rows. See `docs/LOG_RETENTION.md`.
  `run_walk_forward_lookback_search(daily_prices, tickers, cfg, lookback_candidates, train_years,
  test_years, step_years, metric="Sharpe")` (`core/functions_quant_extensions.py`, Epic 15, "Real
  Out-of-Sample Strategy Validation" plan) is a real-engine walk-forward parameter-robustness
  search, backing `notebooks/research/out_of_sample_validation.ipynb`. This module already had
  `pre_registered_split()`/`walk_forward_lookback_holding()`/`bootstrap_sharpe_ci()`, fully
  coded and wired into `DHI0016_notebook1_research_and_EDA_IMPROVED.ipynb`'s cells 49-57, but
  confirmed by reading those cells directly, never actually executed against real data before
  this, the exact same "scaffold exists, never run" pattern already found and fixed for other
  tooling in Epic 12/13. A real design gap was found in that existing scaffold while wiring this
  up, not previously known: cell 52's `_quick_backtest()` helper is a simplified mean-monthly-
  return approximation (no regime filter, no volatility targeting, no stop-loss, no
  slippage/commission) and reimplements picks selection via `df_ranks.apply(lambda x:
  x.nsmallest(top_n)...)` with no `.dropna()` first, the exact real bug `get_top_etfs()`'s own
  docstring documents and fixes elsewhere in this codebase (`nsmallest()` backfills with
  NaN-ranked entries when fewer than `top_n` valid ranks exist). `run_walk_forward_lookback_
  search()` deliberately does NOT extend or reuse `_quick_backtest()`; it wires through the REAL
  `core/strategy_signals.py`'s `generate_strategy_monthly_picks()` + `backtest/
  momentum_backtest.py`'s `run_custom_backtest()` (the same real pipeline Epic 13/14's own
  crash-stress notebooks already use), matching this codebase's repeated "single source of
  truth, live and backtest must never diverge" principle, and gets the `.dropna()`-before-
  `.nsmallest()` fix for free since it goes through `generate_strategy_monthly_picks()`'s already-
  fixed selection path. Uses lazy (function-local) imports for `run_custom_backtest`/
  `generate_strategy_monthly_picks`, same precedent as `execution/live_signal.py`'s own lazy
  imports, since `core/strategy_signals.py` already imports FROM this file
  (`resolve_momentum_scores()`-adjacent helpers indirectly, see that file's own bullet), a
  direct top-level import back would be circular. Reuses the SAME rolling fold-slicing logic
  `walk_forward_lookback_holding()` already has (train/test windows advancing by `step_years`),
  applied to the real pipeline instead of injected generic callables, returns the same per-fold
  DataFrame shape (`fold_start`/`train_end`/`test_end`/`chosen_lookback`/`train_{metric}`/
  `test_{metric}`/`test_CAGR`). Reapplies Epic 14's own "bound only at the end, not the start"
  lesson (see `backtest/momentum_backtest.py`'s bullet below): each fold's price panel passed to
  `generate_strategy_monthly_picks()`/`run_custom_backtest()` is bounded only by `< train_end` or
  `< test_end`, never re-truncated at the fold's own start, so regime/vol/lookback calculations
  inside the real pipeline always see as much real pre-fold history as the full `daily_prices`
  panel actually has, avoiding a repeat of the bug class Epic 14 found and fixed.
  **Run for real for the first time 2026-08-04** against Epic 13's cached 17-ticker long-history
  ETF proxy-universe panel (`notebooks/research/crash_test_daily_prices.pkl`, 2005-01-03 through
  present, same documented ticker-availability scope boundary as Epic 13, `portfolio1`'s real
  `default_risk` config applied to the proxy universe, not portfolio1's exact current tickers),
  `pre_registered_split(split_date="2015-01-01")`, `LOOKBACK_CANDIDATES = [6, 9, 12, 15, 18]`
  months, `train_years=4, test_years=1, step_years=1` (5 real folds fit inside the ~10-year train
  window). Real result, honestly reported, not oversold: chosen lookback per fold was
  `[9, 9, 12, 12, 12]` (converging toward the shipped `lookback_period: 12` default, not away
  from it), train Sharpe `[1.14, 0.86, 0.82, 0.70, 0.67]` vs. test Sharpe
  `[1.58, 0.51, 0.03, 0.41, 2.21]`, mean train Sharpe 0.84 vs. mean test Sharpe 0.95, i.e. test
  performance was NOT systematically degraded relative to train across folds, the overfitting
  signature the methodology exists to catch did not show up here. The most frequently chosen
  lookback (`12`, matching 3 of 5 folds) was then evaluated on the `2015-01-01` to present
  holdout **exactly once**: CAGR 3.49%, AnnVol 10.26%, Sharpe 0.39, Sortino 0.50, MaxDrawdown
  -20.08%, WinRate 60.1%, Beta 0.47, **Alpha -2.63%** (annualized, vs. SPY). A block-bootstrap
  90% confidence interval on the holdout Sharpe (`bootstrap_sharpe_ci()`, `n_bootstrap=2000,
  block_size=6`) came back `[-0.11, 0.95]`, spanning zero. **Honest read**: the shipped
  `lookback_period` is a genuinely robust parameter choice, not an artifact of overfitting to
  one historical sample, but the single pre-registered holdout itself does not show a
  statistically confident positive edge over a passive SPY benchmark on this proxy universe,
  mixed, not oversold, not a confirmed edge and not a refutation either. See `README.md`'s
  Project Maturity & Safety section and Known Gaps entry for the full numbers and caveats.
  **Extended to the SHORT-TERM (weekly) regime, Epic 16, "Real Out-of-Sample Validation -
  Short-Term (Weekly) Momentum Regime" plan**: Epic 15 above only validated `portfolio1`'s
  monthly regime; `docs/STRATEGY_THEORY.md` still flagged weekly momentum as untested by this
  project's own walk-forward tooling, a live gap since `portfolio2`/`portfolio3` both run this
  exact weekly regime today. `run_walk_forward_lookback_search()` needed ZERO code changes to
  support this, confirmed before writing the plan, not assumed: `backtest/momentum_backtest.py`'s
  `_build_report()` (`monthly = daily.resample("ME").last()`) always buckets its report to
  month-end regardless of `holding_period`, so the function's reused `run_custom_backtest()` call
  and the notebook's `bootstrap_sharpe_ci()` (`sqrt(12)` annualization) stay valid unchanged
  against a weekly-rebalanced equity curve, only the `lookback_candidates` grid and the loaded
  portfolio config differ. A real test gap was found and closed first:
  `TestRunWalkForwardLookbackSearch` only exercised `holding_period=1` (monthly), the weekly/
  `round(x * 4)` branch had zero coverage before this epic (`test_weekly_regime_produces_
  multiple_folds_with_sensible_real_results`, `tests/core/test_functions_quant_extensions.py`).
  `notebooks/research/out_of_sample_validation_weekly.ipynb` (new) mirrors Epic 15's notebook
  exactly, loading `portfolio2`'s real config instead of `portfolio1`'s, same cached 17-ticker
  proxy panel, same `pre_registered_split(split_date="2015-01-01")` for direct comparability,
  `lookback_candidates=[0.5, 0.75, 1.0, 1.5, 2.0]` (week-quarters, -> 2/3/4/6/8 weeks via the
  existing `round(x * 4)` formula) in place of Epic 15's month-scale grid.
  **Run for real 2026-08-04**: chosen lookback per fold was `[1.5, 2.0, 2.0, 2.0, 2.0]` (6/8/8/
  8/8 weeks, converging to 8 weeks in 4 of 5 folds), train Sharpe `[0.30, 0.60, 0.74, 0.86,
  1.27]` vs. test Sharpe `[1.61, 2.34, -0.15, 0.91, 1.98]`, mean train Sharpe 0.76 vs. mean test
  Sharpe **1.34**, again not degraded relative to train, the same non-overfitting signature Epic
  15 found. **A real, honest divergence from Epic 15's monthly result**: the search converged to
  a LONGER lookback (8 weeks) than `portfolio2`'s shipped `lookback_period: 1.0` (4 weeks), not
  toward it, the opposite of the monthly regime's result. Not grounds to change the shipped
  config off one proxy-universe search, but a real data point, not glossed over. The single
  pre-registered holdout (`2015-01-02` to `2026-08-04`, `lookback_period=2.0` i.e. 8 weeks):
  CAGR 3.72%, AnnVol 7.49%, Sharpe 0.53, Sortino 0.77, MaxDrawdown -16.66%, WinRate 59.4%, Beta
  0.33, Alpha -0.70%. Block-bootstrap 90% CI on the holdout Sharpe: `[0.08, 1.05]` (97.2% of
  resamples positive), **entirely above zero**, unlike the monthly regime's CI which spanned
  zero. **Honest read**: statistically the strongest out-of-sample result in the project so far
  on the Sharpe dimension, real non-zero risk-adjusted return with reasonable confidence, but the
  still-negative alpha means most of that return traces to market-beta exposure (0.33 beta)
  rather than a clearly momentum-specific edge, same caution as the monthly result, not a
  green light. See `README.md`'s Project Maturity & Safety section and Known Gaps entry for the
  full numbers and caveats.
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
  replicates `get_top_etfs()`'s exact behavior (`ranks_row.dropna().nsmallest(top_n)`),
  `absolute_momentum` delegates to
  `select_absolute_momentum_picks(latest_scores, tickers, defensive_ticker)` (no `top_n` cutoff
  at all, every ticker with a positive OWN trailing score is held, `defensive_ticker` alone
  otherwise, `defensive_ticker` must be priced alongside the portfolio's own `tickers:`, the same
  "must be priced" requirement `dual_momentum`'s `use_absolute_momentum` already documents).
  `.dropna()` BEFORE `.nsmallest()`, not after, fixes a real, confirmed bug found via Epic 2 of
  the "Rebalance Reporting Clarity & Selection-Logic Fixes" plan's own real-deployed-code
  verification: `pandas.Series.nsmallest(n)` backfills with NaN rows when fewer than `n`
  non-null values exist (confirmed directly: `pd.Series([nan, nan]).nsmallest(2)` returns BOTH
  NaN entries, not an empty Series), so a NaN-ranked ticker (e.g. one zeroed out by
  `use_liquidity_filter`) could still get selected into `top_n` whenever fewer than `top_n`
  tickers had a valid rank, silently defeating the whole point of the liquidity filter in exactly
  the case it matters most. Confirmed both in a unit test and against real deployed code: a temp
  portfolio with `min_avg_dollar_volume` set absurdly high (every real ticker's dollar volume
  falls below it) previously still generated real BUY orders for the "illiquid" tickers; after
  the fix, zero orders. `get_top_etfs()` (`execution/live_signal.py`) got the identical fix.
  `is_universe_negative(scores_row, tickers)` (Epic 6, same plan) is the shared predicate (every
  valid score `<= 0`, a zero score is not positive, same convention
  `select_absolute_momentum_picks()` already uses) backing `cfg.use_negative_universe_cash_filter`
  (opt-in, default `False`): `resolve_strategy_picks()` checks this FIRST, before the
  `strategy_type` dispatch, forcing an immediate empty pick list (literal cash) when it
  triggers, so it correctly takes precedence over `absolute_momentum`'s own
  `select_absolute_momentum_picks()` (which never itself returns empty). `execution/
  live_signal.py`'s `run()` reuses this SAME predicate (not just reading `picks` empty) to
  detect, after the fact, whether THIS constraint specifically (not an unrelated cause like
  `use_liquidity_filter`) is what emptied `picks`, for a dedicated
  `MARKET_WIDE_NEGATIVE_MOMENTUM_CASH` alert and to guard the `use_absolute_momentum` overlay
  (see that file's own bullet for the real interaction bug this guard fixes).
  `generate_strategy_monthly_picks()`'s per-date loop got a real, confirmed parity fix while
  wiring this in: a date where `resolve_strategy_picks()` explicitly decided on ZERO picks
  (this constraint, or the pre-existing liquidity-filter case) used to be silently SKIPPED from
  the returned `monthly_picks` Series entirely, identical to the genuinely-different "no signal
  at all yet" case (lookback window not satisfied), letting a LATER rebalance's
  `monthly_picks.get(date, [])` lookup silently fall through to a STALE prior period's picks
  instead of correctly seeing "nothing was eligible then." Fixed: the loop's skip condition is
  now `if scores_row is None or scores_row.empty: continue` (only the genuine no-signal case),
  every other date is included even when `resolve_strategy_picks()` explicitly returned `[]`.
  See `docs/RISK_CONSTRAINTS.md`'s "Whole-Book Negative Momentum Cash Filter" for the documented
  scope boundary this fixed the SELECTION-layer half of: the narrower EXECUTION-layer half (a
  backtest's `run_risk_managed_backtest()` not force-liquidating on an explicit empty
  `target_tickers`) was deliberately left alone at the time, and has SINCE been fixed too, see
  this file's own bullet below.
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
  `generate_strategy_monthly_picks()` gained an optional `daily_volume: pd.DataFrame | None =
  None` param, the backtest-side counterpart to `execution/live_signal.py`'s `run()`'s live
  liquidity-filter wiring (see that file's own bullet for the full mechanism and its
  `absolute_momentum` caveat, identical here). Applied via
  `core/functions_quant_extensions.py`'s `liquidity_filter()` right after `ranks =
  assign_ranks(scores)`, before the per-date picks loop. Unlike the `hybrid_multi_factor`
  point-in-time-bias case just above, historical volume genuinely exists and isn't a look-ahead
  risk, so `cfg.use_liquidity_filter=True` without `daily_volume` provided raises a loud
  `ValueError` (same "fail loud, not a silent wrong number" precedent as
  `hybrid_multi_factor`'s `NotImplementedError`), rather than silently skipping the requested
  constraint. `daily_volume=None` (the default) is byte-identical to before this param existed.
  **Real out-of-sample validation for the other selectable strategy types, Epic 17, "Real
  Out-of-Sample Validation for the Other Selectable Momentum Strategies" plan**: Epic 15/16
  only validated `strategy_type: momentum` (monthly + weekly). Of the other 10,
  `relative_momentum` (documented alias) and `volatility_scaled_momentum` (preset already
  matches `portfolio1`'s own default) need no separate run, `hybrid_multi_factor` cannot be
  backtested at all (see this file's own bullet above), leaving 6 real variants for a full
  `run_walk_forward_lookback_search()` + holdout run plus `multi_timeframe_composite` on a
  holdout-only basis (its `resolve_strategy_scores()` dispatch never reads `lookback_period` at
  all, confirmed by reading it, a grid search over it would be a no-op).
  `notebooks/research/out_of_sample_validation_strategy_types.ipynb` (new) builds each variant's
  config from `dataclasses.asdict(portfolio1_cfg)` + `apply_strategy_type_preset()`, same real
  reuse pattern as Epic 15/16. **A real methodological bug found and fixed while building this,
  a test-harness issue, not a production code bug**: starting from a fully-materialized
  dataclass dict (every field present, including defaults) silently defeats
  `apply_strategy_type_preset()`'s "only fill in fields the user hasn't already set" contract,
  confirmed directly: the first run produced byte-identical results for `dual_momentum`/
  `correlation_weighted_momentum`/`rank_sign_momentum` vs. plain `momentum`, traced to
  `portfolio1`'s own `default_risk` already explicitly pinning every field these 3 presets would
  otherwise set. This is real, confirmed LIVE-config behavior too, not just a test artifact, see
  `docs/MOMENTUM_STRATEGIES.md`'s updated "How presets compose" section for the full writeup.
  Fixed for this validation by setting each preset's own field value directly (matching
  `daily_runner.STRATEGY_TYPE_PRESETS` exactly), not by changing `apply_strategy_type_preset()`
  itself, which behaves correctly on the sparse raw YAML dicts `load_config()` actually gives it.
  `SHY` (already in the cached proxy universe) substitutes for the default `defensive_ticker`
  (`"BIL"`, not in the cached panel) for `dual_momentum`/`absolute_momentum`, same proxy-universe
  substitution precedent as prior epics. **Real results** (run 2026-08-05, monthly regime,
  `portfolio1`'s config, 5-fold walk-forward, `lookback_candidates=[6,9,12,15,18]` months, same
  `pre_registered_split(split_date="2015-01-01")` as Epic 15): `dual_momentum`'s backtest is
  byte-identical to plain `momentum` (`use_absolute_momentum` is documented LIVE-ONLY, no
  backtest effect, `use_regime_filter` was already on, nothing left to differ), a correct,
  expected result, not a bug. `correlation_weighted_momentum` (mean train/test Sharpe 0.85/1.02,
  holdout Sharpe 0.38, alpha -2.48%), `rank_sign_momentum` (0.83/0.93, holdout 0.40, alpha
  -2.70%), and `path_dependent_momentum` (chosen lookback 9mo, 0.84/0.86, holdout 0.37, alpha
  -2.75%) all cluster close to plain `momentum`'s own already-published result.
  `absolute_momentum` shows the strongest walk-forward Sharpe of the base-score types
  (1.31/1.24) but the weakest holdout CAGR (1.79%) and widest, most-negative bootstrap CI
  (`[-0.20, 0.87]`), a real, honest divergence between train/test robustness and the single
  holdout outcome. `residual_momentum` (1.22/1.25, holdout Sharpe **0.46**, alpha **-0.17%**,
  90% CI **`[0.08, 0.96]`**, entirely above zero) produced the strongest out-of-sample result of
  any epic in this project so far. `multi_timeframe_composite`'s holdout-only result (Sharpe
  0.55, alpha -0.92%, CI `[0.07, 1.10]`) is the second-strongest number, but rests on weaker
  evidence, no walk-forward robustness check applies to it. None of this changes the shipped
  `strategy_type: momentum` default, it's additional evidence for anyone considering an
  alternative, not a recommendation to switch. See `README.md`'s Known Gaps entry for the full
  per-strategy table.
  **Extended to the WEEKLY regime, Epic 18, "Real Out-of-Sample Validation for the Other
  Selectable Momentum Strategies, Weekly Regime" plan**: closes the gap Epic 17 above explicitly
  flagged, mirroring Epic 16's own monthly-to-weekly extension of Epic 15. Same 6 variants +
  `multi_timeframe_composite` holdout-only, `portfolio2`'s real weekly config, week-quarter
  `lookback_candidates=[0.5, 0.75, 1.0, 1.5, 2.0]`. Two real mechanical facts confirmed by
  reading the code directly before running anything: `resolve_path_dependent_momentum_scores()`
  already correctly branches on `holding_period < 1` (no special handling needed), and
  `multi_timeframe_composite`'s `resolve_strategy_scores()` dispatch ALWAYS resamples to monthly
  regardless of `holding_period`, confirmed directly, so its weekly-regime run tests "a
  monthly-timeframe-blended signal, rebalanced weekly," not a weekly signal, a real, distinct
  scenario from Epic 17's monthly-rebalance version. **This ALSO surfaced a real, pre-existing
  documentation bug, fixed alongside this epic**: `docs/MOMENTUM_STRATEGIES.md`'s own "Best
  Parameters" table previously recommended `multi_timeframe_lookbacks: [1, 2, 4]` described as
  "weeks" for the weekly preset, wrong given the always-monthly resample just confirmed, those
  values would actually be interpreted as MONTHS; corrected to `[3, 6, 12]` (same as monthly,
  there is no weekly-scale variant of this field) with an explanatory note.
  **A real no-op case anticipated in advance, not stumbled into**: `portfolio2`'s own
  `risk_overrides` already sets `use_correlation_penalty: true` directly, so `correlation_
  weighted_momentum`'s preset has zero effect vs. `portfolio2`'s own already-published Epic 16
  weekly `momentum` baseline, confirmed by the real run (`dual_momentum` and `correlation_
  weighted_momentum` both came back byte-identical to each other AND to Epic 16's own baseline:
  chosen_lookback 8wk, holdout Sharpe 0.52, CAGR 3.71%, alpha -0.72%, CI `[0.08, 1.04]`).
  **Real results** (run 2026-08-05): `rank_sign_momentum` (equal-weight sizing, chosen lookback
  8wk, mean train/test Sharpe 0.72/1.39, holdout Sharpe **0.63**, CAGR **5.27%**, alpha -0.50%,
  CI `[0.19, 1.13]`) produced the best full-search weekly holdout of the batch.
  `residual_momentum` (1.08/1.34, holdout Sharpe 0.59, alpha **-0.09%**, CI `[0.12, 1.14]`) again
  shows the closest-to-zero alpha of any variant tested across Epic 15-18 combined, the same
  standout pattern as its monthly result (Epic 17), not a one-off. `absolute_momentum` (chosen
  lookback only 3wk, holdout Sharpe 0.29, alpha -1.76%, CI `[-0.16, 0.75]`) and `path_dependent_
  momentum` (6wk, holdout Sharpe 0.41, alpha -1.73%, CI `[-0.06, 0.91]`) are the weakest of the
  batch, echoing their weaker monthly-regime showing too. `multi_timeframe_composite`'s
  holdout-only result (Sharpe 0.61, alpha -0.63%, CI `[0.11, 1.15]`) is again strong despite its
  underlying signal staying monthly. This closes the out-of-sample validation program for the 6
  real non-alias `strategy_type` variants across both regimes. See `README.md`'s Known Gaps
  entry for the full per-strategy table.
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
  `execution/live_signal.py`'s `compute_required_lookback_days(cfg, buffer_days=60)` sizes the
  LIVE `fetch_live_prices()` call to what the portfolio's config actually needs, fixing a real,
  confirmed incident: `fetch_live_prices()`'s old fixed `lookback_days=400` default was NEVER
  scaled to `lookback_period`/`holding_period`, and gave the shipped default
  (`lookback_period=12`) only a 1-monthly-bar margin; a monthly `lookback_period` as
  unremarkable as `18`, or a weekly one around `15` (60 weeks), produced an ENTIRELY NaN
  latest-row score (`calculate_period_returns()`'s `pct_change(periods=...)` with insufficient
  history), which `get_top_etfs()`'s `nsmallest()` silently turns into ZERO picks, no exception,
  no diagnostic. Covers every real consumer of `daily_prices`, not just momentum ranking (the
  same DataFrame also feeds `compute_target_weights()`'s regime filter, portfolio/position vol
  targeting, and correlation checks, each with the identical silent-NaN failure mode if
  under-fetched). Wired into BOTH real fetch call sites: `daily_runner.py`'s "ALWAYS runs" block
  and `run()`'s own internal fallback fetch. LIVE-ONLY, `lookback_period` has no effect on the
  backtest engine. A defensive backstop (`scores.empty` after `resolve_strategy_scores(...)
  .dropna(how="all")`, right where `scores`/`latest_scores` are computed in `run()`) logs an
  `INSUFFICIENT_PRICE_HISTORY` `WARNING` (via `log_alert()`, matching this file's own
  log-only-no-direct-email convention, `daily_runner.py` owns email-sending for every other
  advisory check, don't reintroduce a direct `send_action_email()` call from inside this file)
  for the residual edge case sizing alone can't fix (a vendor genuinely not having that much
  real history for a given ticker), so a future occurrence is immediately diagnosable instead of
  a silent empty rebalance (which, worse, would also SELL every currently-held position, since
  `generate_orders()`'s target universe would be empty too, not merely "no new buys").
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
  `_apply_position_caps()` (the `max_position_weight` cap-and-redistribute pass
  `resolve_target_weights()` applies unconditionally) had a real, confirmed bug, fixed via Epic 4
  of the "Rebalance Reporting Clarity & Selection-Logic Fixes" plan: when a ticker over the cap
  has no ticker left under it to redistribute the excess into (a single-ticker portfolio hitting
  the cap, or every picked ticker simultaneously over cap), the loop correctly `break`s without
  redistributing, but the OLD code's final renormalize-to-`1.0` step then unconditionally
  rescaled every weight back up, silently defeating the cap (a single ticker capped to `0.35`
  ended back at `1.0`). A new `redistribution_incomplete` flag tracks exactly this case and skips
  the renormalize, correctly leaving weights summing to LESS than `1.0`, the undistributable
  excess genuinely unallocated (cash) rather than invested anyway. Real downstream consequence:
  `generate_orders()`'s `money_invested` sums-to-`total_value * gross_exposure` invariant now
  only holds when the cap never has to leave a shortfall, see that function's own docstring and
  `docs/RISK_CONSTRAINTS.md`'s "Position Size Hard-Cap".
  `ticker_sectors: dict = field(default_factory=dict)` + `max_sector_weight: float | None = None`
  (Epic 3, "Redefining Stop-Loss Price, Plus Two Remaining Known Gaps" plan, Nice-to-Have tier,
  opt-in, `{}`/`None` byte-identical to before) back the "Sector / Asset-Class Concentration Cap":
  `max_position_weight`/`position_vol_budget` only ever constrain a SINGLE ticker, nothing
  previously stopped several picks in the SAME sector from combining into an outsized aggregate
  exposure. `ticker_sectors` is a manual `{ticker: sector_name}` mapping, deliberately, this
  project has no vendor sector-data integration (`core/fundamentals.py` covers P/E, PEG, ROE,
  Debt-to-Equity, Current Ratio, not sector), a ticker absent from the mapping is never
  grouped/capped at all, not a silent error. `_apply_sector_caps(weights, ticker_sectors,
  max_sector_weight)` (`backtest/momentum_backtest.py`) is wired into `resolve_target_weights()`
  as the LAST step, after `_apply_position_caps()` and the optional
  `_apply_volatility_budget_caps()`, so it constrains the FINAL, fully-capped weights. For any
  sector whose summed weight exceeds the cap, every ticker in that sector scales down
  proportionally so the sector sums to exactly the cap. Deliberately simpler than
  `_apply_position_caps()`'s redistribute-to-others logic: the freed weight is left as
  unallocated gross exposure (cash), NOT redistributed elsewhere, a conscious, conservative
  choice, redistributing into other tickers/sectors could push one of THEM over its own
  `max_position_weight` or `max_sector_weight`, a multi-constraint interaction
  `_apply_position_caps()`'s own single-dimension redistribution never has to reason about, same
  "reduce exposure rather than silently violate a cap" precedent as that function's own
  `redistribution_incomplete` fix. See `docs/RISK_CONSTRAINTS.md`'s "Sector / Asset-Class
  Concentration Cap".
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
  `use_negative_universe_cash_filter` (default `False`, opt-in, same "changes the actual signal
  when enabled" caution) backs the "Whole-Book Negative Momentum Cash Filter" (Epic 6,
  "Rebalance Reporting Clarity & Selection-Logic Fixes" plan): distinct from
  `use_absolute_momentum` above (a per-ticker swap, still ends up invested), this is a
  whole-book decision, literal cash, when NOTHING in the eligible universe shows positive
  momentum. Wired into `core/strategy_signals.py`'s `resolve_strategy_picks()` (see that file's
  own bullet for the full mechanism, the real `absolute_momentum_overlay()` interaction bug it
  fixes, and `docs/RISK_CONSTRAINTS.md`'s documented backtest-execution-layer scope boundary).
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
  `regime_vol_threshold: float | None = None` (opt-in, `None` byte-identical to before) +
  `regime_vol_lookback_days: int = 21` blend a second dimension into the existing SMA-only
  regime filter's `regime_scalar`: the regime benchmark's own trailing realized volatility
  (annualized) exceeding this threshold also pushes exposure defensive, even when the SMA trend
  is still bullish, so a bullish-but-suddenly-volatile market gets throttled too, not just a
  bearish one. `run_risk_managed_backtest()` precomputes a `regime_high_vol` boolean series
  (benchmark's rolling realized vol vs. threshold, reindexed to the price panel) alongside the
  pre-existing `regime_bullish` series; the per-date loop ORs the two
  (`regime_scalar = min_gross_exposure if (not bullish or high_vol) else 1.0`), still ONE scalar
  composed multiplicatively with `vol_scalar` exactly as before, not a new hard gate.
  `execution/live_signal.py`'s `compute_target_weights()` gets the identical formula (see that
  bullet below), same "live and backtest must not diverge" principle every other regime/vol
  mechanism here follows. See `docs/RISK_CONSTRAINTS.md`'s "Regime Filter: Volatility
  Dimension".
  `momentum_crash_lookback_days: int | None = None` + `momentum_crash_derate: float = 0.5`
  (Epic 14, Daniel & Moskowitz 2016 "Momentum Crashes") are a THIRD regime dimension, opt-in,
  `None` byte-identical to before. A real design trap was found and avoided while planning this:
  the first instinct was to reuse `min_gross_exposure` for a new bear-market-AND-high-vol
  condition (matching how `use_correlation_spike_regime` already clamps to that same shared
  floor), but tracing the logic confirmed this would be COMPLETELY REDUNDANT, since
  `regime_vol_threshold`'s `high_vol` ALONE already floors `regime_scalar` to
  `min_gross_exposure` today, an AND-condition requiring the SAME `high_vol` flag and clamping to
  the SAME floor adds nothing. DM's actual finding is narrower and more dangerous than "high vol
  alone": momentum crashes specifically when the market has been in a SUSTAINED prior downturn
  AND is volatile AT THE SAME TIME (past losers, excluded from a long-only book, violently
  rebound during exactly this joint regime). Implemented instead as an ADDITIONAL multiplicative
  derate stacked ON TOP of `regime_scalar * vol_scalar`
  (`gross_exposure = min(max_gross_exposure, regime_scalar * vol_scalar * momentum_crash_scalar)`,
  `momentum_crash_scalar = momentum_crash_derate if (bear_now and high_vol_now) else 1.0`), able
  to push exposure BELOW `min_gross_exposure` specifically during this one empirically-worse
  joint regime, which nothing else here can do, the real, genuinely incremental protection.
  Requires `regime_vol_threshold` to also be set (reuses that SAME elevated-vol signal rather
  than a duplicate threshold field), validated in `__post_init__`, fail loud not silent, same
  precedent as `use_liquidity_filter`'s missing-`daily_volume` `ValueError`.
  `momentum_crash_bear` (`bench.pct_change(periods=momentum_crash_lookback_days) < 0`, the
  bear-market half) is precomputed alongside `regime_bullish`/`regime_high_vol`, reusing
  `regime_high_vol` for the vol half, no duplicate vol computation.
  `execution/live_signal.py`'s `compute_target_weights()` gets the identical formula (single
  most-recent-value computation, same `.iloc[-1]`/`.tail(...)` pattern `regime_vol_threshold`'s
  own live code already uses), plus a new `MOMENTUM_CRASH_PROTECTION_ACTIVE` `WARNING` alert
  (`log_alert()`), same triple-step pattern as `MARKET_VOLATILITY_REGIME_DEFENSIVE`, fired only
  when THIS condition (not the pre-existing OR condition) is what's driving the extra derate.
  Confirmed via 4 deterministic live-side unit tests (`TestMomentumCrashDynamicScaling`) that this
  is genuinely an AND, not reachable via `high_vol` or the bear condition alone.
  Two real, confirmed gaps were found and fixed while validating this against real 2008/2020/2022
  historical data (`notebooks/research/crash_period_stress_test.ipynb`, Epic 13's cached proxy
  universe), not synthetic: (1) `compute_required_lookback_days()` (`execution/live_signal.py`)
  did not include `momentum_crash_lookback_days` in its candidates, despite its own docstring's
  "covers every real consumer of `daily_prices`" promise; `momentum_crash_lookback_days` (e.g.
  504, ~24 months) is far larger than every other candidate there, so without this,
  `bench.pct_change(periods=504).iloc[-1]` would be ALWAYS `NaN` against `fetch_live_prices()`'s
  own 400-day default, silently meaning this feature could never fire in real live trading, the
  identical silent-NaN failure mode that function exists to prevent for every other consumer.
  Fixed: added to the candidates list. (2) A deeper, previously-latent issue in
  `run_risk_managed_backtest()` itself: `regime_bullish`/`regime_high_vol`/`momentum_crash_bear`
  were all computed from `prices` (the ALREADY-simulation-window-masked panel, starting only at
  `sim_start_date - 1 day`), not from `close_full` (the caller's full, pre-mask `daily_prices`),
  so a long lookback could be entirely `NaN` even when the caller's own panel had plenty more
  real history before that. Fixed: `bench` now sources from `close_full`, every output still
  `.reindex(prices.index)` so only in-window dates are ever looked up during the day-loop.
  Purely additive: byte-identical whenever the caller already provided enough buffer (the common
  case, confirmed by the full pre-existing suite passing unchanged), only fixes the previously-
  silent shortfall otherwise; benefits `regime_sma_window`/`regime_vol_threshold` too, not just
  this new field, though their smaller windows made the gap far less visible before now.
  A third, separate methodology bug was found in the crash-period notebook itself (not `src/`):
  it was passing an already-window-truncated price panel (crash-period start to end only) to
  `run_custom_backtest()`, discarding the 2005+ history a 504-day lookback needs even after fix
  (2) above, since the notebook's OWN truncation happened before the function ever saw the data.
  Fixed: the notebook now bounds the price panel only at the window's END, not its START (picks
  stay window-bound, governing `sim_start_date` exactly as before).
  **Real validation results** (12 backtests, 2008/2020/2022, both regimes, `momentum_crash_
  lookback_days=504`/`derate=0.5`/`regime_vol_threshold=0.25`, run 2026-08-04, after both fixes):
  the condition fired 9/39/1/2 times across the four 2008-GFC/2020-COVID monthly/weekly
  scenarios (0 times in 2022's slower, non-crash-shaped decline), and when it fired, modestly
  IMPROVED max drawdown at a modest cost to total return, honest and mixed, not a clean win the
  way Epic 13's baseline-vs-full comparison was; this specific 3-way notebook comparison also
  can't fully isolate the marginal effect in isolation (the `full + momentum_crash_protection`
  variant necessarily also turns on `regime_vol_threshold` as a prerequisite, a confound the
  deterministic unit tests don't have). See `README.md`'s Known Gaps entry for the full table.
  A real, previously-uncatalogued IBKR informational code (`2109`, "Order Event Warning:
  Attribute 'Outside Regular Trading Hours' is ignored... PlaceOrder is now being processed")
  surfaced during this epic's real paper-account regression test (2026-08-04) and was added to
  `IBKR_INFORMATIONAL_CODES`, confirmed non-fatal (orders carrying it filled normally with a real
  `execDetails`/`commissionReport` in that same run).
  `run_risk_managed_backtest()`'s rebalance-trigger condition (Epic 2, "Redefining Stop-Loss
  Price, Plus Two Remaining Known Gaps" plan) closes the backtest/live parity gap documented
  above and in `docs/RISK_CONSTRAINTS.md`'s "Whole-Book Negative Momentum Cash Filter": was `if
  target_tickers and not circuit_breaker_halted:`, wrapping regime/vol/sizing AND
  `target_dollar` construction together, so an EXPLICIT empty `target_tickers` (the whole-book
  negative-momentum cash filter, or the liquidity filter, zeroing out every pick) skipped the
  ENTIRE block, silently carrying forward whatever was already held instead of selling it,
  unlike live's `generate_orders()`, which already funnels an empty `target_weights` through the
  same sell/buy pipeline as any other rebalance. Now `if not circuit_breaker_halted:`, with only
  the regime/vol/sizing sub-block conditional on `target_tickers` (unchanged when truthy); the
  `else` branch sets `target_dollar = {}` directly and logs an `"EXPLICIT EMPTY PICKS"` line, no
  new sell/buy logic, this reuses the EXACT SAME `current_value`/`raw_trades`/aggregate-drift-
  skip/drift-threshold/sells-then-buys pipeline (unchanged, unindented) that already correctly
  liquidates a real position whenever `target_dollar` doesn't include it. `circuit_breaker_halted`
  still overrides both branches identically (unchanged): a halted portfolio is neither
  rebalanced nor force-liquidated, matching live's own circuit-breaker semantics ("halt NEW
  rebalancing," not "force an exit"). A genuine, pre-existing, unrelated quirk was found and
  worked around (not fixed, out of that epic's scope at the time) while building that epic's
  regression tests: the very FIRST signal date in ANY `monthly_picks` series could have its
  computed rebalance date collide with `run_risk_managed_backtest()`'s own simulation-window
  start date (`sim_start_date`, itself derived from that same first signal date), which the
  day-loop's old `for today in prices.index[1:]:` then silently excluded, so the FIRST rebalance
  in a short/synthetic `monthly_picks` series could silently never fire, depending on exact
  calendar alignment (confirmed directly: reproducible with `pd.bdate_range` fixtures, whether it
  triggered depended on where weekends/`NYSE` holidays fell relative to that first signal's month
  boundary). Real, multi-year `monthly_picks` series used elsewhere in this project's own
  tests/notebooks never surfaced this (losing only the very first of many rebalances doesn't
  visibly break anything), which is presumably why it was never caught before this. **Fixed**
  (Epic 11, "Fix First-Rebalance-Date Collision" plan): the day-loop now iterates the FULL
  `prices.index` (the `[1:]` slice is gone). `prices.index[0]` is meant to be a pre-simulation
  "T-1" buffer day (the price-panel mask keeps one calendar day before `sim_start_date`), correct
  when a real trading day exists there; when it doesn't (the collision case), `prices.index[0]`
  collapses to `sim_start_date` itself, and the old slice skipped it. Processing it unconditionally
  is safe in BOTH cases: in the common case it's never itself a computed rebalance date, so the
  day-loop's own blocks are no-ops for it except the unconditional end-of-day valuation, which
  appends an exact duplicate of the pre-loop `portfolio_history` seed (same date, same
  `initial_capital`); `_build_report()`'s own `daily[~daily.index.duplicated(keep="last")]`
  (confirmed by reading it, unchanged) already collapses that duplicate, so this is byte-identical
  output for every real multi-year backtest and every pre-existing test fixture. In the collision
  case, the rebalance block now correctly fires on `sim_start_date`, a real position opens, and the
  loop's own valuation overwrites the seed's placeholder value via that same dedup. See
  `tests/backtest/test_momentum_backtest.py`'s `TestFirstRebalanceDateCollision` (deliberately
  does NOT use the "December warm-up" padding pattern other tests in that file use specifically to
  avoid this collision, reproduces it directly instead).
  `use_trailing_stop: bool = False` + `trailing_stop_pct: float | None = None` (Epic 1,
  "Institutional Momentum Best-Practice Gaps" plan, opt-in, `False` byte-identical to before)
  back the "Trailing Stop-Loss" constraint: distinct from `stop_loss_pct` (fixed from entry,
  never ratchets), this exits once price has fallen `trailing_stop_pct` from a position's OWN
  highest price since entry, locking in gains as a position runs up, not just capping losses. A
  deliberate Python-side daily ratchet (same "daily check" philosophy as the existing fixed
  stop-loss block right above it in `run_risk_managed_backtest()`'s day loop), NOT a
  broker-native IBKR `TRAIL` order (a considered choice, not an oversight, see
  `docs/RISK_CONSTRAINTS.md`'s "Trailing Stop-Loss" for the full rationale). A `running_high: dict`
  tracked alongside the pre-existing `entry_prices: dict`, identical lifecycle (both set on a new
  BUY, both cleared together on any exit, whether triggered by the fixed stop, the trailing stop,
  the time-based stop, or a normal rebalance SELL), so a closed-then-reopened position always
  starts a fresh trail rather than inheriting a stale high. `execution/live_signal.py`'s
  `check_and_handle_trailing_stops()` is the LIVE counterpart, see that file's own bullet.
  `sizing_method` gained a 4th value, `"risk_based"` (Epic 3, "Institutional Momentum
  Best-Practice Gaps" plan), fixed-fractional/Van Tharp sizing, Part I's "risk 1-2% of capital
  per trade" rule: `_risk_based_weights(picks, cfg)`, parallel to `_score_proportional_weights()`/
  `_equal_weight_weights()`, `weight = cfg.risk_per_trade_pct / resolve_ticker_stop_loss_pct(t,
  cfg)` per pick (a tighter stop gets a larger weight), a pick with a disabled per-ticker stop
  falls back to an equal-weight `1/N` slice for just that ticker (same per-ticker-fallback
  precedent `_score_proportional_weights()`'s missing-score case uses). New `risk_per_trade_pct:
  float = 0.02` field, validated `(0, 1)` unconditionally same as `stop_loss_pct`; `sizing_method`'s
  `__post_init__` validation extended to accept `"risk_based"`. Deliberately does NOT normalize
  to sum to `1.0` the way the other three `sizing_method`s do (aggregate exposure emerges from
  the risk budget): a raw sum over `1.0` scales down proportionally (preserving each position's
  relative risk allocation), a raw sum under `1.0` is left as genuine unallocated cash, same
  "leave undistributable weight as cash" precedent `_apply_position_caps()`'s
  `redistribution_incomplete` and `_apply_sector_caps()` already establish. Wired into
  `resolve_target_weights()` as a 4th `elif cfg.sizing_method == "risk_based":` branch, same
  correlation-penalty/position-cap/vol-budget/sector-cap pipeline applied afterward as the other
  three.
  `resolve_ticker_stop_loss_pct(ticker, cfg)` MOVED here from `execution/live_signal.py` (which
  now `from ..backtest.momentum_backtest import (..., resolve_ticker_stop_loss_pct)` and
  re-exports it unchanged for its own call sites and `daily_runner.py`'s existing import, zero
  behavior change, confirmed by every pre-existing test of it still passing unmodified): a pure
  function of `BacktestConfig` alone, needed by this module's own new `_risk_based_weights()`
  without introducing a `backtest/` -> `execution/` import, which would be circular
  (`execution/live_signal.py` already imports `BacktestConfig`/`resolve_target_weights`/
  `compute_vol_scalar`/etc. FROM this module at module load time, the established one-directional
  dependency the whole codebase already follows).
  A real, confirmed bug found and fixed while adding `risk_based` sizing, in
  `_apply_position_caps()`, a function unrelated to this feature at the code level but shared by
  every `sizing_method`: its final renormalize targeted a hardcoded `1.0`, not the input's own
  pre-cap total, invisible until now because every PRE-EXISTING `sizing_method` already sums to
  `~1.0` before this function runs. `risk_based`'s deliberately-often-under-`1.0` input exposed
  it directly: even when NO ticker was anywhere near `max_position_weight` (nothing to cap at
  all), the old code still silently rescaled the whole book back up to `1.0`, defeating
  `risk_based` sizing's entire point. Fixed: the renormalize now targets `original_total` (the
  input's own sum captured before the cap-and-redistribute loop runs), byte-identical to the old
  hardcoded `1.0` for every sizing method whose input already summed to `1.0` (confirmed by the
  full pre-existing `TestApplyPositionCaps`/`TestResolveTargetWeights` suites passing unchanged),
  and correctly a no-op for `risk_based`'s genuinely-under-invested input. See
  `docs/RISK_CONSTRAINTS.md`'s "Risk-Based (\"Fixed-Fractional\") Position Sizing" and "Position
  Size Hard-Cap"'s own updated section.
  `enabled_risk_strategies: list = field(default_factory=list)` (Epic 1, "Institutional
  Risk-Management Features" plan, Story 1.1) is a SECOND, deliberately separate opt-in enabling
  mechanism alongside this file's many pre-existing `use_X: bool` fields, `[]` default
  byte-identical to before this field existed. Motivated by a real, requested audit against 24
  institutional/hedge-fund risk-management practices (`docs/RISK_CONSTRAINTS.md`'s new
  "Institutional risk-practice audit" section has the full 24-item table): 8 already implemented,
  4 fundamentally Not Applicable (this system is strictly long-only, cash-only, single-broker, no
  shorting/derivatives/margin, same category as the pre-existing HTB Sentinel row), 12 real gaps
  phased into Epics 1-4 by risk tier. Reserved ONLY for genuinely independent, stackable overlay
  strategies, not a mutually-exclusive dispatch choice (Equal Risk Contribution, a NEW
  `sizing_method` value below, is deliberately NOT here, `sizing_method` already IS this
  codebase's own "pick by name" mechanism for that, the same way `"risk_based"` needs no separate
  `use_risk_based_sizing: bool` today). `RISK_STRATEGY_ALIASES: dict[str, frozenset[str]]`
  (module-level, alongside `ALLOWED_STRATEGY_TYPES`) maps each canonical overlay name to its
  accepted alias strings; `risk_strategy_enabled(cfg, canonical) -> bool` is the one helper every
  gated call site uses. `__post_init__` validates every string in `enabled_risk_strategies`
  resolves via the registry, failing loud on an unrecognized name (a typo'd alias silently doing
  nothing would be a worse failure mode, matching `sizing_method`'s own unknown-value handling).
  Two canonical names ship with Epic 1: `var_cvar_budget` (Story 1.4, an active VaR/CVaR
  pre-trade gross-exposure throttle) and `liquidity_adjusted_sizing` (Story 1.5, continuous
  ADV-based position-size scaling, distinct from the pre-existing advisory-only `max_pct_of_adv`/
  `check_capacity()` warning, which is left completely unchanged). See
  `docs/RISK_CONSTRAINTS.md`'s "Opt-in overlay strategies (`enabled_risk_strategies`)" section.
  `core/covariance.py`'s `shrinkage_covariance(returns, shrinkage=None)` (Epic 1, Story 1.2) is a
  NEW, focused module (matching `core/technical_indicators.py`'s "pure numerical domain gets its
  own file" precedent), a Ledoit-Wolf-style shrinkage covariance estimator shrinking the raw
  sample covariance toward a constant-correlation target, better-conditioned than a raw sample
  matrix when the number of return observations is small relative to the number of tickers. A
  real, confirmed gap found while auditing this project's existing correlation mechanisms:
  `detect_correlation_spike()` and `_correlation_penalty_weights()` (this file) both compute
  their correlation matrix via raw `pandas.DataFrame.corr()`, zero regularization, confirmed by
  reading both directly. `shrinkage=None` (default) computes a practical analytic shrinkage
  intensity, honestly documented in the function's own docstring as a simplified variant of
  Ledoit & Wolf (2004)'s original formula, not a research-grade replication of the paper;
  `shrinkage=0.0` degenerates to the raw sample covariance exactly (the regression anchor). New,
  unwired utility on its own, zero behavior change anywhere until opted into.
  `use_shrinkage_covariance: bool = False` (plain bool, a modifier of the already-`use_X`-gated
  `use_correlation_penalty` mechanism, deliberately NOT routed through `enabled_risk_strategies`)
  wires it into `_correlation_penalty_weights()`: `False` (default) is bit-identical to this
  function's pre-Epic-1 behavior (confirmed by a dedicated regression test); `True` derives the
  correlation matrix from the shrunk covariance instead, converted back to a correlation matrix
  before the SAME downweighting formula already there, this changes HOW the matrix is estimated,
  not WHAT the penalty does with it. Falls back to the raw-`.corr()` path on a degenerate
  edge-case window (e.g. too few tickers/observations for a shrinkage solve after column
  alignment) rather than crashing a rebalance.
  `sizing_method` gained a 5th value, `"equal_risk_contribution"` (Epic 1, Story 1.3):
  `_equal_risk_contribution_weights(picks, daily_prices, as_of, cfg)`, a standard iterative
  risk-parity solve (multiplicative proportional scaling toward equal marginal risk
  contribution, damped via a sqrt step for numerical stability, ERC has no general closed form),
  using `shrinkage_covariance()` above as its covariance input directly, not raw sample
  covariance, the textbook motivating case for shrinkage: a risk-parity solve is sensitive to a
  poorly conditioned matrix, exactly the regime a `correlation_lookback_days`-scale window small
  relative to the number of tickers produces. Selected purely via `sizing_method`'s own existing
  "pick by name" mechanism, deliberately NOT gated via `enabled_risk_strategies` (see that
  registry's own bullet above for why: `sizing_method` already IS this codebase's "pick by name"
  mechanism for a mutually-exclusive sizing choice, the same way `"risk_based"` needs no separate
  `use_risk_based_sizing: bool` today). When `use_correlation_penalty` is also enabled, the
  correlation penalty still applies on top of ERC's own risk-balanced weights, same as every
  other `sizing_method`, consistent rather than special-cased. Degenerate case (fewer than 2
  valid tickers, or an ill-posed/degenerate covariance solve): falls back to
  `_inverse_vol_weights()`, same per-edge-case-fallback precedent
  `_score_proportional_weights()`'s own missing-score case already establishes. Wired into
  `resolve_target_weights()` as a 5th `elif cfg.sizing_method == "equal_risk_contribution":`
  branch, same position/shape as the other four, same downstream position-cap/vol-budget/
  sector-cap pipeline applied afterward. Live side needed zero new code:
  `execution/live_signal.py`'s `compute_target_weights()` already calls this same shared
  `resolve_target_weights()` function, confirmed via a dedicated live/backtest parity test
  proving byte-identical weights given identical inputs. See `docs/RISK_CONSTRAINTS.md`'s
  "Equal Risk Contribution (ERC) Sizing" section.
  `compute_var_cvar_scalar(cvar_pct, var_budget_pct, min_gross_exposure, max_gross_exposure)`
  (Epic 1, Story 1.4, co-located with `compute_vol_scalar()`, exact same shape/fallback
  semantics: `clip(var_budget_pct / cvar_pct, min, max)`, falls back to `max_gross_exposure`
  when `cvar_pct` is `None`/`0`) is a NEW multiplicative gross-exposure scalar closing a real,
  confirmed gap found during this epic's audit: `core/functions_quant_extensions.py`'s
  `historical_var_cvar()` existed, fully coded, before this epic, but was wired into nothing,
  its only 2 callers in the entire repository were `tests/test_governance.py`, confirmed by
  grep, zero references in `execution/live_signal.py`, `backtest/momentum_backtest.py`, or
  `daily_runner.py`, the exact "scaffold exists, never wired in" pattern this project has closed
  before (Epic 15's `run_walk_forward_lookback_search()`, Epic 6's
  `absolute_momentum_overlay()`). Gated via `enabled_risk_strategies: [var_cvar_budget]` (new
  `var_budget_pct: float | None = None`, `var_cvar_confidence: float = 0.95`,
  `var_cvar_lookback_days: int = 252`), `__post_init__` requires `var_budget_pct` be set when
  enabled (loud error, matching `use_trailing_stop`/`trailing_stop_pct`'s precedent). Composed
  as an ADDITIONAL multiplicative term alongside `vol_scalar`/`momentum_crash_scalar`, NOT a
  step inside `resolve_target_weights()`, that function operates purely in weight-space and has
  no concept of a realized portfolio-returns series to compute CVaR from, unlike
  `target_portfolio_vol`'s `vol_scalar`, which each engine already sources independently:
  `run_risk_managed_backtest()` sources it from the simulated `portfolio_history` equity curve's
  trailing `var_cvar_lookback_days` returns (the same source `_realized_portfolio_vol()` already
  uses for `vol_scalar`), while `execution/live_signal.py`'s `compute_target_weights()` sources
  it from a NEW shared `_weighted_portfolio_returns_series()` helper, extracted (a pure refactor,
  confirmed byte-identical via regression test) from what used to be
  `_realized_weighted_portfolio_vol()`'s own inline body, so the two live-side consumers of
  "what counts as the portfolio's own realized returns" can never silently disagree. This
  preserves the existing, deliberate backtest/live realized-vol sourcing asymmetry
  `target_portfolio_vol` already documents. Each engine appends `var_cvar_scalar` to
  `gross_exposure`'s multiplicative composition in ITS OWN existing order (backtest:
  `regime_scalar * vol_scalar * momentum_crash_scalar * var_cvar_scalar`; live: same, computed
  after `vol_scalar` per that file's own existing sequence), not copy-pasted verbatim between
  the two files, a real parity trap this project has hit before with other scalars. A new
  `VAR_CVAR_BUDGET_EXCEEDED` `WARNING` (`log_alert()`) fires live when the scalar meaningfully
  throttles exposure, same triple-step pattern as `MARKET_VOLATILITY_REGIME_DEFENSIVE`. See
  `docs/RISK_CONSTRAINTS.md`'s "VaR/CVaR Budget (Active Pre-Trade Constraint)" section.
  `core/functions_quant_extensions.py`'s new `scale_dollar_targets_for_capacity(target_dollar,
  df_volume, df_prices, as_of, max_pct_of_adv, lookback_days=21)` (Epic 1, Story 1.5,
  Liquidity-Adjusted Position Sizing) is the ACTIVE counterpart to the pre-existing
  `check_capacity()` (co-located, same file), which is purely advisory, confirmed by reading it:
  it runs strictly AFTER `execution/live_signal.py`'s `run()` has already called
  `generate_orders()` and computed final share counts, and only logs a `CAPACITY WARNING`, never
  mutating a size. The new function reuses `check_capacity()`'s own ADV-dollar-volume formula
  internally (not a second, divergent formula) and scales down any ticker's target dollar
  allocation exceeding `max_pct_of_adv * adv_dollar` to exactly that ceiling; a ticker under the
  cap is unchanged, freed capacity left as unallocated cash, not redistributed, the same "reduce
  exposure rather than silently violate a cap" precedent `_apply_sector_caps()`
  (`backtest/momentum_backtest.py`) already establishes. Gated via `enabled_risk_strategies:
  [liquidity_adjusted_sizing]`, resolved via new `resolve_ticker_max_pct_of_adv(ticker, cfg)`
  (`backtest/momentum_backtest.py`, same shape/placement as `resolve_ticker_stop_loss_pct()`),
  backing a 3rd allowed `ticker_risk_overrides` key (`max_pct_of_adv`, alongside the pre-existing
  `enabled`/`stop_loss_pct`), NOT gated by `enabled`, a distinct concern from the stop-loss
  check. Deliberately implemented DOWNSTREAM of `resolve_target_weights()`, not inside it: that
  function operates purely in weight-space with no concept of total deployable capital, pushing
  dollar-awareness into it would require widening a single-source-of-truth function's signature
  for every existing caller across both engines, a materially larger and riskier change. Wired
  into each caller's own dollar-space code, right where `target_dollar = total_value *
  gross_exposure * weight` is already computed: `execution/live_signal.py`'s `run()` (reusing
  the SAME `df_volume` already fetched for `use_liquidity_filter`/`use_volume_confirmation`, its
  gate widened to also cover this feature, zero new fetch mechanism), feeding a new
  `generate_orders()` param, `target_dollar_override: dict | None = None` (a ticker present here
  REPLACES the normal computation for that ticker only, `None` default byte-identical), tickers
  grouped by their resolved per-ticker cap so the shared function's single-cap-per-call contract
  still gets exactly one call per distinct cap value, not one per ticker. The pre-existing
  advisory `max_pct_of_adv > 0` / `check_capacity()` block is left COMPLETELY unchanged, this is
  a new, separately opt-in active path alongside it, not a replacement.
  `run_risk_managed_backtest()` gained a matching optional `daily_volume: pd.DataFrame | None =
  None` param (and `run_custom_backtest()`'s own `**risk_overrides` now pops/forwards it), same
  "opt-in extra data the caller must supply, fail loud if missing" precedent
  `use_liquidity_filter`'s own `daily_volume` requirement already established (a loud
  `ValueError` when `liquidity_adjusted_sizing` is enabled without it, not a silent skip), byte-
  identical when the feature is disabled. See `docs/RISK_CONSTRAINTS.md`'s "Liquidity-Adjusted
  Position Sizing (Active)" section.
  **Epic 1 real verification, run 2026-08-05, all 5 stories together**: full pytest suite 994
  passed (up from 939 pre-Epic-1, +55 new tests), zero regressions. A throwaway 10-ticker test
  portfolio (4 config variants: ERC alone, VaR/CVaR budget alone, liquidity-adjusted sizing
  alone, all three combined) was run via `daily-runner --config <test>.yaml --force-rebalance`
  (dry-run, confirmed clean) and then `--live --port 7497` against a real IBKR paper account,
  BOTH natively and inside the Docker image rebuilt with this epic's code
  (`docker compose up -d --build`, confirmed the image picked up the new `src/` changes via
  build/container timestamps). Real BUY fills confirmed via `execDetails`/`commissionReport` in
  all 4 variants, both environments, identical mechanism behavior native vs. Docker (same CVaR
  values, same ERC weight shapes). VaR/CVaR scalar computed a real CVaR (2.83%-3.07% across
  runs) against the configured 5% budget, correctly staying at `scalar=1.00` (budget not
  exceeded at this position size, the mechanism runs and computes correctly, this particular
  test didn't happen to hit the throttling branch, see the unit/integration tests for that case
  instead). ERC sizing produced genuinely non-uniform, risk-balanced weights (e.g. AAPL ~35%,
  AMD ~8%, tracking their real relative volatility), distinct from both equal-weight and
  inverse-vol shapes. Pre-existing safety mechanisms (`OVERLAPPING_TICKER_SCOPED`,
  `UNRECOGNIZED_POSITION`, the Turnover Limit warning) all fired correctly against the new
  features with zero interaction bugs. See `docs/RISK_CONSTRAINTS.md`'s own matching
  verification note under "Opt-in overlay strategies" for the full writeup.
  `use_atr_trailing_stop`/`atr_trailing_stop_multiplier`/`atr_period` (Epic 2, "Institutional
  Risk-Management Features" plan) is a volatility-ADAPTIVE trailing-stop distance (Wilder's ATR,
  `core/technical_indicators.py`'s `atr()`, previously only ever consumed by the email report's
  technical-indicator section, now wired into a real exit decision for the first time), an
  alternative to `use_trailing_stop`'s FIXED percentage distance. Comparison shape is
  DELIBERATELY different, not an inconsistency: the percentage trail compares `(price - high) /
  high <= -trailing_stop_pct` (a fraction); the ATR trail compares `(high - price) >=
  atr_trailing_stop_multiplier * ATR` (an absolute price/dollar distance), since ATR is quoted
  in the ticker's own price units, not a percentage. Shares the SAME `running_high` dict the
  percentage-trail block already maintains ("highest price since entry" is the identical
  quantity regardless of distance formula), a new independent block in
  `run_risk_managed_backtest()`'s day-loop, structurally parallel to the existing
  `use_trailing_stop` block (same fill/slippage/commission/cleanup mechanics, only the trigger
  condition and log message differ), checked AFTER it against `list(holdings.keys())`, so a
  ticker already exited by the percentage trail is naturally skipped, "whichever triggers first
  wins" by construction. A real, confirmed OHLC-plumbing gap was found and closed, the largest
  real cost in this epic: `daily_prices` throughout this function is close-only, confirmed by
  reading `_split_price_panel()` directly (it extracts only `close`/`open` from a MultiIndex
  panel, silently discarding `high`/`low` even when present), so ATR (needing high/low/close)
  cannot be computed from `daily_prices` alone. `run_risk_managed_backtest()`/
  `run_custom_backtest()` gained new optional `daily_highs`/`daily_lows` DataFrame params (same
  column shape as `daily_prices`), REQUIRED (loud `ValueError`, not a silent skip) when
  `use_atr_trailing_stop` is `True`, same "opt-in extra data the caller must supply" precedent
  `use_liquidity_filter`'s `daily_volume` requirement already established. `atr_by_ticker: dict`
  is precomputed ONCE per ticker before the day-loop starts (a pure function of the already-
  available OHLC panels, not recomputed incrementally inside the loop, matching this function's
  existing `regime_bullish`/`regime_high_vol`/`momentum_crash_bear` precomputation precedent),
  sourced from `close_full`/the full `daily_highs`/`daily_lows` (not the already-window-masked
  `prices`), the same "bound only at the end, not the start" lesson Epic 14's
  `momentum_crash_lookback_days` fix already applied, so a short simulation window doesn't
  starve ATR's own lookback. New `resolve_ticker_atr_multiplier(ticker, cfg)` resolver (same
  shape/placement as `resolve_ticker_stop_loss_pct()`/`resolve_ticker_max_pct_of_adv()`), a 4th
  allowed `ticker_risk_overrides` key (`atr_trailing_stop_multiplier`); unlike
  `resolve_ticker_max_pct_of_adv()`, this DOES honor `'enabled': false` (returns `None`), ATR
  trail is a stop-loss-family exit mechanism, one consistent "enabled" meaning per ticker across
  every exit check, not a second, divergent one. **Deliberately has NO broker-native
  (`attach_broker_trailing_stop`-style) IBKR `TRAIL` counterpart**, a considered scope decision,
  not an oversight: IBKR's `TRAIL` order only fixes its distance ONCE at submission time (either
  a percent or a fixed-dollar trail amount, confirmed, never dynamically recomputed), so a
  broker-native ATR version would only ever be "ATR distance as of order placement, held fixed
  thereafter," a materially weaker and potentially misleading claim of "ATR-based" protection.
  See `docs/RISK_CONSTRAINTS.md`'s "ATR-Based Trailing Stop" section.
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
  `cfg.use_negative_universe_cash_filter` (Epic 6, "Rebalance Reporting Clarity &
  Selection-Logic Fixes" plan, opt-in, default `False`): a whole-book constraint, distinct from
  `use_absolute_momentum` above (per-ticker swap, still ends up invested). `run()` recomputes
  `market_wide_negative = cfg.use_negative_universe_cash_filter and
  is_universe_negative(latest_scores, tickers)` right after `picks = resolve_strategy_picks(...)`
  (the actual force-to-empty already happened inside that call, this recomputation is purely to
  detect the CAUSE for the two things below). A real, confirmed interaction bug found while
  wiring this in: `absolute_momentum_overlay()` falls back to `[defensive_ticker]` whenever
  handed an ALREADY-EMPTY picks list, which would have silently re-injected a position and
  defeated this constraint entirely whenever `use_absolute_momentum` was also on; the
  `use_absolute_momentum` overlay block is now guarded with `and not market_wide_negative` so it
  never runs in that specific case (an UNRELATED empty-picks cause, e.g. liquidity filtering,
  still gets the overlay applied exactly as before, unchanged). A new
  `MARKET_WIDE_NEGATIVE_MOMENTUM_CASH` `WARNING` (`log_alert()`) fires specifically when this
  constraint (not an unrelated cause) is what emptied `picks`, alongside the more generic
  `NO_ELIGIBLE_TICKERS` from Epic 5 above (both mean different things, see
  `docs/RISK_CONSTRAINTS.md`'s "Whole-Book Negative Momentum Cash Filter"). Wired into
  `core/strategy_signals.py`'s `resolve_strategy_picks()` (see that file's own bullet), the
  single shared function both `run()` and `generate_strategy_monthly_picks()` call, guaranteeing
  live/backtest parity by construction for the SELECTION decision; see that section for a
  documented, deliberate scope boundary around the backtest EXECUTION engine
  (`run_risk_managed_backtest()`) not yet actively liquidating on an empty-picks period the way
  live's `generate_orders()` does.
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
  PRICE-vendor selection only). UPDATED (Epic 6, "Stale-Price Reporting + Live Price-Vendor
  Priority" plan): `daily_runner.py` USED TO deliberately never populate `run()`'s own
  `fmp_api_key`/`eodhd_api_key` params at all (confirmed by every strategy-plan epic's live
  validation at the time, production price data came from `yfinance` only); it now does, a
  deliberate, informed reversal, see this file's own `daily_runner.py` bullet below. This
  fundamentals-fetch call site's own independent `os.environ.get(...)` read is UNCHANGED by that
  reversal: price-vendor selection and fundamentals-vendor selection are deliberately kept as
  separate concerns that happen to read the same two env vars today, not coupled, so either can
  diverge in the future (e.g. a fundamentals-only key distinct from the price-feed key) without
  silently affecting the other. Reusing the price-fetch keys for fundamentals here would have
  silently switched the real production price vendor for EVERY portfolio the first time
  `daily_runner.py` started passing real keys through for fundamentals ALONE, an unrelated,
  unbudgeted side effect discovered and avoided while wiring up `hybrid_multi_factor`, still true
  today even though `daily_runner.py` now legitimately passes real price-vendor keys elsewhere.
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
  `attach_broker_stop_loss` is off (the default). `stop_loss_pct` itself (shared by
  `auto_execute_stop_loss` and this bracket's `auxPrice`) is FIXED from entry price in both
  paths, not trailing, confirmed by reading both, it never ratchets as a position gains; see
  `docs/RISK_CONSTRAINTS.md`'s "Stop-Loss Width" section for recommended per-regime values
  (`0.10` short-term, `0.15`-`0.20` long-term) and why the wider long-term value only reproduces
  half of the cited "trailing stop" research (room to breathe, not gain-locking on its own,
  a broker-native `TRAIL` mechanism for the gain-locking half now exists, see below).
  `place_orders_ibkr()`'s `attach_broker_trailing_stop`/`trailing_stop_pct` params (Epic 9,
  "Broker-Native Trailing Stop" plan, from `BacktestConfig`, belt-and-suspenders alongside
  `use_trailing_stop`'s Python-side daily ratchet, `daily_runner.py`'s
  `check_and_handle_trailing_stops()`, see `docs/RISK_CONSTRAINTS.md`'s "Broker-Native Trailing
  Stop") attach a real IBKR `TRAIL` order at BUY time, same shape as the `attach_broker_stop_loss`
  bracket above: parent BUY (`transmit=False`) + child `TRAIL` SELL (`parentId` linked,
  `orderType="TRAIL"`, `trailingPercent = trailing_stop_pct * 100`, a percent number not a dollar
  amount, `trailStopPrice = expected_prices[ticker] * (1 - trailing_stop_pct)` computed and
  logged explicitly at submission time rather than left to IBKR's own auto-calculation, same
  transparency precedent as the STP bracket's own `auxPrice`, `tif="GTC"`). No reference price ->
  falls back to a plain, unprotected BUY, same fallback shape as the fixed bracket.
  `trailing_stop_order_ids[ticker]` / `results[ticker]["trailing_stop_order_id"]` ->
  `orders[ticker]["broker_trailing_stop_order_id"]` mirror `stop_order_ids`/`stop_order_id`/
  `broker_stop_order_id`'s identical in-memory-only, audit-only scope exactly (confirmed by
  reading the code: neither the STP nor the TRAIL order id is a trade-log CSV column, nor read
  anywhere in `interfaces/notifications.py`).
  **OCA pairing, confirmed with the project owner**: when a portfolio enables BOTH
  `attach_broker_stop_loss` and `attach_broker_trailing_stop` for the same BUY, both children
  attach as an IBKR One-Cancels-All group (`ocaGroup = f"OCA_{ticker}_{parent_oid}"`,
  `ocaType=1` on both), so whichever triggers first cancels the other at the broker, matching
  `trailing_stop_pct`'s own documented "whichever triggers first wins" semantics extended to the
  broker level rather than diverging from it. Sequencing: the STP child (if both attach) carries
  `transmit=False` and the TRAIL child (always placed last) carries `transmit=True`, transmitting
  the whole 3-order bracket atomically; when only one bracket type is enabled, no OCA fields are
  set at all, unchanged single-child behavior. The existing cancel-before-sell mechanism
  (`reqAllOpenOrders()`/`cancelOrder()`, described above) is widened to also match a resting
  `(symbol, SELL, TRAIL)` order, not just `STP`, and its outer gate widened to
  `attach_broker_stop_loss or attach_broker_trailing_stop`, firing whenever EITHER bracket type
  could be resting. **A real, confirmed pre-existing gap found and fixed while wiring this in,
  unrelated to the TRAIL order type itself**: `daily_runner.py`'s three auto-exit call sites
  (`check_and_handle_stop_losses()`, `check_and_handle_time_stops()`,
  `check_and_handle_trailing_stops()`) were never passing `attach_broker_stop_loss`/
  `stop_loss_pct` to `place_orders_ibkr()` at all, confirmed by reading all three, meaning a
  resting broker STP was never cancelled before one of these three checks' own auto-exit SELL,
  only the rebalance path (`run()`) had this protection before now. Fixed: all three now pass the
  same four kwargs (`attach_broker_stop_loss`/`stop_loss_pct`/`attach_broker_trailing_stop`/
  `trailing_stop_pct`) the rebalance path already did.
  `BacktestConfig.attach_broker_trailing_stop: bool = False` (opt-in, byte-identical when off)
  reuses `trailing_stop_pct` for the trail width, same reuse precedent
  `attach_broker_stop_loss` sets for `stop_loss_pct`, deliberately independent of
  `use_trailing_stop`. `__post_init__`'s existing `use_trailing_stop`/`trailing_stop_pct`
  validation is generalized to also require a configured width when
  `attach_broker_trailing_stop` is set, independent of `use_trailing_stop`'s own value.
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
  `_with_context()` also sets `transaction_amount` on every order (Epic 3 of the "Rebalance
  Reporting Clarity & Selection-Logic Fixes" plan), the ACTUAL dollar amount bought/sold THIS
  transaction (`shares * price`), `0.0` for every HOLD. Fixes a real, confirmed source of
  confusion: a full-exit SELL (a ticker leaving the target universe entirely) has
  `money_invested = 0` (correctly reflecting the post-rebalance TARGET, which for that ticker is
  zero), previously the ONLY way to see what was actually sold in dollar terms was the free-text
  `reason` string. The flooring-remainder-redeployment block (right below, `redeploy_flooring_
  remainder`) mutates `shares` on the top-ranked BUY pick AFTER `_with_context()` already ran, so
  it explicitly recomputes `transaction_amount` there too, or it would go stale for that one
  order. `log_orders()` and `log_signal_rankings()` both gained a matching `transaction_amount`
  CSV column (append-at-end, before `row_hash`, same schema-evolution caveat as every prior
  addition, archive old log files first), and `notifications.py`'s
  `build_rebalance_summary_html()` gained a "Transaction $" column next to "Shares", reading
  `order.get("transaction_amount", 0.0)` the same way every other column reads straight off the
  `orders` dict.
  `run()` now returns an `OrdersResult` (a `dict` subclass, byte-identical to a plain
  `{ticker: order}` dict for every existing caller, `.items()`/`.values()`/`len()`/`bool()`/`in`/
  equality all work unchanged) with an added `.full_signal_universe` attribute: `{ticker:
  {'rank', 'signal_score', 'close_price', 'selection_status'}}` for EVERY configured ticker with
  a valid score this rebalance, not just the `top_n` actually selected. Built right after `picks`
  (post absolute-momentum-swap) is finalized, using the SAME `latest_ranks_row`/`latest_scores`
  the existing (still `picks`-scoped, unchanged) `signal_context` dict already reads, that
  narrowing to `picks` was a reporting-scope choice, not a data-availability one; every
  `strategy_type` already computes ranks/scores for the full universe via the generic
  `assign_ranks(resolve_strategy_scores(...))` pipeline. `selection_status` is
  `f"Top {top_n} (Selected)"` (or `"Selected (Absolute Momentum)"` when `cfg.strategy_type ==
  "absolute_momentum"`, which has no `top_n` cutoff) for a ticker in `picks`, else `"Watchlist /
  Reserve"`. Deliberately NOT a second return value (`orders, universe = run(...)`), that would
  break every existing call site's unpacking; deliberately NOT merged into `signal_context`/the
  trade log either, per an explicit design decision to keep that log's "decisions actually made"
  meaning uncontaminated by tickers nothing was ever decided about.
  `OrdersResult` also gained a `.picks_were_empty: bool = False` attribute (Epic 5, "Rebalance
  Reporting Clarity & Selection-Logic Fixes" plan), set right after `picks` is finalized (post
  absolute-momentum swap): `True` when `not picks`, i.e. no ticker survived selection this
  rebalance even though scores/ranks were computed fine (e.g. `use_liquidity_filter` zeroed
  every rank), distinct from an empty `orders` dict for an unrelated reason like
  `AGGREGATE_DRIFT_SKIP` (eligible tickers existed, drift was just trivial). Fires a new
  `NO_ELIGIBLE_TICKERS` `WARNING` alert (`log_alert()`) at the point of detection, and is carried
  through on both the final `result` and the `AGGREGATE_DRIFT_SKIP` early-return `skip_result`
  (so the flag survives even when that later skip check also triggers). Purely alerting/
  reporting, no new sizing logic: `generate_orders()` already safely sold any current holdings
  to cash and bought nothing when `target_weights` was empty, this just makes that specific
  cause visible instead of looking identical to a routine no-action rebalance. Read by
  `daily_runner.py`'s no-action email branch (see `interfaces/notifications.py`'s
  `build_no_action_summary_html()` bullet).
  `compute_stop_loss_price(action, cfg, close_price, avg_entry_price=None, ticker=None)` (Epic 8,
  "Stop-Loss Price Reporting Fix" plan) reports a REAL PER-SHARE stop-loss trigger price,
  matching what the two real enforcement mechanisms actually compute for the same position, NOT
  a dollar-amount-at-risk figure. This is a REVERSAL of an earlier design
  (`money_invested * stop_loss_pct`, at the time "a deliberate, explicit product decision,
  confirmed directly with the project owner") on a new, explicit, informed instruction: the
  dollar figure was uniformly available live and dry-run, but didn't match the real per-share
  threshold this app actually enforces, confusing to compare against a live quote. Reference
  price selection: `BUY` uses `close_price` (no entry exists yet, estimates where the stop would
  land once filled near today's close); `HOLD` uses `avg_entry_price` when it's a real, positive
  value (a fixed stop is measured FROM ENTRY, not from today's price, per `stop_loss_pct`'s own
  "FIXED from entry, not trailing" contract, so this matches the threshold actually in force),
  falling back to `close_price` when the entry price genuinely isn't known (dry-run without
  `persist_dry_run_state`); `SELL` or no usable reference price: `None`. `stop_loss_pct` is
  resolved exactly as before, via `resolve_ticker_stop_loss_pct()` when `ticker` is given, `None`
  short-circuits to `None` regardless of action. This function remains purely REPORTING-only; it
  is not, and must not be, wired into either REAL stop-loss enforcement mechanism, both of which
  correctly need a real per-share reference price and compute it independently:
  `check_and_handle_stop_losses()`'s daily percentage-drawdown check and
  `place_orders_ibkr()`'s broker-side bracket order (`attach_broker_stop_loss`'s `auxPrice`),
  both derived directly from `avg_entry_price`, never through this function. `generate_orders()`
  gained a new optional `current_positions: dict | None = None` param (`{ticker: {'shares',
  'avg_entry_price'}}`, same shape `check_and_handle_stop_losses()`/`get_ibkr_positions()` use),
  feeding this function's `avg_entry_price` argument via a lookup keyed by ticker; `None`
  (default) is byte-identical to no entry price being known for any ticker (falls back to
  `close_price`), so every pre-existing call site/test keeps working unchanged. `run()` gained
  the identical optional `current_positions` param, threaded straight through to its
  `generate_orders()` call; `daily_runner.py`'s own call site to `run()` already builds a
  `current_positions` dict right before deriving `current_holdings` from it
  (`current_holdings = {t: p["shares"] for t, p in current_positions.items()}`), so this reuses
  that exact same dict, no new fetch. Each order also gains a `stop_loss_price_is_estimated: bool`
  field, `True` whenever the reference price used was NOT a real `avg_entry_price` (every `BUY`,
  plus any `HOLD` that fell back to `close_price`), read by `interfaces/notifications.py`'s
  `build_signal_universe_html()` to extend the pre-existing "(estimated)" qualifier (previously
  keyed off `action == "BUY"` alone) to also cover a HOLD row without a known entry price, with a
  `.get(..., action == "BUY")` fallback so an order dict predating this field still renders
  correctly. `log_signal_rankings()`'s `stop_loss_price` CSV column is a VALUE-SEMANTICS change,
  not a schema change (same column, same position): rows written before Epic 8 hold a
  dollar-at-risk figure, rows written after hold a real per-share price, see
  `docs/SIGNAL_RANKINGS_LOG.md`'s schema-evolution note.
  `log_signal_rankings(full_signal_universe, orders, dry_run, path, cfg=None)` writes one
  hash-chained row per full-universe ticker to `logs/signal_rankings_log_<portfolio>.csv` (a new
  `signal_rankings_log_path` param on `run()`, built by `daily_runner.py` next to `trade_log_path`
  the same way), reusing `core/audit_log.py`'s `append_hash_chained_row()` directly (same
  precedent as the alert log, avoiding a fourth bespoke hash-chain implementation). A ticker
  present in `orders` gets its real action/shares/money/stop-loss columns; one absent (watchlist)
  gets `action="WATCHLIST"` and zeroed/blank money/shares/stop-loss. See
  `docs/SIGNAL_RANKINGS_LOG.md`.
  Rows are written sorted by `momentum_rank` ascending (1 = best), a ticker with no rank sorts
  after every ranked ticker, ordered by `signal_score` descending among themselves (Epic 1 of the
  "Rebalance Reporting Clarity & Selection-Logic Fixes" plan, fixing a real, confirmed gap: this
  function previously iterated `full_signal_universe.items()` in raw dict/ticker-iteration order,
  not rank order, confirmed against a real deployed run: `portfolio1`'s 19-ticker log came back
  AMD(rank 1) through ORCL(rank 19) in exact ascending order, `portfolio2`'s 58-ticker log the
  same, 1 through 58). `interfaces/notifications.py`'s `build_signal_universe_html()` applies the
  identical sort key independently (see that file's own bullet below), the two functions don't
  share a helper, deliberately, matching this codebase's existing pattern of `full_signal_universe`
  having multiple independent consumers.
  `run()`'s `full_signal_universe` construction (Epic 2 of the same plan) now distinguishes THREE
  outcomes for a non-selected ticker, previously flattened into one flat `"Watchlist / Reserve"`
  regardless of why it wasn't picked, a real, confirmed gap: `"Excluded (Illiquid)"` (the ticker
  HAD a valid rank before `liquidity_filter()` ran but not after, only possible when
  `cfg.use_liquidity_filter` is on, detected via a new `pre_liquidity_ranks_row` snapshot taken
  right after `ranks = assign_ranks(scores)`, before the filter reassigns `ranks`);
  `"Excluded (Negative Momentum)"` (`signal_score < 0`, applies across all 11 `strategy_type`s, a
  negative composite/residual/blended score is still a meaningfully destructive signal under that
  strategy's own logic, not only for the 7 base-score types where the score is a literal trailing
  return); else unchanged `"Watchlist / Reserve"` (still-positive momentum, simply outranked).
  `log_signal_rankings()`'s CSV `action` column and `interfaces/notifications.py`'s
  `build_signal_universe_html()`'s Action column both derive `action = "EXCLUDED"` from either
  `"Excluded (...)"` variant (else `"WATCHLIST"`), the more specific reason stays in
  `selection_status`, giving a 6-value Action set: `BUY`/`SELL`/`HOLD` (from a real order, this
  part was already correct and untouched by this epic), `WATCHLIST`, and the two `EXCLUDED`
  variants. See `docs/SIGNAL_RANKINGS_LOG.md`'s five documented `selection_status` values.
  `resolve_ticker_stop_loss_pct(ticker, cfg) -> float | None` is the single source of truth for
  per-ticker stop-loss resolution, `BacktestConfig.ticker_risk_overrides` (`{}` default, zero
  behavior change for a ticker with no entry): returns `None` when
  `ticker_risk_overrides[ticker]['enabled']` is explicitly `False` (stop-loss check disabled
  entirely for that ticker, never flagged/sold, no broker-side bracket attached regardless of
  `attach_broker_stop_loss`), else the ticker's own `stop_loss_pct` override if given, else the
  portfolio's own `cfg.stop_loss_pct` unchanged. Wired into THREE places, all previously reading
  `cfg.stop_loss_pct` as one flat portfolio-wide scalar: `daily_runner.py`'s
  `check_and_handle_stop_losses()` (the daily drawdown check), `compute_stop_loss_price()` (now
  takes an optional `ticker` param, `None` default is byte-identical to before this existed,
  returns `None` immediately for a disabled ticker regardless of action), and
  `place_orders_ibkr()`'s broker-side bracket (`generate_orders()` now stashes the RESOLVED
  per-ticker width onto each order as `order["stop_loss_pct"]`, `place_orders_ibkr()` reads
  `order.get("stop_loss_pct", stop_loss_pct)`, its own scalar param becomes the fallback for an
  order that doesn't carry the key, e.g. `check_and_handle_stop_losses()`'s hand-built
  `exit_orders`, which are always `SELL` and never reach the bracket-attach branch anyway). See
  `docs/RISK_CONSTRAINTS.md`'s "Per-Ticker Stop-Loss Override".
  `redeploy_flooring_remainder` (`BacktestConfig`, `False` default, byte-identical behavior when
  off): `generate_orders()`'s main per-ticker loop is unchanged, a new block runs AFTER it
  (before `return orders`), pools the whole-share-flooring leftover across every BUY this
  rebalance (`abs(drift_dollar) - floored_shares * price` per BUY, recomputed from the SAME
  `target_dollar`/`current_value` dicts already in scope) and redeploys it as extra whole shares
  of the single BUY ticker with the lowest `rank` in `signal_context` (falls back to the first
  BUY ticker if none carry rank info, e.g. a `custom_weights`-sized rebalance with no ranking
  step at all, rather than silently dropping the remainder). No-op (byte-identical to before)
  when there are no BUYs this rebalance, `allow_fractional_shares` is `True` (nothing to pool),
  or the pooled remainder can't afford even one more share of the top pick.
  `money_invested`/`pct_money_invested`/`rank`/`signal_score`/`stop_loss_price` on the affected
  order are left exactly as already set by `_with_context()`, they describe the TARGET
  allocation model, not the post-redeployment share count, only `shares`/`reason` change. See
  `docs/RISK_CONSTRAINTS.md`'s "Flooring Remainder Redeployment".
  `use_liquidity_filter`/`min_avg_dollar_volume`/`liquidity_lookback_days` (`BacktestConfig`,
  opt-in, `False` default byte-identical to before) wire
  `core/functions_quant_extensions.py`'s `liquidity_filter()` (previously fully coded but zero
  production call sites, notebook-only) into LIVE selection for the first time: in `run()`,
  right after `ranks = assign_ranks(scores)`, BEFORE `latest_scores`/`latest_ranks_row` are
  derived, volume is fetched via the EXISTING `fetch_ohlcv_for_tickers()` (one
  `get_stock_prices()` call per ticker) and `ranks` (not `scores`) gets NaN'd out for any ticker
  below `min_avg_dollar_volume`'s trailing average, so `resolve_strategy_picks()`'s
  `nsmallest()`-based selection naturally excludes it, distinct from `max_pct_of_adv`'s
  POST-selection advisory-only warning below. No volume fetched at all for any ticker (a vendor
  outage) logs a `WARNING` and leaves `ranks` untouched rather than crashing or silently
  filtering everything out. CAVEAT, confirmed by reading `core/strategy_signals.py`'s
  `select_absolute_momentum_picks()`, not assumed: `strategy_type == "absolute_momentum"`
  selects by each ticker's OWN trailing score directly, never consulting rank at all, so this
  filter has NO effect under that one `strategy_type`, documented plainly in
  `docs/RISK_CONSTRAINTS.md`'s "Liquidity / Universe Filter", not silently glossed over. A
  filtered ticker's `signal_score` stays valid (only `ranks`, not `scores`, gets touched), so it
  still surfaces in `full_signal_universe`/the Full Signal Universe table as `"Watchlist /
  Reserve"` with a blank rank, that code's existing `pd.notna()` guard on the rank already
  handles this correctly with no changes needed there.
  `use_technical_confirmation`/`technical_confirmation_min_sma_window`/
  `technical_confirmation_max_rsi`/`technical_confirmation_require_macd_bullish` (`BacktestConfig`,
  Epic 2, "Institutional Momentum Best-Practice Gaps" plan, opt-in, `False` default
  byte-identical to before) wire `core/functions_quant_extensions.py`'s NEW
  `technical_confirmation_filter()` into selection, motivated by a real gap found reviewing IBKR's
  Quant "Momentum Trading" Parts I/II against this codebase: `core/technical_indicators.py`'s
  SMA/RSI/MACD were computed for the email report ONLY, never wired into a selection decision.
  Applied at the SAME rank-NaN'ing point as `use_liquidity_filter` immediately above (right after
  it, in both `run()` and `core/strategy_signals.py`'s `generate_strategy_monthly_picks()`),
  close-price-only (no separate OHLCV/volume fetch needed, unlike the liquidity filter), so it
  achieves full LIVE+BACKTEST parity by construction off the same `daily_prices` panel every
  scorer already uses, unlike `hybrid_multi_factor`'s fundamentals (no point-in-time historical
  source, LIVE-ONLY). `__post_init__` requires at least one of the three sub-checks set when the
  toggle is `true` (fail loud, same precedent as `hybrid_multi_factor`'s `NotImplementedError`/
  `use_liquidity_filter`'s missing-`daily_volume` `ValueError`). A new `pre_technical_ranks_row`
  snapshot (captured after the liquidity filter but before this one) lets a technically-excluded
  ticker get its own `"Excluded (Technical)"` `full_signal_universe`/`selection_status` value,
  distinct from `"Excluded (Illiquid)"`/`"Excluded (Negative Momentum)"`/`"Watchlist / Reserve"`;
  `log_signal_rankings()`'s and `notifications.py`'s existing `selection_status.startswith
  ("Excluded")` derivation of the `EXCLUDED` action is already generic, confirmed by reading both,
  needed no change to pick up the new status. See `docs/RISK_CONSTRAINTS.md`'s "Technical-Indicator
  Entry Confirmation".
  `use_volume_confirmation`/`volume_confirmation_lookback_days`/`volume_confirmation_min_ratio`
  (`BacktestConfig`, Epic 4, "Institutional Momentum Best-Practice Gaps" plan, the last of the
  four gaps found reviewing IBKR's own Quant momentum-trading articles, opt-in, `False` default
  byte-identical to before) wire `core/functions_quant_extensions.py`'s NEW
  `volume_confirmation_filter()` into selection, distinct from `use_liquidity_filter` (an
  ABSOLUTE dollar-volume tradability threshold): this is a RELATIVE volume-TREND confirmation of
  the price move itself, Part I's "rising volume confirms the move." The trailing
  `volume_confirmation_lookback_days` window (default `20`) splits into two equal halves,
  eligible only if `recent_half_avg_volume / earlier_half_avg_volume >=
  volume_confirmation_min_ratio` (default `1.0`, at least flat-or-rising participation). Applied
  right after the technical-confirmation filter, in both `run()` and `core/strategy_signals.py`'s
  `generate_strategy_monthly_picks()`. The existing OHLCV fetch gate widened from `if
  cfg.use_liquidity_filter and not ranks.empty:` to `if (cfg.use_liquidity_filter or
  cfg.use_volume_confirmation) and not ranks.empty:`, so the SAME already-fetched `df_volume` now
  feeds both filters, zero extra API calls when both are enabled, one shared fetch when only
  `use_volume_confirmation` is. `generate_strategy_monthly_picks()`'s pre-existing `daily_volume`
  param is likewise now shared: `use_volume_confirmation=True` without `daily_volume` supplied
  raises the same loud `ValueError` precedent `use_liquidity_filter` already established. A new
  `pre_volume_ranks_row` snapshot (captured after the technical filter but before this one) gives
  a volume-excluded ticker its own `"Excluded (Low Volume Confirmation)"`
  `full_signal_universe`/`selection_status`, distinguishable from every other exclusion reason;
  `log_signal_rankings()`'s/`notifications.py`'s existing `startswith("Excluded")` derivation
  needed no change, confirmed generic. See `docs/RISK_CONSTRAINTS.md`'s "Volume-Confirmed Signal
  Quality".
  `full_signal_universe`'s `close_price` gained a stale-price fallback (Epic 5, "Stale-Price
  Reporting + Live Price-Vendor Priority" plan), motivated by a real, confirmed incident: right
  now `yf.download()` (`core/functions.py`'s `_fetch_yf()`, zero caching, a fresh call every
  time) is returning `NaN` OHLC for the most recent trading day across many tickers at once (a
  genuine, external Yahoo Finance data-quality gap, confirmed directly against AMD/ASML/SPY/MSFT
  simultaneously, not ticker-specific), which used to render as a bare `"$nan"` in the Full
  Signal Universe report with no indication anything was wrong. When `latest_prices.get(t)` is
  `None`/`NaN`, the `full_signal_universe` entry now falls back to `daily_prices[t]`'s last
  non-NaN value, and a new `close_price_as_of: pd.Timestamp | None` key records which date that
  fallback price is actually from (`None` when the displayed price IS the current rebalance's
  own fresh close). **A critical, deliberate safety boundary**: this fallback is scoped ENTIRELY
  to the `full_signal_universe` reporting dict; `latest_prices` itself (used by
  `generate_orders()`, the daily stop-loss check, portfolio snapshot writing, and everywhere else
  a real trading/risk decision is made) is completely untouched, still sees the genuine `NaN` and
  still correctly resolves to `"no live price available"` / a skipped check wherever it already
  does today, a stale price must never silently become the reference price for a real decision,
  only for what's DISPLAYED in this one report. `interfaces/notifications.py`'s
  `build_signal_universe_html()` renders the new field inline (`"$539.69 (as of 2026-07-23)"`)
  when set, unchanged when not. `log_signal_rankings()`'s `SIGNAL_RANKINGS_LOG_HEADER` gained a
  matching `close_price_as_of` column (appended before `row_hash`, same schema-evolution
  precedent as every prior column addition here, archive old `signal_rankings_log_*.csv` files
  first). See `docs/SIGNAL_RANKINGS_LOG.md`.
  `full_signal_universe` also gained a `rank_delta` key (Epic 7, "Rank Delta (Momentum Rank
  Trend) Column" plan, opt-in per portfolio via `cfg.use_rank_delta`, `False` default,
  byte-identical when off): each ticker's momentum rank exactly `lookback_period` ago minus its
  rank today (positive = moved up N positions). A real, confirmed constraint drove the design,
  not assumed: computing a genuine (non-NaN) historical comparison point needs the `ranks`
  history to span roughly 2x `lookback_period`, since that older row itself needs its own full
  lookback window of history behind IT too (traced through `calculate_period_returns()`'s
  `pct_change(periods=N)` math), not just `lookback_period` + `compute_required_lookback_days()`'s
  existing `buffer_days=60` margin. `compute_required_lookback_days()` now adds a `2 *
  momentum_days` candidate when `cfg.use_rank_delta` is `True` (no effect when off), and a new
  `resolve_lookback_period_row_count(lookback_period, holding_period)` pure helper (deliberately
  duplicating, not refactoring, `resolve_momentum_scores()`'s own regime formula, same precedent
  `compute_required_lookback_days()` already set for this identical formula) gives `run()` the
  exact row-count to look back into the FINAL, post-all-filters `ranks` DataFrame (the same basis
  `latest_ranks_row` itself uses, so a filtered-out ticker's `rank_delta` is consistently `None`
  the same way its `rank` is already `None`). `None` when the flag is off, the ticker isn't
  currently ranked, or there wasn't enough history for a real historical value (gracefully, not
  a crash). Confirmed with the project owner before implementing: an opt-in flag (not always-on)
  was the deliberate choice specifically because of this real fetch-size cost, matching every
  other feature here with the same trade-off (`use_liquidity_filter`,
  `use_technical_confirmation`, `use_volume_confirmation`). `log_signal_rankings()`'s
  `SIGNAL_RANKINGS_LOG_HEADER` gained a matching `rank_delta` column (appended before `row_hash`,
  after `close_price_as_of`, same schema-evolution precedent, archive old
  `signal_rankings_log_*.csv` files first), the raw signed integer, machine-readable, distinct
  from `interfaces/notifications.py`'s `build_signal_universe_html()`, which renders the SAME
  value as colored rich text instead (`▲ +N` green / `▼ -N` red / `-` flat / `N/A`, reusing this
  table's own existing BUY-green/SELL-red hex codes, no new color convention), positioned right
  after the Momentum Rank column. Confirmed explicitly with the project owner: this column does
  NOT change the table's existing sort order (still Momentum Rank ascending, `signal_score`
  descending tiebreak), Rank Delta is additive only, never a sort key. See
  `docs/SIGNAL_RANKINGS_LOG.md`'s "Rank Delta" column and `docs/EMAIL_REPORTING.md`'s updated
  Full Signal Universe table description.
  `compute_target_weights()`'s regime filter block now also blends in `cfg.regime_vol_threshold`
  (see `BacktestConfig`'s own bullet above for the shared formula and backtest-parity
  implementation): when set, the regime benchmark's trailing realized volatility
  (`bench.pct_change().tail(regime_vol_lookback_days).std() * sqrt(252)`) exceeding the
  threshold ORs into the same `regime_scalar` the SMA-trend check already computes, `None`
  (default) is byte-identical to before. A new `MARKET_VOLATILITY_REGIME_DEFENSIVE` `WARNING`
  (`log_alert()`, same triple-step pattern as `CORRELATION_SPIKE_DETECTED`) fires ONLY when
  volatility alone, not the SMA trend, is what pushed the scalar defensive (`high_vol and
  bullish`), so it's distinguishable from the pre-existing SMA-only "Regime filter: ... below
  its SMA" log line rather than double-reporting the same bearish case. Confirmed end-to-end
  against a real IBKR paper account (2026-07-21): with `regime_vol_threshold: 0.001` (forced far
  below any real value) and SPY genuinely above its 150D SMA, the filter logged `realized_vol=
  11.95% (threshold=0.10%) -> scalar=0.20`, the alert fired, `Gross exposure: 20.0%` propagated
  through to real order sizing, and real paper `BUY 5 EFA`/`BUY 7 EEM` orders were placed at
  that throttled size rather than the 100% an SMA-only check would have allowed. See
  `docs/RISK_CONSTRAINTS.md`'s "Regime Filter: Volatility Dimension".
  A real, confirmed bug found and fixed while paper-verifying Epic 1's trailing stop (below),
  unrelated to trailing-stop logic itself: `generate_orders()`'s price-validity guard,
  `if price is None or price <= 0:`, does not catch a `NaN` price (`NaN <= 0` is `False` in
  Python, a comparison against `NaN` is always `False`), so a `NaN` price silently fell through
  as if valid, reaching `int(shares)` further down and crashing with `ValueError: cannot convert
  float NaN to integer`. Confirmed directly against a real paper-account run, not synthetic:
  yfinance's most recent trading-day row for XLRE was `NaN` (a vendor data-lag case, the
  most-recent session's close not yet populated), not a hypothetical edge case. Fixed by adding
  an explicit `pd.isna(price)` check alongside the existing `None`/`<= 0` checks, a `NaN` price
  now correctly resolves to the same safe `"no live price available"` HOLD every other
  missing-price case already gets, rather than propagating into share-count arithmetic. See
  `docs/RISK_CONSTRAINTS.md`'s "Trailing Stop-Loss" for the full incident writeup.
  `check_and_handle_trailing_stops()` (`daily_runner.py`, Epic 1, "Institutional Momentum
  Best-Practice Gaps" plan) is the LIVE counterpart to this file's `run_risk_managed_backtest()`
  trailing-stop block (see that file's own bullet for the shared design rationale); this
  function's own bullet lives under `daily_runner.py` below, alongside its two siblings
  `check_and_handle_stop_losses()`/`check_and_handle_time_stops()`.
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
  tests for the pattern. `docker-entrypoint.sh`'s `RISK_MONITOR_CRON` applies ONE schedule to
  every portfolio listed in `RISK_MONITOR_PORTFOLIOS` (one shared cron line generated per
  loop iteration), confirmed by reading the script, there is no per-portfolio schedule env var;
  giving a short-term and a long-term portfolio genuinely different check times in the same
  container needs a host-level cron/Task Scheduler `docker exec` workaround, not a `.env` change,
  see `docs/DEPLOYMENT.md`'s "Recommended `risk_monitor.py` timing" section.
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
  `build_no_action_summary_html()` gained an optional `picks_were_empty: bool = False` param
  (Epic 5, "Rebalance Reporting Clarity & Selection-Logic Fixes" plan, `False` byte-identical to
  before this param existed): when `execution/live_signal.py`'s `run()`'s new
  `OrdersResult.picks_were_empty` is `True` (NO ticker survived selection this rebalance,
  scores/ranks were computed fine but nothing passed filtering, e.g. `use_liquidity_filter`
  zeroed every rank), the email shows a distinct "no tickers passed selection... holding cash"
  message instead of the generic "no order changes" text, previously indistinguishable from a
  routine trivial-drift skip (`AGGREGATE_DRIFT_SKIP`). `daily_runner.py`'s call site passes
  `orders_result.picks_were_empty` straight through, no new plumbing needed. A new
  `NO_ELIGIBLE_TICKERS` `WARNING` (`log_alert()`, same pattern as every other advisory alert)
  fires from `run()` itself right after `picks` is finalized (post absolute-momentum swap, which
  never itself returns empty, `select_absolute_momentum_picks()` always falls back to
  `[defensive_ticker]`), distinct from `INSUFFICIENT_PRICE_HISTORY` (a different, EARLIER
  failure mode, `scores.empty`, no score could even be computed). This is alerting/reporting
  only, no new sizing logic: `generate_orders()` already safely sells any current holdings to
  cash and buys nothing when `target_weights` is empty, confirmed real behavior, not new here.
  See `docs/ALERT_LOG.md`'s `NO_ELIGIBLE_TICKERS` row.
  `notifications.py`'s `build_signal_universe_html(full_signal_universe, orders, top_n,
  strategy_type, dry_run=False)` builds the second "Full Signal Universe" table appended below
  `build_rebalance_summary_html()`'s existing table in the same rebalance email, reading
  `execution/live_signal.py`'s `run()`'s new `OrdersResult.full_signal_universe` attribute (see
  that file's own bullet), reusing `_describe_fill_outcome()` for tickers present in `orders`.
  Rows render sorted by `momentum_rank` ascending (1 = best), unranked tickers trailing ordered
  by `signal_score` descending, same sort key `execution/live_signal.py`'s `log_signal_rankings()`
  applies independently (Epic 1, "Rebalance Reporting Clarity & Selection-Logic Fixes" plan),
  this function previously rendered rows in `full_signal_universe`'s raw dict order, not rank
  order. See `docs/SIGNAL_RANKINGS_LOG.md`.
  The Action column (Epic 2, same plan) derives `"EXCLUDED"` (color `#8e44ad`, distinct from
  `BUY`'s green/`SELL`'s red/`WATCHLIST`'s grey) from a `selection_status` starting with
  `"Excluded"`, else `"WATCHLIST"` unchanged, for any ticker with no order this rebalance; the
  Reason column shows the specific `selection_status` text (`"Excluded (Negative Momentum)"` or
  `"Excluded (Illiquid)"`) for an excluded ticker instead of the generic `"Not selected this
  rebalance"`. The `order is not None` branch (a ticker WITH a real BUY/SELL/HOLD decision this
  rebalance) is untouched, this epic only splits the no-order fallback branch.
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
  `detect_and_log_config_change(portfolio_name, cfg)` is dynamic config reload's audit trail,
  not the reload itself: `daily-runner`/`risk_monitor.py` are stateless CLI processes, cron
  spawns a brand-new one on every tick, `load_config()` was ALREADY re-read fresh every
  invocation before this existed, there was nothing to "apply" that wasn't already applied by
  construction. The real, confirmed gap was Docker-specific: `Dockerfile`'s `COPY config.yaml .`
  baked it into the image at BUILD time, and `docker-compose.yml`'s `volumes:` list didn't
  bind-mount it (only `./logs`/`./data` were), so a host edit never reached the running
  container without a rebuild or a manual `docker cp`. Fixed by bind-mounting
  `./config.yaml:/app/config.yaml` in `docker-compose.yml` (the Dockerfile's own `COPY` stays as
  a fallback for standalone `docker build`/`docker run` usage without compose, the bind mount
  always wins under normal operation). `detect_and_log_config_change()` persists a full
  `dataclasses.asdict(cfg)` JSON snapshot to `data_dir()`'s `last_config_snapshot_<portfolio>.json`
  (same naming precedent as `risk/circuit_breaker.py`'s `_peak_equity_path()`), diffs it against
  the previous run's snapshot, and fires a `CONFIG_CHANGED` INFO alert (`log_alert()`) with a
  full field-by-field `"field: old -> new"` diff whenever it differs; a portfolio's very first
  run (no prior snapshot) silently establishes the baseline, no alert. Wired into the
  per-portfolio loop's "ALWAYS runs" section (every invocation, not gated by rebalance day).
  Deliberately NOT wired into `risk/risk_monitor.py`, which never constructs a `BacktestConfig`
  at all (only reads `total_value`), preserving its independence principle unchanged. See
  `docs/DEPLOYMENT.md`'s "What needs what" section for the full restart-vs-nothing matrix,
  including why `.env`/`docker-entrypoint.sh` genuinely can't be hot-reloaded the same way
  (env vars are baked into the container process at creation, not re-read per file).
  `build_and_send_portfolio_report(name, cfg, current_positions, latest_prices, trade_log_path,
  total_value, dry_run, notification_cfg, macro_indicators, report_type)` (Epic 1, "Fast,
  Auto-Applied Email-Triggered Reports" plan) is a pure extraction, zero behavior change: the
  monthly and daily report blocks used to be two separate inline code blocks in the
  per-portfolio loop (snapshot read, `fnx.compare_to_benchmark()`/`since_inception_performance()`/
  `monthly_window_comparison()`-or-`daily_window_comparison()`, `fetch_ohlcv_for_tickers()` +
  `compute_latest_indicators()`/`get_cached_or_fetch_fundamentals()` for held tickers,
  `build_position_performance()`, `measure_live_performance()` for real FIFO P&L, then
  `send_monthly_report()`/`send_daily_report()`), now one shared function selecting between the
  two via `report_type: "monthly" | "daily"`. Both EXISTING call sites (the `monthly_report_
  day_of_month` gate and the `send_daily` gate) keep their own gating logic unchanged, calling
  this function only once that decision is already made; the function itself only owns the ONE
  universal precondition every report needs regardless of caller: a
  `portfolio_snapshot_<name>.csv` must already exist (returns `False`, sends nothing, for a
  portfolio that's never completed a prior run). Extracted specifically so the on-demand
  `TRIGGER_REPORT` email command (Epic 2, `check_and_apply_email_commands()`) can call the
  EXACT same report-building logic the scheduled path already uses, guaranteeing an on-demand
  report can never silently diverge from what the scheduled one would have shown, same
  single-shared-function principle this codebase already applies everywhere else
  (`resolve_target_weights()`, `resolve_strategy_picks()`, `compute_vol_scalar()`). Confirmed
  byte-identical via the full existing test suite (no new failures) plus 4 new direct unit tests
  (`TestBuildAndSendPortfolioReport`, `tests/test_daily_runner.py`) asserting the no-snapshot
  no-op, correct `report_type` -> window-comparison-function and `send_*_report()` routing, and
  the no-held-tickers OHLCV-fetch skip.
  **A real, confirmed bug found and fixed while verifying the extraction above against real
  accumulated data, not synthetic**: `notifications.send_daily` had never actually been turned
  on with real deployed history before (confirmed, this session was the first time), and doing
  so crashed with `TypeError: float() argument must be a string or a real number, not 'Series'`
  for every portfolio. Root cause: `write_portfolio_snapshot()`'s own docstring says it writes
  one row per RUN, not per calendar day, so more than one manual `--force-rebalance`/`--live`
  run on the same day (routine during testing, or a retry after a crash) produces multiple
  `data/portfolio_snapshot_<name>.csv` rows sharing a date (confirmed directly: one real
  portfolio's file had 36 rows for a single date after this session's own heavy same-day
  testing). `core/functions_quant_extensions.py`'s `daily_window_comparison()`/
  `monthly_window_comparison()` both do a scalar `port_cgi.loc[latest_date]` lookup after
  `.set_index("date")`, which returns a Series (not a scalar) whenever the index has duplicate
  values, breaking the subsequent `float(...)` call. Fixed in BOTH functions:
  `df.drop_duplicates(subset="date", keep="last")` right before `.set_index("date")`, keeping
  the most recent same-day snapshot for that date's point-in-time lookup. Deliberately NOT
  applied to `compare_to_benchmark()`/`since_inception_performance()` in the same file, despite
  reading the identical file: those two AGGREGATE every row's `period_return` via `.prod()` (a
  cumulative product of ALL incremental changes since each row's own predecessor, regardless of
  calendar date), never doing a scalar date lookup, so they don't crash on duplicate dates, and
  deduplicating them would have been WRONG, silently discarding real, legitimate incremental
  return data, confirmed directly: applying the same dedup there broke
  `TestBenchmarkComparison::test_cumulative_return_math`'s own compounding-math assertion,
  caught before it shipped. Regression tests added for both fixed functions
  (`test_duplicate_same_day_rows_do_not_raise`, `tests/core/test_functions_quant_extensions.py`).
  `TRIGGER_REPORT` (Epic 2, same plan) now genuinely auto-applies instead of only being logged
  for manual follow-through: `interfaces/email_commands.py`'s `TriggerReportCommand` gained an
  optional `report_type: Literal["MONTHLY", "DAILY"] = "DAILY"` field (parsed from an optional
  `REPORT_TYPE:` email line, same optional-field convention as `SET_MAX_DRAWDOWN`'s `VALUE:`/
  `ALERTS_REPORT`'s `LIMIT:`), and `check_and_apply_email_commands()` gained a new
  `elif isinstance(cmd, TriggerReportCommand):` branch, pulled OUT of the generic
  `LIQUIDATE`/`ADJUST_PARAM` manual-follow-through `else` bucket. Read-only/side-effect-free
  (sends an email, changes nothing about trading state), so unlike `PAUSE`/`RESUME`/
  `SKIP_NEXT_REBALANCE` it applies in BOTH dry-run and `--live` mode, same category as the
  pre-existing `STATUS`/`ALERTS_REPORT` commands. `check_and_apply_email_commands()`'s
  signature gained four new optional params (`portfolios`, `resolved_total_values`,
  `notification_cfg`, `macro_indicators`, all `None` by default, byte-identical behavior for
  every pre-existing call site that omits them, including every pre-existing test): `main()`'s
  own call site now passes its already-computed `portfolios`/`resolved_total_values`/
  `notification_cfg`/`macro_indicators` straight through, no new fetching there. This required
  moving the `macro_indicators = get_cached_or_fetch_macro_indicators(...)` fetch to run
  BEFORE the email-commands check (previously after it), a pure reordering, no behavior change
  to the fetch itself, since a `TRIGGER_REPORT` needs it to build a report immediately. The new
  branch fetches ONLY what a report needs right then, not the full per-portfolio rebalance
  machinery (no orphaned-ticker classification, no `scope_overlapping_holdings()`): current
  positions via a real `get_ibkr_positions()` broker query in `--live`, or
  `reconstruct_dry_run_positions()` in dry-run (only if `cfg.persist_dry_run_state`, else `{}`,
  matching the main per-portfolio loop's own default), and latest prices via
  `fetch_live_prices()`/`compute_required_lookback_days()` (lazily imported inside the branch,
  matching the existing lazy-import precedent for these two names elsewhere in this file), then
  calls `build_and_send_portfolio_report()` (Epic 1) directly with the requested
  `report_type.lower()`. When `portfolios`/`resolved_total_values` aren't supplied (e.g. an
  older integration or a test only exercising other commands), `TRIGGER_REPORT` falls back to
  the same manual-follow-through reply `LIQUIDATE`/`ADJUST_PARAM` get, rather than crashing.
  When `build_and_send_portfolio_report()` returns `False` (no `portfolio_snapshot_<name>.csv`
  exists yet, the portfolio has never completed a run), a dedicated reply explains that instead
  of silence. See `docs/EMAIL_COMMANDS.md`'s updated command table and "What happens to each
  command" section.
  **A real, confirmed, pre-existing incident found and fixed while Docker-verifying
  `TRIGGER_REPORT` against real accumulated data, unrelated to Epic 2's own code**:
  `live_trades_log_portfolio1.csv`/`live_trades_log_portfolio2.csv` had a stale 13-column
  header (predating a `transaction_amount` column added to `log_orders()`'s schema earlier
  in this project's history) while containing a genuine mix of 13- and 14-field rows, making
  `pd.read_csv()` raise `ParserError: ... Expected 13 fields ... saw 14` for the WHOLE file,
  confirmed to be entirely unrelated to duplicate dates or Docker/filesystem flakiness first
  suspected while diagnosing it, a real dead end ruled out by reproducing the identical error
  via `build_and_send_portfolio_report()` called in complete isolation. Fixed via a one-time
  manual migration (not part of the shipped package, `rotate_hash_chained_log()` couldn't be
  reused directly since it always writes the SAME original header to both outputs by design):
  rows split by ACTUAL field count into a `.archive_<timestamp>.csv` (old header, old rows,
  re-chained from `GENESIS`) and a rewritten active file (corrected new header, new rows, also
  re-chained), verified via `pd.read_csv()`, `read_trade_log_with_archives()`, and
  `verify_log_integrity()` on all four resulting files. See `docs/LOG_RETENTION.md`'s "A real,
  confirmed incident this mechanism was manually applied to retroactively" for the full
  writeup, including the false-positive `awk`-based diagnostic dead end (quoted commas in a
  dollar-formatted `reason` field defeat naive comma-splitting) worth knowing about before
  trusting `awk -F','` as a diagnostic for any CSV in this project again.
  `--check-commands-only` (Epic 3, same plan) is the new lightweight, frequent-cron-friendly
  entry point: reuses `main()`'s existing arg parsing/config load/`resolve_total_values()`
  setup unchanged, calls the Epic 2-enriched `check_and_apply_email_commands()`, then `return`s
  immediately, deliberately skipping shared log retention, config-change detection, and the
  entire per-portfolio stop-loss/rebalance loop, inserted right after the email-commands
  try/except and before `apply_shared_log_retention()`. With no pending command the cost is one
  IMAP search and nothing else, `PAUSE`/`RESUME`/`SKIP_NEXT_REBALANCE`/`SET_MAX_DRAWDOWN` still
  only actually apply with `--live` (unchanged `check_and_apply_email_commands()` semantics),
  `TRIGGER_REPORT`/`STATUS`/`ALERTS_REPORT` apply in both modes. `docker-entrypoint.sh` gained a
  new `EMAIL_CHECK_CRON` env var (default `*/5 * * * *`, `docker-compose.yml`/`.env.example`,
  identical `"${VAR:-default}"` pattern as `DAILY_RUNNER_CRON`/`RISK_MONITOR_CRON`) generating a
  `daily-runner --check-commands-only >> /app/logs/email_check_$(date +\%Y\%m\%d).log 2>&1` cron
  line (dry-run by default, commented `--live`/`--confirm-live-trading` variants alongside it,
  same deliberate not-env-var-driven friction as the main `daily-runner` line). See
  `docs/EMAIL_COMMANDS.md`'s new "Command latency" section and `docs/DEPLOYMENT.md`'s native
  Task Scheduler/cron equivalent (Path B, step 8).
  The monthly report (Epic 4, same plan) is now decoupled from `is_rebalance_day()`: its
  `report_day and datetime.today().day == report_day` check was moved out of the `if
  args.force_rebalance or is_rebalance_day(...):` block and into the unconditional "ALWAYS
  runs" section, right after the daily report block, calling `build_and_send_portfolio_report(
  ..., report_type="monthly")` there instead, a pure call-site relocation, same
  `current_positions`/`latest_prices`/`trade_log_path`/`total_value` already in scope. Fixes a
  real gap: previously, a monthly report date that didn't happen to ALSO be a rebalance day (or
  a forced one) silently never fired that month, e.g. a portfolio on a quarterly rebalance
  cadence whose `monthly_report_day_of_month` doesn't land on one of those few rebalance dates.
  The daily report never had this problem, it already lived in the unconditional block; this
  epic brings the monthly report in line with that existing, correct pattern. One small,
  deliberate deviation from a byte-identical relocation: the moved block is now wrapped in a
  `try/except Exception -> logger.warning("[%s] Monthly report skipped due to error
  (non-fatal): %s", ...)`, matching the daily report block right next to it (which already had
  one) and every other ALWAYS-runs check in this function; the OLD, un-wrapped call site would
  have let a report-building exception propagate to `main()`'s own outer
  `except Exception -> sys.exit(1)`, aborting every REMAINING portfolio's rebalance for the
  day, a real, newly-relevant risk once this check runs unconditionally rather than only
  alongside an already-successful rebalance. See `docs/EMAIL_REPORTING.md`'s updated PERIODIC
  section.
  `check_and_handle_trailing_stops()` (Epic 1, "Institutional Momentum Best-Practice Gaps" plan)
  is a third daily exit check, parallel to and following the exact same flag-or-auto-execute
  shape as `check_and_handle_stop_losses()`/`check_and_handle_time_stops()` (same
  `cfg.auto_execute_stop_loss` toggle, same `log_alert()`/`log_orders()`/`place_orders_ibkr()`
  triple-step). Persists a per-portfolio high-water-mark JSON file,
  `data_dir() / f"trailing_stop_hwm_{portfolio}.json"` (`{ticker: high_price}`), same
  flat-file-in-`data_dir()` convention as `risk/circuit_breaker.py`'s `_peak_equity_path()` and
  `detect_and_log_config_change()`'s config snapshot, written atomically (temp file +
  `os.replace()`, matching `_write_rebalance_in_progress_marker()`'s precedent). Each run: prune
  any ticker no longer in `current_positions` (a full exit, so a later re-entry starts a fresh
  trail rather than inheriting a stale high), update `hwm = max(stored.get(t, entry_price),
  latest_price)` for every currently-held ticker, flag for exit once drawdown from that high
  breaches `cfg.trailing_stop_pct`. Reuses `resolve_ticker_stop_loss_pct()`'s existing
  `ticker_risk_overrides[ticker]['enabled']` kill-switch, same single "stop-checking off for this
  ticker" semantics the fixed stop-loss check already uses.
  A real, confirmed pre-existing gap found and fixed while wiring this third check in, unrelated
  to trailing-stop logic itself: `check_and_handle_stop_losses()` and
  `check_and_handle_time_stops()` already ran back-to-back against the SAME `current_positions`
  snapshot with zero de-duplication, confirmed by reading the call site, a ticker breaching BOTH
  in the same run would already generate two independent live SELL orders before this fix, adding
  a third independent check without fixing this would have made a three-way collision possible.
  All three functions now take an optional `already_flagged: set | None = None` param (`None`
  default byte-identical to before this param existed for any pre-existing caller/test): a ticker
  already in the set (flagged by an earlier check this run) is skipped, and each function's own
  newly-flagged tickers are added to the set (mutated in place) before returning. The
  per-portfolio loop's call site now constructs one `already_flagged_this_run: set` and threads
  it through all three calls.
  Live price-vendor priority (Epic 6, "Stale-Price Reporting + Live Price-Vendor Priority" plan):
  all THREE of this file's `fetch_live_prices()`-related call sites (the `TRIGGER_REPORT` email-
  command branch, the per-portfolio "ALWAYS runs" block, and the call to `execution/
  live_signal.py`'s `run()` itself) now read `os.environ.get("FMP_API_KEY")`/
  `os.environ.get("EODHD_API_KEY")` and pass them through as `fmp_api_key=`/`eodhd_api_key=`
  kwargs, same inline-read pattern the existing fundamentals call site already used. This is a
  **deliberate, informed reversal** of a prior deliberate choice (this file previously NEVER
  populated these params at all, confirmed by every earlier epic's own live validation,
  production price data came from `yfinance` only regardless of whether these keys were
  configured), not an oversight, prompted by a real, confirmed incident: investigating a `$nan`
  "Current Close Price" in a real rebalance email led to discovering `yf.download()` currently
  returns `NaN` OHLC for the most recent trading day across many tickers at once (see Epic 5,
  same plan). `core/functions.py`'s `get_bulk_prices()` already implemented a real `FMP ->
  EODHD -> yfinance` auto-fallback for VENDOR SELECTION (`source=None` mode tries FMP on the
  FIRST ticker only, catches ANY exception including a quota-exceeded `HTTPError` from
  `urlopen()`, falls to EODHD, catches, falls to `yfinance`), and `fetch_live_prices()` already
  accepted and forwarded these params, so wiring real keys through was expected to be the whole
  fix.
  **A second real, confirmed bug found via this epic's own real-key production verification
  (not synthetic, not anticipated by the plan)**: the vendor chosen above is detected from ONE
  ticker only, but a real API key can fail for a SUBSET of tickers under that SAME vendor even
  after the first one succeeded, confirmed directly against the real `.env` `FMP_API_KEY`: it
  returned `402 Payment Required` for several symbols (`ADI`, `ARM`, `ASML`, `AVGO`, `IBKR`,
  `ORCL`, `QCOM`, `VGT`, `VOO`) while succeeding for others (`AAPL`, `AMD`, `AMZN`, `GOOGL`,
  `META`, `MSFT`, `NVDA`, `PLTR`, `TSLA`, `TSM`) in the exact same batch, a real plan-tier
  limitation, not a bug in the key itself. The OLD per-ticker loop passed a FIXED
  `source=source_used` for every ticker (no fallback for an individual ticker's failure,
  `get_stock_prices()`'s explicit-source mode raises straight through), silently DROPPING any
  ticker that failed under the batch-detected vendor from the returned DataFrame entirely;
  with enough tickers dropped this way, `daily_prices` no longer covered every configured
  ticker, crashing `execution/live_signal.py`'s `run()` with a `KeyError` several frames away in
  `core/strategy_signals.py`'s `resolve_strategy_scores()` (`daily_prices[list(tickers)]`), not
  obviously connected to a vendor `402` at a glance, confirmed via a real
  `daily-runner --force-rebalance` crash against the real production `config.yaml`. Fixed: when
  the vendor was auto-detected (`source is None`, the only mode any real call site in this
  project uses), each ticker's own fetch now cascades through the REMAINING vendors in priority
  order (starting from `source_used`, so the common, all-succeed case is unchanged, exactly one
  attempt per ticker) before giving up on that specific ticker, matching "if exhausted, continue
  to the next API, download the whole portfolio's data" exactly. A second, smaller bug found
  and fixed alongside it: the adjusted-close COLUMN NAME to extract (`adjClose` for FMP,
  `adjusted_close` for EODHD, `Adj Close` for yfinance) was computed ONCE for the whole batch
  from `source_used`, so a ticker that fell back to a DIFFERENT vendor had its real data present
  but its price column unrecognized (`"Warning: No price column found"`, silently dropped
  again); `_price_col_for()` (new small helper) is now resolved PER TICKER, from whichever
  vendor actually answered for it. Explicit-source callers (`source` passed directly to
  `get_bulk_prices()` itself, not auto-detected) are UNCHANGED by both fixes, no cascade,
  preserving that strict single-vendor contract. See `tests/core/test_functions.py` (a
  brand-new file, this module had zero prior pytest coverage).
  Deliberately scoped to `fetch_live_prices()` only
  (the momentum-ranking/reporting price feed); `fetch_ohlcv_for_tickers()` (technical indicators,
  the liquidity filter, technical-confirmation volume) is untouched, still never receives real
  keys from any `daily_runner.py` call site, out of scope for this epic. Real, immediate
  production impact once `.env`'s `FMP_API_KEY`/`EODHD_API_KEY` are valid and not already
  exhausted: every real portfolio's live price feed genuinely starts sourcing from FMP first
  instead of always falling through to `yfinance`.
  `check_and_handle_trailing_stops()`'s ATR-Based Trailing Stop extension (Epic 2, "Institutional
  Risk-Management Features" plan, `cfg.use_atr_trailing_stop`) shares the SAME `stored_hwm`
  dict/file the percentage trail above already persists, no schema change: a second per-ticker
  pass, gated separately (`if cfg.use_atr_trailing_stop and held_tickers:`), skipping any ticker
  already in `flagged` (from the percentage trail) or `already_flagged` (from an earlier check
  this run), so a position breaching BOTH trail types in one run generates exactly one exit
  order. Fetches OHLC for CURRENTLY-HELD tickers only via the EXISTING
  `fetch_ohlcv_for_tickers()` (already imported/used elsewhere in this file for the email
  report's technical indicators, no new fetch mechanism), a much smaller live-side cost than the
  backtest's full-panel plumbing (see `backtest/momentum_backtest.py`'s own bullet for that
  side). A ticker missing from the OHLCV fetch result (vendor gap) skips the ATR check for that
  ticker only, not the whole run. Exit condition mirrors the backtest's own dollar-distance
  comparison exactly: `(hwm - latest_price) >= multiplier * atr_latest`, gated by
  `resolve_ticker_atr_multiplier()`'s own `None`-means-disabled contract. New
  `ATR_TRAILING_STOP_TRIGGERED` `CRITICAL` alert (`log_alert()`), same severity as the existing
  `TRAILING_STOP_TRIGGERED`, distinguishable in the alerts log. The function's early-return guard
  changed from `if not cfg.use_trailing_stop: return []` to `if not cfg.use_trailing_stop and not
  cfg.use_atr_trailing_stop: return []`, so the function now correctly runs (and persists the HWM
  file) when EITHER trail type alone is enabled, not just the percentage one. See
  `docs/RISK_CONSTRAINTS.md`'s "ATR-Based Trailing Stop" section for the full mechanism and the
  backtest-side OHLC-plumbing gap this epic closed.
  **Epic 2 real verification, run 2026-08-05**: full pytest suite 1014 passed (up from 994
  pre-Epic-2, +20 new tests), zero regressions. Real end-to-end paper-account confirmation (port
  7497), both natively and inside a Docker container rebuilt with this epic's code: a real BUY
  into a throwaway single-ticker test position, then a second invocation with a deliberately
  tiny `atr_trailing_stop_multiplier` (`0.01`) to force a near-certain trigger from ordinary
  bid/ask tick noise, confirmed a REAL computed ATR from real fetched OHLCV (e.g. `$8.17` for
  INTC, not simulated), a real `ATR_TRAILING_STOP_TRIGGERED` alert, and a real auto-executed
  SELL closing the position (`execDetails`/`commissionReport` confirmed). Repeated with two
  different tickers for confidence, then confirmed identically inside Docker (same real ATR
  value, alert email sent successfully there too). Both the regular-trading-hours order-queuing
  path (informational `error 399`, order correctly queued for next session, not a crash) and the
  `allow_extended_hours` real-fill path were exercised for real.
  **A real, confirmed cross-CONFIG-FILE ticker-overlap incident was found during this
  verification, NOT fixed, still open**: `scope_overlapping_holdings()` only protects against
  ticker overlap BETWEEN PORTFOLIOS WITHIN ONE LOADED `config.yaml` (confirmed by reading
  `check_ticker_overlap()`, it inspects only the currently-loaded file's own portfolios), not
  overlap between the currently-loaded config and ANY OTHER config file that has traded the same
  real account. A throwaway single-ticker test config sharing a ticker (`NVDA`) with this
  project's own real `portfolio1` saw the REAL whole-account position and generated a real
  (paper, no financial loss) full-liquidation SELL of it. See `docs/RISK_CONSTRAINTS.md`'s "ATR-
  Based Trailing Stop" section and `README.md`'s Known Gaps for the full incident writeup; the
  practical mitigation until a real fix exists is procedural (never point `--config` at a file
  whose tickers overlap another config trading the same account).

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
- `docs/SIGNAL_RANKINGS_LOG.md`, the full ranked-universe log/email table (Momentum Rank,
  Lookback Return, Selection Status, Stop-Loss Price for every configured ticker, not just the
  ones selected), and why it's a separate file/table from the trade log

## Constraints for documentation
- Do not use "—" to comment, document the code or add this marks on files.