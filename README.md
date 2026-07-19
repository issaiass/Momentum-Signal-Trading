# Momentum ETF Rotation, Research, Backtest, and Live Trading

<details open>
<summary> <b>Brief Review<b></summary>

A cross-sectional momentum ETF rotation strategy, built end-to-end: research and signal design,
a risk-managed event-driven backtest engine, and a live trading system that talks to Interactive
Brokers (single or multiple portfolios, paper or real accounts). The strategy logic itself is
simple, rank a universe of sector/asset-class ETFs by trailing momentum, hold the top N, rotate
monthly, the bulk of the engineering is in making that simple idea safe to run unattended:
circuit breakers, idempotent scheduling, tamper-evident audit logs, email-commanded remote
control, and a config-approval gate before any real order can be placed.

Below is a real backtest (2006-2025, real ETF price history, the project's own
`run_custom_backtest()`) of the rotation strategy against a plain buy-and-hold SPY benchmark,
across three scenarios that vary signal concentration (`top_n`) and rebalance cadence
(`holding_period`), the two structural dimensions this project's own research notebooks already
study, not a search across many configs for a flattering one, see
`notebooks/research/DHI0016_notebook3_full_backtest_IMPROVED.ipynb`'s "Multi-Scenario Equity
Curve" cell for the exact reproducible source:

<p align="center">
<img src="docs/img/equity_curve.png?raw=true" alt="Momentum ETF rotation scenarios vs buy-and-hold SPY equity curve" width="85%"/>
</p>

| Scenario | CAGR | Ann. Vol | Sharpe | Max Drawdown |
|---|---|---|---|---|
| Default (`top_n=10`, `holding_period=1`) | 6.79% | 10.17% | 0.70 | -27.48% |
| Concentrated (`top_n=5`, `holding_period=1`) | 7.07% | 12.12% | 0.63 | -32.37% |
| Concentrated + Quarterly (`top_n=5`, `holding_period=3`) | 6.64% | 12.19% | 0.59 | -22.03% |
| SPY (buy-and-hold benchmark) | 10.67% | 15.20% | 0.75 | -50.78% |

