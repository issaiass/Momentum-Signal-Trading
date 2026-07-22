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
    select_absolute_momentum_picks, resolve_strategy_picks, is_universe_negative,
    resolve_residual_momentum_scores, resolve_path_dependent_momentum_scores,
    resolve_hybrid_multi_factor_scores,
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


class TestResolvePathDependentMomentumScores:
    """
    resolve_path_dependent_momentum_scores() (Epic 6): rewards a smooth, consistent trend over a
    choppy one reaching the same endpoint. path_adjusted_score = raw_score * trend_r_squared,
    where trend_r_squared is the R^2 of a linear fit to log-price over the lookback window.
    """

    def _smooth_vs_choppy_prices(self, n=100, total_return=0.5):
        dates = pd.bdate_range("2023-01-01", periods=n)
        t = np.arange(n)
        smooth = 100 * (1 + total_return) ** (t / (n - 1))
        # Same start/end price as `smooth`, but with a large oscillation superimposed that
        # damps to zero exactly at the last point, so both series land on the IDENTICAL total
        # return, only the PATH differs.
        oscillation = 15 * np.sin(t / 3.0) * (1 - t / (n - 1))
        choppy = smooth + oscillation
        choppy[-1] = smooth[-1]
        return pd.DataFrame({"SMOOTH": smooth, "CHOPPY": choppy}, index=dates)

    def test_smooth_trend_ranks_above_choppy_trend_at_identical_total_return(self):
        prices = self._smooth_vs_choppy_prices()
        scores = resolve_path_dependent_momentum_scores(
            prices, ["SMOOTH", "CHOPPY"], lookback_period=4, holding_period=1,
        )
        latest = scores.dropna(how="all").iloc[-1]

        # Sanity check on the setup itself: identical RAW total return.
        raw_smooth = prices["SMOOTH"].iloc[-1] / prices["SMOOTH"].iloc[0] - 1
        raw_choppy = prices["CHOPPY"].iloc[-1] / prices["CHOPPY"].iloc[0] - 1
        assert raw_smooth == pytest.approx(raw_choppy, abs=1e-9)

        # But the smooth trend's higher R^2 gives it a higher path-adjusted score.
        assert latest["SMOOTH"] > latest["CHOPPY"]

    def test_scopes_to_the_given_tickers(self):
        prices = self._smooth_vs_choppy_prices()
        prices["EXTRA"] = prices["SMOOTH"] * 1.5
        scores = resolve_path_dependent_momentum_scores(
            prices, ["SMOOTH", "CHOPPY"], lookback_period=4, holding_period=1,
        )
        assert "EXTRA" not in scores.columns


class TestResolveStrategyScoresHybridMultiFactorDispatch:
    """
    resolve_strategy_scores()'s "hybrid_multi_factor" branch, LIVE-ONLY: fetches fundamentals
    per ticker via core/fundamentals.py's get_cached_or_fetch_fundamentals() (mocked here, no
    real network/cache), then delegates to resolve_hybrid_multi_factor_scores().
    """

    def test_dispatches_and_fetches_fundamentals_per_ticker(self, monkeypatch):
        import momentum_trading.core.strategy_signals as strategy_signals

        calls = []

        def fake_fetch(ticker, fmp_api_key, eodhd_api_key):
            calls.append((ticker, fmp_api_key, eodhd_api_key))
            return {"pe_ratio": 15, "roe": 0.15}

        monkeypatch.setattr(strategy_signals, "get_cached_or_fetch_fundamentals", fake_fetch)

        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, strategy_type="hybrid_multi_factor")
        result = strategy_signals.resolve_strategy_scores(
            prices, ["A", "B", "C"], cfg, cfg.lookback_period,
            fmp_api_key="fmp-key", eodhd_api_key="eodhd-key",
        )
        assert not result.empty
        assert set(t for t, _, _ in calls) == {"A", "B", "C"}
        assert all(fmp == "fmp-key" and eodhd == "eodhd-key" for _, fmp, eodhd in calls)

    def test_generate_strategy_monthly_picks_raises_not_implemented(self):
        # No point-in-time historical fundamentals exist, backtesting this strategy_type would
        # silently look-ahead bias the result, must fail loudly instead.
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, strategy_type="hybrid_multi_factor")
        with pytest.raises(NotImplementedError, match="hybrid_multi_factor"):
            generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)


