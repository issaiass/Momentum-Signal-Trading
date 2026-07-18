# Testing Guide

> **New to this project?** Start with `../README.md`. This file explains the `tests/` suite
> specifically: how to run it, what each file covers, and how to interpret a failure.

## Setup and running

```bash
pip install -r requirements-dev.txt   # adds pytest on top of requirements.txt
pytest tests/ -v                       # run everything, verbose
```

Common variations:

```bash
pytest tests/test_momentum_backtest.py -v        # just one file
pytest tests/ -k "circuit_breaker" -v             # just tests matching a name pattern
pytest tests/ -x                                   # stop at the first failure
pytest tests/ --tb=short                            # shorter tracebacks
```

No network access, no broker connection, and no real market data are required to run the
suite, everything runs against synthetic (seeded, reproducible) data or mocked functions.
That's a deliberate scope boundary; see "What this suite does NOT tell you" below.

## Test organization

| File | Covers |
|---|---|
| `conftest.py` | Shared fixtures (see below), no tests of its own |
| `test_architecture.py` | Package-restructure regressions: import boundaries, cross-directory path resolution (subprocess-based), the circuit-breaker extraction's decoupling, console-script installability |
| `backtest/test_momentum_backtest.py` | Core backtest engine: `BacktestConfig` validation (including `allow_fractional_shares`, `top_n`, fractional `holding_period`, fractional `lookback_period` for short-term/weekly momentum configs, and the three new risk-constraint fields `max_turnover_pct`/`skip_month_guardrail`/`position_vol_budget`), the shared `resolve_target_weights()` sizing path including `_apply_volatility_budget_caps()`'s per-ticker vol-budget cap (confirmed complementary to, not overridden by, the flat `max_position_weight` cap), crash-protection mechanisms (circuit breaker, correlation spike detection, liquidity stress) |
| `execution/test_live_signal.py` | Live order generation (BUY/SELL/HOLD sizing), real FIFO P&L math, multi-portfolio orchestration, `get_top_etfs()`'s `top_n` selection behavior, the live-trading equivalents of backtest-only risk fields: `compute_aggregate_drift()`, `derive_entry_date()`, correlation-spike exposure scaling, `place_orders_ibkr()`'s sells-before-buys sequencing, cash-aware buy sizing, whole-share flooring for fractional orders (IBKR has no fractional equity API support), that orders dropped before ever reaching IBKR (flooring to 0 shares, cash-scaling to 0 shares) are still recorded in the returned results as `DROPPED_FRACTIONAL`/`DROPPED_INSUFFICIENT_CASH` rather than silently omitted, and that informational IBKR order-status codes never corrupt a real order's tracked status (mocked IBKR, no real broker needed), and that each wired alert point actually appends the right row to the alert log, plus `is_rebalance_day()`'s monthly and weekly-granularity scheduling logic, the `is_holding_period_too_frequent()`/`is_lookback_period_too_short()` threshold boundaries, `resolve_momentum_scores()`'s hand-verified weekly-vs-monthly momentum ranking arithmetic (all four documented short-term lookback pairs, a regression check that the monthly branch matches the pre-existing computation exactly, an integration test confirming `run()` actually wires `holding_period` through, and the opt-in `skip_month_guardrail`'s monthly-window shift, its no-op cases at `lookback_period <= 3` and in the weekly regime), the `docs/RISK_CONSTRAINTS.md` advisory checks `is_lookback_shorter_than_holding()`/`is_lookback_to_holding_ratio_too_low()`/`is_turnover_too_high()` and `compute_turnover()`'s hand-computed position-count ratio, and `build_position_performance()`'s return-since-entry calc, omission rules (missing entry price/zero shares/unknown live price), and graceful handling of an undeterminable entry date |
| `test_daily_runner.py` | CLI/operational layer: `config.yaml` schema validation, idempotent rebalance locking, alert fallback, circuit-breaker persistence-across-runs, the live time-based stop (`check_and_handle_time_stops`) and price-based stop-loss (`check_and_handle_stop_losses`), multi-portfolio capital safety on a shared IBKR account (`resolve_total_values()`'s remainder math, `check_ticker_overlap()`), `send_warning`/`send_email_command_feedback` config-type validation, the `ALERTS_REPORT` email command's end-to-end read-and-reply path, the `send_email_command_feedback` flag gating reply emails without blocking command application, per-command apply-time `ERROR` isolation (one command failing to apply does not abort the rest of the batch), the same-inbox visibility warning plus `--test-email`'s exit-code/no-config-load behavior, and `lookback_period` resolving independently per portfolio via `risk_overrides` |
| `test_docker_entrypoint.py` | `docker-entrypoint.sh`'s crontab generation, run as a real subprocess: configurable schedule times, per-portfolio `risk_monitor.py` coverage |
| `test_governance.py` | Institutional governance: VaR/CVaR, scenario shocks, capacity checks, tamper-evident hash-chained audit log, independent `risk_monitor.py` (including its `config.yaml` capital fallback), config-approval gate |
| `test_reporting.py` | Investor-facing reporting: portfolio snapshots, rank/signal-score trade context, benchmark comparison, external-holdings correlation check, multi-lookback signal blending |
| `test_execution_safety.py` | Broker resilience follow-ups + additional execution safety: dollar drawdown breaker, slippage tolerance, stale price feed protection, time-based stops |
| `interfaces/test_notifications.py` | Categorized email notifications: CRITICAL cannot be filtered, STANDARD/PERIODIC/DAILY/WARNING respect config (DAILY uniquely defaults to off), HTML/chart generation degrades gracefully, the rebalance summary's "What Actually Happened" column correctly reflects real fills, dropped orders, rejections, still-open orders, and dry-run mode, plus the monthly/daily report builders' shared strategy-stats/technical-indicators/fundamental-indicators/macro-context/position-performance sections and `build_comparison_bar_chart()` |
| `interfaces/test_email_commands.py` | Email-commanded remote actions: sender authentication, `ADJUST_PARAM` allowlist including `top_n` (security-critical), `LIQUIDATE` confirmation phrase, `ALERTS_REPORT` parsing, fail-safe behavior on malformed input, the subject-marker guard against a same-inbox reply cascade, `log_command_attempt()`/`build_reply_body()`'s three-way `ACCEPTED`/`REJECTED`/`ERROR` outcome (default-unchanged regression plus explicit `ERROR` behavior), and a mocked-IMAP poll-level `ERROR` case (connection failure before any message is fetched) |
| `interfaces/test_email_diagnostics.py` | `run_email_diagnostics()`'s live SMTP/IMAP checks (mocked `smtplib`/`imaplib`, no real network): pass/fail/skip reporting per check, and the Gmail-App-Password / Outlook-OAuth2-specific remediation hints on an authentication failure |
| `core/test_audit_log.py` | The shared hash-chain helper (`append_hash_chained_row()`) every new alert-log write goes through, `log_alert()`'s schema, and `read_recent_alerts()`'s filtering/limit/ordering behavior backing the `ALERTS_REPORT` email command |
| `core/test_technical_indicators.py` | Hand-rolled SMA/EMA/RSI/MACD/ATR/Bollinger/ADX/VWAP/OBV: hand-verifiable known-value cases (constant/monotonic price series), RSI/ADX boundary checks ([0, 100]), and `compute_latest_indicators()`'s graceful empty-dict behavior on insufficient history |
| `core/test_functions_quant_extensions.py` | The new live-performance-report wiring: `since_inception_performance()`'s graceful Sharpe/Sortino degradation on short history, `daily_window_comparison()`/`monthly_window_comparison()`'s window-omission behavior, and a regression guard confirming `monthly_window_comparison()` never raises against short live histories (the bug that ruled out reusing `trailing_returns()` directly, see `CLAUDE.md`) |
| `core/test_fundamentals.py` | FMP-first/EODHD-fallback fetch of P/E, PEG, ROE, Debt-to-Equity, Current Ratio: vendor fallback behavior (FMP fails -> EODHD tried; both fail -> `{}`, never an exception), no-API-key short-circuit, and the file cache's hit/miss/expiry/corrupt-file/per-ticker-independence behavior |
| `core/test_macro_data.py` | FRED-sourced Fed Funds Rate/CPI: `FRED_API_KEY` unset short-circuits before any network attempt, one series failing doesn't block the other, FRED's "." missing-value marker is handled without raising, and the same file-cache hit/miss/expiry/corrupt-file behavior as fundamentals |

Current count: **413 tests**, all passing. Every test file and class has a docstring explaining
*why* that group of tests exists, not just what it checks, read those docstrings first if a
test's purpose isn't obvious from its name.

## Fixtures (`conftest.py`)

| Fixture | What it provides | Why |
|---|---|---|
| `synthetic_daily_prices` | Seeded (`np.random.seed(0)`), 5-ticker, ~2.5-year daily price panel | Deterministic input so tests are reproducible run to run, **not** real market data, and never claimed to be |
| `synthetic_monthly_picks` | Top-2-by-momentum picks derived from the fixture above | Matches the same signal pattern used elsewhere in this project, so backtest-engine tests exercise realistic-shaped inputs |
| `sample_config_dict` | A minimal, valid `config.yaml`-shaped dict | Baseline for schema-validation tests to mutate into invalid variants |

**Important scope note:** these fixtures test *code mechanics*, does the sizing math work,
does validation catch bad input, does the audit log survive tampering. They do **not** test
*strategy validity*, nothing here tells you whether momentum as a strategy makes money. That
question requires running the actual walk-forward/holdout tooling (Notebook 1) against real
market data, which is a separate, not-yet-completed step (see `../README.md`'s "Project
Maturity & Safety" section).

## How to interpret a failure

**A failure in this suite after you haven't changed any code** usually means an environment
difference, not a real regression. The most common historical example in this project: a
pandas version difference (`pct_change(fill_method=...)` was removed in pandas 3.x) caused
`ValueError`s that had nothing to do with the actual logic being wrong. Before assuming a real
bug:
1. Check `requirements.txt`/`requirements-dev.txt` versions match what's actually installed
   (`pip freeze | grep -i pandas`, etc.)
2. Check the traceback's actual exception type and message, a `TypeError`/`ValueError` from a
   library call is more likely an environment mismatch than a `AssertionError` from an actual
   test assertion
3. Re-run just that one test in isolation (`pytest tests/path::TestClass::test_name -v`) to
   rule out test-ordering side effects

**A failure after you HAVE changed code**, read the specific test's docstring/comment first;
it should tell you what behavior it's protecting and why, which is usually enough to tell you
whether your change legitimately altered that behavior (update the test) or broke something
unintentionally (fix the code).

## When to add a new test

- **Any new `BacktestConfig` field**, add both a validation test (invalid value raises) and a
  run-succeeds test (a full `run_custom_backtest()` call with the new field enabled doesn't
  crash), matching the pattern in `TestBacktestConfigValidation`/`TestBacktestRuns`.
- **Any new function with a numeric claim** (e.g. "this downweights correlated assets," "this
  computes P&L correctly"), assert the actual *direction or value* of the effect, not just
  "it ran without error." Several tests in this suite include a hand-verifiable calculation in
  a comment for exactly this reason.
- **Any change to the trade-log CSV schema**, add a test confirming
  `measure_live_performance()`'s FIFO parsing still works with the new schema (see
  `TestSignalContextInOrders::test_pnl_parsing_unaffected_by_wider_schema` for the pattern).
- **Any new config-schema field in `config.yaml`**, add a validation test in
  `test_daily_runner.py::TestConfigSchemaValidation` for both the valid and invalid case.

## What this suite does NOT tell you

Worth repeating because it's easy to forget once a suite is green: **413 passing tests confirm
the code does what it's supposed to do, mechanically. They do not confirm the momentum
strategy itself is profitable or safe in a real crash**, that's a separate question from
execution mechanics, which have now been confirmed against a real (paper) broker connection.
See `../README.md`'s "Project Maturity & Safety" and "Known Gaps" sections for what remains
unvalidated.
