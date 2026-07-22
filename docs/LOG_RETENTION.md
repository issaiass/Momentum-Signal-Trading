# Log Retention (Archive & Rotate)

## What this is

An opt-in mechanism (`BacktestConfig.enable_log_retention`, default `false`, byte-identical
behavior when off) that keeps this project's time-series CSV files from growing unbounded
forever. When enabled for a portfolio, rows older than a retention window (derived from that
portfolio's own `lookback_period`/`holding_period`) are moved out of the active file into a
sibling archive file, **never deleted**.

**Why this exists:** before this existed, every one of the five files below grew forever with no
trim/cap mechanism at all (confirmed by search, not assumed). `enable_log_retention` closes that
gap, deliberately via archiving rather than deletion, since four of the five files are
tamper-evident hash chains (see below) where literal deletion would break verification for the
whole file.

## The five files this applies to

| File | Hash-chained? | Rotated by |
|---|---|---|
| `logs/live_trades_log_<portfolio>.csv` | Yes | `daily_runner.py::apply_portfolio_log_retention()`, per-portfolio |
| `logs/signal_rankings_log_<portfolio>.csv` | Yes | `daily_runner.py::apply_portfolio_log_retention()`, per-portfolio |
| `data/portfolio_snapshot_<portfolio>.csv` | No | `daily_runner.py::apply_portfolio_log_retention()`, per-portfolio |
| `logs/alerts_log.csv` | Yes | `daily_runner.py::apply_shared_log_retention()`, shared across all portfolios |
| `logs/email_commands_log.csv` | Yes | `daily_runner.py::apply_shared_log_retention()`, shared across all portfolios |

## The retention window formula

`compute_retention_window_days(lookback_period, holding_period)` (`core/audit_log.py`) computes
`3 * (lookback_period + holding_period)`, converted to calendar days using the SAME
month/week-quarter convention `execution/live_signal.py`'s `resolve_momentum_scores()`/
`compute_required_lookback_days()` already established elsewhere in this codebase, rather than
inventing a new day-per-month approximation:

- The regime (weekly vs. monthly) is decided ONCE, from `holding_period < 1`, exactly like
  `resolve_momentum_scores()` already does, **not independently per field**. A monthly-sized
  `lookback_period` under a weekly `holding_period` is still expressed on the week-scale
  (CLAUDE.md's documented "same week-scale as its rebalance cadence, not mixed months/weeks"
  rule).
- Weekly regime (`holding_period < 1`): each period converts via `round(period * 4)` weeks * 7
  days.
- Monthly regime: each period converts via `round(period) * 31` days.
- The two converted values are summed, then multiplied by 3.

Example: the shipped default (`lookback_period=12`, `holding_period=1`, monthly regime) gives
`3 * (12*31 + 1*31) = 1209` days (~3.3 years) of retained history before anything is archived.

## Per-portfolio vs. shared logs

The trade log, signal rankings log, and portfolio snapshot each belong to exactly one portfolio,
so each is rotated using THAT portfolio's own resolved window.

`alerts_log.csv`/`email_commands_log.csv` are shared across every portfolio (see
`docs/ALERT_LOG.md`), so they can't be rotated per-portfolio. `apply_shared_log_retention()`
uses the **largest** resolved window across every portfolio with `enable_log_retention=True` (the
conservative choice: guarantees at least as much history is kept as every opted-in portfolio
individually needs). Entirely skipped if zero portfolios opt in.

## How rotation actually works (`core/audit_log.py`)

`rotate_hash_chained_log(log_path, cutoff_date)` (hash-chained files) and
`rotate_plain_log(log_path, cutoff_date, timestamp_col)` (the non-hash-chained portfolio
snapshot) share the same mechanics:

1. Guarded by the same `acquire_log_lock()`/`release_log_lock()` critical section every append to
   these files already uses (the exact primitive that fixed a real concurrent-write hash-chain
   race earlier in this project), so rotation can never race a concurrent writer.
2. Rows are split at `cutoff_date` on the file's timestamp column (`"timestamp"` for the four
   hash-chained logs, `"date"` for the portfolio snapshot). A row whose timestamp can't be parsed
   is conservatively **kept**, never archived, rotation should never be the reason a
   malformed-but-real row silently disappears.
3. A no-op (`{"rotated": False}`) when nothing is old enough to move, the common case on most
   days.
4. Otherwise, the old rows are written to a new sibling `<log_path>.archive_<run_timestamp>.csv`
   file, and the active `log_path` is rewritten with just the remaining rows. Both writes are
   atomic (temp file + `os.replace()`), so a crash mid-rotation can't leave either file
   half-written.
5. For hash-chained files only: **both** resulting files get a freshly recomputed `row_hash`
   chain, independently re-seeded from `"GENESIS"`. This means `execution/live_signal.py`'s
   `verify_log_integrity()` (unchanged) keeps working correctly on both files with zero code
   changes there. The archive file's `row_hash` values will differ from what was originally
   written in the live file, this is expected and harmless: tamper-evidence going FORWARD from
   the moment of rotation is what matters, not preserving the exact original hash bytes across a
   legitimate, logged administrative operation (a `LOG_ROTATED` alert records exactly when and
   how much was moved, see `docs/ALERT_LOG.md`).

## The FIFO cost-basis safety guarantee

A real correctness risk was identified and designed around while building this: `execution/
live_signal.py`'s `measure_live_performance()` (and transitively `reconstruct_dry_run_positions()`/
`derive_own_live_positions()`) and `derive_entry_date()` all do FIFO cost-basis reconstruction
from the trade log. If a BUY row for a position that's **still open today** were archived away by
naive time-based rotation, these functions would silently lose that lot's cost basis, corrupting
P&L and stop-loss-price accuracy for that position forever, with no error.

`read_trade_log_with_archives(trade_log_path)` (`execution/live_signal.py`) fixes this: it
concatenates the active trade log with every sibling `<trade_log_path>.archive_*.csv` file
(the exact naming pattern `rotate_hash_chained_log()` writes), sorted by timestamp, and is what
all four of the functions above now read from instead of a plain `pd.read_csv(trade_log_path)`.
Zero archives found (retention disabled, or no rotation has happened yet) is byte-identical in
content to the plain read every caller had before archives existed. A still-open position's
archived entry lot is therefore **never** lost from P&L/cost-basis calculations, regardless of
how many times its trade log has been rotated.

No equivalent merge exists (or is needed) for the signal rankings log, alert log, email command
log, or portfolio snapshot, none of them feed FIFO position reconstruction; a rotated-away row
there simply isn't shown by "recent" reads, exactly the intended effect of retention.

## The portfolio_snapshot tradeoff

`data/portfolio_snapshot_<portfolio>.csv` feeds longer-horizon performance stats (e.g.
Since-Inception windows, `core/functions_quant_extensions.py`'s `since_inception_performance()`).
Rotating it under the same window as the trade/signal-rankings logs reduces the history available
to those calculations once rows fall out of the active file. This is an accepted, deliberate
tradeoff (confirmed with the project owner before implementing), not an oversight: the archived
file remains fully readable (plain CSV, no hash chain to worry about) if a longer window is ever
needed for a manual, ad-hoc analysis.

## Reading an archived file

An archive produced from a hash-chained log is itself a complete, independently verifiable
hash-chained log, `execution/live_signal.py`'s `verify_log_integrity(archive_path)` works on it
unchanged, exactly as it does on the active file. An archive produced from the portfolio snapshot
is a plain CSV with the same columns as the active file, just older rows.
