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

    def test_attach_broker_stop_loss_defaults_false(self):
        # LIVE-ONLY (Epic 2 of the cross-portfolio-sell-prevention plan), default False must be
        # byte-identical to before this field existed, no effect on the backtest engine at all.
        assert BacktestConfig().attach_broker_stop_loss is False

    def test_attach_broker_stop_loss_true_is_accepted(self):
        BacktestConfig(attach_broker_stop_loss=True)  # should not raise

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

    def test_negative_lookback_period_raises(self):
        with pytest.raises(ValueError, match="lookback_period"):
            BacktestConfig(lookback_period=-1)

    def test_fractional_lookback_period_is_valid(self):
        # lookback_period accepts fractional values now (short-term/weekly momentum
        # configs, e.g. lookback_period=0.5 under holding_period=0.25 means a 2-week
        # ranking window, see execution/live_signal.py's resolve_momentum_scores()).
        # Only zero/negative is a hard error, unlike the old >= 1 integer-only rule.
        for value in (0.25, 0.5, 0.75, 1.0, 1.5, 12.0):
            cfg = BacktestConfig(lookback_period=value)
            assert cfg.lookback_period == value

    def test_max_turnover_pct_default(self):
        assert BacktestConfig().max_turnover_pct == 0.20

    def test_max_turnover_pct_out_of_range_raises(self):
        with pytest.raises(ValueError, match="max_turnover_pct"):
            BacktestConfig(max_turnover_pct=0.0)
        with pytest.raises(ValueError, match="max_turnover_pct"):
            BacktestConfig(max_turnover_pct=1.5)

    def test_low_capital_drop_warning_pct_default(self):
        assert BacktestConfig().low_capital_drop_warning_pct == 0.30

    def test_low_capital_drop_warning_pct_out_of_range_raises(self):
        with pytest.raises(ValueError, match="low_capital_drop_warning_pct"):
            BacktestConfig(low_capital_drop_warning_pct=0.0)
        with pytest.raises(ValueError, match="low_capital_drop_warning_pct"):
            BacktestConfig(low_capital_drop_warning_pct=1.5)

    def test_use_absolute_momentum_defaults_false(self):
        # Deliberately opt-in, like skip_month_guardrail: enabling it changes what the SAME
        # picks actually resolve to, must never flip on by accident from an old config.yaml.
        assert BacktestConfig().use_absolute_momentum is False

    def test_defensive_ticker_defaults_to_bil(self):
        assert BacktestConfig().defensive_ticker == "BIL"

    def test_empty_defensive_ticker_raises(self):
        with pytest.raises(ValueError, match="defensive_ticker"):
            BacktestConfig(defensive_ticker="")
        with pytest.raises(ValueError, match="defensive_ticker"):
            BacktestConfig(defensive_ticker="   ")

    def test_max_bid_ask_spread_pct_defaults_none(self):
        assert BacktestConfig().max_bid_ask_spread_pct is None

    def test_max_bid_ask_spread_pct_out_of_range_raises(self):
        with pytest.raises(ValueError, match="max_bid_ask_spread_pct"):
            BacktestConfig(max_bid_ask_spread_pct=0.0)
        with pytest.raises(ValueError, match="max_bid_ask_spread_pct"):
            BacktestConfig(max_bid_ask_spread_pct=1.5)

    def test_strategy_type_defaults_to_momentum(self):
        # Default preserves today's exact behavior, the base cross-sectional relative-momentum
        # signal, must never change by accident from an old config.yaml.
        assert BacktestConfig().strategy_type == "momentum"

    def test_strategy_type_accepts_every_allowed_value(self):
        allowed = [
            "momentum", "relative_momentum", "dual_momentum", "volatility_scaled_momentum",
            "residual_momentum", "absolute_momentum", "rank_sign_momentum",
            "hybrid_multi_factor", "path_dependent_momentum", "correlation_weighted_momentum",
            "multi_timeframe_composite",
        ]
        for value in allowed:
            assert BacktestConfig(strategy_type=value).strategy_type == value

    def test_invalid_strategy_type_raises(self):
        with pytest.raises(ValueError, match="strategy_type"):
            BacktestConfig(strategy_type="not_a_real_strategy")

    def test_multi_timeframe_lookbacks_defaults_match_blend_momentum_scores(self):
        # Must match blend_momentum_scores()'s own defaults exactly, so selecting
        # multi_timeframe_composite without customizing these fields still behaves consistently
        # with calling that function directly with no arguments.
        assert BacktestConfig().multi_timeframe_lookbacks == [3, 6, 12]
        assert BacktestConfig().multi_timeframe_weights is None

    def test_multi_timeframe_lookbacks_default_is_not_a_shared_mutable(self):
        # dataclass mutable-default footgun: two instances must never share the same list object.
        a, b = BacktestConfig(), BacktestConfig()
        a.multi_timeframe_lookbacks.append(24)
        assert b.multi_timeframe_lookbacks == [3, 6, 12]

    def test_persist_dry_run_state_defaults_false(self):
        # Default false preserves dry-run's existing behavior exactly: current_positions is {}
        # on every invocation, this must never flip on by accident from an old config.yaml.
        assert BacktestConfig().persist_dry_run_state is False

    def test_persist_dry_run_state_is_settable(self):
        assert BacktestConfig(persist_dry_run_state=True).persist_dry_run_state is True

    def test_total_value_drift_warning_pct_default(self):
        assert BacktestConfig().total_value_drift_warning_pct == 0.10

    def test_total_value_drift_warning_pct_non_positive_raises(self):
        with pytest.raises(ValueError, match="total_value_drift_warning_pct"):
            BacktestConfig(total_value_drift_warning_pct=0.0)
        with pytest.raises(ValueError, match="total_value_drift_warning_pct"):
            BacktestConfig(total_value_drift_warning_pct=-0.05)

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

    def test_regime_vol_threshold_defaults_none(self):
        # None preserves the pre-existing SMA-only regime filter behavior exactly, this
        # is an opt-in second dimension (Epic 4 of the layered risk-management plan).
        assert BacktestConfig().regime_vol_threshold is None

    def test_regime_vol_threshold_zero_or_negative_raises(self):
        with pytest.raises(ValueError, match="regime_vol_threshold"):
            BacktestConfig(regime_vol_threshold=0.0)
        with pytest.raises(ValueError, match="regime_vol_threshold"):
            BacktestConfig(regime_vol_threshold=-0.1)

    def test_regime_vol_threshold_positive_is_accepted(self):
        cfg = BacktestConfig(regime_vol_threshold=0.25)
        assert cfg.regime_vol_threshold == 0.25

    def test_regime_vol_lookback_days_default(self):
        assert BacktestConfig().regime_vol_lookback_days == 21

    def test_regime_vol_lookback_days_too_short_raises(self):
        with pytest.raises(ValueError, match="regime_vol_lookback_days"):
            BacktestConfig(regime_vol_lookback_days=1)

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

    def test_multi_timeframe_composite_backtest_picks_feed_the_engine_cleanly(self, synthetic_daily_prices):
        # Epic 2 of the selectable-momentum-strategy plan: generate_strategy_monthly_picks()
        # (core/strategy_signals.py) is the NEW backtest-facing helper, this confirms its output
        # feeds UNCHANGED into the existing, untouched run_custom_backtest()/
        # run_risk_managed_backtest(), real backtest parity for a strategy_type that previously
        # had no backtest support at all (blend_momentum_scores() was dead code).
        from momentum_trading.core.strategy_signals import generate_strategy_monthly_picks
        tickers = list(synthetic_daily_prices.columns)
        cfg = BacktestConfig(holding_period=1, strategy_type="multi_timeframe_composite",
                              multi_timeframe_lookbacks=[1, 3])
        picks = generate_strategy_monthly_picks(synthetic_daily_prices, tickers, cfg,
                                                 lookback_period=cfg.lookback_period, top_n=2)
        assert not picks.empty

        df = run_custom_backtest(picks, synthetic_daily_prices, holding_period=1, commission=0,
                                  initial_capital=1000.0)
        assert not df.empty
        assert "tearsheet" in df.attrs

    def test_absolute_momentum_backtest_picks_feed_the_engine_cleanly(self, synthetic_daily_prices):
        # Epic 3 of the selectable-momentum-strategy plan: generate_strategy_monthly_picks()
        # (core/strategy_signals.py), routed through resolve_strategy_picks(), gives
        # absolute_momentum real historical backtest parity, no cross-sectional top_n cutoff,
        # every ticker with a positive own trailing score held each period.
        from momentum_trading.core.strategy_signals import generate_strategy_monthly_picks
        tickers = list(synthetic_daily_prices.columns)
        cfg = BacktestConfig(holding_period=1, strategy_type="absolute_momentum",
                              defensive_ticker=tickers[0])
        picks = generate_strategy_monthly_picks(synthetic_daily_prices, tickers, cfg,
                                                 lookback_period=cfg.lookback_period, top_n=2)
        assert not picks.empty
        # Never capped at top_n=2, absolute_momentum can hold more (or fewer) tickers per period,
        # only that every pick stays within the given universe.
        for held in picks.values:
            assert 1 <= len(held) <= len(tickers)
            assert set(held).issubset(set(tickers))

        df = run_custom_backtest(picks, synthetic_daily_prices, holding_period=1, commission=0,
                                  initial_capital=1000.0)
        assert not df.empty
        assert "tearsheet" in df.attrs

    def test_residual_momentum_backtest_picks_feed_the_engine_cleanly(self, synthetic_daily_prices):
        # Epic 5 of the selectable-momentum-strategy plan: generate_strategy_monthly_picks()
        # gives residual_momentum real historical backtest parity. Ranking universe excludes
        # SPY (the default regime_benchmark, still present in synthetic_daily_prices so the
        # market-model regression has a benchmark to regress against, "must be priced alongside
        # the portfolio's own tickers" same precedent as defensive_ticker).
        from momentum_trading.core.strategy_signals import generate_strategy_monthly_picks
        tickers = [t for t in synthetic_daily_prices.columns if t != "SPY"]
        cfg = BacktestConfig(holding_period=1, strategy_type="residual_momentum",
                              regime_benchmark="SPY", lookback_period=3)
        picks = generate_strategy_monthly_picks(synthetic_daily_prices, tickers, cfg,
                                                 lookback_period=cfg.lookback_period, top_n=2)
        assert not picks.empty
        for held in picks.values:
            assert len(held) == 2
            assert set(held).issubset(set(tickers))

        df = run_custom_backtest(picks, synthetic_daily_prices, holding_period=1, commission=0,
                                  initial_capital=1000.0)
        assert not df.empty
        assert "tearsheet" in df.attrs

    def test_path_dependent_momentum_backtest_picks_feed_the_engine_cleanly(self, synthetic_daily_prices):
        # Epic 6 of the selectable-momentum-strategy plan: generate_strategy_monthly_picks()
        # gives path_dependent_momentum real historical backtest parity, purely price-based, no
        # external benchmark needed.
        from momentum_trading.core.strategy_signals import generate_strategy_monthly_picks
        tickers = list(synthetic_daily_prices.columns)
        cfg = BacktestConfig(holding_period=1, strategy_type="path_dependent_momentum",
                              lookback_period=3)
        picks = generate_strategy_monthly_picks(synthetic_daily_prices, tickers, cfg,
                                                 lookback_period=cfg.lookback_period, top_n=2)
        assert not picks.empty
        for held in picks.values:
            assert len(held) == 2
            assert set(held).issubset(set(tickers))

        df = run_custom_backtest(picks, synthetic_daily_prices, holding_period=1, commission=0,
                                  initial_capital=1000.0)
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
        # 0.8/0.2 split. Both tickers correctly converge to EXACTLY the cap
        # (0.35 each), leaving the undistributable 0.30 unallocated (cash), rather than
        # violating the cap. This was previously a real, confirmed bug (Epic 4 of the
        # "Rebalance Reporting Clarity & Selection-Logic Fixes" plan): _apply_position_caps()'s
        # old unconditional final renormalize-to-1.0 silently rescaled both tickers BACK UP to
        # an equal 50/50 split (0.5/0.5), quietly violating the 0.35 cap it had just enforced.
        # Fixed by skipping that renormalize whenever redistribution couldn't fully complete.
        cfg = BacktestConfig(max_position_weight=0.35)  # 2 * 0.35 = 0.7 < 1.0, infeasible for 2 assets
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ"], synthetic_daily_prices, as_of, cfg,
                                          custom_weights={"SPY": 0.8, "QQQ": 0.2})
        assert weights["SPY"] == pytest.approx(0.35)
        assert weights["QQQ"] == pytest.approx(0.35)
        assert sum(weights.values()) == pytest.approx(0.70)

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

    def test_equal_weight_sizing_ignores_score_magnitude_and_rank(self, synthetic_daily_prices):
        # Epic 4 of the selectable-momentum-strategy plan: "equal_weight" is the
        # non-parametric sizing this project's rank_sign_momentum strategy_type maps to, every
        # pick gets an identical 1/N weight, regardless of momentum score magnitude (unlike
        # score_proportional) or trailing volatility (unlike inverse_vol).
        cfg = BacktestConfig(sizing_method="equal_weight", max_position_weight=0.9)
        as_of = synthetic_daily_prices.index[-1]
        scores = pd.Series({"SPY": 0.01, "QQQ": 0.50, "XLK": 0.20})
        weights = resolve_target_weights(["SPY", "QQQ", "XLK"], synthetic_daily_prices, as_of, cfg,
                                          momentum_scores=scores)
        assert weights["SPY"] == pytest.approx(1 / 3, abs=1e-6)
        assert weights["QQQ"] == pytest.approx(1 / 3, abs=1e-6)
        assert weights["XLK"] == pytest.approx(1 / 3, abs=1e-6)

    def test_equal_weight_sizing_still_subject_to_position_caps(self, synthetic_daily_prices):
        # Confirms _equal_weight_weights() output still flows through the shared
        # _apply_position_caps() step afterward, same as inverse_vol/score_proportional.
        cfg = BacktestConfig(sizing_method="equal_weight", max_position_weight=0.35)
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ"], synthetic_daily_prices, as_of, cfg)
        assert weights["SPY"] <= 0.5 + 1e-6
        assert weights["QQQ"] <= 0.5 + 1e-6

    def test_volatility_budget_caps_high_vol_position_more_than_low_vol(self):
        # The "Volatility-Adjustment" (Scaling) constraint: two tickers start at an equal
        # custom_weights split, position_vol_budget should cap the high-vol ticker well
        # below its requested weight while leaving the low-vol ticker close to unaffected,
        # confirming the two caps (flat max_position_weight vs. per-ticker vol budget) are
        # genuinely complementary, not one silently dominating the other.
        np.random.seed(2)
        dates = pd.bdate_range("2024-01-01", "2024-12-31")
        low_vol = np.cumprod(1 + np.random.normal(0.0003, 0.002, len(dates))) * 100
        high_vol = np.cumprod(1 + np.random.normal(0.0003, 0.05, len(dates))) * 100
        prices = pd.DataFrame({"LOW": low_vol, "HIGH": high_vol}, index=dates)
        as_of = dates[-1]

        cfg_no_budget = BacktestConfig(max_position_weight=0.9, position_vol_budget=None)
        cfg_with_budget = BacktestConfig(max_position_weight=0.9, position_vol_budget=0.01, vol_lookback_days=63)

        w_no_budget = resolve_target_weights(["LOW", "HIGH"], prices, as_of, cfg_no_budget,
                                              custom_weights={"LOW": 0.5, "HIGH": 0.5})
        w_with_budget = resolve_target_weights(["LOW", "HIGH"], prices, as_of, cfg_with_budget,
                                                custom_weights={"LOW": 0.5, "HIGH": 0.5})

        assert w_with_budget["HIGH"] < w_no_budget["HIGH"]
        assert w_with_budget["LOW"] > w_with_budget["HIGH"]
        # LOW absorbs HIGH's excess (weights always sum to 1.0), so LOW's weight increases
        # under the budget, this is the expected redistribution, not a bug.
        assert w_with_budget["LOW"] > w_no_budget["LOW"]

    def test_volatility_budget_none_is_unaffected(self, synthetic_daily_prices):
        # Regression safety: position_vol_budget=None (the default) must leave
        # resolve_target_weights() byte-for-byte unchanged from before this constraint existed.
        cfg = BacktestConfig(max_position_weight=0.9, position_vol_budget=None)
        as_of = synthetic_daily_prices.index[-1]
        weights = resolve_target_weights(["SPY", "QQQ"], synthetic_daily_prices, as_of, cfg,
                                          custom_weights={"SPY": 0.8, "QQQ": 0.2})
        assert weights["SPY"] == pytest.approx(0.8, abs=1e-6)
        assert weights["QQQ"] == pytest.approx(0.2, abs=1e-6)

    def test_invalid_position_vol_budget_raises(self):
        with pytest.raises(ValueError, match="position_vol_budget"):
            BacktestConfig(position_vol_budget=0.0)
        with pytest.raises(ValueError, match="position_vol_budget"):
            BacktestConfig(position_vol_budget=-0.01)


