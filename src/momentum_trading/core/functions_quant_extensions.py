"""
functions_quant_extensions.py

Additions to functions.py addressing the gaps flagged in the notebook review:
  1. Liquidity/capacity filtering  -> liquidity_filter()
  2. Walk-forward (train/test) parameter selection -> walk_forward_lookback_holding()
  3. Block-bootstrap confidence intervals on Sharpe -> bootstrap_sharpe_ci()

Import alongside functions.py:
    from momentum_trading.core import functions as fn
    from momentum_trading.core import functions_quant_extensions as fnx

These are additive -- nothing in functions.py or momentum_backtest.py is modified,
so existing notebook cells keep working unchanged.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# 1. LIQUIDITY / CAPACITY FILTER
# --------------------------------------------------------------------------- #
def liquidity_filter(
    df_ranks: pd.DataFrame,
    df_prices: pd.DataFrame,
    df_volume: pd.DataFrame | None = None,
    min_avg_dollar_volume: float = 1_000_000.0,
    lookback_days: int = 63,
) -> pd.DataFrame:
    """
    Zero out (set to NaN) any ETF's rank on any month-end date where its trailing
    average daily dollar volume falls below `min_avg_dollar_volume`.

    Without this, decile/top_n analysis can select ETFs that are too thin to
    actually trade at the assumed size -- a common way backtests overstate
    real-world achievable returns.

    Parameters
    ----------
    df_ranks : pd.DataFrame
        Output of assign_ranks() -- index=month-end dates, columns=tickers.
    df_prices : pd.DataFrame
        Daily close prices, columns=tickers (same universe as df_ranks).
    df_volume : pd.DataFrame, optional
        Daily share volume, columns=tickers. If None, the filter is a no-op
        (returns df_ranks unchanged) -- you cannot assess liquidity without
        volume data, so this makes the missing-data case explicit rather than
        silently skipping the check.
    min_avg_dollar_volume : float
        Minimum trailing average daily dollar volume (price * volume) required
        for a ticker to remain eligible on a given rebalance date.
    lookback_days : int
        Trading-day window used to compute the trailing average dollar volume.

    Returns
    -------
    pd.DataFrame
        Copy of df_ranks with ineligible (ticker, date) cells set to NaN, so
        get_top_etfs()'s nsmallest() will naturally skip them.
    """
    if df_volume is None:
        return df_ranks.copy()

    dollar_volume = (df_prices * df_volume).rolling(lookback_days, min_periods=lookback_days // 2).mean()
    dollar_volume_at_rank_dates = dollar_volume.reindex(df_ranks.index, method="ffill")

    eligible = dollar_volume_at_rank_dates >= min_avg_dollar_volume
    filtered_ranks = df_ranks.copy()
    filtered_ranks = filtered_ranks.where(eligible.reindex_like(filtered_ranks), np.nan)
    return filtered_ranks


def check_capacity(
    target_dollar_positions: dict, df_volume: pd.DataFrame, df_prices: pd.DataFrame,
    as_of: pd.Timestamp, max_pct_of_adv: float = 0.05, lookback_days: int = 21,
) -> dict:
    """
    Epic 2, Story 2.4: flags any position whose target size would exceed
    max_pct_of_adv (average daily dollar volume) -- a proxy for market impact
    risk. Institutions typically cap single-day participation well under 10%
    of ADV to avoid moving the price against themselves; 5% is a conservative
    retail-scale default here.

    This is advisory (returns flags), not a hard block -- wire the result into
    daily_runner.py as a pre-trade warning, or make it a hard block once
    you've decided on your own risk tolerance for this.

    Parameters
    ----------
    target_dollar_positions : dict {ticker: target $ notional}
    df_volume : daily share volume, columns=tickers
    df_prices : daily close prices, columns=tickers
    as_of : date to evaluate ADV as of

    Returns
    -------
    dict {ticker: {'target_dollar', 'adv_dollar', 'pct_of_adv', 'flagged': bool}}
    """
    if df_volume is None or df_volume.empty:
        return {t: {"target_dollar": v, "adv_dollar": None, "pct_of_adv": None, "flagged": False}
                for t, v in target_dollar_positions.items()}

    dollar_volume = (df_prices * df_volume).rolling(lookback_days, min_periods=lookback_days // 2).mean()
    result = {}
    for ticker, target in target_dollar_positions.items():
        if ticker not in dollar_volume.columns or as_of not in dollar_volume.index:
            result[ticker] = {"target_dollar": target, "adv_dollar": None, "pct_of_adv": None, "flagged": False}
            continue
        adv = dollar_volume.loc[as_of, ticker]
        if pd.isna(adv) or adv <= 0:
            result[ticker] = {"target_dollar": target, "adv_dollar": None, "pct_of_adv": None, "flagged": False}
            continue
        pct = abs(target) / adv
        result[ticker] = {
            "target_dollar": target, "adv_dollar": adv, "pct_of_adv": pct,
            "flagged": bool(pct > max_pct_of_adv),
        }
    return result


# --------------------------------------------------------------------------- #
# 2. WALK-FORWARD PARAMETER SELECTION
# --------------------------------------------------------------------------- #
def walk_forward_lookback_holding(
    df_prices_monthly: pd.DataFrame,
    calculate_period_returns_fn,
    assign_ranks_fn,
    backtest_fn,
    lookback_candidates: list[int],
    holding_candidates: list[int],
    train_years: int = 8,
    test_years: int = 2,
    step_years: int = 2,
    metric: str = "Sharpe",
) -> pd.DataFrame:
    """
    True walk-forward validation: for each rolling window, pick the best
    (lookback, holding) combo using ONLY the training slice, then evaluate that
    choice on the immediately following, unseen test slice. This is the direct
    fix for "tuning params on the full 2005-2025 sample and backtesting on the
    same sample" -- every reported test-period number here is out-of-sample.

    Parameters
    ----------
    df_prices_monthly : pd.DataFrame
        Monthly close prices, columns=tickers.
    calculate_period_returns_fn, assign_ranks_fn : callables
        Pass in your existing calculate_period_returns / assign_ranks functions
        from the notebook so this stays consistent with your signal logic.
    backtest_fn : callable
        A function(df_ranks_or_picks, price_slice) -> dict with at least
        {'Sharpe': float, 'CAGR': float, ...}. Wire this to your
        run_custom_backtest + tearsheet, or a lighter monthly-return version.
    lookback_candidates, holding_candidates : list[int]
        Grid of lookback/holding periods (in months) to search over.
    train_years, test_years, step_years : int
        Rolling window sizing, in years.
    metric : str
        Which tearsheet key to optimize for on the training slice.

    Returns
    -------
    pd.DataFrame
        One row per walk-forward fold: chosen params, train metric, test metric.
        A real edge should show test metrics that are positive and reasonably
        close to train metrics -- a big train/test gap is the signature of
        overfitting the parameter grid.
    """
    dates = df_prices_monthly.index
    start = dates.min()
    end = dates.max()

    fold_start = start
    records = []

    while True:
        train_end = fold_start + pd.DateOffset(years=train_years)
        test_end = train_end + pd.DateOffset(years=test_years)
        if test_end > end:
            break

        train_slice = df_prices_monthly[(dates >= fold_start) & (dates < train_end)]
        test_slice = df_prices_monthly[(dates >= train_end) & (dates < test_end)]

        best_params, best_train_metric = None, -np.inf
        for lb in lookback_candidates:
            for hp in holding_candidates:
                scores = calculate_period_returns_fn(train_slice, period=lb).dropna(how="all")
                ranks = assign_ranks_fn(scores)
                result = backtest_fn(ranks, train_slice, holding_period=hp)
                train_metric = result.get(metric, -np.inf)
                if train_metric is not None and train_metric > best_train_metric:
                    best_train_metric = train_metric
                    best_params = (lb, hp)

        if best_params is None:
            fold_start += pd.DateOffset(years=step_years)
            continue

        lb, hp = best_params
        scores_test = calculate_period_returns_fn(test_slice, period=lb).dropna(how="all")
        ranks_test = assign_ranks_fn(scores_test)
        test_result = backtest_fn(ranks_test, test_slice, holding_period=hp)

        records.append({
            "fold_start": fold_start,
            "train_end": train_end,
            "test_end": test_end,
            "chosen_lookback": lb,
            "chosen_holding": hp,
            f"train_{metric}": best_train_metric,
            f"test_{metric}": test_result.get(metric, np.nan),
            "test_CAGR": test_result.get("CAGR", np.nan),
        })

        fold_start += pd.DateOffset(years=step_years)

    return pd.DataFrame(records)


# --------------------------------------------------------------------------- #
# 3. BLOCK BOOTSTRAP CONFIDENCE INTERVAL ON SHARPE
# --------------------------------------------------------------------------- #
def bootstrap_sharpe_ci(
    monthly_returns: pd.Series,
    n_bootstrap: int = 2000,
    block_size: int = 6,
    annualization_factor: int = 12,
    confidence: float = 0.90,
    random_seed: int = 42,
) -> dict:
    """
    Block-bootstrap resampling of the monthly return series to get a confidence
    interval on the Sharpe ratio, instead of trusting a single point estimate
    from one historical path.

    Block bootstrap (not i.i.d. resampling) preserves short-run autocorrelation
    in returns, which matters for momentum strategies since their returns are
    not independent month to month.

    Returns
    -------
    dict with point_estimate, ci_low, ci_high, and the full bootstrap distribution
    (for plotting a histogram if you want to visualize it).
    """
    rng = np.random.default_rng(random_seed)
    r = monthly_returns.dropna().values
    n = len(r)
    if n < block_size * 2:
        raise ValueError(f"Series too short ({n} months) for block_size={block_size}.")

    def sharpe(x):
        vol = x.std()
        return (x.mean() * annualization_factor) / (vol * np.sqrt(annualization_factor)) if vol > 0 else np.nan

    point_estimate = sharpe(r)

    n_blocks = int(np.ceil(n / block_size))
    boot_sharpes = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        starts = rng.integers(0, n - block_size + 1, size=n_blocks)
        sample = np.concatenate([r[s:s + block_size] for s in starts])[:n]
        boot_sharpes[i] = sharpe(sample)

    alpha = 1 - confidence
    ci_low, ci_high = np.nanquantile(boot_sharpes, [alpha / 2, 1 - alpha / 2])

    return {
        "point_estimate": point_estimate,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "confidence": confidence,
        "pct_bootstrap_samples_positive": float(np.mean(boot_sharpes > 0)),
        "distribution": boot_sharpes,
    }


# --------------------------------------------------------------------------- #
# 4. PRE-REGISTERED TRAIN / HOLDOUT SPLIT
# --------------------------------------------------------------------------- #
def pre_registered_split(
    df_prices_monthly: pd.DataFrame,
    split_date: str = "2015-01-01",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Commit to a single train/holdout split BEFORE any parameter scanning.

    This is the fix for "scan the full-sample heatmap, then pick the best combo" --
    the heatmap in Notebook 1 should only ever be built on `train`, and `holdout`
    should not be touched (not even glanced at) until the final chosen config is
    locked in. Report holdout performance exactly once.

    Returns
    -------
    (train, holdout) : both pd.DataFrame slices of df_prices_monthly.
    """
    split = pd.to_datetime(split_date)
    train = df_prices_monthly[df_prices_monthly.index < split]
    holdout = df_prices_monthly[df_prices_monthly.index >= split]
    return train, holdout


