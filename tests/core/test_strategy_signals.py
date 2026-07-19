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
    select_absolute_momentum_picks, resolve_strategy_picks,
    resolve_residual_momentum_scores,
)
from momentum_trading.execution.live_signal import resolve_momentum_scores
from momentum_trading.core.functions_quant_extensions import blend_momentum_scores


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
        "correlation_weighted_momentum", "rank_sign_momentum",
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


class TestMultiTimeframeComposite:
    """
    strategy_type == "multi_timeframe_composite" (Epic 2): wires up
    core/functions_quant_extensions.py's blend_momentum_scores(), previously fully coded but
    dead code (zero production call sites). Resamples to monthly FIRST (matching that function's
    own documented "resample to monthly first for the conventional N-month momentum meaning"
    guidance, the same convention resolve_momentum_scores()'s monthly branch already uses), then
    blends across cfg.multi_timeframe_lookbacks.
    """

    def test_matches_a_direct_blend_momentum_scores_call(self):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, strategy_type="multi_timeframe_composite",
                              multi_timeframe_lookbacks=[2, 4])
        result = resolve_strategy_scores(prices, ["A", "B", "C"], cfg, cfg.lookback_period)

        monthly_prices = prices[["A", "B", "C"]].resample("ME").last()
        expected = blend_momentum_scores(monthly_prices, lookbacks=[2, 4], weights=None)
        pd.testing.assert_frame_equal(result, expected)

    def test_differs_from_default_momentum_on_the_same_universe(self):
        prices = _synthetic_prices()
        default_cfg = BacktestConfig(holding_period=1, lookback_period=6)
        composite_cfg = BacktestConfig(holding_period=1, strategy_type="multi_timeframe_composite",
                                        multi_timeframe_lookbacks=[1, 12])
        default_scores = resolve_strategy_scores(prices, ["A", "B", "C"], default_cfg, default_cfg.lookback_period)
        composite_scores = resolve_strategy_scores(prices, ["A", "B", "C"], composite_cfg, composite_cfg.lookback_period)
        # Genuinely different signal construction, not expected to match the single-lookback score.
        assert not default_scores.dropna(how="all").equals(composite_scores.dropna(how="all"))

    def test_generate_strategy_monthly_picks_supports_it(self):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, strategy_type="multi_timeframe_composite",
                              multi_timeframe_lookbacks=[2, 4])
        picks = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)
        assert not picks.empty
        for tickers in picks.values:
            assert len(tickers) == 2


class TestResolveResidualMomentumScores:
    """
    resolve_residual_momentum_scores() (Epic 5): ranks tickers by IDIOSYNCRATIC (benchmark-
    adjusted) trailing return, not raw total return. Per ticker, estimates market-model beta via
    OLS on trailing daily returns against `benchmark`, then residual_score = raw_period_return -
    beta * raw_benchmark_period_return.
    """

    def _beta_vs_alpha_prices(self, seed=3, n=100):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2023-01-01", periods=n)
        bench_returns = rng.normal(0.003, 0.004, n)
        bench_prices = 100 * np.cumprod(1 + bench_returns)
        # A: pure beta=2 leverage on the benchmark, zero idiosyncratic return, a LARGE raw
        # return fully explained by amplified benchmark exposure.
        a_prices = 100 * np.cumprod(1 + 2 * bench_returns)
        # B: beta=1 (tracks the benchmark 1:1) plus a small constant daily idiosyncratic excess,
        # a SMALLER raw return than A, but genuine alpha, not benchmark-explained.
        b_prices = 100 * np.cumprod(1 + bench_returns + 0.001)
        return pd.DataFrame({"BENCH": bench_prices, "A": a_prices, "B": b_prices}, index=dates)

    def test_high_beta_large_raw_return_ranks_below_genuine_alpha(self):
        prices = self._beta_vs_alpha_prices()
        scores = resolve_residual_momentum_scores(
            prices, ["A", "B"], benchmark="BENCH", lookback_period=4, holding_period=1,
        )
        latest = scores.dropna(how="all").iloc[-1]
        # Sanity check on the setup itself: A's RAW return is genuinely larger than B's.
        raw_a = prices["A"].iloc[-1] / prices["A"].iloc[0] - 1
        raw_b = prices["B"].iloc[-1] / prices["B"].iloc[0] - 1
        assert raw_a > raw_b
        # But the RESIDUAL (idiosyncratic) score ranks B above A, the entire point of this
        # strategy_type: A's large raw return is fully explained by its 2x benchmark beta.
        assert latest["B"] > latest["A"]

    def test_missing_benchmark_raises_a_clear_error(self):
        prices = pd.DataFrame({"A": [100, 101, 102]}, index=pd.bdate_range("2023-01-01", periods=3))
        with pytest.raises(ValueError, match="BENCH"):
            resolve_residual_momentum_scores(prices, ["A"], benchmark="BENCH",
                                              lookback_period=1, holding_period=1)


