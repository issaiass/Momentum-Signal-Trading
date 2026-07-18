"""
tests/test_momentum_backtest.py

Covers the core backtest engine: BacktestConfig validation, the shared
resolve_target_weights() sizing path, and the crash-protection mechanisms
(circuit breaker, correlation spike detection, liquidity stress handling).

Formalizes the ad hoc smoke tests run during implementation into a repeatable
suite. Run with: pytest tests/test_momentum_backtest.py -v

See TESTING.md for how to run the full suite, fixture explanations, and how
to interpret a failure.
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.backtest.momentum_backtest import (
    BacktestConfig, run_custom_backtest, resolve_target_weights,
)


class TestBacktestConfigValidation:
    """
    Guards against BacktestConfig accepting nonsensical parameter combinations
    that would silently produce wrong sizing/risk behavior instead of failing
    loudly at construction time, this is the __post_init__ validation added
    specifically because earlier bugs in this project came from bad config
    values propagating unnoticed into a live-money-adjacent code path.
    """

    def test_default_config_is_valid(self):
        # Sanity baseline: if this ever raises, every other test in the suite
        # that relies on BacktestConfig() defaults would fail for the wrong reason.
        BacktestConfig()  # should not raise

    def test_min_exceeds_max_gross_exposure_raises(self):
        # A min exposure floor above the max ceiling is contradictory and would
        # make the vol-targeting/regime-scaling logic produce an impossible clamp.
        with pytest.raises(ValueError, match="min_gross_exposure"):
            BacktestConfig(min_gross_exposure=0.9, max_gross_exposure=0.5)

    def test_invalid_stop_loss_pct_raises(self):
        # stop_loss_pct is a fraction (0,1); >1 would mean "stop out after losing
        # more than 100%", which is meaningless and signals a units mistake
        # (e.g. someone passed 15 instead of 0.15).
        with pytest.raises(ValueError, match="stop_loss_pct"):
            BacktestConfig(stop_loss_pct=1.5)

    def test_invalid_top_n_raises(self):
        # top_n selects how many top-momentum names actually get held each
        # rebalance, 0 or negative is meaningless (an empty or undefined
        # portfolio) and signals a config mistake, same class of bug as an
        # inverted min/max exposure.
        with pytest.raises(ValueError, match="top_n"):
            BacktestConfig(top_n=0)

    def test_invalid_lookback_period_raises(self):
        # lookback_period is the trailing-months window used to rank tickers by
        # momentum, 0 or negative is meaningless (no return window to rank on),
        # same class of bug as an invalid top_n.
        with pytest.raises(ValueError, match="lookback_period"):
            BacktestConfig(lookback_period=0)

    def test_zero_or_negative_holding_period_raises(self):
        # holding_period must be > 0, 0 or negative months between rebalances is
        # meaningless. This is the ONLY hard validation on holding_period: fractional
        # values (including ones faster than weekly) are legitimate, well-defined
        # schedules, just flagged via a non-blocking WARNING elsewhere (daily_runner.py),
        # not rejected here.
        with pytest.raises(ValueError, match="holding_period"):
            BacktestConfig(holding_period=0)
        with pytest.raises(ValueError, match="holding_period"):
            BacktestConfig(holding_period=-0.5)

    def test_fractional_holding_period_accepted(self):
        # Confirms the int -> float type change didn't just silently truncate/coerce,
        # 0.25 (weekly) and 0.75 (every 3 weeks) must construct successfully and keep
        # their exact fractional value.
        assert BacktestConfig(holding_period=0.25).holding_period == 0.25
        assert BacktestConfig(holding_period=0.75).holding_period == 0.75

    def test_negative_drift_threshold_raises(self):
        # A negative drift threshold would make the "skip trivial rebalances"
        # cost-control logic fire on EVERY rebalance instead of none, silently
        # defeating its own purpose rather than erroring.
        with pytest.raises(ValueError, match="drift_threshold"):
            BacktestConfig(drift_threshold=-0.01)

    def test_negative_initial_capital_raises(self):
        # Guards against a units/sign mistake propagating into every downstream
        # dollar calculation in the backtest loop.
        with pytest.raises(ValueError, match="initial_capital"):
            BacktestConfig(initial_capital=-100)

    def test_run_custom_backtest_rejects_invalid_override(self, synthetic_monthly_picks, synthetic_daily_prices):
        # The run_custom_backtest() wrapper builds a BacktestConfig internally
        # from **kwargs, this confirms validation actually fires through that
        # path too, not just when constructing BacktestConfig directly (an
        # earlier version of this wrapper used setattr() after construction,
        # which silently BYPASSED __post_init__ validation entirely).
        with pytest.raises(ValueError):
            run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices, stop_loss_pct=2.0)


class TestBacktestRuns:
    """
    Confirms each optional risk-management feature actually runs end-to-end
    without raising, when enabled via run_custom_backtest()'s **kwargs
    override path. These are deliberately loose ("did it run and produce
    output") rather than asserting specific numbers, the numeric behavior of
    each feature is asserted more precisely in TestResolveTargetWeights and
    TestCrashProtection below. This class exists to catch integration bugs
    (e.g. a new config field that compiles but crashes only when actually
    wired into the daily rebalance loop).
    """

    def test_default_run_produces_output(self, synthetic_monthly_picks, synthetic_daily_prices):
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  holding_period=1, commission=0, initial_capital=1000.0)
        assert not df.empty
        assert "tearsheet" in df.attrs

    def test_correlation_penalty_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, use_correlation_penalty=True)
        assert not df.empty

    def test_aggregate_drift_skip_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, aggregate_drift_threshold=0.05)
        assert not df.empty

    def test_custom_weights_by_date_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        cw = {d: {t: 1 / len(tks) for t in tks} for d, tks in synthetic_monthly_picks.items()}
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, custom_weights_by_date=cw)
        assert not df.empty


class TestResolveTargetWeights:
    """
    resolve_target_weights() is the SINGLE shared sizing function called by
    both the backtest engine and the live trading path (live_signal.py),
    these tests exist specifically to prevent the two paths from silently
    diverging, which was an explicit design goal after this project's history
    of live/backtest logic accidentally drifting apart.
    """

    def test_custom_weights_pass_through_when_cap_feasible(self, synthetic_daily_prices):
        # Confirms hand-specified weights are honored exactly when the position
        # cap doesn't force a change, i.e. custom_weights isn't silently
        # overridden by the algorithmic sizing path when it shouldn't be.
        cfg = BacktestConfig(max_position_weight=0.9)
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ"], synthetic_daily_prices, as_of, cfg,
                                          custom_weights={"SPY": 0.8, "QQQ": 0.2})
        assert weights["SPY"] == pytest.approx(0.8, abs=1e-6)
        assert weights["QQQ"] == pytest.approx(0.2, abs=1e-6)

    def test_custom_weights_capped_when_infeasible(self, synthetic_daily_prices):
        # 2 assets * 0.35 cap = 0.7 max achievable, but weights must sum to 1.0,
        # mathematically infeasible to respect both the cap and the requested
        # 0.8/0.2 split. This documents the ACTUAL (somewhat surprising)
        # behavior in that case: the capping algorithm's iterative redistribution
        # converges to an equal 50/50 split, not an error and not a silent
        # violation of the cap. Discovered as a real edge case during manual
        # testing earlier in this project, worth pinning down explicitly so
        # a future change to the capping algorithm doesn't silently alter it.
        cfg = BacktestConfig(max_position_weight=0.35)  # 2 * 0.35 = 0.7 < 1.0, infeasible for 2 assets
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ"], synthetic_daily_prices, as_of, cfg,
                                          custom_weights={"SPY": 0.8, "QQQ": 0.2})
        assert weights["SPY"] <= 0.5 + 1e-6  # symmetric infeasibility converges to equal split

    def test_correlation_penalty_downweights_correlated_pair(self):
        # Builds A and B as near-duplicates (same underlying shock + tiny
        # independent noise) and C as genuinely independent, then confirms the
        # penalty actually shifts weight AWAY from the correlated pair and
        # TOWARD the independent asset, not just that it runs without error.
        # This is the core claim of the correlation-penalty feature; asserting
        # the direction of the effect, not just its existence, is the point.
        np.random.seed(1)
        dates = pd.bdate_range("2018-01-01", "2018-12-31")
        base = np.cumprod(1 + np.random.normal(0.001, 0.015, len(dates))) * 100
        prices = pd.DataFrame({
            "A": base * (1 + np.random.normal(0, 0.001, len(dates))),
            "B": base * (1 + np.random.normal(0, 0.001, len(dates))),
            "C": np.cumprod(1 + np.random.normal(0.001, 0.015, len(dates))) * 100,
        }, index=dates)

        cfg_off = BacktestConfig(max_position_weight=0.9, use_correlation_penalty=False)
        cfg_on = BacktestConfig(max_position_weight=0.9, use_correlation_penalty=True, correlation_penalty_strength=0.8)

        w_off = resolve_target_weights(["A", "B", "C"], prices, dates[-1], cfg_off)
        w_on = resolve_target_weights(["A", "B", "C"], prices, dates[-1], cfg_on)

        # correlated pair (A,B) should be downweighted relative to independent C when penalty is on
        assert w_on["C"] > w_off["C"]
        assert w_on["A"] < w_off["A"]

    def test_custom_weights_with_no_matching_tickers_raises(self, synthetic_daily_prices):
        # Guards against a silent no-op: if custom_weights references tickers
        # that aren't in the actual picks list (e.g. a stale config after the
        # universe changed), this should fail loudly rather than quietly
        # produce an empty/zero allocation.
        cfg = BacktestConfig()
        as_of = synthetic_daily_prices.index[-1]
        with pytest.raises(ValueError, match="custom_weights"):
            resolve_target_weights(["SPY"], synthetic_daily_prices, as_of, cfg,
                                    custom_weights={"QQQ": 1.0})

    def test_score_proportional_weights_by_signal_strength(self, synthetic_daily_prices):
        # Confirms weight is proportional to score, not
        # equal or inverse-vol, C (score 0.20) should get roughly 4x A's
        # weight (score 0.05), verified by exact fraction, not just "more than".
        cfg = BacktestConfig(sizing_method="score_proportional", max_position_weight=0.9)
        as_of = synthetic_daily_prices.index[-1]
        scores = pd.Series({"SPY": 0.05, "QQQ": 0.10, "XLK": 0.20})
        weights = resolve_target_weights(["SPY", "QQQ", "XLK"], synthetic_daily_prices, as_of, cfg,
                                          momentum_scores=scores)
        assert weights["XLK"] == pytest.approx(0.20 / 0.35, abs=1e-6)
        assert weights["XLK"] > weights["QQQ"] > weights["SPY"]

    def test_score_proportional_falls_back_to_equal_weight_without_scores(self, synthetic_daily_prices):
        # If sizing_method="score_proportional" but no scores are supplied
        # (e.g. a caller forgot to pass them), the function must not crash or
        # silently produce a zero allocation, equal weight is the safe default.
        cfg = BacktestConfig(sizing_method="score_proportional", max_position_weight=0.9)
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ", "XLK"], synthetic_daily_prices, as_of, cfg,
                                          momentum_scores=None)
        assert weights["SPY"] == pytest.approx(1 / 3, abs=1e-6)

    def test_invalid_sizing_method_raises(self):
        with pytest.raises(ValueError, match="sizing_method"):
            BacktestConfig(sizing_method="bogus")


class TestCrashProtection:
    """
    Circuit breaker, correlation spike detection, liquidity stress
    handling. These exist to reduce (not eliminate, see the circuit
    breaker's documented limitation in momentum_backtest.py) downside risk
    during sharp market moves. Tests here confirm the mechanisms fire
    correctly on synthetic, deliberately-shaped data (a genuine correlation
    spike, a genuine vol spike), they do NOT validate real crash performance,
    which has never been tested against real market history in this project.
    """

    def test_circuit_breaker_config_validates(self):
        # max_portfolio_drawdown_pct is a fraction [0, 1); >=1 would mean
        # "halt only after losing 100%+", which can never trigger and silently
        # disables the feature while looking configured.
        with pytest.raises(ValueError, match="max_portfolio_drawdown_pct"):
            BacktestConfig(max_portfolio_drawdown_pct=1.5)

    def test_circuit_breaker_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Integration smoke test only, the actual "does it reduce drawdown"
        # question was investigated manually during implementation and found
        # to be more nuanced than a simple assertion could capture (the
        # breaker halts NEW entries but does not force-liquidate existing
        # positions, so it doesn't guarantee a drawdown cap by itself). See
        # momentum_backtest.py's docstring for that finding.
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, max_portfolio_drawdown_pct=0.20)
        assert not df.empty

    def test_correlation_spike_detector_flags_genuine_spike(self):
        # Constructs a panel where correlation is normal for most of the year,
        # then deliberately collapses to near-1 in the final 10 days (idiosyncratic
        # noise scaled to near-zero, leaving only the shared shock). Confirms the
        # detector distinguishes "normal" (15 days before the spike) from
        # "spiking" (at the end), a false positive or false negative here would
        # mean the fast-reacting crash signal either never fires or fires
        # constantly, both of which defeat its purpose.
        np.random.seed(7)
        dates = pd.bdate_range("2018-01-01", "2018-12-31")
        n = len(dates)
        common_shock = np.random.normal(0, 0.005, n)
        data = {}
        for name in ["A", "B", "C"]:
            idio = np.random.normal(0.0005, 0.01, n)
            idio[-10:] *= 0.05  # correlation spikes in the last 10 days
            data[name] = np.cumprod(1 + idio + common_shock) * 100
        prices = pd.DataFrame(data, index=dates)

        from momentum_trading.backtest.momentum_backtest import detect_correlation_spike
        assert bool(detect_correlation_spike(prices, dates[-15])) is False
        assert bool(detect_correlation_spike(prices, dates[-1])) is True

    def test_correlation_spike_regime_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Integration smoke test: confirms the detector wires into the
        # regime-scalar computation inside the actual rebalance loop without
        # crashing, on top of the standalone unit test above.
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, use_correlation_spike_regime=True,
                                  use_regime_filter=False)
        assert not df.empty

    def test_liquidity_stress_multiplier_validates(self):
        # The multiplier scales slippage UP under stress; a value <1.0 would
        # mean "reduce slippage during a liquidity crisis," which is backwards
        # and almost certainly a config mistake rather than an intended setting.
        with pytest.raises(ValueError, match="liquidity_stress_multiplier"):
            BacktestConfig(liquidity_stress_multiplier=0.5)

    def test_liquidity_stress_and_reduce_only_run_succeed(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Integration smoke tests for both the slippage-multiplier path and the
        # reduce-only (block-new-BUYs) path, confirms neither crashes the
        # rebalance loop when enabled together with other default settings.
        df1 = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                   initial_capital=1000.0, liquidity_stress_multiplier=2.0)
        assert not df1.empty
        df2 = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                   initial_capital=1000.0, liquidity_stress_reduce_only=True,
                                   liquidity_stress_vol_ratio=1.5)
        assert not df2.empty
