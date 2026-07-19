# Running the Momentum Strategy

> **New to this project?** Start with `../README.md` for a file overview and folder structure.

This covers day-to-day *operating* commands once the system is installed (see `DEPLOYMENT.md`
for one-time setup on a new machine).

All commands below assume you're in the project folder with `config.yaml` present
(`cp config.example.yaml config.yaml` if you haven't already, then edit it).

---

## 1. Single portfolio

A single portfolio is just `config.yaml` with one entry under `portfolios:`, no different
code path, `daily_runner.py` loops through whatever's defined there, once per portfolio.

```yaml
# config.yaml
default_risk:
  holding_period: 1
  stop_loss_pct: 0.12
  use_regime_filter: true
  regime_benchmark: SPY

portfolios:
  main:
    tickers: [SPY, QQQ, XLK, XLF, XLE, XLY, XLP, XLU, GLD, TLT, BIL]
    custom_weights: null      # null = algorithmic inverse-vol sizing
    total_value: null         # null = pull real account value from IBKR (--live only)
```

Test it (safe, dry-run, no broker connection needed):
```bash
daily-runner --force-rebalance
```

`--force-rebalance` runs the rebalance logic immediately regardless of today's date, so you
can verify the signal/sizing/order output without waiting for a real rebalance day.

---

## 2. Multiple portfolios with different capital

Add more entries under `portfolios:`. Each portfolio has its own signal, sizing, orders, and
trade log (`logs/live_trades_log_<name>.csv`), but if two or more portfolios trade through
the **same real IBKR account** (the normal case: one `--port`, one TWS/Gateway login), their
capital and positions are NOT automatically kept independent. The system makes this safe rather
than silently wrong; see the capital and ticker-overlap notes below before running more than
one portfolio `--live`.

```yaml
portfolios:
  portfolio1:
    tickers: [SPY, QQQ, XLK, XLF, XLE, XLY, XLP, XLU, GLD, TLT, BIL]
    custom_weights: null
    total_value: null        # gets the REMAINDER of the account after portfolio2's $2,500

  portfolio2:
    tickers: [XLF, XLE, GLD, TLT]
    custom_weights: {XLF: 0.4, XLE: 0.3, GLD: 0.2, TLT: 0.1}
    total_value: 2500.0      # this portfolio uses a fixed $2,500 regardless of account value
    risk_overrides:
      top_n: 3                # holds only its top 3 of 4 tickers each rebalance,
                               # independent of default_risk.top_n or any other portfolio
```

- `total_value: null` → **not** "pull the full account value", it's the real account's
  NetLiquidation *minus every other portfolio's fixed `total_value`* (only meaningful with
  `--live`; dry-run uses a flat $1000 placeholder instead, unaffected by other portfolios,
  since dry-run tests signal/order logic, not real capital math). At most **one** portfolio in
  the whole file may be `null`, `daily-runner` refuses to start otherwise (the "remainder" is
  ambiguous with more than one candidate). If the other portfolios' fixed allocations already
  consume the whole account, the run aborts with an alert email rather than proceeding with
  zero/negative capital.
- `total_value: <number>` → uses that fixed dollar amount every run, useful for allocating a
  specific slice of a larger account to one strategy variant, or for sub-account-style testing.
  If every portfolio uses a fixed number and they sum to more than the real account value,
  `daily-runner` sends a warning alert (non-fatal, the broker will reject/reduce individual
  orders rather than overdraw, but review it).
- Different `custom_weights` per portfolio let you compare algorithmic sizing against a
  hand-specified allocation side by side, on the same schedule, same run.
- **Ticker overlap across portfolios** (e.g. both portfolios above hold XLF/XLE/GLD/TLT) is
  checked once at the start of every run and triggers a warning email + log line if found,
  each portfolio computes and submits its own orders independently, so a shared ticker on a
  shared account risks uncoordinated, conflicting orders against the same real position. This
  is a warning, not a blocking error (some setups intentionally run different weightings on the
  same tickers across portfolios, like the example above), review the warning, don't ignore it.
- `top_n` (how many top-momentum-ranked tickers to actually hold) is a normal
  `default_risk`/`risk_overrides` field like any other, give each portfolio its own value
  the same way you'd override `stop_loss_pct`. There is no limit on the number of portfolios,
  and each one's `top_n` (and every other risk field) resolves completely independently of
  the others, see `config.example.yaml` for a second worked example.