class TestSelectAbsoluteMomentumPicks:
    """
    select_absolute_momentum_picks() (Epic 3): a genuinely different selection mode, no
    cross-sectional ranking/top_n cutoff at all, every ticker whose OWN trailing score is
    positive is held, defensive_ticker alone otherwise.
    """

    def test_all_positive_universe_holds_everything(self):
        scores = pd.Series({"A": 0.05, "B": 0.02, "C": 0.10})
        result = select_absolute_momentum_picks(scores, ["A", "B", "C"], "BIL")
        assert set(result) == {"A", "B", "C"}

    def test_mixed_universe_holds_only_the_positive_trend_subset(self):
        scores = pd.Series({"A": 0.05, "B": -0.02, "C": 0.10})
        result = select_absolute_momentum_picks(scores, ["A", "B", "C"], "BIL")
        assert set(result) == {"A", "C"}

    def test_all_negative_resolves_to_defensive_ticker_only(self):
        scores = pd.Series({"A": -0.05, "B": -0.02, "C": -0.10})
        result = select_absolute_momentum_picks(scores, ["A", "B", "C"], "BIL")
        assert result == ["BIL"]

    def test_none_scores_resolves_to_defensive_ticker_only(self):
        # No score history available yet, conservative fallback, not a crash.
        result = select_absolute_momentum_picks(None, ["A", "B", "C"], "BIL")
        assert result == ["BIL"]

    def test_zero_score_is_not_positive(self):
        # A flat/zero trailing return is not "positive momentum", must not be held.
        scores = pd.Series({"A": 0.0, "B": 0.05})
        result = select_absolute_momentum_picks(scores, ["A", "B"], "BIL")
        assert set(result) == {"B"}

    def test_ignores_tickers_outside_the_given_universe(self):
        scores = pd.Series({"A": 0.05, "EXTRA": 0.99})
        result = select_absolute_momentum_picks(scores, ["A"], "BIL")
        assert result == ["A"]


class TestResolveStrategyPicks:
    """
    resolve_strategy_picks() centralizes the "top_n cross-sectional cutoff vs. absolute
    per-ticker selection" decision, shared by run() (live) and
    generate_strategy_monthly_picks() (backtest), so they can't diverge on it.
    """

    def test_default_strategy_type_matches_get_top_etfs_exactly(self):
        from momentum_trading.execution.live_signal import get_top_etfs
        scores_row = pd.Series({"A": 0.05, "B": 0.02, "C": 0.10})
        ranks_row = scores_row.rank(ascending=False)
        cfg = BacktestConfig(holding_period=1)
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B", "C"], cfg, top_n=2)

        ranks_df = pd.DataFrame([ranks_row])
        expected = get_top_etfs(ranks_df, top_n=2)
        assert set(result) == set(expected)

    def test_absolute_momentum_bypasses_top_n_entirely(self):
        # 3 tickers all positive, top_n=1, but absolute_momentum must hold ALL of them, not cap
        # at top_n, that cross-sectional cutoff is exactly what this strategy doesn't use.
        scores_row = pd.Series({"A": 0.05, "B": 0.02, "C": 0.10})
        ranks_row = scores_row.rank(ascending=False)
        cfg = BacktestConfig(holding_period=1, strategy_type="absolute_momentum", defensive_ticker="BIL")
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B", "C"], cfg, top_n=1)
        assert set(result) == {"A", "B", "C"}

    def test_none_ranks_row_returns_empty_for_default_strategy(self):
        cfg = BacktestConfig(holding_period=1)
        result = resolve_strategy_picks(None, None, ["A", "B"], cfg, top_n=2)
        assert result == []


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
