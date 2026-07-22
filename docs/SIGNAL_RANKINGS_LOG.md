# Signal Rankings Log

## What this is

A persistent, tamper-evident, queryable record of the FULL ranked momentum universe every
rebalance: every configured ticker with a valid momentum score, not just the `top_n` actually
selected/traded. Written to `logs/signal_rankings_log_<portfolio>.csv`, one file per portfolio
(same convention as the trade log).

**Why this exists:** `execution/live_signal.py`'s `run()` already computes a rank and score for
every configured ticker (generically, across all 11 `strategy_type`s), but the trade log
(`logs/live_trades_log_<portfolio>.csv`) and its email table only ever showed the tickers that
were actually selected/traded this rebalance. A ticker ranked, say, 6th out of 10 with `top_n=5`
was invisible everywhere, not because the data didn't exist, but because nothing surfaced it.
This log (plus a matching second table in the rebalance email, see `docs/EMAIL_REPORTING.md`)
closes that gap.

## How this differs from the trade log

Two separate logs, deliberately kept apart, not merged:

| Log | File | What it records |
|---|---|---|
| Trade log | `logs/live_trades_log_<portfolio>.csv` | Real BUY/SELL/HOLD order DECISIONS only, the clean "what actually happened" audit trail |
| **Signal rankings log** | `logs/signal_rankings_log_<portfolio>.csv` | The FULL ranked universe every rebalance, selected AND watchlist/reserve tickers, one row each |

Every ticker in the signal rankings log that was ALSO actually decided on (selected, whether
traded or held) has its `action`/`reason`/`shares`/`money_invested`/`pct_money_invested`/
`stop_loss_price` columns filled in from that real decision, identical to what the trade log
would show for it. A ticker that was ranked but never selected ("watchlist") gets `action =
"WATCHLIST"`, and its money/shares/stop-loss columns are all zeroed/blank, since no position was
ever opened or intended for it.

## Schema

```
timestamp, ticker, action, momentum_rank, signal_score, close_price, selection_status,
money_invested, pct_money_invested, shares, stop_loss_price, reason, dry_run, config_hash,
transaction_amount, row_hash
```