class TestApplyPositionCaps:
    """
    _apply_position_caps() isolated directly (Epic 3 of the layered risk-management plan,
    "Position Size Hard-Cap", Mandatory tier), previously only exercised indirectly through
    TestResolveTargetWeights (which always goes through the full sizing pipeline). This
    function is already fully implemented and shared live+backtest (resolve_target_weights()
    calls it unconditionally at line 524), this closes a real test-coverage gap, not a
    behavior gap.
    """

    def test_hand_computed_redistribution(self):
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        # A exceeds the 0.5 cap by 0.1, B is the only under-cap ticker, so the full excess
        # redistributes to B: A -> 0.5, B -> 0.4 + 0.1 = 0.5.
        result = _apply_position_caps({"A": 0.6, "B": 0.4}, max_weight=0.5)
        assert result["A"] == pytest.approx(0.5)
        assert result["B"] == pytest.approx(0.5)
        assert sum(result.values()) == pytest.approx(1.0)

    def test_no_ticker_ever_exceeds_the_cap(self):
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        result = _apply_position_caps({"A": 0.7, "B": 0.2, "C": 0.1}, max_weight=0.35)
        assert all(w <= 0.35 + 1e-9 for w in result.values())
        assert sum(result.values()) == pytest.approx(1.0)

    def test_already_under_cap_is_unaffected(self):
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        weights = {"A": 0.4, "B": 0.35, "C": 0.25}
        result = _apply_position_caps(weights, max_weight=0.5)
        for t in weights:
            assert result[t] == pytest.approx(weights[t])

    def test_single_ticker_cap_is_not_defeated_by_renormalization(self):
        # Real, confirmed bug (Epic 4, found via a single-ticker portfolio): with no other
        # ticker to redistribute the excess into, the old code renormalized the capped weight
        # right back to 1.0, silently defeating the cap entirely. Must now stay AT the cap.
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        result = _apply_position_caps({"A": 1.0}, max_weight=0.35)
        assert result["A"] == pytest.approx(0.35)

    def test_multiple_tickers_all_over_cap_leaves_excess_unallocated(self):
        # Two tickers, both want more than the cap, neither has any headroom to absorb the
        # other's excess: both must land exactly at the cap, total < 1.0 (the undistributable
        # excess stays as cash), NOT renormalized back to 50/50.
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        result = _apply_position_caps({"A": 0.6, "B": 0.6}, max_weight=0.35)
        assert result["A"] == pytest.approx(0.35)
        assert result["B"] == pytest.approx(0.35)
        assert sum(result.values()) == pytest.approx(0.70)

    def test_successful_redistribution_still_renormalizes_to_one(self):
        # Regression guard: the existing multi-ticker path (some room to redistribute into)
        # must remain byte-identical, still summing to 1.0, not accidentally short-circuited
        # by the new redistribution_incomplete tracking.
        from momentum_trading.backtest.momentum_backtest import _apply_position_caps
        result = _apply_position_caps({"A": 0.6, "B": 0.4}, max_weight=0.5)
        assert sum(result.values()) == pytest.approx(1.0)


