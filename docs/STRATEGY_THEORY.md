# Momentum Strategy: Theory and Worked Example

## The academic basis

Cross-sectional momentum, the idea that assets which have recently outperformed tend to keep
outperforming over the next several months, is one of the most studied anomalies in finance.
The foundational paper is **Jegadeesh and Titman (1993), "Returns to Buying Winners and
Selling Losers: Implications for Stock Market Efficiency"** (*Journal of Finance*), which found
that a strategy of buying the best-performing stocks over the prior 3-12 months and holding
for 3-12 months produced statistically significant excess returns in US equities from
1965-1989. This has since been replicated across dozens of asset classes, countries, and time
periods (see Asness, Moskowitz, and Pedersen's 2013 "Value and Momentum Everywhere" for a
broad cross-asset confirmation).

**Why it's believed to happen** (no single explanation is fully settled):
- **Underreaction theory**: investors are slow to fully incorporate new information (earnings
  surprises, changing fundamentals) into prices, so a genuine trend takes time to fully play
  out in the price.
- **Herding/momentum ignition**: as a trend becomes visible, momentum-following investors
  (systematic and discretionary) pile in, extending the move beyond what fundamentals alone
  would justify.
- **Risk-based explanations**: momentum could partly be compensation for bearing crash risk,
  momentum strategies have historically suffered sharp, fast reversals ("momentum crashes"),
  most notably in 2009, which some researchers argue is the market's way of pricing that tail
  risk into the strategy's average excess return.

**Why it's NOT free money**: transaction costs, the crash risk above, and (critically) the
fact that momentum is now one of the most widely traded, well-known factors means much of its
historical edge may already be arbitraged away relative to the original 1990s studies. This
project's own validation tooling (walk-forward/holdout testing in Notebook 1) exists
specifically to check whether a meaningful edge remains after realistic costs, not to assume
the academic literature guarantees it.

## How this specific implementation works

1. **Lookback**: trailing N-month total return per ETF, configurable via `config.yaml`'s
   `default_risk.lookback_period` (or per-portfolio `risk_overrides`), default 12 months (the
   3-12 month range the academic literature above studied, "long-term momentum" in this
   project's terms). LIVE-ONLY: the backtest engine takes pre-computed picks as input, so this
   field has no effect on backtest results, a backtest's lookback is set wherever the picks were
   computed (typically a research notebook's own `calculate_period_returns(..., period=...)`
   call). Under a weekly `holding_period` (`< 1`, see item 5 below), `lookback_period` switches
   to week-scale instead, `0.5` = 2 weeks, `0.75` = 3 weeks, `1.0` = 4 weeks, `1.5` = 6 weeks
   ("short-term momentum"). Be aware this week-scale window is a genuine departure from the
   3-12 month range the Jegadeesh and Titman study above actually validated, the classic
   literature doesn't cover momentum signals this short, this project's own walk-forward
   tooling hasn't specifically stress-tested it either. Treat short-term momentum as an
   unvalidated variant, not an academically-backed alternative. Both regimes have several risk
   constraints available, non-blocking advisory warnings (Momentum Persistence,
   Lookback-to-Hold Ratio, Turnover Limit) and opt-in config toggles (the Skip-Month Guardrail,
   a per-position Volatility-Adjustment budget), see `docs/RISK_CONSTRAINTS.md` for the full
   list and exact thresholds.
2. **Ranking**: all ETFs in the universe are ranked by that trailing return.
3. **Selection**: the top `top_n` ranked ETFs become the month's picks, configurable via
   `config.yaml`'s `default_risk.top_n` (or per-portfolio `risk_overrides`), default 10,
   clamped to the portfolio's own `tickers` list if that's smaller.
4. **Sizing**: capital is allocated across picks either by inverse volatility (default,
   underweight noisier names) or, if `sizing_method: score_proportional` is set, proportional
   to each pick's momentum score, stronger momentum gets more capital.
5. **Rebalance**: monthly by default, configurable via `holding_period`, which also accepts
   fractional values mapping onto weeks (`0.25` = weekly, `0.5` = every 2 weeks, `0.75` = every 3
   weeks), with drift-threshold filtering to avoid trading on trivial rebalances. Anything faster
   than weekly (`< 0.25`) is allowed but actively discouraged: rebalancing that often adds real
   commission/slippage/whole-share drift cost without a correspondingly short lookback window to
   justify it, a non-blocking WARNING (logged and emailed every run) exists specifically to keep
   that visible. A separate, similar WARNING exists if `lookback_period` itself is set below 2
   weeks under a weekly `holding_period`, that short a window is dominated by price noise rather
   than real trend.
6. **Risk overlays**: regime filter (de-risk when the benchmark is below its long moving
   average), volatility targeting, position caps, correlation penalty, stop-losses, and the
   crash-protection mechanisms sit on top of this core signal, none of them change
   *which* ETFs get picked, only *how much* capital is deployed and when to exit.

## A concrete worked example

Suppose the universe is 5 ETFs, and today is a rebalance date. Trailing 12-month returns:

| Ticker | 12-Month Return | Rank |
|---|---|---|
| XLK (Technology) | +28% | 1 |
| QQQ (Nasdaq-100) | +22% | 2 |
| SPY (S&P 500) | +15% | 3 |
| XLU (Utilities) | +4% | 4 |
| TLT (Long Treasuries) | -8% | 5 |

With `top_n = 3`, the picks are **XLK, QQQ, SPY**, the three strongest trailing performers.
XLU and TLT are excluded this month, not because they're "bad" investments in some absolute
sense, but because the signal only cares about relative recent strength.

**Sizing (inverse-vol, the default):** if XLK has historically been the most volatile of the
three picks and SPY the least, XLK gets the *smallest* weight of the three and SPY the
*largest*, the strategy is expressing a view on *which* names to hold (via ranking) while
trying to equalize *risk contribution* (via sizing), not necessarily conviction.

**Sizing (score-proportional alternative):** the same three picks would instead be
weighted 28:22:15 (normalized), so XLK (the strongest momentum) gets the *largest* weight,
not the smallest. This is a genuinely different bet: it assumes signal strength should drive
capital allocation, at the cost of ignoring volatility differences. Neither approach is
"correct", see Notebook 3's comparison cells for how to test which one performs better on
your actual universe and period.

**Next month**, this whole process repeats independently, if XLU's trailing return has since
overtaken SPY's, XLU enters the picks and SPY may exit, regardless of whether SPY was
profitable during the holding period. The strategy has no memory of "why" it held something
last month; it only asks "who's strongest right now."

## What this means for expectations

- This is a **relative** strategy, it will always hold *something*, even when the whole
  market is falling, unless the regime filter or dual-momentum overlay (Notebook 3) reduces
  exposure. It picks the *relative* winners, which can still be losing money in absolute terms
  during a broad downturn.
- Past backtested performance under this signal has genuinely never been validated against
  real market data in this project (see `../README.md`'s "Project Maturity & Safety" section), the theory
  above explains *why someone might expect* this to work, not confirmation that it does, here,
  now, on your specific universe.