**Row order**: sorted by `momentum_rank` ascending (1 = strongest first), same order in the CSV
and the matching "Full Signal Universe" email table. A ticker with no rank (e.g. excluded by the
liquidity filter, see `docs/RISK_CONSTRAINTS.md`'s "Liquidity / Universe Filter") sorts after
every ranked ticker, ordered by `signal_score` descending among themselves.

- **timestamp**, ISO 8601, when this rebalance ran.
- **ticker**.
- **action**, `BUY`/`SELL`/`HOLD` (from the real order decision), `WATCHLIST` (ranked, not
  selected this rebalance, still-positive momentum), or `EXCLUDED` (ranked, not selected,
  either negative momentum or filtered by the liquidity filter, see `selection_status` below for
  the specific reason).
- **momentum_rank**, 1 = strongest, from the same `assign_ranks()` call every `strategy_type`
  already uses for selection.
- **signal_score**, the raw ranking value. For the 7 `_BASE_SCORE_STRATEGY_TYPES` (`momentum`,
  `relative_momentum`, `dual_momentum`, `volatility_scaled_momentum`,
  `correlation_weighted_momentum`, `absolute_momentum`, `rank_sign_momentum`,
  `core/strategy_signals.py`) this IS the raw trailing `lookback_period` return. For the other 4
  (`multi_timeframe_composite`, `residual_momentum`, `path_dependent_momentum`,
  `hybrid_multi_factor`) it's that strategy's own composite/residual/blended score, not a
  literal price return, see `docs/MOMENTUM_STRATEGIES.md`.
- **close_price**, the price this ticker was ranked/sized against this rebalance.
- **selection_status**, one of five values: `"Top N (Selected)"` (N = that portfolio's `top_n`);
  `"Selected (Absolute Momentum)"` (the `absolute_momentum` `strategy_type` has no `top_n`
  cutoff, every ticker with a positive OWN trailing score is held instead); `"Watchlist /
  Reserve"` (not selected, but `signal_score` is still positive, simply outranked);
  `"Excluded (Negative Momentum)"` (not selected, `signal_score` is negative, applies across all
  11 `strategy_type`s, a negative composite/residual/blended score is still a meaningfully
  destructive signal under that strategy's own logic, not only for the 7 base-score types where
  the score is a literal trailing return); or `"Excluded (Illiquid)"` (not selected, filtered out
  by `use_liquidity_filter`, only possible when that flag is on, see
  `docs/RISK_CONSTRAINTS.md`'s "Liquidity / Universe Filter"). The two `"Excluded"` variants both
  map to `action = "EXCLUDED"`, `"Watchlist / Reserve"` maps to `action = "WATCHLIST"`.
- **money_invested** / **pct_money_invested**, this ticker's TARGET dollar allocation this
  rebalance (`0.00`/`0.00%` for a watchlist ticker), same figures the trade log and rebalance
  email already show for selected tickers.
- **shares**, the real computed share count (`0` for watchlist).
- **transaction_amount**, the ACTUAL dollar amount bought/sold THIS transaction
  (`shares * price`), `0.00` for any `HOLD`/`WATCHLIST`/`EXCLUDED` row. A real, confirmed
  distinct concept from `money_invested` above: for a full-exit `SELL` (the ticker leaves the
  target universe entirely), `money_invested` is `0.00` (correctly reflecting the post-rebalance
  target), `transaction_amount` is what actually got sold.
- **stop_loss_price**, fixed-from-entry (NOT trailing, see `docs/RISK_CONSTRAINTS.md`'s
  "Stop-Loss Width"): an ESTIMATE (`close_price * (1 - stop_loss_pct)`) for a `BUY` this
  rebalance (the real fill price isn't known yet), the REAL value
  (`avg_entry_price * (1 - stop_loss_pct)`) for a `HOLD` on an already-open position (live mode
  only, blank in dry-run), and blank for `SELL`/`WATCHLIST` (no open or intended position).
- **reason**, the order's reason string (blank for watchlist).
- **dry_run**, whether this run had `--live` set.
- **config_hash**, same per-run `BacktestConfig` fingerprint the trade log already writes.
- **row_hash**, tamper-evident hash chain, same convention as the trade log and alert log (each
  row's hash covers the previous row's hash plus this row's other fields, seeded with
  `"GENESIS"`). Written via `core/audit_log.py`'s shared `append_hash_chained_row()` (the same
  helper the alert log uses), not a fourth bespoke hash-chain implementation. Verify with
  `verify_log_integrity()` (`execution/live_signal.py`), which works unchanged against this log.

This is a brand-new log file, not an in-place schema change to anything pre-existing, no
archival step is needed when upgrading to pick this up.

**Note on `transaction_amount`'s own schema evolution**: this column was added after this log
already existed in production (Epic 3, "Rebalance Reporting Clarity & Selection-Logic Fixes"
plan), same "grow at the end, before `row_hash`" precedent as the trade log's own additions
(`docs/RUNNING.md`'s trade-log schema-evolution note). If you have an existing
`signal_rankings_log_<portfolio>.csv` from before this change, archive/rename it before your
first run after upgrading, appending new-schema rows to an old-schema file misaligns columns.

## Reading it

- **Directly**: a plain CSV at `logs/signal_rankings_log_<portfolio>.csv`, open it,
  `pandas.read_csv()` it, or `grep` it like any other log.
- **Via email**: every rebalance email that produces at least one order also includes a second
  "Full Signal Universe" table below the existing rebalance summary table, covering the exact
  same data as this log for that run, see `docs/EMAIL_REPORTING.md`.

## Path resolution

Built the same way as the trade log's own path (`daily_runner.py`, `logs_dir() /
f"signal_rankings_log_{name}.csv"`), so it resolves correctly regardless of the process's
working directory, respecting `MOMENTUM_TRADING_ROOT` the same way every other log in this
project does.
