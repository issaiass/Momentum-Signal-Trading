# Email Notifications & Reporting (Epic 12)

## Four categories

| Category | Color | Filterable? | Examples |
|---|---|---|---|
| **CRITICAL** | Red | **No — always sent** | Stop-loss executions, circuit-breaker trips, config-load failures, connection failures, the Epic 26 capital-allocation error that aborts a run |
| **WARNING** | Amber | Yes (`notifications.send_warning`) | Epic 26/27: fixed portfolio allocations exceeding the real account, ticker overlap across portfolios |
| **STANDARD** | Green | Yes (`notifications.send_standard`) | Routine rebalance BUY/SELL/HOLD summaries |
| **PERIODIC** | Blue | Yes (`notifications.send_periodic`) | Monthly performance report |

CRITICAL cannot be disabled via config — this is deliberate. Suppressing a stop-loss or
circuit-breaker alert is exactly the failure mode this system exists to prevent. See
`tests/test_notifications.py::TestCategoryFiltering::test_critical_ignores_config_attempting_to_disable_it`
for the test that guards this.

WARNING *can* be disabled (unlike CRITICAL) — Epic 27's deliberate distinction is that these
are review-when-convenient risk signals, not run-blocking failures, so a user who's confirmed
their overlapping-ticker setup is intentional can quiet the recurring email. Disabling the
email never disables the underlying log line, though (see below) -- the risk is never fully
invisible even with `send_warning: false`.

## Configuration

In `config.yaml`:

```yaml
notifications:
  send_standard: true             # routine rebalance summaries
  send_periodic: true             # monthly report
  monthly_report_day_of_month: 1  # day of month the report fires; omit/null to disable
  send_warning: true              # Epic 27: multi-portfolio capital-safety warnings.
                                   # Must be a real bool -- a value like "false" (a truthy
                                   # non-empty string) is rejected at config-load time
                                   # rather than silently doing the opposite of what you meant.
```

Plus the same SMTP environment variables already documented in `DEPLOYMENT.md`
(`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_TO_EMAIL`).

## What each category actually contains

**CRITICAL (capital allocation error, Epic 26)** — **"daily_runner: capital allocation
error"**, sent via `daily_runner.py`'s `send_alert_email()` directly (same unconditional path
as stop-loss/circuit-breaker, no config gate at all) and always also logged
(`logger.error`), with the same SMTP-missing ERROR-log fallback every other CRITICAL alert
relies on. Fatal -- aborts the run (`sys.exit(1)`) before any portfolio is touched. Fires when
a `total_value: null` portfolio's computed remainder (real account value minus every other
portfolio's fixed `total_value`) would be zero or negative -- the other portfolios already
consume the whole account. Deliberately NOT made filterable (unlike the two WARNING alerts
below): a run that just silently aborted with no explanation email would be worse than one
that emails about it, so this stays CRITICAL rather than becoming configurable.

**WARNING (multi-portfolio capital safety, Epic 27)** — two related, non-fatal alerts, sent via
`send_action_email(NotificationCategory.WARNING, ...)` (filterable via
`notifications.send_warning`, defaults to sending if unconfigured — same "unconfigured defaults
to on" convention as STANDARD/PERIODIC). **The detailed diagnostic log line for each is written
separately and unconditionally**, before and independent of the email attempt -- so even with
`send_warning: false`, the exact risk detail (which tickers, which portfolios, which dollar
amounts) always reaches the logs; the config toggle only ever controls whether it *also*
reaches your inbox:
- **"Portfolio allocations exceed account value"** — fires in `--live` mode when every
  portfolio has an explicit `total_value` (no `null`) and their sum still exceeds the real
  account's NetLiquidation. Names the shortfall; the run continues (the broker will
  reject/reduce individual orders rather than overdraw), but this should not go unreviewed.
- **"Ticker overlap across portfolios"** — checked once per run (dry-run or `--live`) before
  the per-portfolio loop starts. Fires when the same ticker appears in more than one
  portfolio's `tickers` list -- each portfolio computes and submits orders independently, so a
  shared ticker on a shared IBKR account risks uncoordinated, conflicting orders against the
  same real position. Deliberately a warning, not a blocking error, since some setups
  intentionally run different weightings on overlapping tickers across portfolios.

