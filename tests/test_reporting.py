"""
tests/test_reporting.py

Covers the investor-facing reporting layer: the portfolio snapshot
(continuous "where things stand" record, independent of the trade log),
rank/signal-score context attached to each trade for later review, benchmark-
relative cumulative return comparison, and correlation against a user's other
(external) holdings. Also confirms the trade-log schema changes made for this
layer (new rank/signal_score columns) don't break the existing FIFO P&L parsing
from the predecessor work.

Run with: pytest tests/test_reporting.py -v
"""
import pytest
import pandas as pd

from momentum_trading.execution.live_signal import (
    write_portfolio_snapshot, get_latest_snapshot, generate_orders, log_orders,
    measure_live_performance,
)
from momentum_trading.backtest.momentum_backtest import BacktestConfig
import momentum_trading.core.functions_quant_extensions as fnx


class TestPortfolioSnapshot:
    """
    write_portfolio_snapshot() auto-computes period returns by comparing to
    the PRIOR row -- these tests confirm the first-ever snapshot correctly
    has no period return (nothing to compare against yet) and that the
    second snapshot's return is computed correctly against the first, by
    hand-verifiable math.
    """

    def test_first_snapshot_has_no_period_return(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_portfolio_snapshot("p1", {}, {}, total_value=1000.0, cash=1000.0, benchmark_ticker="SPY",
                                  snapshot_dir=str(tmp_path))
        latest = get_latest_snapshot("p1", snapshot_dir=str(tmp_path))
        assert latest["total_value"] == 1000.0
        assert latest["portfolio_period_return"] == "" or pd_isna(latest["portfolio_period_return"])

    def test_second_snapshot_computes_period_return(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_portfolio_snapshot("p1", {"XLK": {"shares": 5, "avg_entry_price": 200.0}},
                                  {"XLK": 200.0, "SPY": 550.0}, total_value=1000.0, cash=0.0,
                                  benchmark_ticker="SPY", snapshot_dir=str(tmp_path))
        write_portfolio_snapshot("p1", {"XLK": {"shares": 5, "avg_entry_price": 200.0}},
                                  {"XLK": 220.0, "SPY": 561.0}, total_value=1100.0, cash=0.0,
                                  benchmark_ticker="SPY", snapshot_dir=str(tmp_path))
        latest = get_latest_snapshot("p1", snapshot_dir=str(tmp_path))
        assert latest["portfolio_period_return"] == pytest.approx(0.10, abs=1e-4)
        assert latest["benchmark_period_return"] == pytest.approx(0.02, abs=1e-4)

    def test_get_latest_snapshot_returns_none_if_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert get_latest_snapshot("nonexistent", snapshot_dir=str(tmp_path)) is None


class TestSignalContextInOrders:
    """
    rank/signal_score are optional metadata carried alongside each order
    purely for later human review ("why was XLK bought -- it was rank 2 of
    10"). These tests confirm the values actually flow through the full
    pipeline (generate_orders -> log_orders -> the CSV), default to None
    gracefully when not supplied, and -- critically -- that adding these new
    columns to the trade log schema did NOT break measure_live_performance()'s
    existing FIFO P&L parsing, which reads specific named columns and should
    be unaffected by extra ones.
    """
    def test_rank_and_score_pass_through(self):
        cfg = BacktestConfig(min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
            signal_context={"XLK": {"rank": 2, "signal_score": 0.15}},
        )
        assert orders["XLK"]["rank"] == 2
        assert orders["XLK"]["signal_score"] == 0.15

    def test_missing_context_defaults_to_none(self):
        cfg = BacktestConfig(min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["rank"] is None
        assert orders["XLK"]["signal_score"] is None

    def test_rank_score_reach_the_log(self, tmp_path):
        path = str(tmp_path / "log.csv")
        cfg = BacktestConfig(min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
            signal_context={"XLK": {"rank": 1, "signal_score": 0.22}},
        )
        log_orders(orders, {"XLK": 220.0}, True, path=path, cfg=cfg)
        import pandas as pd
        df = pd.read_csv(path)
        assert df.iloc[0]["rank"] == 1
        assert df.iloc[0]["signal_score"] == pytest.approx(0.22)

    def test_pnl_parsing_unaffected_by_wider_schema(self, tmp_path):
        path = str(tmp_path / "log.csv")
        cfg = BacktestConfig(min_trade_size=1.0)
        buy = generate_orders({}, {"XLK": 1.0}, 1.0, 1000.0, {"XLK": 200.0}, cfg,
                               signal_context={"XLK": {"rank": 1, "signal_score": 0.1}})
        log_orders(buy, {"XLK": 200.0}, True, path=path, cfg=cfg)
        sell = generate_orders({"XLK": 5}, {}, 1.0, 1000.0, {"XLK": 220.0}, cfg)
        log_orders(sell, {"XLK": 220.0}, True, path=path, cfg=cfg)

        result = measure_live_performance("2020-01-01", "2030-01-01", latest_prices={}, log_path=path)
        assert result["realized_pnl"] > 0  # bought low, sold high


class TestBenchmarkComparison:
    """
    compare_to_benchmark() chains consecutive period returns from the
    snapshot log into a cumulative figure -- the math is checked by hand
    ((1.05)*(1.0476)-1 style compounding, not simple addition) since a
    common mistake in this kind of function is summing returns instead of
    compounding them, which understates real cumulative performance.
    """
    def test_no_file_returns_error_dict(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = fnx.compare_to_benchmark("nonexistent", snapshot_dir=str(tmp_path))
        assert "error" in result

    def test_cumulative_return_math(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_portfolio_snapshot("p1", {}, {"SPY": 500.0}, total_value=1000.0, cash=1000.0, benchmark_ticker="SPY",
                                  snapshot_dir=str(tmp_path))
        write_portfolio_snapshot("p1", {}, {"SPY": 510.0}, total_value=1050.0, cash=1050.0, benchmark_ticker="SPY",
                                  snapshot_dir=str(tmp_path))
        write_portfolio_snapshot("p1", {}, {"SPY": 520.0}, total_value=1100.0, cash=1100.0, benchmark_ticker="SPY",
                                  snapshot_dir=str(tmp_path))

        result = fnx.compare_to_benchmark("p1", snapshot_dir=str(tmp_path))
        # portfolio: (1050/1000)*(1100/1050) - 1 = 0.10 exactly
        assert result["portfolio_cumulative_return"] == pytest.approx(0.10, abs=1e-4)
        assert result["n_periods"] == 2


class TestExternalCorrelationCheck:
    """
    check_external_correlation() exists because this strategy's OWN internal
    correlation penalty only looks at correlation among its own picks -- it
    has no visibility into a user's other holdings. These tests confirm the
    warning threshold (0.7) actually discriminates: a deliberately-constructed
    highly-correlated series is flagged, an independent one is not, and too
    little date overlap correctly returns NaN rather than a misleadingly
    precise-looking number from too few data points.
    """
    def test_flags_high_correlation(self):
        import numpy as np
        np.random.seed(0)
        dates = pd.date_range("2024-01-01", periods=24, freq="ME")
        strategy = pd.Series(np.random.normal(0.01, 0.03, 24), index=dates)
        correlated = pd.Series(strategy.values * 0.8 + np.random.normal(0, 0.005, 24), index=dates)

        result = fnx.check_external_correlation(strategy, {"other": correlated})
        assert result["per_holding"]["other"]["correlation"] > 0.7
        assert len(result["warnings"]) == 1

    def test_does_not_flag_low_correlation(self):
        import numpy as np
        np.random.seed(1)
        dates = pd.date_range("2024-01-01", periods=24, freq="ME")
        strategy = pd.Series(np.random.normal(0.01, 0.03, 24), index=dates)
        independent = pd.Series(np.random.normal(0.005, 0.02, 24), index=dates)

        result = fnx.check_external_correlation(strategy, {"other": independent})
        assert abs(result["per_holding"]["other"]["correlation"]) < 0.7
        assert len(result["warnings"]) == 0

    def test_insufficient_overlap_returns_nan(self):
        strategy = pd.Series([0.01, 0.02], index=pd.date_range("2024-01-01", periods=2, freq="ME"))
        other = pd.Series([0.01], index=pd.date_range("2024-06-01", periods=1, freq="ME"))
        result = fnx.check_external_correlation(strategy, {"other": other})
        assert pd.isna(result["per_holding"]["other"]["correlation"])


def pd_isna(x):
    import pandas as pd
    return pd.isna(x)


class TestMultiLookbackBlending:
    """
    blend_momentum_scores() combines multiple lookback
    windows into one signal. The math is checked against a hand-computed
    weighted sum (not just "did it run"), and mismatched lookbacks/weights
    lengths must fail loudly rather than silently truncating or broadcasting
    incorrectly.
    """

    def test_blend_matches_hand_calculation(self):
        import numpy as np
        import momentum_trading.core.functions_quant_extensions as fnx
        dates = pd.date_range("2023-01-31", periods=15, freq="ME")
        prices = pd.DataFrame({"A": np.linspace(100, 150, 15)}, index=dates)

        blended = fnx.blend_momentum_scores(prices, lookbacks=[3, 6], weights=[0.5, 0.5])
        expected = (prices.ffill().pct_change(periods=3) * 0.5 + prices.ffill().pct_change(periods=6) * 0.5)
        common_idx = expected.dropna().index
        assert np.allclose(blended.loc[common_idx], expected.loc[common_idx])

    def test_equal_weight_is_default(self):
        import numpy as np
        import momentum_trading.core.functions_quant_extensions as fnx
        dates = pd.date_range("2023-01-31", periods=15, freq="ME")
        prices = pd.DataFrame({"A": np.linspace(100, 150, 15)}, index=dates)

        blended_default = fnx.blend_momentum_scores(prices, lookbacks=[3, 6])
        blended_explicit = fnx.blend_momentum_scores(prices, lookbacks=[3, 6], weights=[0.5, 0.5])
        common_idx = blended_explicit.dropna().index
        assert np.allclose(blended_default.loc[common_idx], blended_explicit.loc[common_idx])

    def test_mismatched_lengths_raises(self):
        import momentum_trading.core.functions_quant_extensions as fnx
        dates = pd.date_range("2023-01-31", periods=15, freq="ME")
        prices = pd.DataFrame({"A": range(15)}, index=dates)
        with pytest.raises(ValueError, match="weights"):
            fnx.blend_momentum_scores(prices, lookbacks=[3, 6], weights=[1.0])