- Automated `risk_monitor.py` coverage in Docker is a **separate** setting
  (`RISK_MONITOR_PORTFOLIOS` in `.env`) from the `portfolios:` list here, adding a portfolio
  to `config.yaml` alone does not automatically monitor it. See `docs/DEPLOYMENT.md`.

Same test command works for all portfolios in the file at once:
```bash
daily-runner --force-rebalance
```

---

## 3. Paper trading account

**Prerequisites:**
- TWS or IB Gateway running and logged into your **paper** account (not live).
- API access enabled: Configure → API → Settings → Enable ActiveX and Socket Clients.
- Confirm you're actually on paper: TWS shows this in the window title/border color, and the
  account ID typically starts with a different prefix than your live account. Double-check
  before proceeding, port number alone is a convention, not a guarantee.
- Default paper port is **7497** (verify against your own TWS/Gateway config).

**Run it:**
```bash
daily-runner --live --port 7497
```

This places real (paper) orders through IBKR, using whatever's scheduled for today,
add `--force-rebalance` to test immediately instead of waiting for a real rebalance date.

**After running, check:**
- Console/log output for each portfolio's picks, weights, and order actions.
- `logs/live_trades_log_<portfolio>.csv`, the audit trail, written before any broker call.
- TWS's own order/execution log, confirms fills actually happened as intended.
- Email inbox (if SMTP configured), should be silent unless something failed.

**Recommended:** run for at least 2-3 real rebalance cycles on paper, verifying every fill
matches the intended order, before moving to Section 4.

**Connection errors, unfamiliar `IBKR error` log lines, or orders that don't fill?** See
`DEPLOYMENT.md`'s "Troubleshooting: IBKR connection" and "Troubleshooting: IBKR order placement"
sections, a `502 Couldn't connect` error that persists across every port you try (Docker only),
the `2104`/`2106`/`2158`-style "data farm connection is OK" lines that show up on every
successful connect, and `IBKR error 10268: The 'EtradeOnly' order attribute is not supported`
firing on every order (a real, now-fixed bug, every affected order was silently rejected
despite the run logging success) are all covered there. `IBKR error 10243: Fractional-sized
order cannot be placed via API` is a separate, **unfixable-in-code IBKR platform limitation**
(fractional equity/ETF orders can never be placed via the API, full stop), `place_orders_ibkr()`
now floors fractional share counts to whole shares before submission so a rebalance can still
execute; see DEPLOYMENT.md for details. Separately, `Order for TICKER did not confirm as Filled
(status=SUBMITTED)` does **not** necessarily mean the order failed, real paper-account fills
have been observed taking longer than the (now-fixed, previously 15s, now 60s default)
`fill_poll_timeout` window. Same goes for `(status=ERROR: Order TIF was set to DAY based on
order preset.)`, `error 10349` is informational (IBKR auto-filled a TIF we never set), not a
rejection; this was a real, now-fixed bug where a genuinely-filled order got permanently marked
failed because of it. **Always check TWS's own execution log before assuming a "did not
confirm" warning means nothing traded.**

**If you ran `--live` before the `10268` fix landed, verify in TWS's own execution log whether
any of those orders actually filled**, don't assume the trade log's "logged" row means the
order went through.

---

## 4. Live / real trading account

**This moves real money. Read this section fully before running the command.**

**Prerequisites:**
- TWS/IB Gateway logged into your **live** account. Verify this explicitly, don't assume;
  check the account ID in TWS matches your real account, not paper.
- Everything from Section 3 already done successfully (paper-traded, verified fills, no
  surprises).
- SMTP alerting configured and tested (see `DEPLOYMENT.md`), you want to know immediately if
  something goes wrong when real capital is involved.
- `config.yaml` reviewed one more time, tickers, custom weights, position caps, stop-loss
  percentages all reflect what you actually intend.

**Run it:**
```bash
daily-runner --live --port 7496 --confirm-live-trading
```

Both `--port 7496` and `--confirm-live-trading` are required together by design, this is a
deliberate double-confirmation. Omitting `--confirm-live-trading` causes the script to refuse
and exit before connecting to anything.

**Pre-flight checklist before the first live run:**
- [ ] Paper-traded successfully for multiple cycles (Section 3)
- [ ] `config.yaml` reviewed line by line
- [ ] SMTP alert test sent and received (see `DEPLOYMENT.md`)
- [ ] TWS confirmed on the live account, not paper
- [ ] You understand `auto_execute_stop_loss` in `config.yaml`'s `default_risk`, `false`
      (default) means stop-losses are flagged + emailed but require manual action; `true`
      means the script sells automatically without asking