class TestResolveHybridMultiFactorScores:
    """
    resolve_hybrid_multi_factor_scores() (Epic 7, LIVE-ONLY): blends a momentum percentile rank
    with a Quality/Value fundamentals composite percentile rank (lower P/E + PEG + Debt-to-Equity,
    higher ROE + Current Ratio score better), simple average of the two. A strong-momentum,
    poor-fundamentals ticker can rank below a moderate-momentum, strong-fundamentals one, proving
    this is a genuine blend, not momentum-only with fundamentals as a tiebreaker.
    """

    def _momentum_prices(self):
        # A: strongest trailing return. B: moderate. C: weakest.
        dates = pd.bdate_range("2023-01-01", periods=90)
        n = len(dates)
        a = np.linspace(100, 150, n)  # +50%
        b = np.linspace(100, 120, n)  # +20%
        c = np.linspace(100, 110, n)  # +10%
        return pd.DataFrame({"A": a, "B": b, "C": c}, index=dates)

    def _fundamentals(self):
        return {
            "A": {"pe_ratio": 80, "peg_ratio": 5.0, "debt_to_equity": 3.0, "roe": 0.02, "current_ratio": 0.8},
            "B": {"pe_ratio": 12, "peg_ratio": 0.8, "debt_to_equity": 0.3, "roe": 0.25, "current_ratio": 2.5},
            "C": {"pe_ratio": 20, "peg_ratio": 1.5, "debt_to_equity": 1.0, "roe": 0.10, "current_ratio": 1.5},
        }

    def test_strong_momentum_poor_fundamentals_ranks_below_moderate_momentum_strong_fundamentals(self):
        prices = self._momentum_prices()
        scores = resolve_hybrid_multi_factor_scores(
            prices, ["A", "B", "C"], lookback_period=3, holding_period=1,
            fundamentals_by_ticker=self._fundamentals(),
        )
        latest = scores.dropna(how="all").iloc[-1]

        # A has the strongest RAW momentum.
        raw_a = prices["A"].iloc[-1] / prices["A"].iloc[0] - 1
        raw_b = prices["B"].iloc[-1] / prices["B"].iloc[0] - 1
        assert raw_a > raw_b

        # But B's vastly better fundamentals push its BLENDED score above A's.
        assert latest["B"] > latest["A"]

    def test_missing_fundamentals_falls_back_to_momentum_only(self):
        # A ticker with no fundamentals data at all (vendor outage, no API key) must not crash
        # or get penalized to zero, it degrades gracefully to momentum-only for that ticker,
        # matching core/fundamentals.py's own established graceful-degradation contract.
        prices = self._momentum_prices()
        scores = resolve_hybrid_multi_factor_scores(
            prices, ["A", "B", "C"], lookback_period=3, holding_period=1,
            fundamentals_by_ticker={"B": self._fundamentals()["B"]},
        )
        latest = scores.dropna(how="all").iloc[-1]
        assert pd.notna(latest["A"])
        assert pd.notna(latest["C"])


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

    def test_nan_ranked_ticker_never_selected_even_to_pad_top_n(self):
        # Real, confirmed bug (found via Epic 2's real-deployed-code verification of the
        # liquidity filter): pandas' nsmallest(n) backfills with NaN rows when fewer than n
        # non-null values exist, so a NaN-ranked ticker (e.g. zeroed out by use_liquidity_filter)
        # could still get selected whenever fewer than top_n tickers had a valid rank.
        scores_row = pd.Series({"A": 0.05, "B": 0.02})
        ranks_row = pd.Series({"A": 1.0, "B": np.nan})  # B liquidity-filtered
        cfg = BacktestConfig(holding_period=1)
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B"], cfg, top_n=2)
        assert result == ["A"]

    def test_all_nan_ranked_returns_empty_not_padded(self):
        scores_row = pd.Series({"A": 0.05, "B": 0.02})
        ranks_row = pd.Series({"A": np.nan, "B": np.nan})
        cfg = BacktestConfig(holding_period=1)
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B"], cfg, top_n=2)
        assert result == []

    def test_negative_universe_cash_filter_forces_empty_picks(self):
        # Epic 6: every ticker non-positive, flag on -> literal cash, not the "least bad" pick.
        scores_row = pd.Series({"A": -0.05, "B": -0.02})
        ranks_row = pd.Series({"A": 1.0, "B": 2.0})
        cfg = BacktestConfig(holding_period=1, use_negative_universe_cash_filter=True)
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B"], cfg, top_n=1)
        assert result == []

    def test_negative_universe_cash_filter_overrides_absolute_momentum_fallback(self):
        # Epic 6's key precedence requirement: absolute_momentum's own
        # select_absolute_momentum_picks() NEVER returns empty (always falls back to
        # defensive_ticker), but the cash filter must win when both trigger at once, forcing
        # a literal empty list instead of [defensive_ticker].
        scores_row = pd.Series({"A": -0.05, "B": -0.02})
        cfg = BacktestConfig(holding_period=1, use_negative_universe_cash_filter=True,
                              strategy_type="absolute_momentum", defensive_ticker="BIL")
        result = resolve_strategy_picks(scores_row, None, ["A", "B"], cfg, top_n=1)
        assert result == []

    def test_negative_universe_cash_filter_unaffected_when_one_score_positive(self):
        # At least one positive score present -> normal selection proceeds unaffected.
        scores_row = pd.Series({"A": -0.05, "B": 0.02})
        ranks_row = pd.Series({"A": 2.0, "B": 1.0})
        cfg = BacktestConfig(holding_period=1, use_negative_universe_cash_filter=True)
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B"], cfg, top_n=1)
        assert result == ["B"]

    def test_negative_universe_cash_filter_off_is_byte_identical(self):
        # Flag off (default) -> byte-identical to today regardless of scores, even an
        # all-negative universe still falls through to normal nsmallest() selection.
        scores_row = pd.Series({"A": -0.05, "B": -0.02})
        ranks_row = pd.Series({"A": 2.0, "B": 1.0})
        cfg = BacktestConfig(holding_period=1)  # flag omitted, default False
        result = resolve_strategy_picks(scores_row, ranks_row, ["A", "B"], cfg, top_n=1)
        assert result == ["B"]


