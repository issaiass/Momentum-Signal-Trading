# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cross-sectional momentum ETF rotation strategy: research/backtest engine plus a live trading
system (single or multi-portfolio, paper or real IBKR accounts via the TWS API). Installable
package at `src/momentum_trading/`, console script `daily-runner`. See `README.md` for the full
file inventory and folder structure — don't re-derive it, it's kept current there.

**This is unvalidated-strategy software with well-tested infrastructure** — the code mechanics
(circuit breakers, idempotency, audit logging) are solid, and a real IBKR paper connection has
now confirmed the execution mechanics work end-to-end (real BUY and SELL fills, verified
directly in TWS — see `README.md`'s "Project Maturity & Safety" section for exactly what has
and hasn't been exercised, including the live/real-money port). But the momentum *strategy*
itself — whether it has real economic edge — has never been run against real historical
out-of-sample data. Keep that distinction in mind: a passing test suite, or even a confirmed
real fill, says nothing about strategy edge.

## Commands

```bash
# Install (editable, with dev deps for pytest)
uv sync                                    # if using uv (uv.lock present)
pip install -e ".[dev]"                    # or plain pip

# Tests — no network/broker required, all synthetic/mocked data
pytest tests/ -v                           # full suite
pytest tests/test_daily_runner.py -v       # one file
pytest tests/ -k "circuit_breaker" -v      # name pattern
pytest tests/path::TestClass::test_name -v # single test
pytest tests/ -x --tb=short                # stop at first failure, short tracebacks

# Run (config.yaml required — cp config.example.yaml config.yaml first)
daily-runner --test-email                  # live SMTP/IMAP check, no config.yaml needed -- run
                                            # this once after editing .env on any machine
daily-runner --force-rebalance             # safe, no broker connection, test signal/order output
daily-runner                               # dry-run default (no --live = never places orders)
daily-runner --live --port 7497            # paper trading
daily-runner --live --port 7496 --confirm-live-trading   # real money — both flags required together
daily-runner --resume-trading <portfolio_name>            # clear a circuit-breaker halt
python -m momentum_trading.risk.risk_monitor --portfolio <name> --max-loss-pct 0.25
    # --initial-capital optional, defaults to portfolios.<name>.total_value in config.yaml

# Docker
docker compose up -d --build
docker exec -it momentum-signal crontab -l              # verify cron schedule
docker exec -it momentum-signal daily-runner --force-rebalance
```

There is no configured linter/formatter (no ruff/black/flake8 config in this repo) — don't
assume one.

## Architecture

Domain-separated sub-packages under `src/momentum_trading/`, each with a specific coupling rule
that tests enforce — don't casually violate these when editing:

- **`core/`** — pure data/signal logic, no execution or I/O side effects. `core/paths.py` is the
  single source of truth for where `config.yaml`/`data/`/`logs/` live (env var override →
  walk up for `pyproject.toml` → CWD fallback). Any new module needing the data or logs dir
  should use `data_dir()`/`logs_dir()` from here, not a bare `"data"` string —
  `tests/test_architecture.py::TestPathResolutionAcrossWorkingDirectories` guards this.
  `core/technical_indicators.py` (SMA/EMA/RSI/MACD/ATR/Bollinger/ADX/VWAP/OBV) is hand-rolled,
  not `pandas-ta` — that package hard-pins `numba==0.61.2`, incompatible with this project's
  `pandas>=3.0.3` under `uv sync`'s full dependency resolution (confirmed by direct attempt:
  installs fine standalone, breaks the project lockfile). `core/functions.py`'s
  `trailing_returns()`/`return_period_dates()` (used by the backtest's `tear_sheet()`) raise a
  `KeyError` against a short, live daily-snapshot history — their `"Since Inception"` window's
  lookback routinely falls outside the fetched market-calendar schedule, and the `"M"`-frequency
  branch skips holiday/weekend snapping entirely. Confirmed only ever exercised against full
  multi-year backtest histories before now — `functions_quant_extensions.py`'s
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
  regardless of subscription) — `core/fundamentals.py` uses FMP's `/stable/` endpoints instead
  (`/stable/ratios` + `/stable/key-metrics` for ROE). `core/functions.py`'s `_fetch_fmp()` price
  fetch has the same migration: `/stable/historical-price-eod/full` for raw OHLCV (what
  `execution/live_signal.py`'s `fetch_ohlcv_for_tickers()` needs) plus a second call to
  `/stable/historical-price-eod/dividend-adjusted` merged in for `adjClose` (what
  `get_bulk_prices()`'s momentum-ranking price series needs — unadjusted close would distort
  rankings around ex-dividend dates). The `/stable/` response is a flat list, unlike
  `/api/v3/`'s `{"historical": [...]}` wrapper — don't reintroduce that key lookup. EODHD's
  fundamentals endpoint returns `403 Only EOD data allowed for free users` on
  a free-tier key — implemented as a fallback per EODHD's documented response shape but
  unverified against a real paid response. Both cache to `data/fundamentals_cache.json` (7-day
  TTL) / `data/macro_cache.json` (30-day TTL) since neither data source changes daily; a failed
  fetch is never cached, so a transient outage or a since-added API key doesn't block retrying.
  `core/macro_data.py` needs its own `FRED_API_KEY` (free, `fred.stlouisfed.org`) — unset means
  the whole macro section is silently omitted, not an error.
- **`backtest/momentum_backtest.py`** — `BacktestConfig` (validated on construction) and
  `resolve_target_weights()`, the sizing logic shared by *both* the backtest engine and live
  execution, specifically so the two paths can't silently diverge. `lookback_period` is LIVE-ONLY
  (mirrors `commission`'s existing BACKTEST-ONLY note, opposite direction) — the engine consumes
  pre-computed `monthly_picks`, so this field only affects `daily_runner.py`'s live rebalance loop.
  `holding_period` is a `float`, not just an `int` — values below `1` map onto weeks (`0.25` =
  weekly) via `execution/live_signal.py`'s `is_rebalance_day()`; only `holding_period <= 0` is a
  hard validation error, sub-weekly values (`< 0.25`) are allowed but flagged (see below).
- **`execution/live_signal.py`** — live signal/order generation, IBKR integration (`ibapi`
  `EClient`/`EWrapper`, not a third-party wrapper), multi-portfolio orchestration, FIFO P&L,
  hash-chained audit log. `fetch_ohlcv_for_tickers()` is distinct from `fetch_live_prices()` --
  the latter returns close-only prices across many tickers at once (for momentum ranking), the
  former returns per-ticker full OHLCV (for `core/technical_indicators.py`), one
  `get_stock_prices()` call per ticker since `get_bulk_prices()` collapses to close-only.
  IBKR routes informational notices (data-farm status, an auto-set TIF,
  etc.) through the *same* `EWrapper.error()` callback as real errors — `IBKR_INFORMATIONAL_CODES`
  is the single source of truth for which codes are safe to log at `INFO` and, critically, must
  never be allowed to overwrite a tracked order's status to `"ERROR: ..."` (that mistake once
  made a real, filled order get reported as rejected — see `DEPLOYMENT.md`'s IBKR troubleshooting
  sections before adding a new code here or touching `place_orders_ibkr()`'s `error()` callback).
  Also: IBKR's API has no fractional equity/ETF order support at all, ever (not an `ibapi`
  version issue) — `place_orders_ibkr()` floors to whole shares at submission time; don't
  reintroduce `cashQty` for `STK` contracts, it doesn't work (confirmed empirically). Orders
  dropped before ever reaching IBKR (flooring to 0 shares, or cash-scaling to 0 shares) never
  get a real orderId, so `_collect_results()` alone would silently omit them — they're tracked
  separately in a `dropped_orders` dict (`DROPPED_FRACTIONAL`/`DROPPED_INSUFFICIENT_CASH`) and
  merged into the returned results, since `interfaces/notifications.py`'s rebalance summary
  email's "What Actually Happened" column depends on every ticker having *some* recorded
  outcome. Any new drop path added to `place_orders_ibkr()` should record into `dropped_orders`
  the same way, not just `continue`.
  `build_position_performance()` feeds the reports' "Position Performance (since entry)" section
  — reuses `avg_entry_price` (already tracked in `current_positions` for
  `check_and_handle_stop_losses()`'s gating) and `derive_entry_date()` (already used by
  `check_and_handle_time_stops()`), both previously computed live and discarded after the
  stop-loss/time-stop check, never surfaced anywhere before this. It's unrealized/mark-to-market
  return on the *currently open* position — distinct from `measure_live_performance()`'s
  aggregate/`per_ticker_realized` P&L (realized+unrealized across the *whole* trade history,
  including closed lots). Only populated in `--live` mode: `current_positions` is `{}` in
  dry-run (`daily_runner.py` never calls `get_ibkr_positions()` without a real connection), so
  this section is empty there — same as Technical/Fundamental Indicators, not a new gap.
- **`risk/circuit_breaker.py`** — extracted from `daily_runner.py` with alerting
  dependency-injected (`alert_fn` param) specifically so `risk/` has zero import dependency on
  `interfaces/` — enforced by an AST-based test
  (`test_risk_module_has_no_dependency_on_interfaces_module`), not just a convention.
- **`risk/risk_monitor.py`** — an intentionally *independent* read-only oversight process. It
  must not import `daily_runner.load_config()`/`BacktestConfig` or share P&L-computation code
  with `execution/live_signal.py` — the whole point is that a bug in the trading logic can't
  also blind the thing watching for it. It has its own minimal FIFO P&L re-derivation and its
  own YAML read for `total_value`. Preserve this segregation in any future edit here.
- **`interfaces/`** — email notifications (categorized CRITICAL/STANDARD/PERIODIC/DAILY/WARNING —
  CRITICAL can never be filtered, DAILY uniquely defaults to OFF when unconfigured, every other
  filterable category defaults to ON) and pydantic-validated email-commanded remote actions.
  `email_commands.py`'s `poll_and_process_commands()` guards against a same-inbox reply
  cascade with two checks together, not one: the `X-Momentum-Trading-Bot` header catches the
  bot's own generated replies, and `BOT_SUBJECT_MARKER`/`_is_bot_thread()` catches a *human's*
  reply to those replies (which never carries the header) — don't remove either one without
  re-reading why both exist. `email_diagnostics.py`'s `run_email_diagnostics()` backs
  `daily-runner --test-email`, a live SMTP+IMAP check independent of `config.yaml`.
  `notifications.py`'s `build_monthly_report_html()`/`build_daily_report_html()` are both thin
  wrappers over a shared `_build_report_html()` — the two reports differ only in cadence/window
  scale, not structure, so keep it that way rather than letting them diverge into two copies.
- **`daily_runner.py`** — the actual scheduled entry point (`daily-runner` console script).
  Loads and schema-validates `config.yaml`, loops over every portfolio defined under
  `portfolios:`, idempotent per day, refuses `--live` unless `config.yaml`'s
  `metadata.approved_by`/`approved_date` are set. `--port`'s default reads the `IBKR_PORT` env
  var (falling back to `7497`) — mirrors `execution/live_signal.py`'s existing `IBKR_HOST` env
  var pattern; an explicit `--port` on the command line always overrides it.

**Config flow**: `config.yaml` (gitignored; copy from `config.example.yaml`) →
`daily_runner.load_config()` builds one `BacktestConfig` per portfolio from
`default_risk` + that portfolio's `risk_overrides`. `total_value: null` means pull real
`NetLiquidation` from IBKR (`--live` only); a number means a fixed capital baseline.

**Safety defaults that are load-bearing, not incidental** — never change these without an
explicit user ask: dry-run is the *unflagged default* (`--live` is opt-in, and there is no
`--dry-run` flag — passing one is an argparse error, since `parse_args()` is strict); real-money
trading requires `--port 7496` **and** `--confirm-live-trading` together; circuit-breaker halts
require explicit `--resume-trading`, never auto-clear; `docker-entrypoint.sh`'s `--live`/
`--confirm-live-trading` are manual-edit-and-rebuild-only, deliberately NOT env-var-driven like
every other setting in that file (`DAILY_RUNNER_CRON`, `IBKR_HOST`/`IBKR_PORT`) — considered and
explicitly rejected, since an env var toggle would let real-money trading get enabled by a plain
`.env` edit alone, no code change or rebuild required.

## Testing conventions

- Entire suite runs on synthetic/seeded data or mocked IBKR calls — no network or broker needed.
  See `docs/TESTING.md` for fixture details and how to interpret a failure (most post-change
  failures are either a real regression or a dependency-version mismatch, not a strategy issue).
- `tests/test_architecture.py` specifically protects the package restructure (import boundaries,
  cross-directory path resolution via subprocess, the circuit-breaker extraction's decoupling) —
  distinct from the rest of the suite, which tests strategy/execution logic.
- When adding a `BacktestConfig` field, a new `config.yaml` schema field, or changing the trade
  log CSV schema, add both a validation test and a run-succeeds test — see `docs/TESTING.md`
  "When to add a new test" for the exact existing patterns to follow.

## Deeper docs (read before touching related code, don't duplicate here)

- `docs/RUNNING.md` — day-to-day run commands, staged rollout (paper → small live → full live)
- `docs/DEPLOYMENT.md` — one-time setup, SMTP/OAuth, Docker/Task Scheduler/systemd specifics
- `docs/TESTING.md` — test organization and fixtures
- `docs/STRATEGY_THEORY.md` — momentum theory, worked example
- `docs/EMAIL_REPORTING.md` / `docs/EMAIL_COMMANDS.md` — notification and remote-command setup