# --------------------------------------------------------------------------- #
# 5. FACTOR DECOMPOSITION (how much is real alpha vs. static beta exposure?)
# --------------------------------------------------------------------------- #
def factor_decomposition(
    strategy_monthly_returns: pd.Series,
    factor_returns: pd.DataFrame,
) -> dict:
    """
    OLS regression of strategy returns on a set of factor/benchmark return series
    (e.g. SPY, TLT, GLD, or a proper momentum factor) to separate real
    cross-sectional alpha from static directional exposure the strategy happens
    to carry.

    Parameters
    ----------
    strategy_monthly_returns : pd.Series
        Monthly strategy returns, DatetimeIndex.
    factor_returns : pd.DataFrame
        Monthly factor/benchmark returns, columns = factor names, same index
        frequency (e.g. SPY, TLT, GLD monthly returns, or a UMD momentum factor
        if you have one).

    Returns
    -------
    dict with 'alpha' (annualized, the part unexplained by the factors),
    'alpha_tstat', 'betas' (dict per factor), 'r_squared'.

    A small betas dict with high r_squared and an insignificant alpha means the
    strategy's "outperformance" is mostly disguised static exposure to those
    factors, not genuine security-selection skill.
    """
    aligned = pd.concat([strategy_monthly_returns.rename("strategy"), factor_returns], axis=1).dropna()
    y = aligned["strategy"].values
    X = aligned.drop(columns="strategy").values
    X_design = np.column_stack([np.ones(len(X)), X])

    coefs, residuals, rank, sv = np.linalg.lstsq(X_design, y, rcond=None)
    fitted = X_design @ coefs
    resid = y - fitted
    n, k = X_design.shape
    dof = max(n - k, 1)
    sigma2 = (resid @ resid) / dof
    cov_beta = sigma2 * np.linalg.pinv(X_design.T @ X_design)
    se = np.sqrt(np.diag(cov_beta))

    alpha_monthly = coefs[0]
    alpha_tstat = alpha_monthly / se[0] if se[0] > 0 else np.nan
    ss_res = np.sum(resid ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan

    betas = dict(zip(aligned.drop(columns="strategy").columns, coefs[1:]))

    return {
        "alpha_annualized": alpha_monthly * 12,
        "alpha_tstat": alpha_tstat,
        "betas": betas,
        "r_squared": r_squared,
        "n_obs": n,
    }


# --------------------------------------------------------------------------- #
# 6. REGIME-CONDITIONAL PERFORMANCE BREAKDOWN
# --------------------------------------------------------------------------- #
def regime_breakdown(
    strategy_monthly_returns: pd.Series,
    benchmark_monthly_returns: pd.Series,
    regimes: dict[str, tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """
    Slice strategy vs. benchmark performance by named stress/regime windows,
    instead of hiding behind one aggregate Sharpe. Defaults to the standard set
    of known equity stress periods; pass your own dict to customize.

    Parameters
    ----------
    regimes : dict[name] -> (start_date, end_date), both 'YYYY-MM-DD' strings.
        Defaults: GFC 2008, COVID crash 2020, 2022 rate-hike bear market, plus
        an implicit "Full Sample" row.

    Returns
    -------
    pd.DataFrame indexed by regime name, columns: Strategy_Return, Benchmark_Return,
    Strategy_MaxDD, Benchmark_MaxDD, Outperformance.
    """
    if regimes is None:
        regimes = {
            "GFC (2008)": ("2008-01-01", "2009-03-01"),
            "COVID Crash (2020)": ("2020-01-01", "2020-06-01"),
            "2022 Bear Market": ("2022-01-01", "2022-12-31"),
        }

    def _cum_return(r):
        return (1 + r).prod() - 1 if len(r) else np.nan

    def _max_dd(r):
        if len(r) == 0:
            return np.nan
        cum = (1 + r).cumprod()
        return (cum / cum.cummax() - 1).min()

    rows = []
    all_regimes = {"Full Sample": (str(strategy_monthly_returns.index.min().date()),
                                    str(strategy_monthly_returns.index.max().date())), **regimes}
    for name, (start, end) in all_regimes.items():
        s = strategy_monthly_returns[(strategy_monthly_returns.index >= start) & (strategy_monthly_returns.index <= end)]
        b = benchmark_monthly_returns[(benchmark_monthly_returns.index >= start) & (benchmark_monthly_returns.index <= end)]
        rows.append({
            "Regime": name,
            "Strategy_Return": _cum_return(s),
            "Benchmark_Return": _cum_return(b),
            "Strategy_MaxDD": _max_dd(s),
            "Benchmark_MaxDD": _max_dd(b),
            "Outperformance": _cum_return(s) - _cum_return(b) if len(s) and len(b) else np.nan,
        })

    return pd.DataFrame(rows).set_index("Regime")


# --------------------------------------------------------------------------- #
# 7. SIGNIFICANCE TEST ON RETURN SPREAD VS. BENCHMARK
# --------------------------------------------------------------------------- #
def bootstrap_spread_significance(
    strategy_monthly_returns: pd.Series,
    benchmark_monthly_returns: pd.Series,
    n_bootstrap: int = 5000,
    block_size: int = 6,
    random_seed: int = 42,
) -> dict:
    """
    Tests whether the strategy's mean monthly outperformance vs. the benchmark
    is distinguishable from zero, via block bootstrap on the return SPREAD
    (not each series separately -- this correctly accounts for their
    correlation, which a naive two-sample t-test would ignore).

    Returns
    -------
    dict with mean_monthly_spread, ci_low, ci_high, p_value_two_sided
    (fraction of bootstrap resamples where the spread's sign flips relative to
    the point estimate -- a simple, robust proxy for significance).
    """
    aligned = pd.concat(
        [strategy_monthly_returns.rename("s"), benchmark_monthly_returns.rename("b")], axis=1
    ).dropna()
    spread = (aligned["s"] - aligned["b"]).values
    n = len(spread)
    if n < block_size * 2:
        raise ValueError(f"Series too short ({n} months) for block_size={block_size}.")

    rng = np.random.default_rng(random_seed)
    point_estimate = spread.mean()

    n_blocks = int(np.ceil(n / block_size))
    boot_means = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        starts = rng.integers(0, n - block_size + 1, size=n_blocks)
        sample = np.concatenate([spread[s:s + block_size] for s in starts])[:n]
        boot_means[i] = sample.mean()

    ci_low, ci_high = np.nanquantile(boot_means, [0.05, 0.95])
    p_value = float(np.mean(boot_means <= 0)) if point_estimate > 0 else float(np.mean(boot_means >= 0))

    return {
        "mean_monthly_spread": point_estimate,
        "annualized_spread": point_estimate * 12,
        "ci_low_90": ci_low,
        "ci_high_90": ci_high,
        "p_value_approx": p_value,
        "distribution": boot_means,
    }


# --------------------------------------------------------------------------- #
# 8. ABSOLUTE MOMENTUM OVERLAY (dual momentum -- Antonacci-style)
# --------------------------------------------------------------------------- #
def absolute_momentum_overlay(
    monthly_picks: pd.Series,
    momentum_scores: pd.DataFrame,
    defensive_ticker: str = "BIL",
) -> pd.Series:
    """
    Relative momentum (picking the top-N by rank) says nothing about whether
    those winners are actually winning in absolute terms -- in a broad
    drawdown (2008, 2022), the "top 10" can still all have negative trailing
    returns, and the strategy holds them anyway. This is the single most
    common real-world fix (Antonacci's "dual momentum" / GEM approach):

    For each month, any pick whose own trailing return (momentum_scores) is
    negative is replaced with a defensive/cash-like ticker (e.g. BIL, SHY)
    instead of being held. Duplicate defensive entries are collapsed to one.

    Parameters
    ----------
    monthly_picks : pd.Series
        Output of get_top_etfs() -- index=dates, values=list of tickers.
    momentum_scores : pd.DataFrame
        Output of calculate_period_returns() -- same index/columns universe,
        trailing return used to rank. Must share monthly_picks' date index.
    defensive_ticker : str
        Ticker to substitute in for any pick with negative absolute momentum.
        Must exist as a column in your price panel (e.g. 'BIL' T-bill ETF,
        'SHY' short treasuries, or plain cash if your backtest engine supports it).

    Returns
    -------
    pd.Series
        Same shape as monthly_picks, with negative-absolute-momentum names
        swapped for defensive_ticker.
    """
    out = {}
    for date, tickers in monthly_picks.items():
        if date not in momentum_scores.index:
            out[date] = tickers
            continue
        row = momentum_scores.loc[date]
        kept = [t for t in tickers if t in row.index and pd.notna(row[t]) and row[t] > 0]
        n_dropped = len(tickers) - len(kept)
        if n_dropped > 0:
            kept.append(defensive_ticker)
        out[date] = kept if kept else [defensive_ticker]
    return pd.Series(out)


# --------------------------------------------------------------------------- #
# 9. SCHEDULED WALK-FORWARD RE-VALIDATION CHECK
# --------------------------------------------------------------------------- #
def scheduled_revalidation_check(
    last_validation_date: str | pd.Timestamp | None,
    revalidation_interval_days: int = 90,
    log_path: str = "revalidation_log.csv",
) -> dict:
    """
    Simple helper to track whether a fresh walk-forward/holdout re-validation
    (Notebook 1's walk-forward cells) is due. The original walk-forward check
    was a one-time run -- this turns it into a periodic cadence so parameter
    drift/overfitting gets caught on an ongoing basis rather than assumed to
    hold forever after a single historical check.

    Parameters
    ----------
    last_validation_date : str/Timestamp/None
        When the walk-forward/holdout validation was last actually run. If
        None, treated as "never run" -- always due.
    revalidation_interval_days : int
        How often re-validation should happen (default ~quarterly).
    log_path : str
        CSV appended to (date, is_due, days_since_last) each time this is
        called with due=True and then acted upon -- call log_revalidation_run()
        below after actually completing a re-validation to reset the clock.

    Returns
    -------
    dict: is_due (bool), days_since_last (int or None), last_validation_date
    """
    today = pd.Timestamp.today().normalize()
    if last_validation_date is None:
        return {"is_due": True, "days_since_last": None, "last_validation_date": None}

    last = pd.Timestamp(last_validation_date)
    days_since = (today - last).days
    return {
        "is_due": days_since >= revalidation_interval_days,
        "days_since_last": days_since,
        "last_validation_date": last,
    }


def log_revalidation_run(log_path: str = "revalidation_log.csv", notes: str = "") -> None:
    """Call this after actually completing a walk-forward re-validation, to reset the clock."""
    import csv, os
    file_exists = os.path.isfile(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "notes"])
        writer.writerow([pd.Timestamp.today().strftime("%Y-%m-%d"), notes])


# --------------------------------------------------------------------------- #
# 10. VALUE-AT-RISK / CONDITIONAL VaR (Epic 2, Story 2.2)
# --------------------------------------------------------------------------- #
def historical_var_cvar(
    returns: pd.Series, confidence: float = 0.95, portfolio_value: float | None = None,
) -> dict:
    """
    Historical (non-parametric) VaR and CVaR from a returns series -- monthly
    or daily, whatever frequency `returns` is in; the output is in the same
    period units unless portfolio_value is supplied for a dollar figure.

    VaR: the loss threshold not expected to be exceeded with `confidence`
    probability, based on the empirical distribution (no normality assumption).
    CVaR (a.k.a. Expected Shortfall): the average loss GIVEN that the VaR
    threshold was breached -- more informative for tail risk since it captures
    how bad the bad case actually is, not just its threshold.

    Returns
    -------
    dict: var_pct, cvar_pct, and var_dollar/cvar_dollar if portfolio_value given.
    """
    r = returns.dropna()
    if r.empty:
        return {"var_pct": np.nan, "cvar_pct": np.nan}

    alpha = 1 - confidence
    var_pct = -r.quantile(alpha)
    tail = r[r <= r.quantile(alpha)]
    cvar_pct = -tail.mean() if len(tail) > 0 else var_pct

    result = {"var_pct": var_pct, "cvar_pct": cvar_pct, "confidence": confidence, "n_obs": len(r)}
    if portfolio_value is not None:
        result["var_dollar"] = var_pct * portfolio_value
        result["cvar_dollar"] = cvar_pct * portfolio_value
    return result


def scenario_shock(
    current_weights: dict, shock_returns: dict, portfolio_value: float,
) -> dict:
    """
    Applies a specified return shock per ticker to current position weights --
    e.g. "what if each holding dropped by its worst historical week's return
    tomorrow." This is a deterministic scenario test, distinct from VaR's
    probabilistic framing -- useful for board/risk-committee-style "what if X
    happens" questions that don't require assuming a return distribution.

    Parameters
    ----------
    current_weights : dict {ticker: weight}, should sum to <= 1.0
    shock_returns : dict {ticker: return}, e.g. {"XLK": -0.15, "SPY": -0.10}
        for "XLK drops 15%, SPY drops 10%". Tickers not in this dict are
        assumed unchanged (shock=0).
    portfolio_value : float

    Returns
    -------
    dict: total_shock_pct, total_shock_dollar, per_ticker_impact (dict)
    """
    per_ticker_impact = {}
    total_shock_pct = 0.0
    for ticker, weight in current_weights.items():
        shock = shock_returns.get(ticker, 0.0)
        impact = weight * shock
        per_ticker_impact[ticker] = {"weight": weight, "shock_applied": shock, "contribution_pct": impact}
        total_shock_pct += impact

    return {
        "total_shock_pct": total_shock_pct,
        "total_shock_dollar": total_shock_pct * portfolio_value,
        "per_ticker_impact": per_ticker_impact,
    }


# --------------------------------------------------------------------------- #
# 11. BENCHMARK COMPARISON FROM SNAPSHOT LOG (Epic 4, Story 4.3)
# --------------------------------------------------------------------------- #
def compare_to_benchmark(name: str, snapshot_dir: str = "data") -> dict:
    """
    Reads write_portfolio_snapshot()'s log (live_signal.py) and returns
    cumulative portfolio return vs. cumulative benchmark return since the
    first snapshot -- a quick "how are we doing vs. SPY" answer without
    needing to open a notebook or replay the trade log.

    Requires the snapshot rows to have portfolio_period_return and
    benchmark_period_return populated (both optional fields -- if a run
    didn't supply them, those rows are skipped in the cumulative product).

    Returns
    -------
    dict: portfolio_cumulative_return, benchmark_cumulative_return,
    outperformance, n_periods, as_of_date. All NaN if insufficient data.
    """
    path = f"{snapshot_dir}/portfolio_snapshot_{name}.csv"
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except FileNotFoundError:
        return {"error": f"No snapshot log found at {path}"}

    df = df.dropna(subset=["portfolio_period_return", "benchmark_period_return"])
    if df.empty:
        return {
            "portfolio_cumulative_return": np.nan, "benchmark_cumulative_return": np.nan,
            "outperformance": np.nan, "n_periods": 0, "as_of_date": None,
            "note": "No rows with both period returns populated yet.",
        }

    port_cum = (1 + df["portfolio_period_return"]).prod() - 1
    bench_cum = (1 + df["benchmark_period_return"]).prod() - 1

    return {
        "portfolio_cumulative_return": port_cum,
        "benchmark_cumulative_return": bench_cum,
        "outperformance": port_cum - bench_cum,
        "n_periods": len(df),
        "as_of_date": df["date"].max(),
    }


# --------------------------------------------------------------------------- #
# 12. EXTERNAL PORTFOLIO CORRELATION CHECK (Epic 6, Story 6.4)
# --------------------------------------------------------------------------- #
def check_external_correlation(
    strategy_returns: pd.Series, other_holdings_returns: dict,
) -> dict:
    """
    Reports correlation between THIS strategy's returns and a user-supplied set
    of your OTHER holdings' returns -- e.g. an existing S&P 500 index fund, a
    bond fund, individual stock positions held outside this system.

    Why this matters: this strategy's own internal correlation penalty
    (BacktestConfig.use_correlation_penalty) only looks at correlation AMONG
    its own picks. It has no visibility into what else you own. A momentum
    sleeve that looks well-diversified internally can still be highly
    correlated with the rest of your net worth (e.g. if it's usually holding
    equity-sector ETFs and you also hold a broad equity index fund elsewhere).

    Parameters
    ----------
    strategy_returns : pd.Series
        This strategy's periodic (e.g. monthly) returns -- from a backtest
        tearsheet, or real returns derived from portfolio_snapshot_*.csv via
        compare_to_benchmark()-style period returns.
    other_holdings_returns : dict {holding_name: pd.Series}
        Periodic returns for each other thing you hold, same frequency and
        aligned index as strategy_returns where possible.

    Returns
    -------
    dict {holding_name: {'correlation': float, 'n_overlapping_periods': int}}
    plus a 'warnings' list flagging any correlation above 0.7 (a common,
    though arbitrary, threshold for "this isn't really diversifying you").
    """
    results = {}
    warnings = []
    for name, other_returns in other_holdings_returns.items():
        aligned = pd.concat([strategy_returns.rename("strategy"), other_returns.rename("other")], axis=1).dropna()
        if len(aligned) < 3:
            results[name] = {"correlation": np.nan, "n_overlapping_periods": len(aligned)}
            continue
        corr = aligned["strategy"].corr(aligned["other"])
        results[name] = {"correlation": corr, "n_overlapping_periods": len(aligned)}
        if pd.notna(corr) and abs(corr) > 0.7:
            warnings.append(
                f"{name}: correlation {corr:.2f} -- this strategy may not be meaningfully "
                f"diversifying your exposure to {name}."
            )

    return {"per_holding": results, "warnings": warnings}


# --------------------------------------------------------------------------- #
# 13. MULTI-LOOKBACK SIGNAL INTEGRATION (Epic 9, Story 9.2)
# --------------------------------------------------------------------------- #
def blend_momentum_scores(
    daily_prices: pd.DataFrame, lookbacks: list[int] = [3, 6, 12], weights: list[float] | None = None,
) -> pd.DataFrame:
    """
    Blends momentum scores across multiple lookback windows instead of relying
    on a single one (e.g. only 12-month). Rationale: different lookbacks
    capture different regimes of momentum -- shorter windows (3mo) react
    faster to regime changes but are noisier; longer windows (12mo) are the
    classic academic momentum window but react slowly to reversals. A blend
    is a reasonable middle ground, not a guaranteed improvement -- validate
    with a real backtest comparison before trusting it over a single lookback
    (see the Notebook 2 demo cell for exactly that comparison).

    Parameters
    ----------
    daily_prices : pd.DataFrame
        Daily close prices, columns = tickers.
    lookbacks : list[int]
        Lookback periods in months (assumes monthly-resampled data; if you
        pass daily data directly, these are interpreted as periods in
        whatever frequency daily_prices is at -- resample to monthly first
        for the conventional "N-month momentum" meaning).
    weights : list[float], optional
        Weight per lookback, same length as `lookbacks`. Defaults to equal
        weighting. Must sum to a positive number (renormalized internally).

    Returns
    -------
    pd.DataFrame
        Blended momentum score per ticker per date, same shape as a single
        calculate_period_returns() output -- drop-in compatible with
        assign_ranks()/get_top_etfs() from the existing signal pipeline.
    """
    if weights is None:
        weights = [1.0] * len(lookbacks)
    if len(weights) != len(lookbacks):
        raise ValueError(f"weights ({len(weights)}) must match lookbacks ({len(lookbacks)}) in length.")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number.")
    norm_weights = [w / total_weight for w in weights]

    blended = None
    for lb, w in zip(lookbacks, norm_weights):
        component = daily_prices.ffill().pct_change(periods=lb) * w
        blended = component if blended is None else blended.add(component, fill_value=0)

    return blended