class TestIsUniverseNegative:
    """
    is_universe_negative() (Epic 6): the shared predicate resolve_strategy_picks() uses to
    decide whether to force empty picks, and execution/live_signal.py's run() reuses
    identically to detect (after the fact) whether THIS specific constraint, not an unrelated
    cause, is what emptied `picks`, for the dedicated MARKET_WIDE_NEGATIVE_MOMENTUM_CASH alert.
    """

    def test_all_negative_is_true(self):
        assert is_universe_negative(pd.Series({"A": -0.1, "B": -0.05}), ["A", "B"]) is True

    def test_all_zero_is_true(self):
        # A zero score is not positive, matches select_absolute_momentum_picks()'s convention.
        assert is_universe_negative(pd.Series({"A": 0.0, "B": 0.0}), ["A", "B"]) is True

    def test_one_positive_is_false(self):
        assert is_universe_negative(pd.Series({"A": -0.1, "B": 0.05}), ["A", "B"]) is False

    def test_none_scores_row_is_false(self):
        assert is_universe_negative(None, ["A", "B"]) is False

    def test_all_nan_scores_is_false(self):
        # Nothing to judge either way, a different failure mode (INSUFFICIENT_PRICE_HISTORY).
        assert is_universe_negative(pd.Series({"A": np.nan, "B": np.nan}), ["A", "B"]) is False

    def test_only_scoped_to_given_tickers(self):
        # A ticker outside `tickers` with a positive score must not save the verdict.
        scores_row = pd.Series({"A": -0.1, "B": -0.05, "OTHER": 0.5})
        assert is_universe_negative(scores_row, ["A", "B"]) is True


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

    def test_use_liquidity_filter_off_is_byte_identical(self):
        # daily_volume passed but the flag is off (default), must not change picks at all.
        prices = _synthetic_prices()
        volume = pd.DataFrame({t: np.full(len(prices), 10.0) for t in ["A", "B", "C"]}, index=prices.index)
        cfg = BacktestConfig(holding_period=1, lookback_period=3)
        without_volume = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)
        with_volume = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period,
                                                        top_n=2, daily_volume=volume)
        for date in without_volume.index:
            assert set(without_volume[date]) == set(with_volume[date])

    def test_use_liquidity_filter_on_without_daily_volume_raises(self):
        prices = _synthetic_prices()
        cfg = BacktestConfig(holding_period=1, lookback_period=3, use_liquidity_filter=True)
        with pytest.raises(ValueError, match="daily_volume"):
            generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period, top_n=2)

    def test_illiquid_ticker_never_selected_even_if_top_ranked(self):
        # C is deliberately given the STRONGEST momentum (would be rank 1 every date) but
        # trades at a trailing dollar volume far below min_avg_dollar_volume, it must never
        # appear in monthly_picks despite otherwise being the best signal.
        dates = pd.bdate_range("2023-01-01", periods=400)
        rng = np.random.default_rng(7)
        prices = pd.DataFrame({
            "A": 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, 400)),
            "B": 100 * np.cumprod(1 + rng.normal(0.0002, 0.01, 400)),
            "C": 100 * np.cumprod(1 + rng.normal(0.002, 0.01, 400)),  # strongest drift, illiquid
        }, index=dates)
        volume = pd.DataFrame({
            "A": np.full(400, 100_000.0), "B": np.full(400, 100_000.0),
            "C": np.full(400, 10.0),  # price ~$100-300 * 10 shares is nowhere near $1M/day
        }, index=dates)
        cfg = BacktestConfig(holding_period=1, lookback_period=3, use_liquidity_filter=True,
                              min_avg_dollar_volume=1_000_000.0)
        picks = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period,
                                                  top_n=1, daily_volume=volume)
        assert not picks.empty
        for tickers in picks.values:
            assert "C" not in tickers

    def test_all_illiquid_dates_included_with_explicit_empty_list_not_skipped(self):
        # Real, confirmed parity gap, fixed via Epic 6: a date where scores/ranks were computed
        # but liquidity filtering zeroed every ticker's rank used to be silently SKIPPED (as if
        # no signal existed at all), letting a LATER rebalance's monthly_picks.get(date, [])
        # lookup silently fall back to a STALE prior period's picks instead of correctly seeing
        # "nothing was eligible." Now correctly included with an explicit [] (a real decision).
        prices = _synthetic_prices()
        volume = pd.DataFrame({t: np.full(len(prices), 10.0) for t in ["A", "B", "C"]}, index=prices.index)
        cfg = BacktestConfig(holding_period=1, lookback_period=3, use_liquidity_filter=True,
                              min_avg_dollar_volume=1_000_000.0)  # every ticker illiquid, always
        picks = generate_strategy_monthly_picks(prices, ["A", "B", "C"], cfg, cfg.lookback_period,
                                                  top_n=2, daily_volume=volume)
        assert not picks.empty  # dates WERE recorded, not silently dropped from the Series
        for tickers in picks.values:
            assert tickers == []  # every date correctly recorded as an explicit "hold cash" decision

    def test_negative_universe_cash_filter_produces_explicit_empty_picks(self):
        # Epic 6, backtest side: a synthetic universe with a strong, consistent NEGATIVE drift
        # (every trailing window's return negative) must produce explicit [] entries, not
        # silently skip those dates or fall back to a stale prior period's picks.
        dates = pd.bdate_range("2023-01-01", periods=400)
        rng = np.random.default_rng(11)
        prices = pd.DataFrame({
            "A": 100 * np.cumprod(1 + rng.normal(-0.01, 0.005, 400)),
            "B": 100 * np.cumprod(1 + rng.normal(-0.008, 0.005, 400)),
        }, index=dates)
        cfg = BacktestConfig(holding_period=1, lookback_period=3, use_negative_universe_cash_filter=True)
        picks = generate_strategy_monthly_picks(prices, ["A", "B"], cfg, cfg.lookback_period, top_n=1)
        assert not picks.empty
        assert any(tickers == [] for tickers in picks.values)