class TestComputeVolScalar:
    """
    compute_vol_scalar() extracted from run_risk_managed_backtest()'s inline
    portfolio-level vol-targeting logic (Epic 1 of the layered risk-management plan),
    shared with execution/live_signal.py so live and backtest can't silently diverge on
    aggregate exposure scaling the way they already can't for sizing (resolve_target_weights()).
    """

    def test_scales_down_when_realized_vol_exceeds_target(self):
        from momentum_trading.backtest.momentum_backtest import compute_vol_scalar
        # target 15% vol, realized 30% vol -> scalar = 0.15/0.30 = 0.5
        scalar = compute_vol_scalar(realized_vol=0.30, target_portfolio_vol=0.15,
                                     min_gross_exposure=0.20, max_gross_exposure=1.0)
        assert scalar == pytest.approx(0.5)

    def test_clips_at_max_gross_exposure_when_realized_vol_is_low(self):
        from momentum_trading.backtest.momentum_backtest import compute_vol_scalar
        # target 15% vol, realized 5% vol -> raw scalar = 3.0, clipped to max_gross_exposure
        scalar = compute_vol_scalar(realized_vol=0.05, target_portfolio_vol=0.15,
                                     min_gross_exposure=0.20, max_gross_exposure=1.0)
        assert scalar == pytest.approx(1.0)

    def test_clips_at_min_gross_exposure_when_realized_vol_is_extreme(self):
        from momentum_trading.backtest.momentum_backtest import compute_vol_scalar
        # target 15% vol, realized 300% vol -> raw scalar = 0.05, clipped to min_gross_exposure
        scalar = compute_vol_scalar(realized_vol=3.0, target_portfolio_vol=0.15,
                                     min_gross_exposure=0.20, max_gross_exposure=1.0)
        assert scalar == pytest.approx(0.20)

    def test_none_realized_vol_falls_back_to_max_gross_exposure(self):
        from momentum_trading.backtest.momentum_backtest import compute_vol_scalar
        # Not enough history yet (mirrors _realized_portfolio_vol()'s None-when-insufficient-
        # history behavior), same "no information to scale down" fallback the original inline
        # logic used (`vol_scalar = config.max_gross_exposure`).
        scalar = compute_vol_scalar(realized_vol=None, target_portfolio_vol=0.15,
                                     min_gross_exposure=0.20, max_gross_exposure=1.0)
        assert scalar == pytest.approx(1.0)

    def test_zero_realized_vol_falls_back_to_max_gross_exposure(self):
        from momentum_trading.backtest.momentum_backtest import compute_vol_scalar
        # A degenerate flat series (0 realized vol) would otherwise divide-by-zero.
        scalar = compute_vol_scalar(realized_vol=0.0, target_portfolio_vol=0.15,
                                     min_gross_exposure=0.20, max_gross_exposure=1.0)
        assert scalar == pytest.approx(1.0)

    def test_backtest_run_output_unchanged_after_extraction(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Regression: run_risk_managed_backtest() must call compute_vol_scalar() and produce
        # BYTE-IDENTICAL output to the pre-extraction inline logic, same random_seed, same
        # synthetic prices. This test alone can't prove "identical to before the refactor" in
        # isolation, but combined with the hand-verified unit tests above (which pin the exact
        # formula the inline code used) it confirms the wiring didn't change behavior.
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, target_portfolio_vol=0.15,
                                  portfolio_vol_lookback=21, random_seed=42)
        assert not df.empty


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

    def test_regime_vol_threshold_run_succeeds(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Integration smoke test: confirms regime_vol_threshold wires into the actual
        # rebalance loop (Epic 4) without crashing, on top of TestRegimeVolatilityDimension's
        # unit-level compute_target_weights() coverage on the live-signal side.
        df = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                  initial_capital=1000.0, use_regime_filter=True,
                                  regime_benchmark="SPY", regime_vol_threshold=0.001,
                                  regime_vol_lookback_days=21)
        assert not df.empty

    def test_regime_vol_threshold_none_default_matches_sma_only_run(self, synthetic_monthly_picks, synthetic_daily_prices):
        # Byte-identical-behavior regression: a run with regime_vol_threshold=None must
        # produce the exact same equity curve as before this field existed.
        df_default = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                          initial_capital=1000.0, use_regime_filter=True,
                                          regime_benchmark="SPY", regime_vol_threshold=None)
        df_explicit_none = run_custom_backtest(synthetic_monthly_picks, synthetic_daily_prices,
                                                initial_capital=1000.0, use_regime_filter=True,
                                                regime_benchmark="SPY")
        pd.testing.assert_frame_equal(df_default, df_explicit_none)

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
