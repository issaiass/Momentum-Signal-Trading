"""
tests/core/test_functions_quant_extensions.py

Covers the new live-performance-report wiring added to core/functions_quant_extensions.py,
since_inception_performance(), daily_window_comparison(), monthly_window_comparison(). None of
functions.py/functions_quant_extensions.py had any prior pytest coverage at all (only exercised
via notebooks), these tests are scoped to the NEW additions only, not a retroactive audit of
the pre-existing module.

Run with: pytest tests/core/test_functions_quant_extensions.py -v
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.core.functions_quant_extensions import (
    since_inception_performance, daily_window_comparison, monthly_window_comparison,
    compute_drawdown_episodes, run_walk_forward_lookback_search,
)
from momentum_trading.backtest.momentum_backtest import BacktestConfig


def _write_snapshot_csv(tmp_path, name, dates, port_returns, bench_returns):
    df = pd.DataFrame({
        "date": dates, "total_value": 1000.0, "cash": 0.0, "positions_value": 0.0,
        "unrealized_pnl": 0.0, "n_positions": 1, "positions_detail": "",
        "benchmark_ticker": "SPY", "benchmark_price": 500.0,
        "portfolio_period_return": port_returns, "benchmark_period_return": bench_returns,
    })
    path = tmp_path / f"portfolio_snapshot_{name}.csv"
    df.to_csv(path, index=False)
    return str(tmp_path)


class TestSinceInceptionPerformance:
    def test_missing_snapshot_log_returns_error(self, tmp_path):
        result = since_inception_performance("nonexistent", snapshot_dir=str(tmp_path))
        assert "error" in result

    def test_short_history_computes_return_stats_but_not_ratios(self, tmp_path):
        # Sharpe/Sortino need >= 252 daily rows (functions.py's own threshold), a 30-row
        # history must gracefully report None for those, not raise or fabricate a number.
        rng = np.random.default_rng(0)
        n = 30
        dates = pd.date_range("2026-06-01", periods=n, freq="B")
        snapshot_dir = _write_snapshot_csv(
            tmp_path, "p1", dates, rng.normal(0.0005, 0.01, n), rng.normal(0.0004, 0.009, n),
        )
        result = since_inception_performance("p1", snapshot_dir=snapshot_dir)
        assert result["sharpe_ratio"] is None
        assert result["sortino_ratio"] is None
        assert result["total_return"] is not None
        assert result["cagr"] is not None
        assert result["max_drawdown"] is not None
        assert result["std_dev"] is not None
        assert result["inception_date"] == dates[0]
        assert result["as_of_date"] == dates[-1]

    def test_total_return_matches_hand_calculation(self, tmp_path):
        dates = pd.date_range("2026-06-01", periods=3, freq="B")
        # +10%, then -5%, then +2% -> (1.10 * 0.95 * 1.02) - 1
        port_returns = [0.10, -0.05, 0.02]
        bench_returns = [0.01, 0.01, 0.01]
        snapshot_dir = _write_snapshot_csv(tmp_path, "p1", dates, port_returns, bench_returns)
        result = since_inception_performance("p1", snapshot_dir=snapshot_dir)
        expected = (1.10 * 0.95 * 1.02) - 1
        assert result["total_return"] == pytest.approx(expected)

    def test_no_rows_with_period_returns_yields_error(self, tmp_path):
        dates = pd.date_range("2026-06-01", periods=2, freq="B")
        df = pd.DataFrame({
            "date": dates, "total_value": 1000.0, "cash": 0.0, "positions_value": 0.0,
            "unrealized_pnl": 0.0, "n_positions": 0, "positions_detail": "",
            "benchmark_ticker": "SPY", "benchmark_price": 500.0,
            "portfolio_period_return": [None, None], "benchmark_period_return": [None, None],
        })
        path = tmp_path / "portfolio_snapshot_p1.csv"
        df.to_csv(path, index=False)
        result = since_inception_performance("p1", snapshot_dir=str(tmp_path))
        assert "error" in result


class TestDailyWindowComparison:
    def test_missing_snapshot_log_returns_error(self, tmp_path):
        result = daily_window_comparison("nonexistent", snapshot_dir=str(tmp_path))
        assert "error" in result

    def test_omits_windows_without_enough_history(self, tmp_path):
        # Only 3 days of history, "2 Week"/"3 Week" must be absent, not NaN or fabricated.
        dates = pd.date_range("2026-06-01", periods=3, freq="D")
        snapshot_dir = _write_snapshot_csv(tmp_path, "p1", dates, [0.01, 0.01, 0.01], [0.005, 0.005, 0.005])
        result = daily_window_comparison("p1", snapshot_dir=snapshot_dir)
        assert "2 Week" not in result
        assert "3 Week" not in result

    def test_one_day_window_matches_hand_calculation(self, tmp_path):
        dates = pd.date_range("2026-06-01", periods=2, freq="D")
        snapshot_dir = _write_snapshot_csv(tmp_path, "p1", dates, [0.10, 0.05], [0.01, 0.02])
        result = daily_window_comparison("p1", snapshot_dir=snapshot_dir)
        # "1 Day" compares the latest snapshot back to the prior one: (1.10*1.05)/1.10 - 1 = 0.05
        assert result["1 Day"]["portfolio"] == pytest.approx(0.05)
        assert result["1 Day"]["benchmark"] == pytest.approx(0.02)

    def test_duplicate_same_day_rows_do_not_raise(self, tmp_path):
        # Real, confirmed regression (found verifying send_daily end-to-end against real
        # accumulated data, not synthetic): write_portfolio_snapshot() writes one row per RUN,
        # not per calendar day, so more than one manual run on the same day (routine during
        # testing, or a retry after a crash) produces multiple rows sharing a date. Before the
        # fix, port_cgi.loc[latest_date] returned a Series (multiple matches) instead of a
        # scalar, raising TypeError inside float(). Two rows share 2026-06-02.
        dates = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-02"])
        snapshot_dir = _write_snapshot_csv(tmp_path, "p1", dates, [0.10, 0.05, 0.02], [0.01, 0.02, 0.01])
        result = daily_window_comparison("p1", snapshot_dir=snapshot_dir)
        assert "error" not in result
        # Dedup keeps only the LAST row for the duplicated 2026-06-02 date (the 0.05 row is
        # dropped entirely, not compounded), so "1 Day" (2026-06-02 vs. 2026-06-01) is just the
        # surviving row's own return: (1.10*1.02)/1.10 - 1 = 0.02
        assert result["1 Day"]["portfolio"] == pytest.approx(0.02)


class TestMonthlyWindowComparison:
    def test_missing_snapshot_log_returns_error(self, tmp_path):
        result = monthly_window_comparison("nonexistent", snapshot_dir=str(tmp_path))
        assert "error" in result

    def test_only_available_windows_present_for_short_history(self, tmp_path):
        # ~2 months of history, "1 Month" should be present, "1 Year" must not be.
        rng = np.random.default_rng(1)
        n = 40
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        snapshot_dir = _write_snapshot_csv(
            tmp_path, "p1", dates, rng.normal(0.0005, 0.01, n), rng.normal(0.0004, 0.009, n),
        )
        result = monthly_window_comparison("p1", snapshot_dir=snapshot_dir)
        assert "1 Month" in result
        assert "1 Year" not in result
        assert "as_of_date" in result

    def test_does_not_raise_against_short_live_history(self, tmp_path):
        # Regression guard for the exact bug found during development: functions.py's
        # trailing_returns()/return_period_dates() raised KeyError against short daily-snapshot
        # data (the "Since Inception" window's lookback fell outside the fetched market-calendar
        # schedule). monthly_window_comparison() deliberately doesn't use that machinery,
        # this just confirms it never raises, for histories from 2 rows up to a few months.
        for n in (2, 5, 15, 40):
            dates = pd.date_range("2026-01-01", periods=n, freq="B")
            snapshot_dir = _write_snapshot_csv(tmp_path, f"p_{n}", dates, [0.01] * n, [0.005] * n)
            result = monthly_window_comparison(f"p_{n}", snapshot_dir=snapshot_dir)
            assert "error" not in result

    def test_duplicate_same_day_rows_do_not_raise(self, tmp_path):
        # Same real, confirmed regression as TestDailyWindowComparison's own test: multiple
        # snapshot rows sharing a date (write_portfolio_snapshot() writes one row per RUN, not
        # per calendar day) used to crash port_cgi.loc[latest_date] (returns a Series, not a
        # scalar, when the date is duplicated).
        dates = pd.date_range("2026-01-01", periods=40, freq="B").tolist()
        dates[-1] = dates[-2]  # duplicate the LAST date, same as a same-day re-run
        snapshot_dir = _write_snapshot_csv(tmp_path, "p1", dates, [0.001] * 40, [0.0005] * 40)
        result = monthly_window_comparison("p1", snapshot_dir=snapshot_dir)
        assert "error" not in result
        assert "1 Month" in result


class TestComputeDrawdownEpisodes:
    """
    compute_drawdown_episodes() (Epic 13, "Real Historical Crash-Period Stress Test" plan): the
    recovery-time counterpart to _build_report()'s single-scalar max drawdown, one row per
    peak-to-new-high episode.
    """

    def test_monotonically_increasing_series_has_zero_episodes(self):
        s = pd.Series([1.0, 1.05, 1.1, 1.2, 1.3], index=pd.date_range("2026-01-01", periods=5))
        result = compute_drawdown_episodes(s)
        assert result.empty
        assert list(result.columns) == [
            "peak_date", "trough_date", "trough_pct", "recovery_date", "recovery_days",
        ]

    def test_two_episodes_both_recover(self):
        dates = pd.date_range("2026-01-01", periods=9)
        # peak 1.1 (day1) -> trough 0.9 (day3) -> recovers day6 (1.15, a new high)
        # peak 1.15 (day6) -> trough 1.1 (day7) -> recovers day8 (1.2, a new high)
        values = [1.0, 1.1, 1.05, 0.9, 0.95, 1.0, 1.15, 1.1, 1.2]
        s = pd.Series(values, index=dates)
        result = compute_drawdown_episodes(s)

        assert len(result) == 2

        first = result.iloc[0]
        assert first["peak_date"] == dates[1]
        assert first["trough_date"] == dates[3]
        assert first["trough_pct"] == pytest.approx(0.9 / 1.1 - 1.0)
        assert first["recovery_date"] == dates[6]
        assert first["recovery_days"] == (dates[6] - dates[3]).days

        second = result.iloc[1]
        assert second["peak_date"] == dates[6]
        assert second["trough_date"] == dates[7]
        assert second["trough_pct"] == pytest.approx(1.1 / 1.15 - 1.0)
        assert second["recovery_date"] == dates[8]
        assert second["recovery_days"] == (dates[8] - dates[7]).days

    def test_still_in_drawdown_at_series_end_has_no_recovery_date(self):
        dates = pd.date_range("2026-01-01", periods=5)
        # peak 1.1 (day1), trough 0.8 (day4), never makes a new high before the series ends
        values = [1.0, 1.1, 1.0, 0.9, 0.8]
        s = pd.Series(values, index=dates)
        result = compute_drawdown_episodes(s)

        assert len(result) == 1
        episode = result.iloc[0]
        assert episode["peak_date"] == dates[1]
        assert episode["trough_date"] == dates[4]
        assert episode["trough_pct"] == pytest.approx(0.8 / 1.1 - 1.0)
        assert pd.isna(episode["recovery_date"])
        assert pd.isna(episode["recovery_days"])

    def test_empty_series_returns_empty_dataframe(self):
        s = pd.Series([], dtype=float)
        result = compute_drawdown_episodes(s)
        assert result.empty


class TestRunWalkForwardLookbackSearch:
    """
    run_walk_forward_lookback_search() (Epic 15, "Real Out-of-Sample Strategy Validation"
    plan): the real-engine counterpart to walk_forward_lookback_holding() above, wired through
    generate_strategy_monthly_picks()/run_custom_backtest() (this project's actual signal +
    execution pipeline) instead of injected generic callables.
    """

    def _trending_prices(self, years=7, seed=41):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2015-01-01", periods=int(years * 252))
        n = len(dates)
        a = 100 * np.cumprod(1 + rng.normal(0.0006, 0.008, n))  # strong, consistent up-trend
        b = 100 * np.cumprod(1 + rng.normal(0.0002, 0.008, n))  # modest up-trend
        c = 100 * np.cumprod(1 + rng.normal(0.0000, 0.008, n))  # flat/noisy
        return pd.DataFrame({"A": a, "B": b, "C": c}, index=dates)

    def _cfg(self):
        # use_regime_filter=False avoids needing a separate benchmark ticker; vol targeting
        # pinned at 100% so the test isolates the walk-forward SELECTION mechanics, matching
        # Epic 14's own "target_portfolio_vol=10.0 to effectively disable throttling" precedent.
        return BacktestConfig(
            use_regime_filter=False,
            top_n=1,
            holding_period=1,
            initial_capital=1000.0,
            commission=0.0,
            target_portfolio_vol=10.0,
            min_gross_exposure=1.0,
            max_gross_exposure=1.0,
            stop_loss_pct=0.95,
        )

    def test_produces_multiple_folds_with_correct_columns(self):
        prices = self._trending_prices()
        result = run_walk_forward_lookback_search(
            prices, ["A", "B", "C"], self._cfg(),
            lookback_candidates=[3, 6, 12],
            train_years=3, test_years=1, step_years=1,
        )
        assert len(result) >= 2
        assert list(result.columns) == [
            "fold_start", "train_end", "test_end", "chosen_lookback",
            "train_Sharpe", "test_Sharpe", "test_CAGR",
        ]
        assert result["chosen_lookback"].isin([3, 6, 12]).all()

    def test_empty_for_insufficient_history(self):
        dates = pd.bdate_range("2020-01-01", periods=100)
        prices = pd.DataFrame({"A": 100 * np.cumprod(1 + np.full(100, 0.001))}, index=dates)
        result = run_walk_forward_lookback_search(
            prices, ["A"], self._cfg(), lookback_candidates=[3, 6],
            train_years=3, test_years=1, step_years=1,
        )
        assert result.empty
        assert list(result.columns) == [
            "fold_start", "train_end", "test_end", "chosen_lookback",
            "train_Sharpe", "test_Sharpe", "test_CAGR",
        ]

    def test_strongly_trending_ticker_produces_sensible_real_results(self):
        # A's drift dominates B/C's at every reasonable lookback, so the real pipeline
        # (regime filter off, vol targeting disabled) should consistently select it and
        # produce real, finite Sharpe values, not silently NaN/empty folds throughout, a
        # smoke test that the real generate_strategy_monthly_picks()/run_custom_backtest()
        # wiring is actually driving the result.
        prices = self._trending_prices()
        result = run_walk_forward_lookback_search(
            prices, ["A", "B", "C"], self._cfg(),
            lookback_candidates=[3, 6, 12],
            train_years=3, test_years=1, step_years=1,
        )
        assert not result.empty
        assert result["train_Sharpe"].notna().all()
        assert result["test_Sharpe"].notna().any()

    def _cfg_weekly(self):
        # Same isolation precedent as _cfg(), but holding_period < 1 (Epic 16, "Real
        # Out-of-Sample Validation - Short-Term (Weekly) Momentum Regime" plan): exercises the
        # week-quarter lookback_period branch (round(x * 4) in resolve_momentum_scores()),
        # which run_walk_forward_lookback_search() had zero test coverage for before this.
        return BacktestConfig(
            use_regime_filter=False,
            top_n=1,
            holding_period=0.25,
            initial_capital=1000.0,
            commission=0.0,
            target_portfolio_vol=10.0,
            min_gross_exposure=1.0,
            max_gross_exposure=1.0,
            stop_loss_pct=0.95,
        )

    def test_weekly_regime_produces_multiple_folds_with_sensible_real_results(self):
        # lookback_candidates in week-quarter units (0.5/1.0/1.5 -> round(x*4) = 2/4/6 weeks),
        # the same convention config.yaml's own portfolio2 risk_overrides documents.
        prices = self._trending_prices()
        result = run_walk_forward_lookback_search(
            prices, ["A", "B", "C"], self._cfg_weekly(),
            lookback_candidates=[0.5, 1.0, 1.5],
            train_years=3, test_years=1, step_years=1,
        )
        assert len(result) >= 2
        assert list(result.columns) == [
            "fold_start", "train_end", "test_end", "chosen_lookback",
            "train_Sharpe", "test_Sharpe", "test_CAGR",
        ]
        assert result["chosen_lookback"].isin([0.5, 1.0, 1.5]).all()
        assert result["train_Sharpe"].notna().all()
        assert result["test_Sharpe"].notna().any()