---

## 4.5. Crash protection: circuit breaker and config approval

Two things worth understanding before your first live run:

**Config approval gate.** `--live` refuses to run unless `config.yaml`'s `metadata.approved_by`
and `metadata.approved_date` are filled in (not `null`). This is a lightweight paper trail
confirming a human actually reviewed this exact config before it traded real money:

```yaml
metadata:
  version: "1.0.0"
  approved_by: "your name"
  approved_date: "2026-07-11"
```

**Circuit breaker.** If `max_portfolio_drawdown_pct` is set (e.g. `0.20` = 20%) in a
portfolio's risk config, the system tracks peak equity and halts new rebalancing once
drawdown from that peak is breached. Important: **it does not force-liquidate existing
positions** (that's still only the per-ticker `stop_loss_pct`'s job), it only stops
rotating into *new* risk. It also does **not auto-resume** even if equity recovers; a human
must explicitly clear it:

```bash
daily-runner --resume-trading portfolio1
```

An independent `risk_monitor.py` process can also trip this same halt (see `DEPLOYMENT.md`),
based on realized losses in the trade log, separate from the trading script's own logic.

## 4.6. Checking on your portfolio (reporting)

Every run of `daily_runner.py` (rebalance day or not) writes a snapshot row to
`data/portfolio_snapshot_<name>.csv`: total value, cash, positions, unrealized P&L, and
(from the second run onward) the period return for both your portfolio and the benchmark.

Fastest way to check "where do things stand":
```python
from live_signal import get_latest_snapshot
get_latest_snapshot("portfolio1")
```

For a chart and cumulative return vs. benchmark, open `portfolio_snapshot_report.ipynb`,
an investor-facing check, not a replacement for the full trade log or
`measure_live_performance()`. Beyond the original chart/table, it also demonstrates every
measure the email reports show (position performance since entry, technical/fundamental
indicators, macro context, since-inception stats, trailing-window comparison chart) using the
same underlying functions, so you can see exactly how each report section is computed.

## 4.7. Staged Operational Rollout

Don't jump straight to Section 4 (live trading). Four stages, in order, but first, a basic
sanity gate that applies before Stage 1:

**Before Stage 1: `pytest tests/ -v` should pass cleanly.** This only confirms the code
mechanics work (order math, config validation, audit logging), it says nothing about whether
the strategy itself is any good, which is exactly what Stage 1 exists to check. But if the
test suite doesn't pass on your machine, nothing downstream should be trusted either. See
`TESTING.md` if anything fails.

### Stage 1, Historical validation (do this first, costs nothing but time)
Run the walk-forward/holdout validation (Notebook 1) and factor decomposition (Notebook 3)
against your **real** price data for the first time. Document the actual numbers, CAGR,
Sharpe, holdout performance, alpha t-stat. If you skip this and go straight to paper trading,
you're testing execution mechanics, not whether the strategy has any edge at all.

**Example scenario:** you run Notebook 1's pre-registered split with `split_date='2015-01-01'`.
If the holdout (2015+) Sharpe is meaningfully worse than the full-sample heatmap's best
lookback/holding combo, that gap is your real overfitting estimate, don't proceed to Stage 2
until you've looked at this number and decided it's acceptable.

### Stage 2, Paper trading, minimum duration
Minimum **3 full rebalance cycles** (3 months at `holding_period=1`), and this window must
include at least one materially volatile week, if your 3 months happen to be unusually calm,
extend until you've seen the circuit breaker/stop-loss logic actually get tested by real
market movement, not just quiet drift.

```yaml
# config.yaml, Stage 2 example: small paper universe, conservative risk settings
default_risk:
  holding_period: 1
  stop_loss_pct: 0.10
  use_regime_filter: true
  max_portfolio_drawdown_pct: 0.15   # tighter than you'd eventually run live, to see it trigger
portfolios:
  paper_test:
    tickers: [SPY, QQQ, XLK, XLF, XLE]
    total_value: 10000.0
```
```bash
daily-runner --live --port 7497 --force-rebalance   # first run, verify manually
daily-runner --live --port 7497                     # then let it run on schedule
```

