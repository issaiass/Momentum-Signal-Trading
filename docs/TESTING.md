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
suite — everything runs against synthetic (seeded, reproducible) data or mocked functions.
That's a deliberate scope boundary; see "What this suite does NOT tell you" below.

## Test organization

| File | Covers |
|---|---|
| `conftest.py` | Shared fixtures (see below) — no tests of its own |
| `test_architecture.py` | Package-restructure regressions: import boundaries, cross-directory path resolution (subprocess-based), the circuit-breaker extraction's decoupling, console-script installability |
| `backtest/test_momentum_backtest.py` | Core backtest engine: `BacktestConfig` validation (including `allow_fractional_shares`, `top_n`), the shared `resolve_target_weights()` sizing path, crash-protection mechanisms (circuit breaker, correlation spike detection, liquidity stress) |
| `execution/test_live_signal.py` | Live order generation (BUY/SELL/HOLD sizing), real FIFO P&L math, multi-portfolio orchestration, `get_top_etfs()`'s `top_n` selection behavior, (Epic 25) the live-trading equivalents of backtest-only risk fields: `compute_aggregate_drift()`, `derive_entry_date()`, correlation-spike exposure scaling, (Epic 28) `place_orders_ibkr()`'s sells-before-buys sequencing, cash-aware buy sizing, whole-share flooring for fractional orders (IBKR has no fractional equity API support), that orders dropped before ever reaching IBKR (flooring to 0 shares, cash-scaling to 0 shares) are still recorded in the returned results as `DROPPED_FRACTIONAL`/`DROPPED_INSUFFICIENT_CASH` rather than silently omitted, and that informational IBKR order-status codes never corrupt a real order's tracked status (mocked IBKR, no real broker needed), and (Epic 29) that each wired alert point actually appends the right row to the alert log |
| `test_daily_runner.py` | CLI/operational layer: `config.yaml` schema validation, idempotent rebalance locking, alert fallback, circuit-breaker persistence-across-runs, (Epic 25) the live time-based stop (`check_and_handle_time_stops`) and price-based stop-loss (`check_and_handle_stop_losses`), (Epic 26) multi-portfolio capital safety on a shared IBKR account (`resolve_total_values()`'s remainder math, `check_ticker_overlap()`), (Epic 27) `send_warning` config-type validation, and (Epic 29) the `ALERTS_REPORT` email command's end-to-end read-and-reply path |
| `test_docker_entrypoint.py` | `docker-entrypoint.sh`'s crontab generation, run as a real subprocess: configurable schedule times, per-portfolio `risk_monitor.py` coverage (Epic 22) |
| `test_epic2_governance.py` | Institutional governance: VaR/CVaR, scenario shocks, capacity checks, tamper-evident hash-chained audit log, independent `risk_monitor.py` (including its `config.yaml` capital fallback, Epic 19), config-approval gate |
| `test_epic4_reporting.py` | Investor-facing reporting: portfolio snapshots, rank/signal-score trade context, benchmark comparison, external-holdings correlation check, multi-lookback signal blending |
| `test_epic8_10_safety.py` | Broker resilience follow-ups + additional execution safety: dollar drawdown breaker, slippage tolerance, stale price feed protection, time-based stops |
| `interfaces/test_notifications.py` | Categorized email notifications: CRITICAL cannot be filtered, STANDARD/PERIODIC/WARNING (Epic 27) respect config, HTML/chart generation degrades gracefully, the rebalance summary's "What Actually Happened" column correctly reflects real fills, dropped orders, rejections, still-open orders, and dry-run mode |
| `interfaces/test_email_commands.py` | Email-commanded remote actions: sender authentication, `ADJUST_PARAM` allowlist including `top_n` (Epic 29, security-critical), `LIQUIDATE` confirmation phrase, `ALERTS_REPORT` parsing (Epic 29), fail-safe behavior on malformed input |
| `core/test_audit_log.py` (Epic 29) | The shared hash-chain helper (`append_hash_chained_row()`) every new alert-log write goes through, `log_alert()`'s schema, and `read_recent_alerts()`'s filtering/limit/ordering behavior backing the `ALERTS_REPORT` email command |

Current count: **251 tests**, all passing. Every test file and class has a docstring explaining
*why* that group of tests exists, not just what it checks — read those docstrings first if a
test's purpose isn't obvious from its name.

## Fixtures (`conftest.py`)

| Fixture | What it provides | Why |
|---|---|---|
| `synthetic_daily_prices` | Seeded (`np.random.seed(0)`), 5-ticker, ~2.5-year daily price panel | Deterministic input so tests are reproducible run to run — **not** real market data, and never claimed to be |
| `synthetic_monthly_picks` | Top-2-by-momentum picks derived from the fixture above | Matches the same signal pattern used elsewhere in this project, so backtest-engine tests exercise realistic-shaped inputs |
| `sample_config_dict` | A minimal, valid `config.yaml`-shaped dict | Baseline for schema-validation tests to mutate into invalid variants |

**Important scope note:** these fixtures test *code mechanics* — does the sizing math work,
does validation catch bad input, does the audit log survive tampering. They do **not** test
*strategy validity* — nothing here tells you whether momentum as a strategy makes money. That
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
2. Check the traceback's actual exception type and message — a `TypeError`/`ValueError` from a
   library call is more likely an environment mismatch than a `AssertionError` from an actual
   test assertion
3. Re-run just that one test in isolation (`pytest tests/path::TestClass::test_name -v`) to
   rule out test-ordering side effects

**A failure after you HAVE changed code** — read the specific test's docstring/comment first;
it should tell you what behavior it's protecting and why, which is usually enough to tell you
whether your change legitimately altered that behavior (update the test) or broke something
unintentionally (fix the code).

## When to add a new test

- **Any new `BacktestConfig` field** — add both a validation test (invalid value raises) and a
  run-succeeds test (a full `run_custom_backtest()` call with the new field enabled doesn't
  crash), matching the pattern in `TestBacktestConfigValidation`/`TestBacktestRuns`.
- **Any new function with a numeric claim** (e.g. "this downweights correlated assets," "this
  computes P&L correctly") — assert the actual *direction or value* of the effect, not just
  "it ran without error." Several tests in this suite include a hand-verifiable calculation in
  a comment for exactly this reason.
- **Any change to the trade-log CSV schema** — add a test confirming
  `measure_live_performance()`'s FIFO parsing still works with the new schema (see
  `TestSignalContextInOrders::test_pnl_parsing_unaffected_by_wider_schema` for the pattern).
- **Any new config-schema field in `config.yaml`** — add a validation test in
  `test_daily_runner.py::TestConfigSchemaValidation` for both the valid and invalid case.

## What this suite does NOT tell you

Worth repeating because it's easy to forget once a suite is green: **251 passing tests confirm
the code does what it's supposed to do, mechanically. They do not confirm the momentum
strategy itself is profitable or safe in a real crash** — that's a separate question from
execution mechanics, which have now been confirmed against a real (paper) broker connection.
See `../README.md`'s "Project Maturity & Safety" and "Known Gaps" sections for what remains
unvalidated.
