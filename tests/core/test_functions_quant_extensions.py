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
)


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
