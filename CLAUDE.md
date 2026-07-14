# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cross-sectional momentum ETF rotation strategy: research/backtest engine plus a live trading
system (single or multi-portfolio, paper or real IBKR accounts via the TWS API). Installable
package at `src/momentum_trading/`, console script `daily-runner`. See `README.md` for the full
file inventory and folder structure — don't re-derive it, it's kept current there.

**This is unvalidated-strategy software with well-tested infrastructure** — the code mechanics
(circuit breakers, idempotency, audit logging) are solid, but the momentum strategy itself has
never been run against real data or a real broker connection (`README.md`'s "Are You Ready?"
table). Keep that distinction in mind: a passing test suite says nothing about strategy edge.

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
- **`backtest/momentum_backtest.py`** — `BacktestConfig` (validated on construction) and
  `resolve_target_weights()`, the sizing logic shared by *both* the backtest engine and live
  execution, specifically so the two paths can't silently diverge.
- **`execution/live_signal.py`** — live signal/order generation, IBKR integration (`ibapi`
  `EClient`/`EWrapper`, not a third-party wrapper), multi-portfolio orchestration, FIFO P&L,
  hash-chained audit log.
- **`risk/circuit_breaker.py`** — extracted from `daily_runner.py` with alerting
  dependency-injected (`alert_fn` param) specifically so `risk/` has zero import dependency on
  `interfaces/` — enforced by an AST-based test
  (`test_risk_module_has_no_dependency_on_interfaces_module`), not just a convention.
- **`risk/risk_monitor.py`** — an intentionally *independent* read-only oversight process. It
  must not import `daily_runner.load_config()`/`BacktestConfig` or share P&L-computation code
  with `execution/live_signal.py` — the whole point is that a bug in the trading logic can't
  also blind the thing watching for it. It has its own minimal FIFO P&L re-derivation and its
  own YAML read for `total_value`. Preserve this segregation in any future edit here.
- **`interfaces/`** — email notifications (categorized CRITICAL/STANDARD/PERIODIC — CRITICAL
  can never be filtered) and pydantic-validated email-commanded remote actions.
- **`daily_runner.py`** — the actual scheduled entry point (`daily-runner` console script).
  Loads and schema-validates `config.yaml`, loops over every portfolio defined under
  `portfolios:`, idempotent per day, refuses `--live` unless `config.yaml`'s
  `metadata.approved_by`/`approved_date` are set.

**Config flow**: `config.yaml` (gitignored; copy from `config.example.yaml`) →
`daily_runner.load_config()` builds one `BacktestConfig` per portfolio from
`default_risk` + that portfolio's `risk_overrides`. `total_value: null` means pull real
`NetLiquidation` from IBKR (`--live` only); a number means a fixed capital baseline.

**Safety defaults that are load-bearing, not incidental** — never change these without an
explicit user ask: dry-run is the *unflagged default* (`--live` is opt-in, and there is no
`--dry-run` flag — passing one is an argparse error, since `parse_args()` is strict); real-money
trading requires `--port 7496` **and** `--confirm-live-trading` together; circuit-breaker halts
require explicit `--resume-trading`, never auto-clear.

**Epic/Story references in comments** (e.g. `Epic 17, Story 17.3`) trace design decisions back
to their origin and rationale — read the referenced module's docstring when a comment cites one,
it usually explains a non-obvious constraint (e.g. why `risk_monitor.py` avoids shared code).

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
