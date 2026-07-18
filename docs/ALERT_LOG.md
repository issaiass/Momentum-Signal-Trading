# Alert Log

## What this is

A persistent, tamper-evident, queryable record of every alert/warning-worthy event the bot
encounters -- stop-loss and time-stop triggers, circuit-breaker trips, ticker overlap, capital
allocation errors, insufficient cash, slippage exceeded, correlation spikes, aggregate-drift
skips, and stale price feeds. Written to `logs/alerts_log.csv`.

**Why this exists:** before this log existed, all of these events were only ever `logger.warning()`/
`logger.error()` console lines. That's fine if stdout happens to be redirected to a file (the
Docker cron entries do this), but there was no dedicated, structured, queryable history the way
trade decisions and email commands already had -- and nothing to email back on request. This
log is purely additive: it does not replace or change any existing console logging or email
alert at any of these call sites.

## How this differs from the other two logs

This project now has three separate, purpose-built logs. Do not confuse them:

| Log | File | What it records |
|---|---|---|
| Trade log | `logs/live_trades_log_<portfolio>.csv` | BUY/SELL/HOLD order decisions, one file per portfolio |
| Email command log | `logs/email_commands_log.csv` | Every parsed email command attempt (accepted or rejected) |
| **Alert log** | `logs/alerts_log.csv` | Every alert/warning-worthy event, across all portfolios in one shared file |

The alert log is a single shared file (like the email command log), not one per portfolio (like
the trade log) -- a unified timeline across portfolios is more useful for "what happened this
run" than reconstructing it from N separate files, and some alert types (`TICKER_OVERLAP`,
`CAPITAL_ALLOCATION_ERROR`, `OVER_ALLOCATION`) are inherently cross-portfolio and logged under
the pseudo-portfolio name `"ALL"` rather than any single real portfolio.

## Schema

```
timestamp, portfolio, alert_type, severity, message, row_hash
```

- **timestamp** -- ISO 8601, when the alert fired.
- **portfolio** -- the portfolio name from `config.yaml`, or `"ALL"` for cross-portfolio alerts.
- **alert_type** -- one of the fixed set below.
- **severity** -- `CRITICAL`, `WARNING`, or `INFO`, matching the existing `NotificationCategory`
  tiers where applicable (not a new taxonomy to learn).
- **message** -- human-readable detail (the same information already in the paired console log
  line).
- **row_hash** -- tamper-evident hash chain, same convention as the trade log and email command
  log (each row's hash covers the previous row's hash plus this row's other fields, seeded with
  `"GENESIS"`). A plain CSV can still be freely rewritten by a script with direct file access --
  this makes tampering *detectable* (recomputed hashes won't match), not impossible. Verify with
  `verify_log_integrity()` (`execution/live_signal.py`), which works unchanged against this log
  since it shares the exact same convention.

## Every `alert_type`

| `alert_type` | Severity | Fires from | Meaning |
|---|---|---|---|
| `STOP_LOSS_TRIGGERED` | CRITICAL | `daily_runner.py::check_and_handle_stop_losses()` | A position dropped past `stop_loss_pct` from its entry price |
| `TIME_STOP_TRIGGERED` | CRITICAL | `daily_runner.py::check_and_handle_time_stops()` | A position has been held >= `max_holding_days` |
| `AGGREGATE_DRIFT_SKIP` | INFO | `execution/live_signal.py::run()` | Whole-portfolio drift was below `aggregate_drift_threshold`; the rebalance was skipped |
| `CORRELATION_SPIKE_DETECTED` | WARNING | `execution/live_signal.py::compute_target_weights()` | Cross-asset correlation spiked; exposure was defensively scaled down to `min_gross_exposure` |
| `TICKER_OVERLAP` | WARNING | `daily_runner.py` (main loop) | The same ticker appears in more than one portfolio sharing an IBKR account -- logged under portfolio `"ALL"` |
| `CAPITAL_ALLOCATION_ERROR` | CRITICAL | `daily_runner.py` (main loop) | Ambiguous or invalid `total_value: null` configuration across portfolios -- the run refuses to proceed; logged under portfolio `"ALL"` |
| `OVER_ALLOCATION` | WARNING | `daily_runner.py` (main loop) | Fixed `total_value`s across portfolios exceed the real account value -- logged under portfolio `"ALL"` |
| `INSUFFICIENT_CASH` | WARNING | `execution/live_signal.py::place_orders_ibkr()` | Buy orders total more than available cash after sells cleared |
| `CIRCUIT_BREAKER_TRIPPED` | CRITICAL | `risk/circuit_breaker.py::check_circuit_breaker()` | Drawdown from peak equity breached the percentage and/or dollar breaker; rebalancing halted for that portfolio |
| `CIRCUIT_BREAKER_RESUMED` | INFO | `risk/circuit_breaker.py::resume_trading()` | An operator explicitly cleared a halted circuit breaker (no automatic clearing exists) |
| `STALE_PRICE_FEED` | CRITICAL | `daily_runner.py` (main loop) | Latest price data is older than `max_price_staleness_minutes` allows; that portfolio's run was skipped |
| `SLIPPAGE_TOLERANCE_EXCEEDED` | WARNING | `execution/live_signal.py::place_orders_ibkr()` | A fill's price deviated from the expected price by more than `max_slippage_tolerance_pct` -- the fill already executed, this is informational only |

**`TICKER_OVERLAP` is not just a theoretical warning** -- observed directly in a real paper run:
a portfolio inherited a stray position in a ticker *outside its own configured universe*
(`reqPositions()` returns every position on the shared IBKR account, not filtered per
portfolio), and correctly refused to trade it blind (logged as a `HOLD`, since it had no price
for a ticker it never fetches) -- but that also means it can never reconcile or exit that
position on its own. If you run multiple portfolios against one real account, expect this.

## Reading it

- **Directly**: it's a plain CSV at `logs/alerts_log.csv` -- open it, `pandas.read_csv()` it, or
  `grep` it like any other log.
- **Via email**: send `ACTION: ALERTS_REPORT` / `PORTFOLIO: <name or ALL>`
  / optional `LIMIT:` (default 10, capped at 50) to get the most recent matching rows emailed
  back, newest first. See `docs/EMAIL_COMMANDS.md`.
- **Programmatically**: `core.audit_log.read_recent_alerts(portfolio, limit, log_path)` --
  returns a list of dicts, newest first, never raises (a missing or empty log just returns `[]`).

## Path resolution

Every call site threads its log path down from the same `LOCK_DIR` (`core/paths.py`'s
`data_dir()`) the rest of `daily_runner.py`/`risk/circuit_breaker.py` already use for
`peak_equity_*.txt`, halt flags, etc. -- so it resolves correctly regardless of the process's
working directory, respecting `MOMENTUM_TRADING_ROOT` the same way everything else does. It is
**not** a bare relative string resolved against whatever the current working directory happens
to be at call time.
