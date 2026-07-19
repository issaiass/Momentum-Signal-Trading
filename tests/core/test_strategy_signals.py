"""
tests/core/test_strategy_signals.py

Covers core/strategy_signals.py, the selectable momentum strategy_type dispatcher
(docs/MOMENTUM_STRATEGIES.md). resolve_strategy_scores() is the LIVE call site's router;
generate_strategy_monthly_picks() is the BACKTEST-facing historical-picks helper. Both share
the SAME per-strategy scoring logic, so live and backtest can't diverge on which tickers get
picked for a given strategy_type, only the shared regression test each new epic adds here
confirms that parity directly.

Run with: pytest tests/core/test_strategy_signals.py -v
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.backtest.momentum_backtest import BacktestConfig
from momentum_trading.core.strategy_signals import (
    resolve_strategy_scores, generate_strategy_monthly_picks,
)
from momentum_trading.execution.live_signal import resolve_momentum_scores


def _synthetic_prices(n=400, seed=5):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-01", periods=n)
    data = {}
    for name in ["A", "B", "C"]:
        idio = rng.normal(0.0005, 0.01, n)
        data[name] = 100 * np.cumprod(1 + idio)
    return pd.DataFrame(data, index=dates)


class TestResolveStrategyScores:
    """
    resolve_strategy_scores() must be a byte-identical pass-through to the existing
    resolve_momentum_scores() for every strategy_type that only affects sizing/exposure, not
    ranking (the base "momentum" signal and its 4 preset-only siblings from Epic 1).
    """

    def test_default_momentum_matches_resolve_momentum_scores_directly(self):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, lookback_period=3)
        expected = resolve_momentum_scores(prices, cfg.lookback_period, cfg.holding_period,
                                            cfg.skip_month_guardrail)
        result = resolve_strategy_scores(prices, ["A", "B", "C"], cfg, cfg.lookback_period)
        pd.testing.assert_frame_equal(result, expected)

    @pytest.mark.parametrize("strategy_type", [
        "momentum", "relative_momentum", "dual_momentum", "volatility_scaled_momentum",
        "correlation_weighted_momentum",
    ])
    def test_sizing_only_strategy_types_all_match_the_default(self, strategy_type):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, lookback_period=3, strategy_type=strategy_type)
        expected = resolve_momentum_scores(prices, cfg.lookback_period, cfg.holding_period,
                                            cfg.skip_month_guardrail)
        result = resolve_strategy_scores(prices, ["A", "B", "C"], cfg, cfg.lookback_period)
        pd.testing.assert_frame_equal(result, expected)

    def test_scopes_to_the_given_tickers_not_the_whole_daily_prices(self):
        # A ticker present in daily_prices but NOT in the tickers list must never influence or
        # appear in the ranking, mirrors run()'s existing extra_price_tickers scoping guarantee.
        prices = _synthetic_prices()
        prices["EXTRA"] = prices["A"] * 1.5
        cfg = BacktestConfig(holding_period=1, lookback_period=3)
        result = resolve_strategy_scores(prices, ["A", "B", "C"], cfg, cfg.lookback_period)
        assert "EXTRA" not in result.columns


class TestGenerateStrategyMonthlyPicks:
    """
    generate_strategy_monthly_picks(), the backtest-facing historical-picks helper, must produce
    the same shape/selection a hand-rolled notebook-style loop would for the default strategy.
    """

    def test_matches_hand_rolled_history_for_default_momentum(self):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, lookback_period=3)
        picks = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)

        assert not picks.empty
        for date, tickers in picks.items():
            assert len(tickers) == 2
            assert set(tickers).issubset({"A", "B", "C"})

        # Hand-verify one specific date against the underlying scores directly.
        scores = resolve_momentum_scores(prices, cfg.lookback_period, cfg.holding_period, False).dropna(how="all")
        first_date = scores.index[0]
        if first_date in picks.index:
            expected = scores.loc[first_date].dropna().rank(ascending=False).nsmallest(2).index.tolist()
            assert set(picks.loc[first_date]) == set(expected)

    def test_empty_or_all_nan_dates_are_skipped_not_crashed_on(self):
        # A short price history's first few resampled rows are all-NaN (pct_change lookback
        # not satisfied yet), must be skipped cleanly, not raise or produce an empty-list pick.
        prices = _synthetic_prices(n=60)
        cfg = BacktestConfig(holding_period=1, lookback_period=3)
        picks = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)
        for tickers in picks.values:
            assert len(tickers) > 0
