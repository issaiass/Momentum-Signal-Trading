"""
tests/test_epic8_10_safety.py

Covers Epic 8 (broker connection resilience -- see test_live_signal.py's
TestIBKRConnectionRetry for the connection-retry test itself, kept there
since it needs the live_signal.py IBKR mocking pattern) and Epic 10
(additional execution safety checks: dollar drawdown breaker, slippage
tolerance, stale price feed protection, time-based stops).

Run with: pytest tests/test_epic8_10_safety.py -v
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.backtest.momentum_backtest import BacktestConfig, run_custom_backtest
from momentum_trading.execution.live_signal import check_slippage_tolerance, check_price_staleness


class TestDollarDrawdownBreaker:
    """
    max_dollar_drawdown is INDEPENDENT of max_portfolio_drawdown_pct -- either
    can trip a halt on its own. These tests (see also
    test_daily_runner.py::TestCircuitBreaker for the original % version)
    confirm the dollar breaker fires correctly even when the % breaker is
    disabled, and vice versa.
    """

    def test_config_validates_negative_value(self):
        with pytest.raises(ValueError, match="max_dollar_drawdown"):
            BacktestConfig(max_dollar_drawdown=-100)

    def test_dollar_breaker_trips_independently(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import check_circuit_breaker

        # % breaker disabled (0.0), only the dollar breaker active
        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.0, max_dollar_drawdown=100.0)
        assert check_circuit_breaker("p", 1000.0, cfg) is False  # sets peak
        assert check_circuit_breaker("p", 850.0, cfg) is True    # -$150 > $100 threshold

    def test_neither_breaker_trips_under_threshold(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import check_circuit_breaker

        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.50, max_dollar_drawdown=1000.0)
        assert check_circuit_breaker("p", 1000.0, cfg) is False
        assert check_circuit_breaker("p", 950.0, cfg) is False  # -$50/-5%, under both


class TestSlippageToleranceCheck:
    """
    check_slippage_tolerance() cannot un-fill an already-executed order -- its
    only job is to make an excessive deviation VISIBLE (flagged in the result
    dict + logged) rather than silently accepted as if the fill were fine.
    """

    def test_within_tolerance_not_flagged(self):
        result = check_slippage_tolerance(expected_price=100.0, actual_price=100.5, tolerance_pct=0.02)
        assert result["exceeded"] is False
        assert result["deviation_pct"] == pytest.approx(0.005)

    def test_exceeds_tolerance_flagged(self):
        result = check_slippage_tolerance(expected_price=100.0, actual_price=105.0, tolerance_pct=0.02)
        assert result["exceeded"] is True
        assert result["deviation_pct"] == pytest.approx(0.05)

    def test_zero_price_does_not_crash(self):
        # Guards against a division-by-zero if expected/actual price is ever 0
        # (e.g. a data gap) -- should report "not flagged", not raise.
        result = check_slippage_tolerance(expected_price=0.0, actual_price=100.0, tolerance_pct=0.02)
        assert result["exceeded"] is False

    def test_config_validates_out_of_range(self):
        with pytest.raises(ValueError, match="max_slippage_tolerance_pct"):
            BacktestConfig(max_slippage_tolerance_pct=1.5)


class TestStalePriceFeedProtection:
    """
    check_price_staleness() must correctly distinguish fresh data (today's
    date) from stale data (an old cached/frozen feed) and from a total fetch
    failure (empty DataFrame) -- each should be handled distinctly, not
    conflated into one generic "bad data" case.
    """

    def test_fresh_data_not_stale(self):
        fresh = pd.DataFrame({"SPY": [500]}, index=[pd.Timestamp.today().normalize()])
        result = check_price_staleness(fresh, max_staleness_minutes=1440)
        assert result["is_stale"] is False

    def test_old_data_flagged_stale(self):
        stale = pd.DataFrame({"SPY": [500]}, index=[pd.Timestamp.today().normalize() - pd.Timedelta(days=10)])
        result = check_price_staleness(stale, max_staleness_minutes=1440)
        assert result["is_stale"] is True
        assert result["staleness_days"] >= 9

    def test_empty_dataframe_flagged_stale(self):
        # A total fetch failure (empty result) must be treated as unsafe to
        # trade on, not silently ignored as "0 days stale."
        result = check_price_staleness(pd.DataFrame(), max_staleness_minutes=1440)
        assert result["is_stale"] is True

    def test_config_validates_non_positive(self):
        with pytest.raises(ValueError, match="max_price_staleness_minutes"):
            BacktestConfig(max_price_staleness_minutes=0)


class TestTimeBasedStops:
    """
    max_holding_days must force an exit purely based on TIME, independent of
    price -- verified using a perfectly FLAT price series where the
    price-based stop_loss_pct could never trigger on its own, isolating the
    time-based mechanism from the price-based one.
    """

    def test_config_validates_non_positive(self):
        with pytest.raises(ValueError, match="max_holding_days"):
            BacktestConfig(max_holding_days=0)

    def test_forces_exit_on_flat_prices(self, tmp_path):
        np.random.seed(0)
        tickers = ["SPY", "QQQ"]
        dates = pd.bdate_range("2020-01-01", "2020-12-31")
        data = {t: np.full(len(dates), 100.0) for t in tickers}  # perfectly flat -- price stop never fires
        daily_prices = pd.DataFrame(data, index=dates)
        picks = pd.Series({d: tickers for d in daily_prices.resample("ME").last().index})

        log_path = str(tmp_path / "time_stop_test.txt")
        run_custom_backtest(picks, daily_prices, initial_capital=1000.0, holding_period=1,
                             max_holding_days=30, log_file_path=log_path)

        with open(log_path) as f:
            log = f.read()
        assert "TIME-STOP" in log
        # every TIME-STOP entry should report >= 30 days held
        import re
        held_days = [int(m) for m in re.findall(r"held (\d+) days", log)]
        assert len(held_days) > 0
        assert all(d >= 30 for d in held_days)

    def test_disabled_by_default_no_time_stop_fires(self, tmp_path):
        np.random.seed(0)
        tickers = ["SPY", "QQQ"]
        dates = pd.bdate_range("2020-01-01", "2020-06-30")
        data = {t: np.full(len(dates), 100.0) for t in tickers}
        daily_prices = pd.DataFrame(data, index=dates)
        picks = pd.Series({d: tickers for d in daily_prices.resample("ME").last().index})

        log_path = str(tmp_path / "no_time_stop_test.txt")
        run_custom_backtest(picks, daily_prices, initial_capital=1000.0, holding_period=6,
                             log_file_path=log_path)  # max_holding_days=None (default)

        with open(log_path) as f:
            log = f.read()
        assert "TIME-STOP" not in log