**STANDARD (rebalance summary)** — an HTML table per portfolio, sent after each rebalance,
showing ticker / action / shares / reason for every position considered that cycle (including
HOLDs, so you can see what *wasn't* traded and why), plus a **"What Actually Happened"** column
showing the REAL execution outcome per ticker — distinct from the signal's intended action,
since an intended BUY/SELL doesn't always actually fill. Built by
`build_rebalance_summary_html()` from `fill_status`/`fill_price`/`fill_shares`, which
`execution/live_signal.py`'s `run()` merges onto each order after a `--live` call to
`place_orders_ibkr()`:
- **Filled** — `"Filled N @ $price"` (green)
- **Dropped, fractional** — `"Dropped — rounds to 0 whole shares"` (amber) — the order's share
  count floored to 0 whole shares before ever reaching IBKR (IBKR has no fractional equity API
  support; see `DEPLOYMENT.md`'s "Troubleshooting: IBKR order placement")
- **Dropped, insufficient cash** — `"Dropped — insufficient cash"` (amber) — scaled to 0 shares
  by `auto_reduce_buys_on_insufficient_cash`
- **Rejected** — `"Rejected — <reason>"` (red) — a genuine IBKR error (excludes
  `IBKR_INFORMATIONAL_CODES`, which never reach this state)
- **Cancelled/Inactive** — `"<status> — not filled"` (red)
- **Still open** — `"Still open — status <status>"` (amber) — `fill_poll_timeout` elapsed before
  a terminal status arrived (e.g. a limit order still working outside RTH)
- **HOLD** — `"—"` — no order was ever attempted for this ticker this cycle
- **Dry-run** — `"Dry-run — no order sent"` — the rebalance ran without `--live`, so nothing was
  ever sent to a broker; passed through as `build_rebalance_summary_html(..., dry_run=True)`

**PERIODIC (monthly report)** — HTML email with:
- An embedded portfolio-value-over-time chart (PNG, generated via matplotlib)
- Current position summary (total value, cash, unrealized P&L) — from the latest snapshot row
- Actual P&L (realized, unrealized, total, trade count, total return) — from
  `execution/live_signal.py`'s `measure_live_performance()`, which replays the real trade log
  with FIFO lot matching. This is distinct from "Current position"'s unrealized P&L above: that
  one only marks currently-*open* positions from the latest snapshot, while this section also
  includes realized gains from trades that have since closed, and is filtered to rows matching
  the run's actual `dry_run`/`--live` mode (the two modes share one log file).
- Cumulative return vs. benchmark (via `compare_to_benchmark()`)

Degrades gracefully: if there isn't enough snapshot history yet for a chart, benchmark
comparison data isn't available, or no trade log exists yet (`FileNotFoundError`, e.g. no
rebalance has ever fired for this portfolio), those sections are simply omitted rather than
causing the whole report to fail.

## What's implemented vs. deferred

**Implemented and tested** (`tests/interfaces/test_notifications.py`):
- Category filtering logic (CRITICAL unsuppressable, STANDARD/PERIODIC/WARNING configurable)
- HTML generation for rebalance summaries and monthly reports
- Chart embedding via matplotlib
- Graceful degradation on missing/insufficient data

**Deferred, not built in this pass** (flagged explicitly rather than delivered shallow):
- **PDF attachment** for the monthly report — the HTML report covers the same content; a PDF
  version would need a rendering library (e.g. `weasyprint` or `reportlab`) not currently a
  dependency. Straightforward to add later using `build_monthly_report_html()`'s existing
  output as the source content.
- **Pre-trade preview email** (1hr before rebalance) — would need either a second scheduled
  cron entry or internal time-of-day logic inside `daily_runner.py` to compute intended
  picks/weights without executing them. Architecturally compatible with the existing
  `compute_target_weights()` pipeline, just not wired up yet.

## Testing

`tests/test_notifications.py` covers filtering logic and content generation — no actual SMTP
send is tested (would need a real or mocked mail server). Before relying on this in production,
manually verify actual delivery once with real SMTP credentials configured, the same way you'd
paper-trade before going live.