**Read this chart honestly, not optimistically:** none of these three scenarios beat SPY's raw
CAGR or Sharpe over this ~19.6-year window, that's reported plainly here, not hidden. What
concentration and cadence changes DO buy is meaningfully lower volatility and a substantially
shallower max drawdown (-22% to -32% vs. SPY's -51%), consistent with this being a
lower-volatility rotation strategy, not a return-maximizing one, at least in this backtest
window. That's not a bug being hidden, it's the whole point of the "Project Maturity & Safety"
section below. This project is a well-tested *trading system*, not a proven *edge*, and the
README says so on purpose.

**What's actually here:**
- Risk-managed backtest engine, correlation-spike detection, liquidity-stress-aware slippage,
  time-based stops, VaR/CVaR, scenario shocks, capacity checks
- Live execution against IBKR (`ibapi`), connection retry, fill confirmation, sells-before-buys
  sequencing, cash-aware buy sizing, slippage-tolerance checks, whole-share flooring at
  submission time (IBKR's API has no fractional equity/ETF order support at all, see
  `docs/DEPLOYMENT.md`), optional extended-hours (pre-market/after-hours) trading via
  `allow_extended_hours` (switches to LMT + `outsideRth`, since MKT never works outside RTH)
- Multi-portfolio orchestration on one shared IBKR account, with capital-allocation and
  ticker-overlap safety checks
- Portfolio-level circuit breaker (% and $ drawdown), idempotent daily scheduling, config-approval
  gate before `--live` will run. Monthly (default) or weekly rebalancing, both targeting the
  first real NYSE trading day of the period via `pandas_market_calendars`, automatically rolling
  forward past weekends/holidays rather than firing on a fixed calendar date. Long-term
  (monthly-scale, the academically-studied default) or short-term (weekly-scale, an unvalidated
  variant) momentum lookback windows, both configurable via `holding_period`/`lookback_period`
  in `config.example.yaml`. Six long-term/short-term risk constraints (advisory warnings for
  Momentum Persistence, Lookback-to-Hold Ratio, and Turnover Limit; opt-in config toggles for
  the Skip-Month Guardrail and per-position Volatility-Adjustment budget), see
  `docs/RISK_CONSTRAINTS.md`. Restart-safe by construction in `--live` mode (broker-sourced
  holdings, calendar-derived scheduling, persisted local/bind-mounted state, both native Python
  and Docker), plus a non-blocking `MISSED_REBALANCE_DAY` warning if a scheduled rebalance was
  missed entirely while the app was off, see `docs/RUNNING.md`'s "Restart and Resume Behavior"
- Hash-chained, tamper-evident audit logs for trades, email commands, and alerts, three
  separate logs, kept deliberately apart
- Categorized email notifications (CRITICAL/STANDARD/PERIODIC/DAILY/WARNING) and pydantic-
  validated, fail-safe email-commanded remote actions (pause/resume/liquidate/adjust risk
  params/report), the rebalance summary email includes a "What Actually Happened" column
  showing the real fill outcome per ticker (filled, dropped, rejected, still open, dry-run), not
  just the intended signal action. Every email command gets a three-way ACCEPTED/REJECTED/ERROR
  outcome, logged unconditionally and, by default, replied to by email
  (`notifications.send_email_command_feedback`, gates the email only), a command that parses
  successfully but fails while being applied is isolated to itself, not aborting the rest of
  that run's batch, see `docs/EMAIL_COMMANDS.md`
- Monthly and (opt-in, off by default) daily performance reports per portfolio, technical
  indicators (trend/momentum/volatility/volume) and fundamental indicators (P/E, PEG, ROE,
  Debt-to-Equity, Current Ratio) for currently held positions, macro context (Fed Funds Rate,
  CPI), per-ticker position performance since entry (`--live` mode only), strategy
  performance indicators since inception (Total Return, CAGR, Max Drawdown, Std Dev, Sharpe,
  Sortino), and trailing-window benchmark comparison charts (1/3/6-month/YTD/1-year for the
  monthly report, 1-day/1/2/3-week for the daily report)
- Dockerized, self-scheduling deployment (`docker compose up -d`, internal cron, no manual
  triggering needed for normal operation)
- 423-test pytest suite covering code mechanics, order sizing, config validation, audit-log
  integrity, multi-portfolio capital math, entirely on synthetic/mocked data, no live broker
  required to run it

**Recommended config presets** (LIVE-ONLY, tunes `daily_runner.py`'s signal generation, has no
effect on the backtest engine), warning-free starting points for each cadence, full field list
and rationale in `docs/RISK_CONSTRAINTS.md`'s "Recommended Config Presets" section:

| Preset | `holding_period` | `lookback_period` | `top_n` | Notes |
|---|---|---|---|---|
| Long-Term Momentum (Monthly) | `1` | `12` | `10` | Academically-studied default cadence |
| Short-Term Momentum (Weekly) | `0.25` | `1.0` | `5` | Unvalidated variant, see `docs/STRATEGY_THEORY.md` |

The project tree:

```
momentum-trading/
├── README.md                     <- you are here (only doc kept at root, as the entry point)
├── pyproject.toml                 package metadata, dependencies (uv/pip compatible)
├── requirements.txt                generated export of pyproject.toml's deps, for
│                                    pip-only/Docker environments not using uv/editable installs
├── requirements-dev.txt            adds pytest
├── Dockerfile                      containerized daily_runner.py + cron
├── docker-compose.yml              one-command container startup
├── config.example.yaml             copy to config.yaml and edit (config.yaml is gitignored)
├── .gitignore
│
├── docs/                          structural governance & operational guides
│   ├── img/
│   │   ├── equity_curve.png        multi-scenario backtest vs SPY chart, shown above
│   │   ├── momentum_winners_vs_losers.png   Notebook 1 EDA: top vs bottom decile evidence
│   │   ├── momentum_win_rate_heatmap.png    Notebook 1 EDA: win rate by lookback/holding
│   │   └── momentum_decile_returns.png      Notebook 1 EDA: avg forward return by decile
│   ├── DEPLOYMENT.md               one-time setup on a new machine
│   ├── RUNNING.md                  day-to-day run commands
│   ├── TESTING.md                  how to run/interpret the test suite
│   ├── STRATEGY_THEORY.md          momentum theory + worked numeric example
│   ├── EMAIL_REPORTING.md          notification categories, monthly report config
│   ├── EMAIL_COMMANDS.md           remote email commands: syntax, security model
│   ├── ALERT_LOG.md                alert log schema, every alert_type, how it differs
│   │                                 from the trade log and email command log
│   └── RISK_CONSTRAINTS.md         long-term/short-term momentum risk constraints,
│                                     advisory warnings and opt-in config toggles
│
├── notebooks/
│   ├── research/                  strategy design, signal research, backtesting
│   │   ├── DHI0016_notebook1_research_and_EDA_IMPROVED.ipynb    lookback/holding
│   │   │                           grid, walk-forward validation
│   │   ├── DHI0016_notebook2_strategy_coding_IMPROVED.ipynb     signal construction,
│   │   │                           liquidity filter
│   │   └── DHI0016_notebook3_full_backtest_IMPROVED.ipynb       full backtest, factor
│   │                               decomposition, regime breakdown, dual momentum overlay
│   └── operational/               interactive validation & run walkthroughs (safe, dry-run only)
│       ├── live_signal_walkthrough.ipynb
│       ├── daily_runner_walkthrough.ipynb
│       ├── email_commands_walkthrough.ipynb
│       ├── portfolio_snapshot_report.ipynb   investor-facing view: positions,
│       │                           value over time, benchmark comparison, plus every measure
│       │                           the email reports show, position performance since entry
│       │                           (real, FIFO-reconstructed from the trade log, and an
│       │                           illustrative example), technical/fundamental indicators,
│       │                           macro context, since-inception stats, trailing-window chart
│       └── live_vs_backtest_reconciliation.ipynb   real live P&L vs. backtested P&L
│
├── src/momentum_trading/          installable package (`pip install -e .`)
│   ├── __init__.py
│   ├── daily_runner.py             CLI entry point / orchestrator (also registered as
│   │                                the `daily-runner` console script)
│   │
│   ├── core/                      pure data/signal logic, no execution or I/O side effects
│   │   ├── functions.py             data fetching (multi-vendor fallback), tear_sheet,
│   │   │                             shared helpers
│   │   ├── functions_quant_extensions.py   liquidity filter, walk-forward, bootstrap CI,
│   │   │                             factor decomposition, regime breakdown, dual momentum,
│   │   │                             VaR/CVaR, scenario shocks, capacity checks, multi-lookback,
│   │   │                             since-inception + trailing-window live performance stats
│   │   ├── technical_indicators.py  hand-rolled SMA/EMA/RSI/MACD/ATR/Bollinger/ADX/VWAP/OBV,
│   │   │                             not pandas-ta (dependency-conflicts with pandas>=3.0.3)
│   │   ├── fundamentals.py          P/E, PEG, ROE, Debt-to-Equity, Current Ratio for held
│   │   │                             positions, FMP `/stable/` first, EODHD fallback, file-cached
│   │   ├── macro_data.py            Fed Funds Rate, CPI via FRED, needs FRED_API_KEY,
│   │   │                             portfolio-wide (one fetch per run), file-cached
│   │   ├── paths.py                 PROJECT_ROOT resolution, single source of truth for
│   │   │                             where config.yaml/data/logs live, regardless of CWD
│   │   ├── smtp_auth.py             shared SMTP auth for email sending, password-based
│   │   │                             (Gmail) or XOAUTH2 (Outlook/Microsoft 365)
│   │   └── audit_log.py             shared hash-chain append helper + the alert log
│   │                                 (logs/alerts_log.csv), every alert/warning event,
│   │                                 kept separate from the trade log and email command log
│   │
│   ├── backtest/
│   │   └── momentum_backtest.py     risk-managed backtest engine: BacktestConfig,
│   │                                 run_custom_backtest, resolve_target_weights (shared
│   │                                 sizing logic also used by execution/), crash protection
│   │                                 (correlation-spike detection, liquidity-stress handling,
│   │                                 time-based stops)
│   │
│   ├── execution/
│   │   └── live_signal.py           live signal generation, order generation, IBKR
│   │                                 integration (with connection retry), multi-portfolio
│   │                                 orchestration, real P&L measurement, stale-price and
│   │                                 slippage-tolerance checks, per-ticker position
│   │                                 performance since entry (build_position_performance())
│   │
│   ├── risk/
│   │   ├── circuit_breaker.py       portfolio-level circuit breaker (%  and $ thresholds,
│   │   │                             email-override tightening-only enforcement),
│   │   │                             extracted from daily_runner.py so risk logic has no
│   │   │                             dependency on interfaces/ (alerting is dependency-injected)
│   │   └── risk_monitor.py          independent, read-only oversight process, watches
│   │                                 trade logs, can halt trading, cannot place orders
│   │
│   └── interfaces/
│       ├── notifications.py         categorized email notifications (CRITICAL/STANDARD/
│       │                             PERIODIC/DAILY/WARNING) + monthly & daily HTML report
│       │                             generation (shared builder, see CLAUDE.md)
│       ├── email_commands.py        pydantic-validated, fail-safe remote email commands
│       │                             (PAUSE/RESUME/LIQUIDATE/SKIP_NEXT_REBALANCE/
│       │                             TRIGGER_REPORT/ADJUST_PARAM/STATUS/SET_MAX_DRAWDOWN/
│       │                             ALERTS_REPORT)
│       └── email_diagnostics.py     backs `daily-runner --test-email`, live SMTP+IMAP
│                                     check independent of config.yaml
│
└── tests/                         pytest suite (423 tests), mirrors src/ layout where a
    ├── conftest.py                  test's primary subject is a single sub-package;
    ├── test_architecture.py         cross-cutting/integration tests stay at tests/ root
    ├── test_daily_runner.py
    ├── test_docker_entrypoint.py     docker-entrypoint.sh's crontab generation,
    │                                  run as a real subprocess
    ├── test_governance.py
    ├── test_reporting.py
    ├── test_execution_safety.py
    ├── backtest/
    │   └── test_momentum_backtest.py
    ├── core/
    │   ├── test_audit_log.py        hash-chain helper + alert log
    │   ├── test_technical_indicators.py
    │   ├── test_functions_quant_extensions.py   since-inception + trailing-window stats
    │   ├── test_fundamentals.py       P/E, PEG, ROE, Debt-to-Equity, Current Ratio
    │   └── test_macro_data.py         Fed Funds Rate, CPI (FRED)
    ├── execution/
    │   └── test_live_signal.py
    └── interfaces/
        ├── test_email_diagnostics.py
        ├── test_notifications.py
        └── test_email_commands.py
```

</details>

<details open>
<summary> <b>Project Maturity & Safety<b></summary>

### Infrastructure safety ≠ strategy safety

These are two separate questions. This project answers the first one well; **the second one has
not been answered at all yet**:

| Question | Status |
|---|---|
| Does the code have circuit breakers, idempotency, alerting, audit logging? | ✅ Yes, tested |
| Has the strategy shown a positive out-of-sample (holdout) return on real data? | ❌ Never run on real data |
| Has it been validated against real 2008/2020/2022 history? | ❌ Never, only synthetic crash-shaped test data |
| Has it connected to a real broker even once? | ✅ Yes, paper (port 7497) connection, account summary, position fetch, and **confirmed real BUY and SELL order fills** (verified directly in TWS's own execution log across two portfolios, real prices, matching quantities). Getting here surfaced and fixed three real bugs (every order silently rejected while the run logged success; a misleadingly-short fill-confirmation poll window; an informational per-order notice mistaken for a rejection, causing an already-filled order to be logged as failed) and one hard IBKR platform limitation worked around (no fractional equity orders via API, ever, floored to whole shares). The live/real-money port (7496) is still unexercised |
| Has real live-vs-backtest divergence been measured? | ❌ Real trades now exist (paper), but no divergence analysis has been run yet, see `notebooks/operational/live_vs_backtest_reconciliation.ipynb` |

**Do not treat a well-tested codebase as a validated strategy.** See `docs/RUNNING.md`'s staged
rollout plan (Historical Validation → Paper → Small Live → Full Live) before allocating real
capital, and follow it in order, each stage exists because the previous one alone doesn't
answer whether the strategy actually works.

### ⚠️ Before you do anything live

- Everything defaults to **dry-run**, no real orders are ever placed unless you explicitly
  pass `--live`.
- **Paper-trade first.** See `docs/RUNNING.md` Section 3 before Section 4.
- Real-money trading requires two separate explicit flags together
  (`--port 7496 --confirm-live-trading`), this is intentional friction, not a bug.
- **Paper vs. live is not a stored "mode"**, the app is stateless per invocation. `--port 7497`
  vs. `--port 7496` just picks which TWS/IB Gateway port to connect to, and whichever account
  happens to be logged in on that port is what actually trades. This is an IBKR *convention*
  (7497 = paper, 7496 = live), not something the code verifies. Always confirm in TWS itself
  which account is logged in on the port you're about to use, especially before `--live`.
- `daily-runner --force-rebalance` (dry-run) is a fast sanity check for signal/sizing logic,
  it is **not** an all-in-one functionality test. It never opens an IBKR connection, never
  fetches real positions (so stop-loss/time-stop checks never even run), and never exercises
  the `--live` safety gates. Complete the paper-trading stage before trusting the broker-facing
  paths.
- Nothing here is investment advice. Momentum strategies carry real crash risk; past backtest
  performance is not a guarantee of future results.

### Known Gaps (read this before trusting a backtest number)

- **No point-in-time universe**, ETF picks use today's known survivors, backtested into the
  past; survivorship bias is not corrected.
- **Momentum crowding risk**, cross-sectional momentum is widely traded by CTAs/quant funds;
  when many players hold similar positions, momentum reversals ("crashes") tend to be sharper
  and faster than your own backtest can show, because it's a market-structure risk sitting
  outside any single account's data.
- **No tax modeling**, see `docs/RUNNING.md`'s tax-awareness note; realistic after-tax returns
  in a taxable account could be materially lower than any number shown here.
- **No capacity/market-impact validation on real order books**, the capacity check
  (`max_pct_of_adv`) is advisory and based on historical average volume, not real-time
  order-book depth.
- **IBKR's API has no fractional equity/ETF order support, period**, not an `ibapi` version
  issue, not fixable by this codebase. Confirmed both empirically (setting `cashQty` alongside
  `totalQuantity`, exactly per IBKR's own official sample code, still failed with `error 10243`
  for `STK` contracts, `cashQty` only works for forex/CASH-pair orders) and by direct API
  community confirmation. `place_orders_ibkr()` floors fractional share counts to whole shares
  immediately before submission (dropping the order, with a warning, if it floors to 0), the
  only way a live rebalance can place ETF orders at all. `allow_fractional_shares: true` still
  fully applies to backtest sizing and live drift/order-generation math; only the final IBKR
  submission is forced whole. See `DEPLOYMENT.md`'s "Troubleshooting: IBKR order placement".
- **Real paper fills now confirmed (BUY and SELL), but only very recently and only in this
  narrow path**, `get_ibkr_positions()`, `get_ibkr_account_value()`, and `place_orders_ibkr()`
  have all been exercised against a real paper (port 7497) connection, and rebalance orders on
  both portfolios were verified to actually fill (confirmed directly in TWS's own execution log,
  both BUYs and SELLs, real prices, matching quantities). Getting here took four fixes, in
  order: every order was first silently rejected (`error 10268`, an `ibapi`/TWS version
  incompatibility); then every fractional-share order was still rejected (`error 10243`, the
  platform limitation above) until whole-share flooring landed; then real fills were
  misreported as unconfirmed because `place_orders_ibkr()`'s fill-poll window (15s) was shorter
  than actual paper-fill latency (now 60s, configurable via `fill_poll_timeout`); then an
  informational per-order notice (`error 10349`, "Order TIF was set to DAY based on order
  preset") was found to be incorrectly overwriting a real, filled order's tracked status to
  `"ERROR: ..."`, making the poll loop give up watching it, confirmed against a real case where
  the order had genuinely filled in TWS despite being logged as failed. This has been confirmed
  for `--force-rebalance` runs on one paper account, a handful of times, the real-money port,
  sustained/scheduled (non-forced) operation, and behavior across many cycles are all still
  unexercised.
- **Multi-portfolio ticker leakage on a shared account is not just theoretical**, observed
  directly: portfolio2 (tickers `XLF`/`XLE`/`GLD`/`TLT`) inherited a stray `BIL` position from
  portfolio1 via `reqPositions()` (which returns every position on the shared IBKR account, not
  filtered per portfolio), and correctly refused to trade it blind (`HOLD, no live price
  available`, since portfolio2 never fetches prices outside its own ticker universe), but this
  also means it can never reconcile or exit that position on its own. This is the real-world
  shape of the `TICKER OVERLAP` warning every run already prints when portfolios share tickers;
  worth understanding before running multiple portfolios against one real account.

### Who should allocate capital here

Momentum strategies have real, sometimes multi-year, underperformance periods even when the
long-run edge is genuine, this isn't a flaw specific to this implementation, it's inherent to
the factor (the chart above is a live example of that). Only allocate capital that:
- You can leave systematically managed through a genuinely bad multi-month or multi-year
  stretch without needing to intervene emotionally.
- You won't need for at least 1-2 years.
- Represents a deliberate allocation decision, not money you're testing this system with
  because it happens to be available.

</details>

<details open>
<summary> <b>Using The Package<b></summary>

- Clone the repo:
~~~bash
    git clone https://github.com/issaiass/momentum-trading.git
    cd momentum-trading
~~~
- Install (editable install, `uv` or plain `pip`):
~~~bash
    uv sync                                    # if using uv (uv.lock present)
    # or
    pip install -e ".[dev]"                    # dev deps add pytest
~~~
- Copy the example config and edit it (tickers, portfolios, risk settings):
~~~bash
    cp config.example.yaml config.yaml
~~~
- If using email notifications/commands, copy `.env.example` to `.env`, fill in real values, then
  verify them for real before trusting cron/`--live` with them:
~~~bash
    daily-runner --test-email
~~~
- Test signal/order generation, safe, no broker connection, never places an order:
~~~bash
    daily-runner --force-rebalance
~~~
- Paper trade (requires TWS/IB Gateway running, paper account logged in on port 7497):
~~~bash
    daily-runner --live --port 7497
~~~
- Go live, both flags are required together, on purpose:
~~~bash
    daily-runner --live --port 7496 --confirm-live-trading
~~~
- Clear a circuit-breaker halt after reviewing what tripped it:
~~~bash
    daily-runner --resume-trading <portfolio_name>
~~~
- Run the independent, read-only risk monitor:
~~~bash
    python -m momentum_trading.risk.risk_monitor --portfolio <name> --max-loss-pct 0.25
~~~
- Or run it all in Docker, self-scheduling via internal cron, no manual triggering needed:
~~~bash
    docker compose up -d --build
    docker exec -it momentum-signal crontab -l              # verify the schedule
    docker exec -it momentum-signal daily-runner --force-rebalance   # one-off manual check
~~~
- Run the test suite (no network/broker required, synthetic/mocked data throughout):
~~~bash
    pip install -r requirements-dev.txt
    pytest tests/ -v
~~~

Full argument reference: `daily-runner --help`. Day-to-day commands, the staged rollout plan,
and multi-portfolio/Docker specifics live in `docs/RUNNING.md` and `docs/DEPLOYMENT.md`.

</details>

<details open>
<summary> <b>Documentation Map<b></summary>

| I want to... | Read |
|---|---|
| Understand what each file does | This README (above) |
| Install this on a new machine | `docs/DEPLOYMENT.md` |
| Actually run it (single/multi-portfolio, paper/live) | `docs/RUNNING.md` |
| Understand the research/signal methodology | `notebooks/research/DHI0016_notebook1_research_and_EDA_IMPROVED.ipynb` (start there) |
| Understand the momentum strategy's theory + a worked example | `docs/STRATEGY_THEORY.md` |
| Run or understand the test suite | `docs/TESTING.md` |
| Configure/understand email notifications and monthly reports | `docs/EMAIL_REPORTING.md` |
| Configure/understand email-commanded remote actions (PAUSE/RESUME/etc.) | `docs/EMAIL_COMMANDS.md` |
| Understand the alert log (what's recorded, how it differs from the trade/email-command logs) | `docs/ALERT_LOG.md` |
| Understand the long-term/short-term momentum risk constraints (turnover, skip-month, vol budget) | `docs/RISK_CONSTRAINTS.md` |

</details>

<details open>
<summary> <b>Results<b></summary>

The chart in "Brief Review" above is the current representative result: a real ETF price
history backtest, run through this project's own backtest engine, not a hand-tuned or
cherry-picked window. Further validation (walk-forward, regime-conditional breakdown,
out-of-sample holdout) is available via `core/functions_quant_extensions.py` and Notebook 1, see
`docs/STRATEGY_THEORY.md`.

### Underlying Momentum Evidence (Notebook 1 EDA)

The equity-curve chart above is this project's own backtested strategy scenarios against SPY,
specific configurations' outcomes. The three charts below are a different, earlier kind of
evidence: the classic academic cross-sectional momentum anomaly itself, measured directly on
this project's own ETF universe and price history in `notebooks/research/
DHI0016_notebook1_research_and_EDA_IMPROVED.ipynb`, before any of this project's own risk
overlays, sizing, or execution logic are applied. Don't conflate the two, a real anomaly existing
in this universe/history is not the same claim as "this project's specific live configuration
beats SPY."

<p align="center">
<img src="docs/img/momentum_winners_vs_losers.png?raw=true" alt="Cumulative performance of top vs. bottom momentum deciles" width="80%"/>
</p>

**Winners vs. losers**: every month, ETFs are ranked into deciles by trailing 12-month return,
this chart tracks the cumulative, indexed-to-100 performance of the top decile (recent winners)
against the bottom decile (recent losers). The winner decile compounding visibly above the loser
decile over time is the core cross-sectional momentum effect this whole strategy is built on.

<p align="center">
<img src="docs/img/momentum_win_rate_heatmap.png?raw=true" alt="Momentum win rate by lookback and holding period" width="80%"/>
</p>

**Does it hold across parameter choices?**: a lookback-period x holding-period grid, each cell is
the percentage of months the top decile actually beat the bottom decile at that specific
combination. A grid dominated by win rates comfortably above 2.06% across most combinations is
evidence the effect isn't an artifact of one specific lookback/holding choice.

<p align="center">
<img src="docs/img/momentum_decile_returns.png?raw=true" alt="Average 1-month forward return by momentum decile" width="80%"/>
</p>

**The plainest picture**: average 1-month forward return by decile (12-month lookback), decile 1
being the recent losers, decile 10 the recent winners. A roughly monotonic increase from decile
1 to decile 10 is the textbook signature of the momentum anomaly.

</details>

<details open>
<summary> <b>Issues<b></summary>

- No open code defects. The honest open items are the strategy-validation gaps listed under
  "Known Gaps" above, those are tracked as maturity gaps, not bugs.

</details>

<details open>
<summary> <b>Future Work<b></summary>

- Real out-of-sample validation against historical crash periods (2008/2020/2022), not just
  synthetic crash-shaped test data
- A tested live TWS/IB Gateway connection, and measured live-vs-backtest divergence once real
  trades exist
- Point-in-time universe construction to remove survivorship bias
- Tax-aware return modeling for taxable accounts
- Real order-book-based capacity/market-impact validation, beyond the current ADV-based
  advisory check

</details>

<details open>
<summary> <b>Contributing<b></summary>

Your contributions are always welcome! Please feel free to fork and modify the content but
remember to finally do a pull request.

</details>

<details open>
<summary> :iphone: <b>Having Problems?<b></summary>

<p align = "center">

[<img src="https://img.shields.io/badge/linkedin-%230077B5.svg?&style=for-the-badge&logo=linkedin&logoColor=white" />](https://www.linkedin.com/in/riawa)
[<img src="https://img.shields.io/badge/telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white"/>](https://t.me/issaiass)
[<img src="https://img.shields.io/badge/instagram-%23E4405F.svg?&style=for-the-badge&logo=instagram&logoColor=white">](https://www.instagram.com/daqsyspty/)
[<img src="https://img.shields.io/badge/twitter-%231DA1F2.svg?&style=for-the-badge&logo=twitter&logoColor=white" />](https://twitter.com/daqsyspty)
[<img src ="https://img.shields.io/badge/facebook-%233b5998.svg?&style=for-the-badge&logo=facebook&logoColor=white%22">](https://www.facebook.com/daqsyspty)
[<img src="https://img.shields.io/badge/linkedin-%230077B5.svg?&style=for-the-badge&logo=linkedin&logoColor=white" />](https://www.linkedin.com/in/riawe)
[<img src="https://img.shields.io/badge/tiktok-%23000000.svg?&style=for-the-badge&logo=tiktok&logoColor=white" />](https://www.linkedin.com/in/riawe)
[<img src="https://img.shields.io/badge/whatsapp-%23075e54.svg?&style=for-the-badge&logo=whatsapp&logoColor=white" />](https://wa.me/50766168542?text=Hello%20Rangel)
[<img src="https://img.shields.io/badge/hotmail-%23ffbb00.svg?&style=for-the-badge&logo=hotmail&logoColor=white" />](mailto:issaiass@hotmail.com)
[<img src="https://img.shields.io/badge/gmail-%23D14836.svg?&style=for-the-badge&logo=gmail&logoColor=white" />](mailto:riawalles@gmail.com)

</p>

</details>

<details open>
<summary> <b>License<b></summary>
<p align = "center">
No LICENSE file is included in this repository yet, treat the code as all-rights-reserved
until one is added.
</p>
</details>