### Stage 3, Small live capital
Start at a **small fraction** of intended capital (e.g. 10%). Explicit criteria before scaling
up to Stage 4, all of these, not just one:
- [ ] Paper results (Stage 2) and live results (Stage 3) match within a reasonable tolerance
      (e.g. realized slippage within 2x of the backtest's assumed `base_slippage_bps`)
- [ ] No unexplained circuit-breaker trips (any trip should be traceable to a real, understood
      market move, not a bug)
- [ ] `risk_monitor.py` has run independently for the full period with no missed cycles
- [ ] You've personally reviewed every `logs/live_trades_log_*.csv` row at least once

```yaml
# config.yaml, Stage 3 example: same strategy, 10% of eventual target capital
portfolios:
  live_small:
    tickers: [SPY, QQQ, XLK, XLF, XLE, XLY, XLP, XLU, GLD, TLT, BIL]
    total_value: 1000.0   # e.g. 10% of an eventual $10,000 target
metadata:
  approved_by: "your name"
  approved_date: "2026-08-01"
```
```bash
daily-runner --live --port 7496 --confirm-live-trading
```

### Stage 4, Full capital
Only after Stage 3's checklist is complete and reviewed. Scale `total_value` up, re-approve
the config (new `approved_date`), and continue the ongoing discipline in 4.8 below.

## 4.8. Ongoing Operating Discipline

**Monthly review checklist** (or more often if volatility is elevated):
- [ ] Any circuit-breaker trips since last review? Understood and resolved, not just cleared?
- [ ] Any `risk_monitor.py` alerts? Reviewed the underlying cause?
- [ ] Run `live_vs_backtest_reconciliation.ipynb`, how much has live diverged from backtest
      expectations this period?
- [ ] Check `scheduled_revalidation_check()` (Notebook 1), is a fresh walk-forward due?

**Kill criteria, decide these in advance, in writing, before you need them:**
Write your own numbers here; examples only:
```yaml
# NOT a config.yaml field, a personal policy document, e.g. kill_criteria.md
kill_criteria:
  max_acceptable_drawdown: 0.25       # halt entirely, don't just circuit-break, above this
  max_live_vs_backtest_sharpe_gap: 0.5  # if live Sharpe is this much worse than backtest, stop and re-diagnose
  max_months_underperforming_benchmark: 12  # review strategy validity, not just tolerance
```
The point of writing these down now is that the decision to stop isn't made emotionally
during a drawdown, it's a pre-committed rule you follow.

## 4.9. Manual Override Policy

Decide, in advance, whether you will ever manually skip or alter a scheduled rebalance (e.g.
"I'll skip this month, the market feels bad"). The honest default recommendation: **don't**,
momentum's edge, such as it is, depends partly on not second-guessing the signal during
exactly the moments it feels most uncomfortable to follow it.

If you do permit overrides, define the conditions in advance (not in the moment) and log every
override decision with reasoning, extend `logs/live_trades_log_<name>.csv`'s convention
manually if needed, e.g. a `manual_override_log.csv` with the same date/reason/decision-maker
fields, so the decision is auditable later.

## 4.10. Tax Awareness

Monthly rotation across a changing ETF universe generates frequent **short-term capital
gains** in a taxable account. Nothing in this codebase models tax drag, tax-loss harvesting,
or wash-sale rules. Backtest and paper returns are pre-tax.

**Recommendation:** run this strategy inside a tax-advantaged account (IRA, etc.) where
feasible. If running in a taxable account, estimate tax drag manually before trusting any
after-tax return expectation, e.g. at a combined ~30-40% short-term rate, a strategy showing
10% pre-tax CAGR could realistically net 6-7% after tax, before even accounting for state
taxes. This is a rough illustration, not a substitute for real tax advice from a professional
who knows your situation.

## 4.11. Event-Calendar Awareness

Fixed monthly rebalance dates (`holding_period=1`) can land near FOMC meetings, CPI releases,
or earnings clusters for sector ETFs, no automated logic here checks for this. At minimum,
glance at what's scheduled around each rebalance date before `--live` runs, especially during
Stage 2/3 of the rollout above. This is a manual awareness recommendation, not an automated
feature in this codebase.

`holding_period` also accepts fractional values mapping onto weeks (`0.25` = every week, `0.5` =
every 2 weeks, `0.75` = every 3 weeks), see `STRATEGY_THEORY.md` for the theory, or
`DEPLOYMENT.md`'s "Choosing a rebalance cadence" section for worked daily/weekly/monthly examples
(including exactly which env vars change and which don't, for Docker deployments).

## 4.11a. Trading-Day Scheduling (Monthly/Weekly Roll-Forward)

`is_rebalance_day()` (`execution/live_signal.py`) targets the **first real NYSE trading day**
of the period, monthly or weekly, not a fixed calendar date. It's not "check if today is a
trading day AND today is near the 1st", it fetches the exchange's actual trading-session
schedule for the whole month (or week, for fractional `holding_period`) via
`pandas_market_calendars`' `NYSE` calendar (`mcal.get_calendar("NYSE")`), then targets whichever
date is that schedule's first entry. A weekend or market holiday is never IN that schedule, so
if the 1st of the month falls on one, the target is automatically whichever day the market
actually opens next, no explicit `if holiday: shift by one day` branch needed, the roll-forward
happens by construction.

Worked example, test-proven (`tests/execution/test_live_signal.py::TestIsRebalanceDay`): January
1, 2026 is New Year's Day, a market holiday. `is_rebalance_day(1, today="2026-01-01")` is
`False`; `is_rebalance_day(1, today="2026-01-02")` is `True`, the actual first trading day of
that month. The same mechanism applies to the weekly branch:
`test_holiday_shifts_the_weekly_target_day` confirms a week starting on Presidents' Day (a
Monday holiday) targets the following Tuesday instead.

This confirms monthly is both the default configuration (`holding_period: 1` in
`config.example.yaml`) and holiday/weekend-aware, and that weekly (and every 2/3-week) cadences
get the identical trading-calendar treatment, not a separate or weaker mechanism.

## 4.11b. Long-Term vs. Short-Term Momentum

`lookback_period` (the trailing-return window used to RANK tickers, separate from
`holding_period`'s rebalance cadence) accepts fractional values too, but its granularity is
tied to `holding_period`'s regime, not its own value. Under the monthly default
(`holding_period: 1`), `lookback_period` stays in whole months, `12` = the classic
long-term-momentum default. Under a weekly `holding_period` (`< 1`), `lookback_period` switches
to week-scale via the same formula `holding_period` itself uses, `0.5` = 2 weeks, `0.75` = 3
weeks, `1.0` = 4 weeks, `1.5` = 6 weeks, a short-term-momentum configuration:
```yaml
default_risk:
  holding_period: 0.25    # weekly rebalance
  lookback_period: 0.5    # 2-week momentum window
```
See `STRATEGY_THEORY.md`'s "Lookback" item for the theory and the honest caveat that week-scale
lookbacks depart from the academic 3-12 month range the strategy's underlying research actually
validated. A lookback shorter than 2 weeks under a weekly `holding_period` triggers a
non-blocking WARNING (mirrors the `holding_period`-too-frequent warning above), a momentum
signal that short is dominated by noise.

## 4.11c. Restart and Resume Behavior

If you close the app (native Python, `Ctrl+C`) or stop the container and start it again later,
does it resume from where it left off, or start over? Short answer, confirmed by reading the
actual scheduling/state code, not assumed:

**`--live` mode already resumes correctly, by construction, no action needed from you**:
- Scheduling is never a counter. `is_rebalance_day()` (see 4.11a above) recomputes purely from
  TODAY's real date against the NYSE calendar every single run, there is no "days since last
  rebalance" value to get stale or desynced by an outage.
- Current holdings in `--live` mode are a REAL broker query every run
  (`get_ibkr_positions()`), never local memory, so the broker itself, not this app, is the
  source of truth for "what do I currently hold." A restart changes nothing about what's
  actually held.
- Everything scheduling/risk state depends on (idempotency lock files, circuit-breaker
  halt/peak-equity state, trade/alert/email-command logs, portfolio snapshots) lives under
  `data/`/`logs/`. For native Python, that's just a local directory on disk, unaffected by
  process restarts. For Docker, `docker-compose.yml` bind-mounts `./data:/app/data` and
  `./logs:/app/logs` to the host, this persists across both `docker stop`/`start` AND
  `docker compose down`/`up` (the `Dockerfile`'s own `VOLUME` declaration is superseded by
  these compose bind mounts; only deleting the host `./data`/`./logs` folders directly would
  lose this state).

**Dry-run mode (no `--live`) does NOT persist a simulated portfolio across restarts.**
`current_positions` is always `{}` at the start of every dry-run invocation, by design, dry-run
previews signal/sizing/order-generation logic, it was never meant to be a stateful paper-trading
engine, see `README.md`'s "Project Maturity & Safety" section for the broader `--force-rebalance`
scope caveat. Each dry-run run always starts from a hypothetical flat/no-holdings state. If you
want an actual persistent paper portfolio without real money at risk, use `--live --port 7497`
against a real IBKR paper account, that path already resumes correctly for the reasons above,
since the broker (paper or real) is genuinely the source of truth either way. The trade log's
`dry_run=True` rows remain the durable record of "what a dry-run would have done," across every
run, they're just not read back to reconstruct a simulated position.

**One real gap, closed by this project**: if the process/container was off through an ENTIRE
scheduled rebalance day, there is no automatic catch-up, that day's rebalance never happens.
This is a deliberate, alert-only design choice (see "Your confirmed choices" reasoning in this
project's own change history), not an oversight left unaddressed: a non-blocking
`MISSED_REBALANCE_DAY` WARNING (logged and emailed, same pattern as every other advisory check
in this project) fires on the next run when a scheduled rebalance date has no recorded run since
it passed. It stays silent once ANY run happens on or after that missed date, including a manual
catch-up via `daily-runner --force-rebalance --live`, so following the warning's own suggested
remedy correctly clears it on the next run rather than nagging indefinitely. It also stays
silent for a portfolio's very first run ever (nothing to have missed yet). It does NOT
automatically re-run the missed rebalance for you, that decision (and its price, necessarily
today's, not the missed day's) is left to you.

## 4.12. Additional capabilities, quick pointers

- **Alternative position sizing**: set `sizing_method: score_proportional` in `config.yaml`'s
  risk config to weight by momentum strength instead of inverse volatility. See
  `STRATEGY_THEORY.md` for the theory and a worked comparison example.
- **Multi-lookback signals**: `functions_quant_extensions.blend_momentum_scores()` combines
  multiple lookback windows (e.g. 3/6/12-month) into one signal, see Notebook 2 for a demo.
- **Additional safety checks**: `max_dollar_drawdown`, `max_slippage_tolerance_pct`,
  `max_price_staleness_minutes`, `max_holding_days`, all in `config.yaml`'s risk config,
  all disabled by default (`null`/`0`).
- **Email notifications & monthly reports**: see `EMAIL_REPORTING.md`.
- **Email-commanded remote actions** (PAUSE/RESUME/LIQUIDATE/SKIP_NEXT_REBALANCE/
  TRIGGER_REPORT/ADJUST_PARAM/STATUS/SET_MAX_DRAWDOWN): see `EMAIL_COMMANDS.md`, read the
  security model before enabling, and try `email_commands_walkthrough.ipynb` first.
- **Autostart on reboot**: see `DEPLOYMENT.md`'s new "Autostart on Reboot" section for
  Docker/native-Windows/native-Linux specifics.

## 4.13. Order execution: cash vs. margin IBKR accounts

`place_orders_ibkr()` always submits SELL orders first and waits for them to reach a terminal
status (filled, cancelled, or errored) before submitting any BUY, not configurable, since
there's no valid reason to interleave them. This matters because of how the two common IBKR
account types actually handle a BUY that depends on proceeds from a same-cycle SELL:

- **Cash (non-margin) account**: a BUY submitted before its funding SELL has cleared can be
  rejected outright, or trigger a cash-account ("good faith") violation if IBKR allows it to
  go through using not-yet-settled funds. Sells-first sequencing avoids this by construction.
- **Margin account**: the timing gap is usually absorbed by margin buying power without
  incident, but nothing checks that there's actually enough of it.

Independent of account type, after sells clear `default_risk.auto_reduce_buys_on_insufficient_cash`
(default `false`) controls what happens if BUYs still exceed real available cash (queried
fresh from IBKR after the sells settle):
- `false` (default): log + always-visible warning naming the shortfall; BUYs submit at their
  originally computed size regardless. IBKR's own fill/partial-fill/reject behavior is the
  actual backstop, already surfaced via the existing "did not confirm as Filled" log line.
- `true`: proportionally scale down every BUY's share count (floored to whole shares) so the
  total fits within real available cash. An order that floors to 0 shares is dropped rather
  than submitted as a no-op.

This is LIVE-only, dry-run never calls `place_orders_ibkr()` at all, so there's nothing to
configure or observe here until you actually run `--live`.

## 4.14. Extended-hours (pre-market/after-hours) trading

A rebalance running at or right after market close submits plain MKT orders, which IBKR/
exchanges reject outright: `IBKR error 201: Order rejected - reason:Exchange is closed`. This is
**normal, expected behavior, not a bug**, MKT orders only work during regular trading hours
(9:30am-4:00pm ET), full stop, confirmed against IBKR's own TWS API docs.

To actually place orders in NASDAQ's standard extended sessions (pre-market 4:00-9:30am ET,
after-hours 4:00-8:00pm ET), set `default_risk.allow_extended_hours: true`. This is a **real
order-type change**, not just a flag: IBKR only accepts LMT (limit) orders outside RTH, never
MKT, so enabling this switches every live order to LMT with `outsideRth=True`, using the last
known price plus/minus a small buffer (favors getting filled over exact price). If no reference
price is available for a ticker that run, it silently falls back to a regular MKT (RTH-only)
order instead of submitting unpriced.

**This is a genuine economic trade-off, not just a technical toggle**, extended-hours
liquidity is thinner than regular hours, so expect a real chance of no fill, a partial fill, or
a materially worse price than the same order would get during RTH. Off (`false`) by default;
LIVE-only, no effect on the backtest (which is daily-close based and has no concept of session
timing).

## 5. Quick reference

If you're running natively (or inside the container's own shell), use the command as-is. If
the app is running in Docker and you want to trigger the SAME scenario manually (a one-off
check, not waiting for the cron schedule), wrap it in `docker exec -it momentum-signal ...`.
Container name assumes `docker-compose.yml`'s default (`container_name: momentum-signal`).

| Scenario | Native command | Docker equivalent |
|---|---|---|
| Single portfolio, test | `daily-runner --force-rebalance` | `docker exec -it momentum-signal daily-runner --force-rebalance` |
| Multiple portfolios, test | same command, all portfolios in `config.yaml` run together | `docker exec -it momentum-signal daily-runner --force-rebalance` |
| Paper trading | `daily-runner --live --port 7497` | `docker exec -it momentum-signal daily-runner --live --port 7497` |
| Live trading | `daily-runner --live --port 7496 --confirm-live-trading` | `docker exec -it momentum-signal daily-runner --live --port 7496 --confirm-live-trading` |
| Dry run (default, safest) | `daily-runner` | `docker exec -it momentum-signal daily-runner` |
| Resume after circuit-breaker halt | `daily-runner --resume-trading <portfolio_name>` | `docker exec -it momentum-signal daily-runner --resume-trading <portfolio_name>` |
| Run independent risk monitor | `python -m momentum_trading.risk.risk_monitor --portfolio <name> --max-loss-pct 0.25` (`--initial-capital` optional, defaults to `config.yaml`'s `total_value` for that portfolio) | `docker exec -it momentum-signal python -m momentum_trading.risk.risk_monitor --portfolio <name> --max-loss-pct 0.25` |
| Full argument reference | `daily-runner --help` | `docker exec -it momentum-signal daily-runner --help` |
| View the container's actual running cron schedule | n/a | `docker exec -it momentum-signal crontab -l` |
| Follow container logs live | n/a | `docker logs -f momentum-signal` |

**Note on `--live`/paper/live trading inside Docker:** the container's own cron schedule
already runs `daily-runner` automatically (dry-run by default, per `docker-entrypoint.sh`,
which has a commented example line for going live, see `DEPLOYMENT.md`'s "Going live for
real" section to change what the *scheduled* job does). The `docker exec ... daily-runner
--live ...` forms above are for
manually triggering a one-off run outside that schedule; they also require `IBKR_HOST`/
`IBKR_PORT` to actually reach your TWS/Gateway from inside the container (see `DEPLOYMENT.md`).
`IBKR_PORT` also sets `--port`'s default when the flag is omitted, so if it's already correct
in `.env`, an explicit `--port` isn't strictly required, it's shown above for clarity, and
still overrides `IBKR_PORT` if both are set.

**Changing the schedule itself** (not a one-off run) is a `.env` + container-recreate
operation, not a command you run inside the container, see `DEPLOYMENT.md`'s cron schedule
(`DAILY_RUNNER_CRON`/`RISK_MONITOR_CRON`) and multi-portfolio risk monitoring
(`RISK_MONITOR_PORTFOLIOS`) sections.

For one-time Docker setup/build (not day-to-day commands), see `DEPLOYMENT.md`.
