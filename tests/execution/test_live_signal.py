"""
tests/test_live_signal.py

Covers the live-trading order logic: order generation (BUY/SELL/HOLD sizing
and rounding), real FIFO P&L measurement from a trade log, and multi-portfolio
orchestration. Nothing here connects to a real broker, IBKR-dependent
functions (get_ibkr_positions, place_orders_ibkr) are not covered by this
file since they require a live TWS/Gateway connection this test environment
doesn't have.

Run with: pytest tests/test_live_signal.py -v
See TESTING.md for fixture explanations and how to interpret a failure.
"""
import csv
import numpy as np
import pandas as pd
import pytest

from momentum_trading.backtest.momentum_backtest import BacktestConfig, compute_vol_scalar
import momentum_trading.execution.live_signal as live_signal
from momentum_trading.execution.live_signal import (
    generate_orders, log_orders, measure_live_performance, run_multi_portfolio, get_top_etfs,
    compute_aggregate_drift, derive_entry_date, compute_target_weights,
    is_rebalance_day, is_outside_all_trading_windows, is_holding_period_too_frequent, is_lookback_period_too_short,
    is_lookback_shorter_than_holding, is_lookback_to_holding_ratio_too_low,
    compute_turnover, is_turnover_too_high,
    compute_low_capital_drop_fraction, is_low_capital_drop_too_high,
    most_recent_rebalance_target_date,
    build_position_performance, resolve_momentum_scores, calculate_period_returns,
    compute_required_lookback_days,
    _realized_weighted_portfolio_vol, apply_absolute_momentum_filter,
    compute_stop_loss_price, log_signal_rankings, SIGNAL_RANKINGS_LOG_HEADER, OrdersResult,
    resolve_ticker_stop_loss_pct,
)
from momentum_trading.core.audit_log import read_recent_alerts


class TestIsRebalanceDay:
    """
    is_rebalance_day() had ZERO test coverage before this, these tests close that gap
    (regression protection for the pre-existing monthly logic) as well as covering the new
    weekly branch. All dates are injected via the `today` parameter (added specifically for
    this) rather than depending on the real calendar date the suite happens to run on.
    """

    def test_default_fires_on_first_trading_day_of_month(self):
        # Jan 1 2026 is New Year's Day (a market holiday), so Jan 2 is the real first
        # trading day of the month.
        assert is_rebalance_day(1, today=pd.Timestamp("2026-01-02")) is True

    def test_default_does_not_fire_mid_month(self):
        assert is_rebalance_day(1, today=pd.Timestamp("2026-01-05")) is False

    def test_every_other_month_fires_only_on_alternating_months(self):
        # holding_period=2: confirmed fires Feb/Apr, not Jan/Mar, for this calendar,
        # unchanged pre-existing "every Nth month" logic, just now under test for the
        # first time.
        assert is_rebalance_day(2, today=pd.Timestamp("2026-01-02")) is False
        assert is_rebalance_day(2, today=pd.Timestamp("2026-02-02")) is True
        assert is_rebalance_day(2, today=pd.Timestamp("2026-03-02")) is False
        assert is_rebalance_day(2, today=pd.Timestamp("2026-04-01")) is True

    def test_weekly_fires_on_first_trading_day_of_week(self):
        assert is_rebalance_day(0.25, today=pd.Timestamp("2026-01-05")) is True  # Monday

    def test_weekly_does_not_fire_mid_week(self):
        assert is_rebalance_day(0.25, today=pd.Timestamp("2026-01-07")) is False  # Wednesday

    def test_every_three_weeks_fires_only_every_third_week(self):
        # holding_period=0.75 -> weeks_interval=3 (the exact mapping from your own examples:
        # 0.75 = every 3 weeks). Confirms it does NOT fire on the two weeks in between.
        assert is_rebalance_day(0.75, today=pd.Timestamp("2026-01-05")) is True
        assert is_rebalance_day(0.75, today=pd.Timestamp("2026-01-12")) is False
        assert is_rebalance_day(0.75, today=pd.Timestamp("2026-01-19")) is False
        assert is_rebalance_day(0.75, today=pd.Timestamp("2026-01-26")) is True

    def test_holiday_shifts_the_weekly_target_day(self):
        # Presidents' Day 2026 falls on Monday 2026-02-16, the real first trading day
        # of that week is Tuesday 2026-02-17. Confirms the weekly branch is
        # holiday-aware, the same as the pre-existing monthly branch already was.
        assert is_rebalance_day(0.25, today=pd.Timestamp("2026-02-16")) is False
        assert is_rebalance_day(0.25, today=pd.Timestamp("2026-02-17")) is True


class TestMostRecentRebalanceTargetDate:
    """
    Distinct from is_rebalance_day() (which only answers "is TODAY the day"), this answers
    "when was the last day I should have rebalanced", used to detect a rebalance day the
    process/container was not running to catch, not to gate today's own run.
    """

    def test_finds_the_missed_monthly_date(self):
        # Jan 2, 2026 was the real first trading day of January (Jan 1 is New Year's Day).
        # Asking "as of" a later date in the same month with no rebalance since should find it.
        found = most_recent_rebalance_target_date(1, today=pd.Timestamp("2026-01-10"))
        assert found == pd.Timestamp("2026-01-02")

    def test_finds_the_missed_weekly_date(self):
        # 2026-01-05 (Monday) was the target day of that week under a weekly cadence.
        found = most_recent_rebalance_target_date(0.25, today=pd.Timestamp("2026-01-08"))
        assert found == pd.Timestamp("2026-01-05")

    def test_only_looks_strictly_before_today(self):
        # today itself is a rebalance day, but this function only searches BEFORE today, so it
        # must return the PRIOR period's target, not today's own date.
        found = most_recent_rebalance_target_date(1, today=pd.Timestamp("2026-01-02"))
        assert found is not None
        assert found < pd.Timestamp("2026-01-02")

    def test_holiday_aware_like_is_rebalance_day(self):
        # Presidents' Day 2026 (Mon 2026-02-16) shifts to Tuesday 2026-02-17 under a weekly
        # cadence, same holiday-awareness is_rebalance_day() itself has, confirmed here since
        # this function is built directly on top of it.
        found = most_recent_rebalance_target_date(0.25, today=pd.Timestamp("2026-02-20"))
        assert found == pd.Timestamp("2026-02-17")


class TestIsOutsideAllTradingWindows:
    """
    Backs place_orders_ibkr()'s proactive off-hours log line. All times injected via `now`
    (tz-aware America/New_York) rather than the real wall clock. 2026-01-07 is a Wednesday,
    a plain, holiday-free NYSE trading day.
    """

    def test_during_rth_is_not_outside(self):
        now = pd.Timestamp("2026-01-07 10:00", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now) is False

    def test_rth_open_boundary_is_not_outside(self):
        now = pd.Timestamp("2026-01-07 09:30", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now) is False

    def test_rth_close_boundary_is_outside(self):
        now = pd.Timestamp("2026-01-07 16:00", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now) is True

    def test_before_open_without_extended_hours_is_outside(self):
        now = pd.Timestamp("2026-01-07 06:00", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now, allow_extended_hours=False) is True

    def test_before_open_with_extended_hours_is_not_outside(self):
        now = pd.Timestamp("2026-01-07 06:00", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now, allow_extended_hours=True) is False

    def test_after_close_with_extended_hours_is_not_outside(self):
        now = pd.Timestamp("2026-01-07 18:00", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now, allow_extended_hours=True) is False

    def test_late_night_even_with_extended_hours_is_outside(self):
        # This is exactly the scenario from the real incident that prompted this feature:
        # a manual --force-rebalance --live run at ~23:57 ET, well outside extended hours too.
        now = pd.Timestamp("2026-01-07 23:57", tz="America/New_York")
        assert is_outside_all_trading_windows(now=now, allow_extended_hours=True) is True

    def test_weekend_is_outside_regardless_of_time_or_extended_hours(self):
        now = pd.Timestamp("2026-01-10 10:00", tz="America/New_York")  # Saturday
        assert is_outside_all_trading_windows(now=now, allow_extended_hours=True) is True

    def test_naive_timestamp_is_treated_as_eastern(self):
        now = pd.Timestamp("2026-01-07 10:00")  # tz-naive
        assert is_outside_all_trading_windows(now=now) is False


class TestIsHoldingPeriodTooFrequent:
    """
    Single source of truth for the 'faster than weekly' threshold used by
    daily_runner.py's non-blocking WARNING check, these tests pin the exact boundary.
    """

    def test_exactly_weekly_is_not_too_frequent(self):
        assert is_holding_period_too_frequent(0.25) is False

    def test_just_below_weekly_is_too_frequent(self):
        assert is_holding_period_too_frequent(0.24) is True

    def test_monthly_default_is_not_too_frequent(self):
        assert is_holding_period_too_frequent(1.0) is False


class TestIsLookbackPeriodTooShort:
    """
    Single source of truth for the 'shorter than 2 weeks' threshold used by
    daily_runner.py's non-blocking WARNING check, only meaningful in the weekly regime
    (holding_period < 1), these tests pin the exact boundary, including that a 2-week
    lookback (lookback_period=0.5), the shortest of the documented short-term examples,
    does NOT warn.
    """

    def test_two_weeks_is_not_too_short(self):
        assert is_lookback_period_too_short(0.5, 0.25) is False

    def test_one_week_is_too_short(self):
        assert is_lookback_period_too_short(0.25, 0.25) is True

    def test_three_weeks_is_not_too_short(self):
        assert is_lookback_period_too_short(0.75, 0.25) is False

    def test_monthly_regime_is_never_too_short_even_with_a_tiny_value(self):
        # holding_period >= 1 means lookback_period is interpreted in months, not weeks,
        # this check only applies to the weekly regime.
        assert is_lookback_period_too_short(0.1, 1.0) is False

    def test_monthly_default_is_not_too_short(self):
        assert is_lookback_period_too_short(12.0, 1.0) is False


class TestIsLookbackShorterThanHolding:
    """
    The "Momentum Persistence" constraint: lookback_period must be strictly older than
    holding_period, in the SAME regime-appropriate unit resolve_momentum_scores() uses
    (weeks when holding_period < 1, months otherwise). Equality counts as a violation.
    """

    def test_monthly_default_passes(self):
        assert is_lookback_shorter_than_holding(12.0, 1.0) is False

    def test_monthly_equal_values_violate(self):
        assert is_lookback_shorter_than_holding(1.0, 1.0) is True

    def test_monthly_lookback_shorter_than_holding_violates(self):
        assert is_lookback_shorter_than_holding(1.0, 3.0) is True

    def test_weekly_two_week_lookback_over_one_week_holding_passes(self):
        assert is_lookback_shorter_than_holding(0.5, 0.25) is False

    def test_weekly_equal_values_violate(self):
        assert is_lookback_shorter_than_holding(0.25, 0.25) is True


class TestIsLookbackToHoldingRatioTooLow:
    """
    The "Lookback-to-Hold Ratio" constraint: lookback_period / holding_period below 3 risks
    whipsawing. Only the low end is checked (no stated rationale for an upper bound).
    Deliberately independent of is_lookback_shorter_than_holding(), a ratio < 1 trips both.
    """

    def test_ratio_of_twelve_is_not_too_low(self):
        assert is_lookback_to_holding_ratio_too_low(12.0, 1.0) is False

    def test_ratio_of_exactly_three_is_not_too_low(self):
        # boundary: == 3 is not "lower than 3"
        assert is_lookback_to_holding_ratio_too_low(3.0, 1.0) is False

    def test_ratio_just_below_three_is_too_low(self):
        assert is_lookback_to_holding_ratio_too_low(2.9, 1.0) is True

    def test_ratio_below_one_trips_both_constraints(self):
        # lookback shorter than holding (ratio < 1) is also, necessarily, ratio < 3.
        assert is_lookback_shorter_than_holding(1.0, 2.0) is True
        assert is_lookback_to_holding_ratio_too_low(1.0, 2.0) is True

    def test_weekly_regime_ratio_computed_in_weeks(self):
        # 6-week lookback / 1-week holding = ratio 6, not too low.
        assert is_lookback_to_holding_ratio_too_low(1.5, 0.25) is False
        # 2-week lookback / 1-week holding = ratio 2, too low.
        assert is_lookback_to_holding_ratio_too_low(0.5, 0.25) is True


class TestComputeTurnover:
    """
    The "Turnover Limit" constraint: Total_Positions_Changed / Total_Positions for a
    rebalance. Total_Positions is every ticker generate_orders() produced a decision for;
    HOLD (for any reason) doesn't count as a change.
    """

    def test_hand_computed_turnover(self):
        orders = {
            "A": {"action": "BUY"}, "B": {"action": "SELL"},
            "C": {"action": "HOLD"}, "D": {"action": "HOLD"},
            "E": {"action": "HOLD"}, "F": {"action": "HOLD"},
            "G": {"action": "HOLD"}, "H": {"action": "HOLD"},
            "I": {"action": "HOLD"}, "J": {"action": "HOLD"},
        }
        assert compute_turnover(orders) == pytest.approx(0.20)

    def test_empty_orders_is_zero_turnover(self):
        assert compute_turnover({}) == 0.0

    def test_all_hold_is_zero_turnover(self):
        orders = {"A": {"action": "HOLD"}, "B": {"action": "HOLD"}}
        assert compute_turnover(orders) == 0.0

    def test_all_traded_is_full_turnover(self):
        orders = {"A": {"action": "BUY"}, "B": {"action": "SELL"}}
        assert compute_turnover(orders) == 1.0


class TestIsTurnoverTooHigh:
    def test_exactly_at_threshold_is_not_too_high(self):
        assert is_turnover_too_high(0.20, 0.20) is False

    def test_above_threshold_is_too_high(self):
        assert is_turnover_too_high(0.21, 0.20) is True

    def test_below_threshold_is_not_too_high(self):
        assert is_turnover_too_high(0.10, 0.20) is False


class TestComputeLowCapitalDropFraction:
    """
    Fraction of intended BUYs whose computed shares would floor to 0 (IBKR has no
    fractional-equity order support). Checked via shares < 1 directly, not the LIVE-ONLY
    fill_status field, so it works identically in dry-run and --live.
    """

    def test_hand_computed_fraction(self):
        orders = {
            "A": {"action": "BUY", "shares": 0.5},
            "B": {"action": "BUY", "shares": 2.0},
            "C": {"action": "BUY", "shares": 0.1},
            "D": {"action": "BUY", "shares": 3.0},
            "E": {"action": "SELL", "shares": 1.0},
            "F": {"action": "HOLD", "shares": 0},
        }
        fraction, dropped = compute_low_capital_drop_fraction(orders)
        assert fraction == pytest.approx(0.5)
        assert set(dropped) == {"A", "C"}

    def test_empty_orders_is_zero(self):
        assert compute_low_capital_drop_fraction({}) == (0.0, [])

    def test_no_buys_is_zero_not_all_dropped(self):
        orders = {"A": {"action": "SELL", "shares": 1.0}, "B": {"action": "HOLD", "shares": 0}}
        assert compute_low_capital_drop_fraction(orders) == (0.0, [])

    def test_all_buys_floor_to_zero(self):
        orders = {"A": {"action": "BUY", "shares": 0.2}, "B": {"action": "BUY", "shares": 0.9}}
        fraction, dropped = compute_low_capital_drop_fraction(orders)
        assert fraction == 1.0
        assert set(dropped) == {"A", "B"}

    def test_exactly_one_share_is_not_dropped(self):
        orders = {"A": {"action": "BUY", "shares": 1.0}}
        fraction, dropped = compute_low_capital_drop_fraction(orders)
        assert fraction == 0.0
        assert dropped == []


class TestIsLowCapitalDropTooHigh:
    def test_exactly_at_threshold_is_not_too_high(self):
        assert is_low_capital_drop_too_high(0.30, 0.30) is False

    def test_above_threshold_is_too_high(self):
        assert is_low_capital_drop_too_high(0.31, 0.30) is True

    def test_below_threshold_is_not_too_high(self):
        assert is_low_capital_drop_too_high(0.10, 0.30) is False


class TestComputeRequiredLookbackDays:
    """
    Confirmed by direct reproduction (not guessed): the OLD fixed lookback_days=400 default
    gave the shipped default (lookback_period=12, holding_period=1) only a 1-monthly-bar
    margin, and silently produced an all-NaN latest-row score (zero picks) for a monthly
    lookback_period as unremarkable as 18, or a weekly one around 15 (60 weeks). These tests
    pin the formula that replaces the fixed default.
    """

    def test_default_config_gets_comfortable_margin_over_old_400(self):
        # The shipped default (lookback_period=12, holding_period=1) previously had only a
        # 1-monthly-bar margin under the old fixed 400. The new formula must give more room,
        # not just barely clear the same edge.
        cfg = BacktestConfig(lookback_period=12, holding_period=1)
        days = compute_required_lookback_days(cfg)
        assert days > 400

    def test_monthly_scales_with_lookback_period(self):
        cfg_small = BacktestConfig(lookback_period=6, holding_period=1, use_regime_filter=False)
        cfg_large = BacktestConfig(lookback_period=18, holding_period=1, use_regime_filter=False)
        assert compute_required_lookback_days(cfg_large) > compute_required_lookback_days(cfg_small)

    def test_weekly_scales_in_weeks_not_months(self):
        # holding_period < 1: lookback_period is in week-quarters, same round(x*4) formula as
        # resolve_momentum_scores() itself, must stay in lockstep or the fetch window and the
        # ranking window disagree about what "this many periods back" means.
        cfg = BacktestConfig(lookback_period=15, holding_period=0.25, use_regime_filter=False)
        days = compute_required_lookback_days(cfg)
        weeks_lookback = round(15 * 4)  # 60 weeks
        assert days >= weeks_lookback * 7  # at least the raw weeks requirement, plus buffer

    def test_regime_filter_widens_the_window_when_it_dominates(self):
        # A tiny lookback_period under use_regime_filter=True (regime_sma_window=150 default)
        # must not under-fetch relative to the regime filter's own trailing-SMA requirement.
        cfg = BacktestConfig(lookback_period=1, holding_period=0.25, use_regime_filter=True,
                              regime_sma_window=150)
        days = compute_required_lookback_days(cfg)
        assert days >= 150

    def test_correlation_penalty_widens_the_window_when_it_dominates(self):
        cfg = BacktestConfig(lookback_period=1, holding_period=1, use_regime_filter=False,
                              use_correlation_penalty=True, correlation_lookback_days=200)
        days = compute_required_lookback_days(cfg)
        assert days >= 200

    def test_fix_actually_closes_the_gap_for_a_large_monthly_lookback(self):
        # Regression: reproduces this epic's own repro. A 400-day fetch left lookback_period=18
        # with an entirely NaN latest row (zero picks). Fetching compute_required_lookback_days()
        # days instead must NOT be all-NaN.
        cfg = BacktestConfig(lookback_period=18, holding_period=1, use_regime_filter=False)
        days = compute_required_lookback_days(cfg)
        dates = pd.bdate_range(end=pd.Timestamp("2026-07-20"), periods=int(days * 5 / 7))
        rng = np.random.default_rng(1)
        prices = pd.DataFrame(
            {"SPY": 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(dates)))}, index=dates,
        )
        scores = resolve_momentum_scores(prices, lookback_period=18, holding_period=1)
        assert not scores.iloc[-1].isna().all()

    def test_fix_actually_closes_the_gap_for_a_large_weekly_lookback(self):
        cfg = BacktestConfig(lookback_period=15, holding_period=0.25, use_regime_filter=False)
        days = compute_required_lookback_days(cfg)
        dates = pd.bdate_range(end=pd.Timestamp("2026-07-20"), periods=int(days * 5 / 7))
        rng = np.random.default_rng(1)
        prices = pd.DataFrame(
            {"SPY": 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(dates)))}, index=dates,
        )
        scores = resolve_momentum_scores(prices, lookback_period=15, holding_period=0.25)
        assert not scores.iloc[-1].isna().all()


class TestResolveMomentumScores:
    """
    resolve_momentum_scores() is where run() decides monthly vs. weekly momentum ranking,
    ties lookback_period's granularity to holding_period's regime rather than
    lookback_period's own value. These tests hand-verify the weekly branch's exact
    arithmetic (not just "doesn't crash") and confirm the monthly branch is byte-for-byte
    the same computation the old inline code did.
    """

    def _linear_daily_prices(self, n_business_days, start="2026-01-05"):
        # Monday start, price grows by exactly 1.0 per business day so resample("W").last()
        # (which picks each week's Friday close) produces an exact, hand-computable
        # arithmetic sequence: week k's value is 100 + 4 + 5*k (5 business days/week).
        dates = pd.bdate_range(start, periods=n_business_days)
        prices = pd.Series(100.0 + np.arange(n_business_days), index=dates)
        return pd.DataFrame({"XLK": prices})

    def test_weekly_regime_two_week_lookback(self):
        # lookback_period=0.5 under a weekly holding_period -> 2 weeks, the shortest of
        # the documented short-term examples.
        daily_prices = self._linear_daily_prices(35)  # 7 weeks
        scores = resolve_momentum_scores(daily_prices, lookback_period=0.5, holding_period=0.25)
        weekly = daily_prices.resample("W").last()["XLK"]
        expected = (weekly.iloc[2] - weekly.iloc[0]) / weekly.iloc[0]
        assert scores["XLK"].iloc[2] == pytest.approx(expected)
        assert expected == pytest.approx(10 / 104)

    def test_weekly_regime_three_week_lookback(self):
        daily_prices = self._linear_daily_prices(35)
        scores = resolve_momentum_scores(daily_prices, lookback_period=0.75, holding_period=0.25)
        weekly = daily_prices.resample("W").last()["XLK"]
        expected = (weekly.iloc[3] - weekly.iloc[0]) / weekly.iloc[0]
        assert scores["XLK"].iloc[3] == pytest.approx(expected)
        assert expected == pytest.approx(15 / 104)

    def test_weekly_regime_four_week_lookback(self):
        daily_prices = self._linear_daily_prices(35)
        scores = resolve_momentum_scores(daily_prices, lookback_period=1.0, holding_period=0.25)
        weekly = daily_prices.resample("W").last()["XLK"]
        expected = (weekly.iloc[4] - weekly.iloc[0]) / weekly.iloc[0]
        assert scores["XLK"].iloc[4] == pytest.approx(expected)
        assert expected == pytest.approx(20 / 104)

    def test_weekly_regime_six_week_lookback(self):
        daily_prices = self._linear_daily_prices(50)  # 10 weeks, enough for a 6-week lookback
        scores = resolve_momentum_scores(daily_prices, lookback_period=1.5, holding_period=0.25)
        weekly = daily_prices.resample("W").last()["XLK"]
        expected = (weekly.iloc[6] - weekly.iloc[0]) / weekly.iloc[0]
        assert scores["XLK"].iloc[6] == pytest.approx(expected)
        assert expected == pytest.approx(30 / 104)

    def test_tiny_lookback_period_floors_to_one_week(self):
        # max(1, round(0.05 * 4)) = max(1, 0) = 1 week, mirrors is_rebalance_day()'s
        # identical floor for holding_period.
        daily_prices = self._linear_daily_prices(20)
        scores = resolve_momentum_scores(daily_prices, lookback_period=0.05, holding_period=0.25)
        weekly = daily_prices.resample("W").last()["XLK"]
        expected = (weekly.iloc[1] - weekly.iloc[0]) / weekly.iloc[0]
        assert scores["XLK"].iloc[1] == pytest.approx(expected)

    def test_monthly_regime_matches_the_pre_existing_inline_computation(self):
        # Regression safety: holding_period >= 1 must produce EXACTLY what the old
        # inline code in run() did (monthly resample + calculate_period_returns), not a
        # new/different computation.
        dates = pd.bdate_range("2024-01-01", "2026-01-01")
        rng = np.random.default_rng(0)
        prices = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(dates))), index=dates)
        daily_prices = pd.DataFrame({"XLK": prices, "QQQ": prices * 1.5})

        actual = resolve_momentum_scores(daily_prices, lookback_period=12.0, holding_period=1.0)

        monthly_prices = daily_prices.resample("ME").last()
        expected = calculate_period_returns(monthly_prices, period=12)

        pd.testing.assert_frame_equal(actual, expected)

    def test_skip_month_guardrail_defaults_off_and_matches_pre_existing_behavior(self):
        # Regression safety: calling resolve_momentum_scores() without the new parameter at
        # all (as every pre-existing caller does) must still match the un-shifted computation.
        dates = pd.bdate_range("2024-01-01", "2026-01-01")
        rng = np.random.default_rng(1)
        prices = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(dates))), index=dates)
        daily_prices = pd.DataFrame({"XLK": prices})

        actual = resolve_momentum_scores(daily_prices, lookback_period=12.0, holding_period=1.0)
        monthly_prices = daily_prices.resample("ME").last()
        expected = calculate_period_returns(monthly_prices, period=12)
        pd.testing.assert_frame_equal(actual, expected)

    def test_skip_month_guardrail_shifts_the_monthly_window_by_one_bar(self):
        # The "Skip-Month" guardrail (classic academic "12-1 momentum"): excludes the most
        # recent ~month from the ranking window when enabled and lookback_period > 3.
        dates = pd.bdate_range("2024-01-01", "2026-01-01")
        rng = np.random.default_rng(2)
        prices = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(dates))), index=dates)
        daily_prices = pd.DataFrame({"XLK": prices})

        actual = resolve_momentum_scores(
            daily_prices, lookback_period=12.0, holding_period=1.0, skip_month_guardrail=True,
        )
        shifted_monthly = daily_prices.resample("ME").last().shift(1)
        expected = calculate_period_returns(shifted_monthly, period=12)
        pd.testing.assert_frame_equal(actual, expected)

        # And it must differ from the un-shifted (guardrail off) computation, confirming the
        # shift actually changed the signal, not a no-op.
        unshifted = resolve_momentum_scores(daily_prices, lookback_period=12.0, holding_period=1.0)
        assert not actual.dropna(how="all").equals(unshifted.dropna(how="all"))

    def test_skip_month_guardrail_is_a_noop_at_or_below_three_months(self):
        # "for lookback_period > 3 months", exactly 3 does not qualify.
        dates = pd.bdate_range("2024-01-01", "2025-06-01")
        rng = np.random.default_rng(3)
        prices = pd.Series(100 + np.cumsum(rng.normal(0, 1, len(dates))), index=dates)
        daily_prices = pd.DataFrame({"XLK": prices})

        with_flag = resolve_momentum_scores(
            daily_prices, lookback_period=3.0, holding_period=1.0, skip_month_guardrail=True,
        )
        without_flag = resolve_momentum_scores(
            daily_prices, lookback_period=3.0, holding_period=1.0, skip_month_guardrail=False,
        )
        pd.testing.assert_frame_equal(with_flag, without_flag)

    def test_skip_month_guardrail_ignored_in_weekly_regime(self):
        # skip_month_guardrail is inherently a monthly-lookback concept, holding_period < 1
        # (weekly regime) must ignore it entirely, even with a lookback_period > 3.
        daily_prices = self._linear_daily_prices(35)
        with_flag = resolve_momentum_scores(
            daily_prices, lookback_period=1.5, holding_period=0.25, skip_month_guardrail=True,
        )
        without_flag = resolve_momentum_scores(
            daily_prices, lookback_period=1.5, holding_period=0.25, skip_month_guardrail=False,
        )
        pd.testing.assert_frame_equal(with_flag, without_flag)

    def test_run_invokes_the_weekly_path_when_holding_period_is_sub_monthly(self, monkeypatch, tmp_path):
        # Integration confirmation, not just that resolve_momentum_scores() works in
        # isolation, but that run() actually calls it (via core/strategy_signals.py's
        # resolve_strategy_scores() router, the default "momentum" strategy_type's pass-through)
        # with cfg.holding_period (not some other value), end to end, for a real weekly-cadence
        # config. Patched at core.strategy_signals, the actual call site since Epic 1 of the
        # selectable-momentum-strategy plan, run() no longer calls resolve_momentum_scores()
        # directly, live_signal.resolve_momentum_scores itself is unaffected by this patch (a
        # separate name binding), confirming the indirection is real, not just a re-export.
        import momentum_trading.core.strategy_signals as strategy_signals
        dates = pd.bdate_range("2025-01-01", "2026-07-09")
        rng = np.random.default_rng(2)
        data = {t: np.cumprod(1 + rng.normal(0.0005, 0.01, len(dates))) * 100 for t in ["SPY", "QQQ"]}
        fake_prices = pd.DataFrame(data, index=dates)
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda *a, **k: fake_prices)
        monkeypatch.chdir(tmp_path)

        calls = []
        real_resolve = strategy_signals.resolve_momentum_scores

        def spy_resolve(daily_prices, lookback_period, holding_period, skip_month_guardrail=False):
            calls.append((lookback_period, holding_period))
            return real_resolve(daily_prices, lookback_period, holding_period, skip_month_guardrail)

        monkeypatch.setattr(strategy_signals, "resolve_momentum_scores", spy_resolve)

        cfg = BacktestConfig(holding_period=0.25, use_regime_filter=False)
        live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=0.5, dry_run=True,
        )

        assert calls == [(0.5, 0.25)]


class TestRunExtraPriceTickers:
    """
    extra_price_tickers (backs the orphaned-ticker reconciliation feature) widens the internal
    fetch_live_prices() call so generate_orders() can price/exit a currently-held-but-no-longer-
    configured ticker, but must NOT widen the momentum ranking/selection universe, an orphaned
    ticker becoming priced must never make it re-selectable as a new pick, it was deliberately
    removed from the configured tickers list.
    """

    def _fake_prices(self, tickers, seed=3):
        dates = pd.bdate_range("2025-01-01", "2026-07-09")
        rng = np.random.default_rng(seed)
        data = {t: np.cumprod(1 + rng.normal(0.0005, 0.01, len(dates))) * 100 for t in tickers}
        return pd.DataFrame(data, index=dates)

    def test_extra_price_tickers_widen_the_fetch_not_the_ranking(self, monkeypatch, tmp_path):
        fake_prices = self._fake_prices(["SPY", "QQQ", "OLD"])
        fetched = []

        def fake_fetch(tickers, **kwargs):
            fetched.append(list(tickers))
            return fake_prices[list(tickers)]

        monkeypatch.setattr(live_signal, "fetch_live_prices", fake_fetch)
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={"OLD": 10.0}, total_value=1000.0,
            cfg=cfg, top_n=2, lookback_period=1.0, dry_run=True,
            extra_price_tickers=["OLD"],
        )

        # fetch_live_prices was asked for the UNION of the ranking universe and the extra ticker.
        assert set(fetched[0]) == {"SPY", "QQQ", "OLD"}
        # OLD got priced and evaluated for exit (currently held, no longer targeted -> SELL),
        # not stuck as "HOLD, no live price available".
        assert "OLD" in orders
        assert orders["OLD"]["action"] == "SELL"

    def test_default_omits_extra_price_tickers_byte_identical_to_before(self, monkeypatch, tmp_path):
        # Regression: no extra_price_tickers argument (every pre-existing call site) must
        # fetch exactly the configured tickers, nothing more, unchanged from before this
        # parameter existed.
        fake_prices = self._fake_prices(["SPY", "QQQ"])
        fetched = []

        def fake_fetch(tickers, **kwargs):
            fetched.append(list(tickers))
            return fake_prices[list(tickers)]

        monkeypatch.setattr(live_signal, "fetch_live_prices", fake_fetch)
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
        )

        assert fetched[0] == ["SPY", "QQQ"]

    def test_extra_price_ticker_never_appears_as_a_new_buy_pick(self, monkeypatch, tmp_path):
        # Even if OLD's price history would rank it favorably, it must never be selected as a
        # NEW pick, only priced for exit purposes, it was deliberately removed from the
        # configured universe.
        fake_prices = self._fake_prices(["SPY", "QQQ", "OLD"], seed=7)
        monkeypatch.setattr(live_signal, "fetch_live_prices",
                             lambda tickers, **k: fake_prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True, extra_price_tickers=["OLD"],
        )

        # OLD was never held and never in the ranking universe, so it must not appear at all
        # (not even as a HOLD), generate_orders() only ever considers current_holdings union
        # target picks, and OLD is in neither here.
        assert "OLD" not in orders


class TestRunReusesPreFetchedDailyPrices:
    """
    daily_runner.py's "ALWAYS runs" block already fetches prices for tickers +
    confirmed_orphaned before deciding whether today is a rebalance day; when it is, run() was
    previously fetching that SAME data a second time internally, a confirmed redundant network
    round-trip. The daily_prices param lets the caller pass the already-fetched DataFrame
    through instead.
    """

    def _fake_prices(self, tickers, seed=3):
        dates = pd.bdate_range("2025-01-01", "2026-07-09")
        rng = np.random.default_rng(seed)
        data = {t: np.cumprod(1 + rng.normal(0.0005, 0.01, len(dates))) * 100 for t in tickers}
        return pd.DataFrame(data, index=dates)

    def test_pre_fetched_prices_covering_needed_tickers_skips_the_fetch(self, monkeypatch, tmp_path):
        fake_prices = self._fake_prices(["SPY", "QQQ", "OLD"])
        fetch_calls = []
        monkeypatch.setattr(
            live_signal, "fetch_live_prices",
            lambda tickers, **k: fetch_calls.append(list(tickers)) or fake_prices[list(tickers)],
        )
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={"OLD": 10.0}, total_value=1000.0,
            cfg=cfg, top_n=2, lookback_period=1.0, dry_run=True,
            extra_price_tickers=["OLD"], daily_prices=fake_prices,
        )

        assert fetch_calls == []

    def test_pre_fetched_prices_missing_a_needed_ticker_falls_back_to_fetching(self, monkeypatch, tmp_path):
        # daily_prices only covers SPY/QQQ, but extra_price_tickers needs OLD too, so the
        # incomplete pre-fetch must not be trusted, this must fall back exactly as if
        # daily_prices had never been passed.
        narrow_prices = self._fake_prices(["SPY", "QQQ"])
        full_prices = self._fake_prices(["SPY", "QQQ", "OLD"])
        fetch_calls = []
        monkeypatch.setattr(
            live_signal, "fetch_live_prices",
            lambda tickers, **k: fetch_calls.append(list(tickers)) or full_prices[list(tickers)],
        )
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={"OLD": 10.0}, total_value=1000.0,
            cfg=cfg, top_n=2, lookback_period=1.0, dry_run=True,
            extra_price_tickers=["OLD"], daily_prices=narrow_prices,
        )

        assert set(fetch_calls[0]) == {"SPY", "QQQ", "OLD"}
        assert "OLD" in orders  # confirms the fallback fetch actually covered OLD too

    def test_daily_prices_default_none_is_byte_identical_to_before(self, monkeypatch, tmp_path):
        fake_prices = self._fake_prices(["SPY", "QQQ"])
        fetch_calls = []
        monkeypatch.setattr(
            live_signal, "fetch_live_prices",
            lambda tickers, **k: fetch_calls.append(list(tickers)) or fake_prices[list(tickers)],
        )
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        live_signal.run(
            tickers=["SPY", "QQQ"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
        )

        assert fetch_calls == [["SPY", "QQQ"]]


class TestRunInsufficientPriceHistoryWarning:
    """
    Defensive backstop: even with compute_required_lookback_days() sizing the fetch correctly,
    a vendor genuinely not having enough real history for any ticker (or a daily_prices
    passed in directly, e.g. by a caller bypassing daily_runner.py) can still leave every
    resampled date NaN. This must be diagnosable, not a silent empty rebalance.
    """

    def _short_daily_prices(self, n_business_days, seed=1):
        dates = pd.bdate_range(end=pd.Timestamp("2026-07-20"), periods=n_business_days)
        rng = np.random.default_rng(seed)
        return pd.DataFrame(
            {"SPY": 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n_business_days))}, index=dates,
        )

    def test_warns_and_logs_alert_when_scores_come_back_empty(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        # Deliberately too little history for lookback_period=18 (needs ~18 monthly bars).
        short_prices = self._short_daily_prices(60)
        monkeypatch.setattr(ls, "fetch_live_prices", lambda tickers, **k: short_prices[list(tickers)])
        monkeypatch.chdir(tmp_path)
        alerts_log_path = str(tmp_path / "alerts_log.csv")

        cfg = BacktestConfig(holding_period=1, lookback_period=18, use_regime_filter=False)
        with caplog.at_level("WARNING"):
            ls.run(
                tickers=["SPY"], current_holdings={}, total_value=1000.0, cfg=cfg,
                top_n=1, lookback_period=18, dry_run=True, portfolio="p1",
                alerts_log_path=alerts_log_path,
            )

        assert any("No valid momentum scores" in r.message for r in caplog.records)
        with open(alerts_log_path) as f:
            content = f.read()
        assert "INSUFFICIENT_PRICE_HISTORY" in content

    def test_no_warning_with_sufficient_history(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        long_prices = self._short_daily_prices(450)
        monkeypatch.setattr(ls, "fetch_live_prices", lambda tickers, **k: long_prices[list(tickers)])
        monkeypatch.chdir(tmp_path)
        alerts_log_path = str(tmp_path / "alerts_log.csv")

        cfg = BacktestConfig(holding_period=1, lookback_period=12, use_regime_filter=False)
        with caplog.at_level("WARNING"):
            ls.run(
                tickers=["SPY"], current_holdings={}, total_value=1000.0, cfg=cfg,
                top_n=1, lookback_period=12, dry_run=True, portfolio="p1",
                alerts_log_path=alerts_log_path,
            )

        assert not any("No valid momentum scores" in r.message for r in caplog.records)


class TestRunAbsoluteMomentumFilter:
    """
    cfg.use_absolute_momentum end-to-end through run(), wired right after picks are selected
    (before signal_context/sizing/vol-scaling/regime-filtering all act on the FINAL pick list),
    Epic 2 of the layered risk-management plan. BIL is priced via extra_price_tickers here
    purely to isolate the test (proves the OVERLAY, not natural relative ranking, is what
    introduces it); production usage should add defensive_ticker to the portfolio's own
    tickers: list instead (documented on BacktestConfig.defensive_ticker).
    """

    def _declining_and_flat_prices(self, seed=5):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(seed)
        n = len(dates)
        a = np.linspace(100, 50, n) * (1 + rng.normal(0, 0.001, n))
        b = np.linspace(100, 60, n) * (1 + rng.normal(0, 0.001, n))
        bil = np.full(n, 100.0) * (1 + rng.normal(0, 0.0001, n))
        return pd.DataFrame({"A": a, "B": b, "BIL": bil}, index=dates)

    def test_all_negative_absolute_momentum_swaps_entire_book_to_defensive_ticker(self, monkeypatch, tmp_path):
        prices = self._declining_and_flat_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              use_absolute_momentum=True, defensive_ticker="BIL")
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True, extra_price_tickers=["BIL"],
        )

        # Both A and B have negative trailing (1-month) momentum, declining monotonically,
        # both get swapped for the defensive ticker instead of held.
        assert set(orders.keys()) == {"BIL"}
        assert orders["BIL"]["action"] == "BUY"

    def test_default_disabled_is_byte_identical_to_before_this_feature(self, monkeypatch, tmp_path):
        # Regression: use_absolute_momentum=False (default) must resolve to the SAME picks as
        # before this feature existed, both A and B held despite negative absolute momentum,
        # mirrors skip_month_guardrail's exact precedent for an opt-in signal-construction change.
        prices = self._declining_and_flat_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
        )

        assert set(orders.keys()) == {"A", "B"}
        assert orders["A"]["action"] == "BUY"
        assert orders["B"]["action"] == "BUY"

    def test_mixed_positive_and_negative_only_swaps_the_negative_one(self, monkeypatch, tmp_path):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(9)
        n = len(dates)
        # A rises (positive absolute momentum), B declines (negative), BIL flat.
        a = np.linspace(50, 100, n) * (1 + rng.normal(0, 0.001, n))
        b = np.linspace(100, 60, n) * (1 + rng.normal(0, 0.001, n))
        bil = np.full(n, 100.0) * (1 + rng.normal(0, 0.0001, n))
        prices = pd.DataFrame({"A": a, "B": b, "BIL": bil}, index=dates)
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              use_absolute_momentum=True, defensive_ticker="BIL")
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True, extra_price_tickers=["BIL"],
        )

        assert set(orders.keys()) == {"A", "BIL"}
        assert orders["A"]["action"] == "BUY"
        assert orders["BIL"]["action"] == "BUY"


class TestRunMultiTimeframeComposite:
    """
    cfg.strategy_type == "multi_timeframe_composite" end-to-end through run(), Epic 2 of the
    selectable-momentum-strategy plan: wires up core/functions_quant_extensions.py's
    blend_momentum_scores() (previously fully coded but dead code) via
    core/strategy_signals.py's resolve_strategy_scores() router.
    """

    def _diverging_prices(self, seed=3, n=400):
        dates = pd.bdate_range("2023-01-01", periods=n)
        rng = np.random.default_rng(seed)
        data = {}
        for name, drift in [("A", 0.0015), ("B", 0.0002), ("C", -0.0005)]:
            data[name] = 100 * np.cumprod(1 + rng.normal(drift, 0.01, n))
        return pd.DataFrame(data, index=dates)

    def test_composite_picks_can_differ_from_default_momentum(self, monkeypatch, tmp_path):
        prices = self._diverging_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        default_cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        composite_cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                                        strategy_type="multi_timeframe_composite",
                                        multi_timeframe_lookbacks=[1, 12])

        default_orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=default_cfg,
            top_n=2, lookback_period=6.0, dry_run=True,
        )
        composite_orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=composite_cfg,
            top_n=2, lookback_period=6.0, dry_run=True,
        )

        # Both runs complete cleanly (no crash), and both actually select real picks (proves
        # the composite branch produced usable, non-empty scores against real synthetic data).
        assert len(default_orders) >= 2
        assert len(composite_orders) >= 2

    def test_default_strategy_type_is_byte_identical_to_before_this_feature(self, monkeypatch, tmp_path):
        # Regression: strategy_type unset (the default "momentum") must resolve identically to
        # every pre-existing call site, same precedent as every other opt-in strategy_type.
        prices = self._diverging_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=6.0, dry_run=True,
        )
        assert len(orders) == 2
        assert all(o["action"] == "BUY" for o in orders.values())


class TestRunAbsoluteMomentumStrategy:
    """
    cfg.strategy_type == "absolute_momentum" end-to-end through run(), Epic 3 of the
    selectable-momentum-strategy plan: a genuinely different selection mode, no cross-sectional
    ranking/top_n cutoff at all, every ticker whose OWN trailing score is positive is held,
    defensive_ticker alone otherwise. Distinct from cfg.use_absolute_momentum (a post-relative-
    ranking swap, still fundamentally a relative-momentum variant underneath, TestRunAbsoluteMomentumFilter above).
    """

    def _mixed_trend_prices(self, seed=11):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(seed)
        n = len(dates)
        # A and B both rise (positive absolute momentum), C declines (negative), BIL flat.
        a = np.linspace(50, 100, n) * (1 + rng.normal(0, 0.001, n))
        b = np.linspace(60, 90, n) * (1 + rng.normal(0, 0.001, n))
        c = np.linspace(100, 50, n) * (1 + rng.normal(0, 0.001, n))
        bil = np.full(n, 100.0) * (1 + rng.normal(0, 0.0001, n))
        return pd.DataFrame({"A": a, "B": b, "C": c, "BIL": bil}, index=dates)

    def test_bypasses_top_n_and_holds_every_positive_trend_ticker(self, monkeypatch, tmp_path):
        prices = self._mixed_trend_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="absolute_momentum", defensive_ticker="BIL")
        orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=1, lookback_period=1.0, dry_run=True, extra_price_tickers=["BIL"],
        )

        # top_n=1 would normally cap this at a single pick, absolute_momentum must ignore that
        # cutoff entirely and hold BOTH positive-trend tickers, C (negative) excluded.
        assert set(orders.keys()) == {"A", "B"}
        assert orders["A"]["action"] == "BUY"
        assert orders["B"]["action"] == "BUY"

    def test_all_negative_universe_resolves_to_defensive_ticker_only(self, monkeypatch, tmp_path):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(13)
        n = len(dates)
        a = np.linspace(100, 50, n) * (1 + rng.normal(0, 0.001, n))
        b = np.linspace(100, 60, n) * (1 + rng.normal(0, 0.001, n))
        bil = np.full(n, 100.0) * (1 + rng.normal(0, 0.0001, n))
        prices = pd.DataFrame({"A": a, "B": b, "BIL": bil}, index=dates)
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="absolute_momentum", defensive_ticker="BIL")
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True, extra_price_tickers=["BIL"],
        )

        assert set(orders.keys()) == {"BIL"}
        assert orders["BIL"]["action"] == "BUY"

    def test_default_strategy_type_is_byte_identical_to_before_this_feature(self, monkeypatch, tmp_path):
        # Regression: strategy_type unset (the default "momentum") must still respect top_n
        # exactly as before this feature existed.
        prices = self._mixed_trend_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=1, lookback_period=1.0, dry_run=True,
        )

        assert len(orders) == 1


class TestRunResidualMomentumStrategy:
    """
    cfg.strategy_type == "residual_momentum" end-to-end through run(), Epic 5 of the
    selectable-momentum-strategy plan: ranks by benchmark-adjusted (idiosyncratic) trailing
    return rather than raw total return. Reuses cfg.regime_benchmark ("SPY" default), priced via
    extra_price_tickers (never widening the ranking universe itself), same isolation pattern
    TestRunAbsoluteMomentumFilter uses for BIL.
    """

    def _beta_vs_alpha_prices(self, seed=3, n=100):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2023-01-01", periods=n)
        bench_returns = rng.normal(0.003, 0.004, n)
        bench_prices = 100 * np.cumprod(1 + bench_returns)
        # A: pure beta=2 leverage, zero idiosyncratic return, the LARGER raw return.
        a_prices = 100 * np.cumprod(1 + 2 * bench_returns)
        # B: beta=1 plus a small constant idiosyncratic daily excess, a SMALLER raw return but
        # genuine alpha.
        b_prices = 100 * np.cumprod(1 + bench_returns + 0.001)
        return pd.DataFrame({"SPY": bench_prices, "A": a_prices, "B": b_prices}, index=dates)

    def test_ranks_genuine_alpha_above_beta_amplified_raw_return(self, monkeypatch, tmp_path):
        prices = self._beta_vs_alpha_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="residual_momentum", regime_benchmark="SPY")
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=1, lookback_period=4.0, dry_run=True, extra_price_tickers=["SPY"],
        )

        # A has the larger RAW return (2x-leveraged benchmark exposure), but top_n=1 must pick
        # B, whose smaller raw return is genuine idiosyncratic alpha, not benchmark-explained.
        assert set(orders.keys()) == {"B"}

    def test_missing_benchmark_price_raises(self, monkeypatch, tmp_path):
        # SPY not requested via extra_price_tickers, so it's never in daily_prices, this
        # strategy cannot compute a score at all without its benchmark, must fail loudly.
        prices = self._beta_vs_alpha_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="residual_momentum", regime_benchmark="SPY")
        with pytest.raises(ValueError, match="SPY"):
            live_signal.run(
                tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
                top_n=1, lookback_period=4.0, dry_run=True,
            )


class TestRunPathDependentMomentumStrategy:
    """
    cfg.strategy_type == "path_dependent_momentum" end-to-end through run(), Epic 6 of the
    selectable-momentum-strategy plan: rewards a smooth trend over a choppy one at the same total
    return. Purely price-based, no benchmark needed (unlike TestRunResidualMomentumStrategy above).
    """

    def _smooth_vs_choppy_prices(self, n=100, total_return=0.5):
        dates = pd.bdate_range("2023-01-01", periods=n)
        t = np.arange(n)
        smooth = 100 * (1 + total_return) ** (t / (n - 1))
        oscillation = 15 * np.sin(t / 3.0) * (1 - t / (n - 1))
        choppy = smooth + oscillation
        choppy[-1] = smooth[-1]
        return pd.DataFrame({"SMOOTH": smooth, "CHOPPY": choppy}, index=dates)

    def test_smooth_trend_beats_choppy_trend_at_identical_total_return(self, monkeypatch, tmp_path):
        prices = self._smooth_vs_choppy_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="path_dependent_momentum")
        orders = live_signal.run(
            tickers=["SMOOTH", "CHOPPY"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=1, lookback_period=4.0, dry_run=True,
        )

        # Identical raw total return, top_n=1 must pick SMOOTH, the higher-R^2 trend.
        assert set(orders.keys()) == {"SMOOTH"}

    def test_default_strategy_type_is_byte_identical_to_before_this_feature(self, monkeypatch, tmp_path):
        prices = self._smooth_vs_choppy_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["SMOOTH", "CHOPPY"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=4.0, dry_run=True,
        )
        assert len(orders) == 2


class TestRunHybridMultiFactorStrategy:
    """
    cfg.strategy_type == "hybrid_multi_factor" end-to-end through run(), Epic 7 of the
    selectable-momentum-strategy plan, LIVE-ONLY. Fundamentals fetching
    (get_cached_or_fetch_fundamentals()) is mocked here, no real network/cache, matching this
    suite's synthetic/mocked-only convention.
    """

    def _momentum_prices(self):
        dates = pd.bdate_range("2023-01-01", periods=90)
        n = len(dates)
        a = np.linspace(100, 150, n)  # strongest raw momentum
        b = np.linspace(100, 120, n)  # moderate raw momentum
        c = np.linspace(100, 110, n)  # weakest raw momentum
        return pd.DataFrame({"A": a, "B": b, "C": c}, index=dates)

    def test_strong_fundamentals_can_overcome_weaker_raw_momentum(self, monkeypatch, tmp_path):
        import momentum_trading.core.strategy_signals as strategy_signals

        fundamentals = {
            "A": {"pe_ratio": 80, "peg_ratio": 5.0, "debt_to_equity": 3.0, "roe": 0.02, "current_ratio": 0.8},
            "B": {"pe_ratio": 12, "peg_ratio": 0.8, "debt_to_equity": 0.3, "roe": 0.25, "current_ratio": 2.5},
            "C": {"pe_ratio": 20, "peg_ratio": 1.5, "debt_to_equity": 1.0, "roe": 0.10, "current_ratio": 1.5},
        }
        monkeypatch.setattr(strategy_signals, "get_cached_or_fetch_fundamentals",
                             lambda ticker, fmp, eodhd: fundamentals.get(ticker, {}))

        prices = self._momentum_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="hybrid_multi_factor")
        orders = live_signal.run(
            tickers=["A", "B", "C"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=1, lookback_period=3.0, dry_run=True,
        )

        # A has the stronger RAW momentum, but B's vastly better fundamentals win the blend.
        assert set(orders.keys()) == {"B"}


class TestFullSignalUniverse:
    """
    run()'s OrdersResult.full_signal_universe: every configured ticker with a valid momentum
    score this rebalance, not just the top_n actually selected, the gap that made a
    ranked-but-not-selected ("watchlist") ticker invisible everywhere before this existed
    (signal_context, feeding the trade log/Table 1, stays scoped to picks only, unchanged).
    """

    def _ranked_prices(self, seed=21):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(seed)
        n = len(dates)
        # Clear, unambiguous rank order: A > B > C > D by trailing trend strength.
        a = np.linspace(50, 100, n) * (1 + rng.normal(0, 0.0005, n))
        b = np.linspace(50, 85, n) * (1 + rng.normal(0, 0.0005, n))
        c = np.linspace(50, 70, n) * (1 + rng.normal(0, 0.0005, n))
        d = np.linspace(50, 55, n) * (1 + rng.normal(0, 0.0005, n))
        return pd.DataFrame({"A": a, "B": b, "C": c, "D": d}, index=dates)

    def test_every_ticker_appears_not_just_top_n_picks(self, monkeypatch, tmp_path):
        prices = self._ranked_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B", "C", "D"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
            log_path=str(tmp_path / "trade_log.csv"),
            signal_rankings_log_path=str(tmp_path / "signal_rankings_log.csv"),
        )

        # Only A/B are actually selected/traded (top_n=2)...
        assert set(orders.keys()) == {"A", "B"}
        # ...but the full ranked universe covers every configured ticker, including C/D which
        # never appear in `orders` at all.
        assert set(orders.full_signal_universe.keys()) == {"A", "B", "C", "D"}

    def test_selection_status_labels_top_n_vs_watchlist(self, monkeypatch, tmp_path):
        prices = self._ranked_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B", "C", "D"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
            log_path=str(tmp_path / "trade_log.csv"),
            signal_rankings_log_path=str(tmp_path / "signal_rankings_log.csv"),
        )
        universe = orders.full_signal_universe

        assert universe["A"]["selection_status"] == "Top 2 (Selected)"
        assert universe["B"]["selection_status"] == "Top 2 (Selected)"
        assert universe["C"]["selection_status"] == "Watchlist / Reserve"
        assert universe["D"]["selection_status"] == "Watchlist / Reserve"
        assert universe["A"]["rank"] == 1
        assert universe["D"]["rank"] == 4

    def test_watchlist_ticker_not_in_orders_gets_zero_money_and_no_stop_loss(self, monkeypatch, tmp_path):
        prices = self._ranked_prices()
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False)
        orders = live_signal.run(
            tickers=["A", "B", "C", "D"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True,
            log_path=str(tmp_path / "trade_log.csv"),
            signal_rankings_log_path=str(tmp_path / "signal_rankings_log.csv"),
        )

        # D is watchlist-only, never in `orders`, callers building the email/log must treat
        # this as $0.00/0.00%/no stop-loss, not KeyError.
        assert "D" not in orders
        assert "D" in orders.full_signal_universe

    def test_absolute_momentum_labels_selected_differently_no_top_n_cutoff(self, monkeypatch, tmp_path):
        dates = pd.bdate_range("2024-01-01", "2024-05-15")
        rng = np.random.default_rng(31)
        n = len(dates)
        a = np.linspace(50, 100, n) * (1 + rng.normal(0, 0.0005, n))  # positive trend
        b = np.linspace(100, 50, n) * (1 + rng.normal(0, 0.0005, n))  # negative trend
        bil = np.full(n, 100.0) * (1 + rng.normal(0, 0.0001, n))
        prices = pd.DataFrame({"A": a, "B": b, "BIL": bil}, index=dates)
        monkeypatch.setattr(live_signal, "fetch_live_prices", lambda tickers, **k: prices[list(tickers)])
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(holding_period=1, use_regime_filter=False,
                              strategy_type="absolute_momentum", defensive_ticker="BIL")
        orders = live_signal.run(
            tickers=["A", "B"], current_holdings={}, total_value=1000.0, cfg=cfg,
            top_n=2, lookback_period=1.0, dry_run=True, extra_price_tickers=["BIL"],
            log_path=str(tmp_path / "trade_log.csv"),
            signal_rankings_log_path=str(tmp_path / "signal_rankings_log.csv"),
        )
        universe = orders.full_signal_universe

        assert universe["A"]["selection_status"] == "Selected (Absolute Momentum)"
        assert universe["B"]["selection_status"] == "Watchlist / Reserve"


class TestLogSignalRankings:
    """
    log_signal_rankings() writes one hash-chained row per full_signal_universe ticker, a
    SEPARATE log from the trade log (log_orders()), reusing core/audit_log.py's
    append_hash_chained_row() directly (same convention verify_log_integrity() already accepts).
    """

    def _universe(self):
        return {
            "A": {"rank": 1, "signal_score": 0.15, "close_price": 100.0, "selection_status": "Top 1 (Selected)"},
            "B": {"rank": 2, "signal_score": 0.05, "close_price": 50.0, "selection_status": "Watchlist / Reserve"},
        }

    def _orders(self):
        return {
            "A": {"action": "BUY", "shares": 5, "reason": "drift $500.00", "money_invested": 500.0,
                  "pct_money_invested": 1.0, "stop_loss_price": 90.0},
        }

    def test_writes_expected_schema(self, tmp_path):
        path = str(tmp_path / "signal_rankings_log.csv")
        log_signal_rankings(self._universe(), self._orders(), dry_run=True, path=path)
        import pandas as pd
        df = pd.read_csv(path)
        assert list(df.columns) == SIGNAL_RANKINGS_LOG_HEADER
        assert len(df) == 2

    def test_selected_ticker_carries_real_order_fields(self, tmp_path):
        path = str(tmp_path / "signal_rankings_log.csv")
        log_signal_rankings(self._universe(), self._orders(), dry_run=True, path=path)
        import pandas as pd
        df = pd.read_csv(path)
        row = df[df["ticker"] == "A"].iloc[0]
        assert row["action"] == "BUY"
        assert row["shares"] == 5
        assert row["money_invested"] == pytest.approx(500.0)
        assert row["stop_loss_price"] == pytest.approx(90.0)

    def test_watchlist_ticker_gets_zeroed_action_and_money(self, tmp_path):
        path = str(tmp_path / "signal_rankings_log.csv")
        log_signal_rankings(self._universe(), self._orders(), dry_run=True, path=path)
        import pandas as pd
        df = pd.read_csv(path)
        row = df[df["ticker"] == "B"].iloc[0]
        assert row["action"] == "WATCHLIST"
        assert row["shares"] == 0
        assert row["money_invested"] == pytest.approx(0.0)
        assert pd.isna(row["stop_loss_price"])

    def test_hash_chain_verifies_clean(self, tmp_path):
        path = str(tmp_path / "signal_rankings_log.csv")
        log_signal_rankings(self._universe(), self._orders(), dry_run=True, path=path)
        result = live_signal.verify_log_integrity(path)
        assert result["valid"] is True
        assert result["rows_checked"] == 2


class TestGetTopEtfs:
    """
    get_top_etfs() is where BacktestConfig.top_n actually takes effect, it's
    the sole gate between "everything in the portfolio's tickers list" and
    "what actually gets sized and traded". daily_runner.py wiring cfg.top_n
    through to this call was previously a silent no-op: run()'s own
    function-default (10) was used regardless of config.yaml, since top_n was
    never passed through. These tests cover the selection behavior itself.
    """

    def _ranks(self):
        # Lower rank = stronger momentum (assign_ranks uses ascending=False on
        # returns, so rank 1 is the best performer). Five tickers, one row.
        return pd.DataFrame(
            {"SPY": [3], "QQQ": [1], "XLK": [2], "XLU": [5], "TLT": [4]},
        )

    def test_top_n_limits_selection_count(self):
        picks = get_top_etfs(self._ranks(), top_n=3)
        assert len(picks) == 3

    def test_top_n_picks_the_strongest_ranked(self):
        # top_n=3 should be exactly the 3 lowest ranks: QQQ(1), XLK(2), SPY(3),
        # not an arbitrary/unordered subset.
        picks = get_top_etfs(self._ranks(), top_n=3)
        assert set(picks) == {"QQQ", "XLK", "SPY"}

    def test_top_n_larger_than_universe_returns_whole_universe(self):
        # Mirrors daily_runner.py's min(cfg.top_n, len(tickers)) clamp being
        # unnecessary in practice, nsmallest() degrades gracefully on its own.
        picks = get_top_etfs(self._ranks(), top_n=10)
        assert len(picks) == 5


class TestApplyAbsoluteMomentumFilter:
    """
    apply_absolute_momentum_filter(), the live-trading wrapper around
    core/functions_quant_extensions.py's absolute_momentum_overlay() (Epic 2 of the layered
    risk-management plan). That function was fully coded (Antonacci-style dual momentum: any
    pick with a negative own trailing return gets swapped for a defensive/cash ticker) but
    called NOWHERE, this wrapper reuses it directly (wraps the single live picks list in a
    length-1 Series, calls the shared function, unwraps the result) rather than reimplementing
    the swap rule, so backtest and live can never diverge on it.
    """

    def test_all_positive_picks_unchanged(self):
        picks = ["A", "B"]
        scores = pd.Series({"A": 0.05, "B": 0.02})
        result = apply_absolute_momentum_filter(picks, scores, defensive_ticker="BIL")
        assert result == ["A", "B"]

    def test_some_negative_picks_swapped_with_duplicates_collapsed(self):
        picks = ["A", "B", "C"]
        scores = pd.Series({"A": 0.05, "B": -0.01, "C": -0.02})
        result = apply_absolute_momentum_filter(picks, scores, defensive_ticker="BIL")
        assert result == ["A", "BIL"]

    def test_all_negative_picks_resolves_to_defensive_ticker_only(self):
        picks = ["A", "B"]
        scores = pd.Series({"A": -0.01, "B": -0.02})
        result = apply_absolute_momentum_filter(picks, scores, defensive_ticker="BIL")
        assert result == ["BIL"]

    def test_none_scores_returns_picks_unchanged(self):
        # No momentum_scores available (e.g. empty ranking history) -> conservative no-op,
        # not an error, matches this file's existing "unavailable data degrades gracefully"
        # convention (e.g. staleness/None checks elsewhere in this module).
        picks = ["A", "B"]
        result = apply_absolute_momentum_filter(picks, None, defensive_ticker="BIL")
        assert result == ["A", "B"]


class TestResolveTickerStopLossPct:
    """
    Per-ticker, per-portfolio stop-loss override (BacktestConfig.ticker_risk_overrides),
    docs/RISK_CONSTRAINTS.md's "Per-Ticker Stop-Loss Override".
    """
    def test_no_override_falls_back_to_portfolio_default(self):
        cfg = BacktestConfig(stop_loss_pct=0.12)
        assert resolve_ticker_stop_loss_pct("AAPL", cfg) == pytest.approx(0.12)

    def test_disabled_ticker_returns_none(self):
        cfg = BacktestConfig(stop_loss_pct=0.12, ticker_risk_overrides={"AAPL": {"enabled": False}})
        assert resolve_ticker_stop_loss_pct("AAPL", cfg) is None

    def test_disabled_ticker_does_not_affect_other_tickers(self):
        cfg = BacktestConfig(stop_loss_pct=0.12, ticker_risk_overrides={"AAPL": {"enabled": False}})
        assert resolve_ticker_stop_loss_pct("MSFT", cfg) == pytest.approx(0.12)

    def test_custom_pct_overrides_portfolio_default(self):
        cfg = BacktestConfig(stop_loss_pct=0.12,
                              ticker_risk_overrides={"AMD": {"enabled": True, "stop_loss_pct": 0.08}})
        assert resolve_ticker_stop_loss_pct("AMD", cfg) == pytest.approx(0.08)

    def test_custom_pct_without_explicit_enabled_still_applies(self):
        # 'enabled' defaults to True when the key carries a 'stop_loss_pct' but omits it.
        cfg = BacktestConfig(stop_loss_pct=0.12, ticker_risk_overrides={"AMD": {"stop_loss_pct": 0.08}})
        assert resolve_ticker_stop_loss_pct("AMD", cfg) == pytest.approx(0.08)


class TestTickerRiskOverridesValidation:
    """BacktestConfig.__post_init__ validation for ticker_risk_overrides."""
    def test_valid_overrides_construct_cleanly(self):
        BacktestConfig(ticker_risk_overrides={"AAPL": {"enabled": False}, "AMD": {"stop_loss_pct": 0.08}})

    def test_non_dict_rejected(self):
        with pytest.raises(ValueError):
            BacktestConfig(ticker_risk_overrides=["AAPL"])

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError):
            BacktestConfig(ticker_risk_overrides={"AAPL": {"stop_loss_pct": 0.08, "typo_field": 1}})

    def test_non_bool_enabled_rejected(self):
        with pytest.raises(ValueError):
            BacktestConfig(ticker_risk_overrides={"AAPL": {"enabled": "false"}})

    def test_out_of_range_stop_loss_pct_rejected(self):
        with pytest.raises(ValueError):
            BacktestConfig(ticker_risk_overrides={"AAPL": {"stop_loss_pct": 1.5}})


class TestComputeStopLossPrice:
    """
    Fixed-from-entry stop-loss price reporting (docs/RISK_CONSTRAINTS.md's "Stop-Loss Width"),
    NOT a trailing stop, these tests confirm each of the three real cases and the "no price"
    guard, matching the function's own docstring exactly.
    """
    def test_buy_uses_latest_price_as_estimate(self):
        cfg = BacktestConfig(stop_loss_pct=0.10)
        assert compute_stop_loss_price("BUY", cfg, 100.0) == pytest.approx(90.0)

    def test_hold_with_avg_entry_price_uses_real_entry(self):
        cfg = BacktestConfig(stop_loss_pct=0.20)
        # latest_price is deliberately different from avg_entry_price, the stop must be
        # computed from the REAL entry, not today's close, unlike the BUY estimate above.
        assert compute_stop_loss_price("HOLD", cfg, 150.0, avg_entry_price=100.0) == pytest.approx(80.0)

    def test_hold_without_avg_entry_price_is_none(self):
        # dry-run, or any caller that didn't pass current_avg_entry_prices, matching the
        # documented "position-performance fields are live-only" pattern.
        cfg = BacktestConfig(stop_loss_pct=0.10)
        assert compute_stop_loss_price("HOLD", cfg, 100.0) is None

    def test_sell_is_none(self):
        cfg = BacktestConfig(stop_loss_pct=0.10)
        assert compute_stop_loss_price("SELL", cfg, 100.0, avg_entry_price=90.0) is None

    def test_no_price_is_none(self):
        cfg = BacktestConfig(stop_loss_pct=0.10)
        assert compute_stop_loss_price("BUY", cfg, None) is None
        assert compute_stop_loss_price("BUY", cfg, 0.0) is None

    def test_ticker_disabled_override_returns_none_even_with_a_price(self):
        cfg = BacktestConfig(stop_loss_pct=0.10, ticker_risk_overrides={"AAPL": {"enabled": False}})
        assert compute_stop_loss_price("BUY", cfg, 100.0, ticker="AAPL") is None

    def test_ticker_custom_pct_override_is_used(self):
        cfg = BacktestConfig(stop_loss_pct=0.10,
                              ticker_risk_overrides={"AMD": {"stop_loss_pct": 0.25}})
        assert compute_stop_loss_price("BUY", cfg, 100.0, ticker="AMD") == pytest.approx(75.0)

    def test_no_ticker_param_falls_back_to_portfolio_default(self):
        # Regression: omitting ticker (every pre-existing call site) must stay byte-identical.
        cfg = BacktestConfig(stop_loss_pct=0.10, ticker_risk_overrides={"AMD": {"stop_loss_pct": 0.25}})
        assert compute_stop_loss_price("BUY", cfg, 100.0) == pytest.approx(90.0)


class TestGenerateOrders:
    """
    generate_orders() is where target weights become concrete BUY/SELL/HOLD
    decisions with real share counts, bugs here directly translate to wrong
    trades, so these tests focus on the boundary behaviors most likely to be
    wrong: direction (buy vs sell), the min-trade-size cost filter, and
    whole-vs-fractional share rounding (a real source of confusion since the
    backtest engine and this live path must round identically or their
    results silently diverge).
    """

    def test_produces_buy_and_sell(self):
        # Confirms direction is correct in both directions simultaneously
        # (SPY/QQQ need to shrink, XLK needs to grow), a sign error here
        # would be the single worst possible bug in this codebase.
        cfg = BacktestConfig(drift_threshold=0.03, min_trade_size=25.0)
        orders = generate_orders(
            current_holdings={"SPY": 2, "QQQ": 1},
            target_weights={"SPY": 0.5, "XLK": 0.5},
            gross_exposure=1.0, total_value=1000.0,
            latest_prices={"SPY": 550.0, "QQQ": 480.0, "XLK": 220.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "SELL"
        assert orders["XLK"]["action"] == "BUY"
        assert orders["QQQ"]["action"] == "SELL"

    def test_below_min_trade_size_is_hold(self):
        # Confirms the cost-control filter actually suppresses tiny trades
        # rather than executing them anyway, this is the mechanism that
        # keeps turnover/commission drag down on small accounts.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1000.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"SPY": 1.0}, gross_exposure=1.0,
            total_value=100.0, latest_prices={"SPY": 550.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "HOLD"

    def test_fractional_shares_when_enabled(self):
        # allow_fractional_shares=True should size to a real fraction of a
        # share (1000/220=4.5454...), not silently floor to a whole number,
        # confirms the flag actually changes behavior, not just accepted syntax.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0, allow_fractional_shares=True)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["shares"] == pytest.approx(4.5454, abs=1e-3)

    def test_whole_shares_by_default(self):
        # The default (allow_fractional_shares=False) must floor to a whole
        # int, matching the backtest engine's default rounding, if this ever
        # returned a float by mistake, downstream integer-assuming code
        # (e.g. IBKR order quantity formatting) could behave unexpectedly.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["shares"] == 4
        assert isinstance(orders["XLK"]["shares"], int)

    def test_money_invested_is_target_dollar_not_drift(self):
        # money_invested is each ticker's TARGET allocation (total_value * gross_exposure *
        # weight), not the incremental drift_dollar the BUY/SELL decision is based on. SPY is
        # already fully sized (small drift, correctly a HOLD) but still carries a real
        # money_invested figure equal to its target weight's dollar value.
        cfg = BacktestConfig(drift_threshold=0.5, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={"SPY": 1.0}, target_weights={"SPY": 0.5, "XLK": 0.5},
            gross_exposure=1.0, total_value=1000.0,
            latest_prices={"SPY": 500.0, "XLK": 220.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "HOLD"
        assert orders["SPY"]["money_invested"] == pytest.approx(500.0)
        assert orders["XLK"]["money_invested"] == pytest.approx(500.0)

    def test_pct_money_invested_matches_target_weight(self):
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"SPY": 0.6, "XLK": 0.4}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"SPY": 500.0, "XLK": 220.0}, cfg=cfg,
        )
        assert orders["SPY"]["pct_money_invested"] == pytest.approx(0.6)
        assert orders["XLK"]["pct_money_invested"] == pytest.approx(0.4)

    def test_pct_money_invested_scales_with_gross_exposure(self):
        # gross_exposure < 1.0 (vol-scaling/regime throttling) shrinks the capital actually
        # being deployed; pct_money_invested is relative to THAT deployed capital (target
        # weights still sum to 1.0), not to the full total_value.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"SPY": 1.0}, gross_exposure=0.5,
            total_value=1000.0, latest_prices={"SPY": 500.0}, cfg=cfg,
        )
        assert orders["SPY"]["money_invested"] == pytest.approx(500.0)  # 1000 * 0.5 * 1.0
        assert orders["SPY"]["pct_money_invested"] == pytest.approx(1.0)

    def test_no_live_price_hold_still_carries_money_invested(self):
        # The earliest-return HOLD branch (missing price) must not skip money_invested, or the
        # email/log would silently show $0 for a ticker that still has a real target allocation.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"SPY": 0.5, "GHOST": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"SPY": 500.0}, cfg=cfg,  # GHOST has no price
        )
        assert orders["GHOST"]["action"] == "HOLD"
        assert orders["GHOST"]["reason"] == "no live price available"
        assert orders["GHOST"]["money_invested"] == pytest.approx(500.0)
        assert orders["GHOST"]["pct_money_invested"] == pytest.approx(0.5)

    def test_buy_order_carries_estimated_stop_loss_price(self):
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0, stop_loss_pct=0.10)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["action"] == "BUY"
        assert orders["XLK"]["stop_loss_price"] == pytest.approx(198.0)  # 220 * (1 - 0.10)

    def test_hold_on_open_position_uses_real_entry_price_when_provided(self):
        cfg = BacktestConfig(drift_threshold=0.5, min_trade_size=1.0, stop_loss_pct=0.20)
        orders = generate_orders(
            current_holdings={"SPY": 1.0}, target_weights={"SPY": 1.0}, gross_exposure=1.0,
            total_value=500.0, latest_prices={"SPY": 500.0}, cfg=cfg,
            current_avg_entry_prices={"SPY": 400.0},
        )
        assert orders["SPY"]["action"] == "HOLD"
        assert orders["SPY"]["stop_loss_price"] == pytest.approx(320.0)  # 400 * (1 - 0.20)

    def test_hold_without_avg_entry_prices_has_no_stop_loss_price(self):
        # Default behavior (no current_avg_entry_prices passed), byte-identical to before this
        # param existed: stop_loss_price is None for a HOLD on an already-open position.
        cfg = BacktestConfig(drift_threshold=0.5, min_trade_size=1.0, stop_loss_pct=0.20)
        orders = generate_orders(
            current_holdings={"SPY": 1.0}, target_weights={"SPY": 1.0}, gross_exposure=1.0,
            total_value=500.0, latest_prices={"SPY": 500.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "HOLD"
        assert orders["SPY"]["stop_loss_price"] is None

    def test_sell_order_has_no_stop_loss_price(self):
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0, stop_loss_pct=0.10)
        orders = generate_orders(
            current_holdings={"SPY": 5.0}, target_weights={}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"SPY": 500.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "SELL"
        assert orders["SPY"]["stop_loss_price"] is None

    def test_sold_out_ticker_has_zero_money_invested(self):
        # A ticker held but no longer in the target universe at all (full exit) correctly
        # contributes $0 to money_invested, it's not part of this rebalance's allocation.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={"OLD": 5.0}, target_weights={"SPY": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"OLD": 100.0, "SPY": 500.0}, cfg=cfg,
        )
        assert orders["OLD"]["action"] == "SELL"
        assert orders["OLD"]["money_invested"] == pytest.approx(0.0)

    def test_money_invested_sums_to_capital_this_rebalance(self):
        # Summed across every ticker generate_orders() returns, money_invested totals exactly
        # total_value * gross_exposure, the exact invariant the rebalance email's capital
        # header relies on.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={"OLD": 5.0},
            target_weights={"SPY": 0.5, "XLK": 0.3, "QQQ": 0.2}, gross_exposure=0.8,
            total_value=1000.0,
            latest_prices={"OLD": 100.0, "SPY": 500.0, "XLK": 220.0, "QQQ": 400.0}, cfg=cfg,
        )
        total_invested = sum(o["money_invested"] for o in orders.values())
        assert total_invested == pytest.approx(1000.0 * 0.8)


class TestFlooringRemainderRedeployment:
    """
    cfg.redeploy_flooring_remainder (opt-in): pools the whole-share-flooring leftover across
    this rebalance's BUYs and redeploys it as extra whole shares of the single top-ranked BUY
    ticker. docs/RISK_CONSTRAINTS.md's "Flooring Remainder Redeployment".
    """

    def _cfg(self, **kwargs):
        return BacktestConfig(drift_threshold=0.0, min_trade_size=1.0,
                               redeploy_flooring_remainder=True, **kwargs)

    def test_default_off_is_byte_identical_to_before(self):
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)  # flag omitted, default False
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 0.5, "B": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 270.0, "B": 130.0}, cfg=cfg,
            signal_context={"A": {"rank": 1, "signal_score": 0.2}, "B": {"rank": 2, "signal_score": 0.1}},
        )
        assert orders["A"]["shares"] == 1  # no extra share, unchanged flooring behavior
        assert orders["B"]["shares"] == 3

    def test_explicit_false_is_also_byte_identical(self):
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0, redeploy_flooring_remainder=False)
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 0.5, "B": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 270.0, "B": 130.0}, cfg=cfg,
            signal_context={"A": {"rank": 1, "signal_score": 0.2}, "B": {"rank": 2, "signal_score": 0.1}},
        )
        assert orders["A"]["shares"] == 1
        assert orders["B"]["shares"] == 3

    def test_pooled_remainder_lands_on_top_ranked_pick(self):
        # A: target $500 @ $270 -> floors to 1 share ($270), remainder $230
        # B: target $500 @ $130 -> floors to 3 shares ($390), remainder $110
        # Pooled = $340, top-ranked (rank 1) is A, extra_shares = floor(340/270) = 1
        cfg = self._cfg()
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 0.5, "B": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 270.0, "B": 130.0}, cfg=cfg,
            signal_context={"A": {"rank": 1, "signal_score": 0.2}, "B": {"rank": 2, "signal_score": 0.1}},
        )
        assert orders["A"]["shares"] == 2  # 1 original + 1 redeployed
        assert orders["B"]["shares"] == 3  # unchanged
        assert "extra share" in orders["A"]["reason"]

    def test_remainder_too_small_for_even_one_extra_share_is_a_noop(self):
        cfg = self._cfg()
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 1.0}, gross_exposure=1.0,
            total_value=100.0, latest_prices={"A": 99.0}, cfg=cfg,
            signal_context={"A": {"rank": 1, "signal_score": 0.2}},
        )
        # target $100 @ $99 -> floors to 1 share ($99), remainder $1, can't afford another $99 share
        assert orders["A"]["shares"] == 1
        assert "extra share" not in orders["A"]["reason"]

    def test_zero_buys_this_rebalance_is_a_safe_noop(self):
        cfg = self._cfg()
        orders = generate_orders(
            current_holdings={"A": 10.0}, target_weights={}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 100.0}, cfg=cfg,
        )
        assert orders["A"]["action"] == "SELL"  # no BUY at all this rebalance, nothing to redeploy into

    def test_allow_fractional_shares_disables_redeployment(self):
        # Nothing to pool when shares are never floored to whole numbers in the first place.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0,
                              redeploy_flooring_remainder=True, allow_fractional_shares=True)
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 0.5, "B": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 270.0, "B": 130.0}, cfg=cfg,
            signal_context={"A": {"rank": 1, "signal_score": 0.2}, "B": {"rank": 2, "signal_score": 0.1}},
        )
        # generate_orders() truncates fractional shares to 4dp (np.floor(x * 10_000) / 10_000),
        # matching that precision here rather than comparing against the untruncated division.
        assert orders["A"]["shares"] == pytest.approx(1.8518, abs=1e-4)
        assert orders["B"]["shares"] == pytest.approx(3.8461, abs=1e-4)

    def test_falls_back_to_first_buy_when_no_rank_info_available(self):
        # e.g. custom_weights sizing, no signal_context provided at all. Which ticker absorbs
        # the remainder is arbitrary here (no rank to break the tie), and the resulting total
        # share count depends on which one it lands on (a cheaper ticker affords more extra
        # shares from the same pooled dollar remainder), so this only asserts it doesn't crash
        # and DOES redeploy something, not an exact total.
        cfg = self._cfg()
        orders = generate_orders(
            current_holdings={}, target_weights={"A": 0.5, "B": 0.5}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"A": 270.0, "B": 130.0}, cfg=cfg,
        )
        baseline_total = 1 + 3  # A floors to 1 share, B floors to 3, before any redeployment
        total_shares = orders["A"]["shares"] + orders["B"]["shares"]
        assert total_shares > baseline_total
        assert "extra share" in orders["A"]["reason"] or "extra share" in orders["B"]["reason"]


class TestMeasureLivePerformance:
    """
    measure_live_performance() computes REAL money math (FIFO realized/
    unrealized P&L) directly from the trade log CSV, this is what an
    investor would actually see as "how much have I made or lost." A bug
    here means reporting wrong dollar amounts, so the math is checked by
    hand in the test itself, not just asserted against another function's output.
    """

    def test_fifo_realized_and_unrealized_pnl(self, tmp_path):
        # Hand-verifiable: buy 5 @ $200, sell 2 @ $220 -> 2*(220-200) = $40
        # realized. Remaining 3 shares @ $200 cost basis, marked at $230 ->
        # 3*(230-200) = $90 unrealized. Total $130. If FIFO lot-matching logic
        # is ever changed, this test catches a wrong-order matching bug
        # immediately via a wrong dollar figure, not just a crash.
        log_path = tmp_path / "trades.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", True])
            w.writerow(["2026-02-02T09:35:00", "XLK", "SELL", 2, 220.0, "trim", True])

        result = measure_live_performance("2026-01-01", "2026-03-01",
                                           latest_prices={"XLK": 230.0}, log_path=str(log_path))
        assert result["realized_pnl"] == pytest.approx(40.0)
        assert result["unrealized_pnl"] == pytest.approx(90.0)
        assert result["total_pnl"] == pytest.approx(130.0)
        assert result["open_positions"]["XLK"] == pytest.approx(3.0)
        # The 2-share SELL was fully matched against the original 5-share $200 lot (FIFO),
        # so the remaining 3 shares are still entirely at the original $200 cost basis.
        assert result["open_position_avg_cost"]["XLK"] == pytest.approx(200.0)

    def test_missing_log_raises(self, tmp_path):
        # Should fail loudly (no log = no data to measure) rather than
        # silently returning zero P&L, which could be mistaken for "no
        # activity" instead of "the file path is wrong."
        with pytest.raises(FileNotFoundError):
            measure_live_performance("2026-01-01", "2026-03-01", log_path=str(tmp_path / "nonexistent.csv"))

    def test_dry_run_filter_excludes_the_other_mode(self, tmp_path):
        # log_orders() writes both dry-run and live rows to the SAME file, without
        # filtering, a report could silently mix simulated and real fills.
        log_path = tmp_path / "trades.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", True])   # dry-run
            w.writerow(["2026-01-06T09:35:00", "XLK", "BUY", 2, 210.0, "entry", False])  # live

        live_only = measure_live_performance("2026-01-01", "2026-03-01", log_path=str(log_path), dry_run=False)
        assert live_only["trade_count"] == 1
        assert live_only["open_positions"]["XLK"] == pytest.approx(2.0)

        dry_run_only = measure_live_performance("2026-01-01", "2026-03-01", log_path=str(log_path), dry_run=True)
        assert dry_run_only["trade_count"] == 1
        assert dry_run_only["open_positions"]["XLK"] == pytest.approx(5.0)


class TestReconstructDryRunPositions:
    """
    Backs the opt-in dry-run persistence feature (daily_runner.py's persist_dry_run_state
    flag, default off): reconstructs a current_positions-shaped dict
    ({ticker: {'shares', 'avg_entry_price'}}, the same shape get_ibkr_positions() returns)
    from the trade log's dry_run=True rows only, via measure_live_performance()'s EXISTING
    FIFO open_positions/open_position_avg_cost computation, not a new, separately-maintained
    FIFO implementation.
    """

    def test_missing_log_returns_empty_dict(self, tmp_path):
        assert live_signal.reconstruct_dry_run_positions(str(tmp_path / "no_such_log.csv")) == {}

    def test_reconstructs_shares_and_avg_cost_from_dry_run_rows_only(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", True])    # dry-run
            w.writerow(["2026-01-06T09:35:00", "XLK", "BUY", 2, 210.0, "entry", False])   # live, excluded

        positions = live_signal.reconstruct_dry_run_positions(str(log_path))
        assert positions == {"XLK": {"shares": pytest.approx(5.0), "avg_entry_price": pytest.approx(200.0)}}

    def test_fully_closed_dry_run_position_is_absent(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", True])
            w.writerow(["2026-02-02T09:35:00", "XLK", "SELL", 5, 220.0, "exit", True])

        assert live_signal.reconstruct_dry_run_positions(str(log_path)) == {}

    def test_weighted_average_cost_across_two_buys(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 4, 100.0, "entry", True])
            w.writerow(["2026-02-02T09:35:00", "XLK", "BUY", 4, 200.0, "add", True])
        # (4*100 + 4*200) / 8 = 150.0
        positions = live_signal.reconstruct_dry_run_positions(str(log_path))
        assert positions["XLK"]["shares"] == pytest.approx(8.0)
        assert positions["XLK"]["avg_entry_price"] == pytest.approx(150.0)


class TestDeriveOwnLivePositions:
    """
    Live counterpart to TestReconstructDryRunPositions above: reconstructs a
    current_positions-shaped dict from the trade log's dry_run=False rows only, via the
    SAME shared _positions_from_trade_log() helper (not a second, separately-maintained FIFO
    implementation). Backs Epic 1 of the cross-portfolio-sell-prevention plan: this is "what
    does THIS portfolio's own log show it holds," independent of what the shared broker
    account shows for a ticker overall.
    """

    def test_missing_log_returns_empty_dict(self, tmp_path):
        assert live_signal.derive_own_live_positions(str(tmp_path / "no_such_log.csv")) == {}

    def test_reconstructs_shares_and_avg_cost_from_live_rows_only(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", False])   # live
            w.writerow(["2026-01-06T09:35:00", "XLK", "BUY", 2, 210.0, "entry", True])    # dry-run, excluded

        positions = live_signal.derive_own_live_positions(str(log_path))
        assert positions == {"XLK": {"shares": pytest.approx(5.0), "avg_entry_price": pytest.approx(200.0)}}

    def test_fully_closed_live_position_is_absent(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", False])
            w.writerow(["2026-02-02T09:35:00", "XLK", "SELL", 5, 220.0, "exit", False])

        assert live_signal.derive_own_live_positions(str(log_path)) == {}

    def test_weighted_average_cost_across_two_buys(self, tmp_path):
        log_path = tmp_path / "log.csv"
        with open(log_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 4, 100.0, "entry", False])
            w.writerow(["2026-02-02T09:35:00", "XLK", "BUY", 4, 200.0, "add", False])
        # (4*100 + 4*200) / 8 = 150.0
        positions = live_signal.derive_own_live_positions(str(log_path))
        assert positions["XLK"]["shares"] == pytest.approx(8.0)
        assert positions["XLK"]["avg_entry_price"] == pytest.approx(150.0)


class TestRunMultiPortfolio:
    """
    run_multi_portfolio() must keep each portfolio's signal, sizing, and
    trade log fully independent, these tests confirm both the current
    dict-based input shape (with per-portfolio custom_weights) and the older
    plain-list shape (kept for backward compatibility) work, and that
    separate log files are actually created per portfolio, not merged.
    """

    def _mock_fetch(self, tickers, lookback_days=400, fmp_api_key=None, eodhd_api_key=None):
        # Deterministic (seeded) synthetic price panel, isolates this test
        # from network access and from real-vendor data changing over time.
        dates = pd.bdate_range("2025-01-01", "2026-07-09")
        rng = np.random.default_rng(1)
        data = {t: np.cumprod(1 + rng.normal(0.0005, 0.01, len(dates))) * 100 for t in tickers}
        return pd.DataFrame(data, index=dates)

    def test_dict_shape_with_custom_weights(self, monkeypatch, tmp_path):
        # Confirms two portfolios with DIFFERENT settings (one algorithmic,
        # one hand-specified weights) both run correctly in the same call and
        # log to separate files, the core "multiple portfolios, same
        # strategy" guarantee this function exists to provide.
        monkeypatch.setattr(live_signal, "fetch_live_prices", self._mock_fetch)
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(use_regime_filter=False)
        portfolios = {
            "p1": {"tickers": ["SPY", "QQQ", "XLK"], "custom_weights": None},
            "p2": {"tickers": ["XLF", "XLE", "GLD", "TLT"],
                   "custom_weights": {"XLF": 0.4, "XLE": 0.3, "GLD": 0.2, "TLT": 0.1}},
        }
        results = run_multi_portfolio(portfolios, total_value_per_portfolio=1000.0, cfg=cfg, top_n=3, dry_run=True)
        assert "p1" in results and "p2" in results
        assert (tmp_path / "live_trades_log_p1.csv").exists()
        assert (tmp_path / "live_trades_log_p2.csv").exists()

    def test_backward_compat_list_shape(self, monkeypatch, tmp_path):
        # The portfolios input shape changed from plain ticker lists to
        # {"tickers": [...], "custom_weights": ...} dicts during this
        # project. This guards against silently breaking anyone (or any
        # existing config) still using the older, simpler shape.
        monkeypatch.setattr(live_signal, "fetch_live_prices", self._mock_fetch)
        monkeypatch.chdir(tmp_path)

        cfg = BacktestConfig(use_regime_filter=False)
        old_shape = {"legacy": ["SPY", "QQQ"]}
        results = run_multi_portfolio(old_shape, total_value_per_portfolio=500.0, cfg=cfg, top_n=2, dry_run=True)
        assert "legacy" in results


class TestComputeAggregateDrift:
    """
    Live-trading equivalent of the backtest's aggregate-drift
    skip, same formula, extracted as a pure function so it's directly
    unit-testable without a live price feed. Hand-verifiable numbers, not just
    "ran without error" (matching this suite's convention for numeric claims).
    """

    def test_matches_hand_calculation(self):
        # |600-500| + |400-400| = 100; 100 / 1000 = 0.10
        drift = compute_aggregate_drift(
            target_dollar={"A": 600.0, "B": 400.0},
            current_value={"A": 500.0, "B": 400.0},
            total_value=1000.0,
        )
        assert drift == pytest.approx(0.10)

    def test_full_exit_counts_as_drift(self):
        # A ticker with no target (full exit) still contributes its whole
        # current value to the drift sum, 200 / 1000 = 0.20.
        drift = compute_aggregate_drift(
            target_dollar={}, current_value={"A": 200.0}, total_value=1000.0,
        )
        assert drift == pytest.approx(0.20)

    def test_zero_total_value_returns_zero_not_divide_error(self):
        assert compute_aggregate_drift({"A": 100.0}, {"A": 50.0}, 0.0) == 0.0


class TestDeriveEntryDate:
    """
    Live-side equivalent of the backtest's entry_dates
    tracking, entry date must persist across partial adds/trims and reset
    only when the position was last FULLY flat, matching the backtest's exact
    semantics (not just "most recent BUY", which would understate days_held
    for a position that's simply been added to).
    """

    def _write_log(self, tmp_path, rows):
        path = tmp_path / "log.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares"])
            w.writerows(rows)
        return str(path)

    def test_single_buy(self, tmp_path):
        path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "XLK", "BUY", 5]])
        assert derive_entry_date("XLK", path) == pd.Timestamp("2026-01-05T09:35:00")

    def test_partial_add_does_not_reset_entry_date(self, tmp_path):
        path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "XLK", "BUY", 5],
            ["2026-02-02T09:35:00", "XLK", "BUY", 3],   # adds to the still-open position
        ])
        assert derive_entry_date("XLK", path) == pd.Timestamp("2026-01-05T09:35:00")

    def test_partial_trim_does_not_reset_entry_date(self, tmp_path):
        path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "XLK", "BUY", 10],
            ["2026-02-02T09:35:00", "XLK", "SELL", 4],  # trims but doesn't fully exit
        ])
        assert derive_entry_date("XLK", path) == pd.Timestamp("2026-01-05T09:35:00")

    def test_full_exit_then_reentry_resets_entry_date(self, tmp_path):
        path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "XLK", "BUY", 5],
            ["2026-02-02T09:35:00", "XLK", "SELL", 5],  # fully flat here
            ["2026-03-02T09:35:00", "XLK", "BUY", 3],   # brand new position
        ])
        assert derive_entry_date("XLK", path) == pd.Timestamp("2026-03-02T09:35:00")

    def test_currently_flat_returns_none(self, tmp_path):
        path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "XLK", "BUY", 5],
            ["2026-02-02T09:35:00", "XLK", "SELL", 5],
        ])
        assert derive_entry_date("XLK", path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert derive_entry_date("XLK", str(tmp_path / "does_not_exist.csv")) is None

    def test_no_rows_for_ticker_returns_none(self, tmp_path):
        path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "QQQ", "BUY", 5]])
        assert derive_entry_date("XLK", path) is None


class TestBuildPositionPerformance:
    """
    build_position_performance() surfaces per-ticker return-since-entry for the reports'
    "Position Performance" section, reusing avg_entry_price (already tracked in
    current_positions for stop-loss gating) and derive_entry_date() (already used for
    time-stop gating), just not previously rendered anywhere.
    """

    def _write_log(self, tmp_path, rows):
        path = tmp_path / "log.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares"])
            w.writerows(rows)
        return str(path)

    def test_computes_return_since_entry(self, tmp_path):
        log_path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "XLK", "BUY", 10]])
        current_positions = {"XLK": {"shares": 10, "avg_entry_price": 50.0}}
        latest_prices = {"XLK": 55.0}

        result = build_position_performance(current_positions, latest_prices, log_path)

        assert result["XLK"]["entry_date"] == pd.Timestamp("2026-01-05T09:35:00")
        assert result["XLK"]["entry_price"] == 50.0
        assert result["XLK"]["current_price"] == 55.0
        assert result["XLK"]["shares"] == 10
        assert result["XLK"]["return_pct"] == pytest.approx(0.10)
        assert result["XLK"]["market_value"] == pytest.approx(550.0)

    def test_ticker_missing_entry_price_is_omitted(self, tmp_path):
        log_path = self._write_log(tmp_path, [])
        current_positions = {"XLK": {"shares": 10, "avg_entry_price": None}}
        latest_prices = {"XLK": 55.0}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert result == {}

    def test_ticker_with_zero_shares_is_omitted(self, tmp_path):
        log_path = self._write_log(tmp_path, [])
        current_positions = {"XLK": {"shares": 0, "avg_entry_price": 50.0}}
        latest_prices = {"XLK": 55.0}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert result == {}

    def test_ticker_missing_from_latest_prices_is_omitted(self, tmp_path):
        log_path = self._write_log(tmp_path, [])
        current_positions = {"XLK": {"shares": 10, "avg_entry_price": 50.0}}
        latest_prices = {}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert result == {}

    def test_undeterminable_entry_date_does_not_omit_the_row(self, tmp_path):
        # Trade log doesn't cover this ticker's history (e.g. predates the log), the row
        # still renders with entry_date=None rather than being dropped entirely.
        log_path = self._write_log(tmp_path, [])
        current_positions = {"XLK": {"shares": 10, "avg_entry_price": 50.0}}
        latest_prices = {"XLK": 55.0}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert "XLK" in result
        assert result["XLK"]["entry_date"] is None

    def test_negative_return_computed_correctly(self, tmp_path):
        log_path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "XLK", "BUY", 10]])
        current_positions = {"XLK": {"shares": 10, "avg_entry_price": 50.0}}
        latest_prices = {"XLK": 45.0}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert result["XLK"]["return_pct"] == pytest.approx(-0.10)

    def test_multiple_tickers_independent(self, tmp_path):
        log_path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "XLK", "BUY", 10],
            ["2026-02-01T09:35:00", "XLF", "BUY", 20],
        ])
        current_positions = {
            "XLK": {"shares": 10, "avg_entry_price": 50.0},
            "XLF": {"shares": 20, "avg_entry_price": 30.0},
        }
        latest_prices = {"XLK": 55.0, "XLF": 27.0}
        result = build_position_performance(current_positions, latest_prices, log_path)
        assert set(result.keys()) == {"XLK", "XLF"}
        assert result["XLK"]["return_pct"] == pytest.approx(0.10)
        assert result["XLF"]["return_pct"] == pytest.approx(-0.10)


class TestLivePositionCap:
    """
    max_position_weight's live-path enforcement (Epic 3 of the layered risk-management plan,
    "Position Size Hard-Cap", Mandatory tier), previously untested on the live path even
    though the mechanism itself was already correct (compute_target_weights() ->
    resolve_target_weights() -> _apply_position_caps(), the exact same shared function the
    backtest calls, per resolve_target_weights()'s own "single source of truth" docstring).
    This closes a real test-coverage gap, not a behavior gap.
    """

    def test_compute_target_weights_never_exceeds_max_position_weight(self, tmp_path):
        rng = np.random.default_rng(21)
        dates = pd.bdate_range("2024-01-01", periods=90)
        n = len(dates)
        # A is near-flat (very low vol) which inverse-vol sizing would otherwise
        # overweight heavily; B and C carry ordinary vol.
        a = 100 * np.cumprod(1 + rng.normal(0.0002, 0.0005, n))
        b = 50 * np.cumprod(1 + rng.normal(0.0002, 0.02, n))
        c = 75 * np.cumprod(1 + rng.normal(0.0002, 0.02, n))
        prices = pd.DataFrame({"A": a, "B": b, "C": c}, index=dates)

        cfg = BacktestConfig(use_regime_filter=False, use_correlation_spike_regime=False,
                              max_position_weight=0.35)
        alerts_path = str(tmp_path / "alerts_log.csv")
        weights, _ = compute_target_weights(["A", "B", "C"], prices, cfg,
                                              portfolio="p1", alerts_log_path=alerts_path)

        assert max(weights.values()) <= cfg.max_position_weight + 1e-9
        assert sum(weights.values()) == pytest.approx(1.0)


class TestRealizedWeightedPortfolioVol:
    """
    _realized_weighted_portfolio_vol(), the live substitute for momentum_backtest.py's
    _realized_portfolio_vol(): live trading has no simulated equity curve (portfolio_history)
    to measure realized vol from, so this estimates it directly from trailing daily_prices at
    the given target weights, the same "trailing data, not a simulated ledger" pattern
    _inverse_vol_weights() already uses for position-level sizing.
    """

    def _two_ticker_prices(self, n=40, vol_a=0.01, vol_b=0.01, seed=3):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2024-01-01", periods=n)
        a = 100 * np.cumprod(1 + rng.normal(0, vol_a, n))
        b = 50 * np.cumprod(1 + rng.normal(0, vol_b, n))
        return pd.DataFrame({"A": a, "B": b}, index=dates)

    def test_matches_hand_computed_weighted_return_series_vol(self):
        prices = self._two_ticker_prices(n=40)
        weights = {"A": 0.5, "B": 0.5}
        as_of = prices.index[-1]
        result = _realized_weighted_portfolio_vol(weights, prices, as_of, lookback_days=21)

        rets = prices[["A", "B"]].pct_change().dropna()
        window = rets.tail(21)
        hand_computed = float((window["A"] * 0.5 + window["B"] * 0.5).std() * np.sqrt(252))
        assert result == pytest.approx(hand_computed)

    def test_none_when_insufficient_history(self):
        prices = self._two_ticker_prices(n=10)  # fewer than lookback_days + 1
        weights = {"A": 0.5, "B": 0.5}
        as_of = prices.index[-1]
        result = _realized_weighted_portfolio_vol(weights, prices, as_of, lookback_days=21)
        assert result is None

    def test_none_when_no_weighted_tickers_priced(self):
        prices = self._two_ticker_prices(n=40)
        weights = {"C": 1.0}  # not a column in prices at all
        as_of = prices.index[-1]
        result = _realized_weighted_portfolio_vol(weights, prices, as_of, lookback_days=21)
        assert result is None

    def test_high_vol_weighting_produces_higher_realized_vol_than_low_vol_weighting(self):
        prices = self._two_ticker_prices(n=60, vol_a=0.001, vol_b=0.05, seed=11)
        as_of = prices.index[-1]
        low_vol_result = _realized_weighted_portfolio_vol({"A": 1.0, "B": 0.0}, prices, as_of, lookback_days=21)
        high_vol_result = _realized_weighted_portfolio_vol({"A": 0.0, "B": 1.0}, prices, as_of, lookback_days=21)
        assert high_vol_result > low_vol_result


class TestVolTargetingScaling:
    """
    Portfolio-level volatility targeting (target_portfolio_vol), wired into
    compute_target_weights()'s gross_exposure the same way the backtest's
    run_risk_managed_backtest() already composes regime_scalar * vol_scalar (Epic 1 of the
    layered risk-management plan). Previously ONLY existed in the backtest; live trading had no
    aggregate exposure throttling at all, this closes that gap.
    """

    def _synthetic_universe(self, n=60, vol_a=0.001, vol_b=0.001, seed=13):
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2024-01-01", periods=n)
        a = 100 * np.cumprod(1 + rng.normal(0.0005, vol_a, n))
        b = 50 * np.cumprod(1 + rng.normal(0.0005, vol_b, n))
        return pd.DataFrame({"A": a, "B": b}, index=dates)

    def test_high_realized_vol_scales_gross_exposure_below_max(self, tmp_path):
        prices = self._synthetic_universe(vol_a=0.08, vol_b=0.08)  # deliberately high vol
        cfg = BacktestConfig(use_regime_filter=False, use_correlation_spike_regime=False,
                              target_portfolio_vol=0.15, portfolio_vol_lookback=21,
                              min_gross_exposure=0.20, max_gross_exposure=1.0)
        alerts_path = str(tmp_path / "alerts_log.csv")
        weights, gross_exposure = compute_target_weights(["A", "B"], prices, cfg,
                                                           portfolio="p1", alerts_log_path=alerts_path)

        # Hand-verify: same weights, independently recompute realized_vol and the scalar.
        as_of = prices.index[-1]
        realized_vol = _realized_weighted_portfolio_vol(weights, prices, as_of, cfg.portfolio_vol_lookback)
        expected_scalar = compute_vol_scalar(realized_vol, cfg.target_portfolio_vol,
                                              cfg.min_gross_exposure, cfg.max_gross_exposure)
        assert gross_exposure == pytest.approx(expected_scalar)
        assert gross_exposure < cfg.max_gross_exposure

    def test_low_realized_vol_stays_at_max_gross_exposure(self, tmp_path):
        prices = self._synthetic_universe(vol_a=0.0005, vol_b=0.0005)  # deliberately low vol
        cfg = BacktestConfig(use_regime_filter=False, use_correlation_spike_regime=False,
                              target_portfolio_vol=0.15, portfolio_vol_lookback=21,
                              min_gross_exposure=0.20, max_gross_exposure=1.0)
        alerts_path = str(tmp_path / "alerts_log.csv")
        _, gross_exposure = compute_target_weights(["A", "B"], prices, cfg,
                                                     portfolio="p1", alerts_log_path=alerts_path)
        assert gross_exposure == pytest.approx(cfg.max_gross_exposure)

    def test_regime_scalar_and_vol_scalar_compose(self, tmp_path):
        # Both a bearish regime AND high realized vol active at once: gross_exposure must
        # reflect BOTH scalars multiplied together, neither silently overriding the other.
        prices = self._synthetic_universe(vol_a=0.08, vol_b=0.08)
        # Make the regime benchmark itself bearish: below its own SMA.
        bench = pd.Series(
            np.linspace(120, 80, len(prices)), index=prices.index, name="SPY",
        )
        prices = prices.copy()
        prices["SPY"] = bench
        cfg = BacktestConfig(use_regime_filter=True, regime_benchmark="SPY", regime_sma_window=10,
                              use_correlation_spike_regime=False,
                              target_portfolio_vol=0.15, portfolio_vol_lookback=21,
                              min_gross_exposure=0.20, max_gross_exposure=1.0)
        alerts_path = str(tmp_path / "alerts_log.csv")
        weights, gross_exposure = compute_target_weights(["A", "B"], prices, cfg,
                                                           portfolio="p1", alerts_log_path=alerts_path)

        as_of = prices.index[-1]
        realized_vol = _realized_weighted_portfolio_vol(weights, prices, as_of, cfg.portfolio_vol_lookback)
        vol_scalar = compute_vol_scalar(realized_vol, cfg.target_portfolio_vol,
                                         cfg.min_gross_exposure, cfg.max_gross_exposure)
        # Bearish regime -> regime_scalar == min_gross_exposure.
        expected = min(cfg.max_gross_exposure, cfg.min_gross_exposure * vol_scalar)
        assert gross_exposure == pytest.approx(expected)
        # Confirm this composition actually differs from vol_scalar alone (proves both are active).
        assert gross_exposure < vol_scalar


class TestCorrelationSpikeScaling:
    """
    use_correlation_spike_regime's live-trading equivalent,
    same defensive scaling the backtest applies (regime_scalar clamped down to
    min_gross_exposure), wired into compute_target_weights() at the exact point
    the regime filter already scales gross_exposure. Reuses the same synthetic
    price-panel construction as test_momentum_backtest.py's detector test
    (correlation collapses to near-1 in the final 10 days) so both sides are
    provably testing the same underlying signal.
    """

    def _spiking_prices(self):
        np.random.seed(7)
        dates = pd.bdate_range("2018-01-01", "2018-12-31")
        n = len(dates)
        common_shock = np.random.normal(0, 0.005, n)
        data = {}
        for name in ["A", "B", "C"]:
            idio = np.random.normal(0.0005, 0.01, n)
            idio[-10:] *= 0.05  # correlation spikes in the last 10 days
            data[name] = np.cumprod(1 + idio + common_shock) * 100
        return pd.DataFrame(data, index=dates)

    def test_spike_clamps_gross_exposure_to_min(self, tmp_path):
        prices = self._spiking_prices()
        cfg = BacktestConfig(use_regime_filter=False, use_correlation_spike_regime=True,
                              min_gross_exposure=0.2, max_gross_exposure=1.0)
        alerts_path = str(tmp_path / "alerts_log.csv")
        _, gross_exposure = compute_target_weights(["A", "B", "C"], prices, cfg,
                                                     portfolio="p1", alerts_log_path=alerts_path)
        assert gross_exposure == pytest.approx(cfg.min_gross_exposure)

        # CORRELATION_SPIKE_DETECTED must land in the alert log.
        rows = read_recent_alerts(portfolio="p1", log_path=alerts_path)
        assert len(rows) == 1
        assert rows[0]["alert_type"] == "CORRELATION_SPIKE_DETECTED"
        assert rows[0]["severity"] == "WARNING"

    def test_no_scaling_when_disabled(self, tmp_path):
        prices = self._spiking_prices()
        cfg = BacktestConfig(use_regime_filter=False, use_correlation_spike_regime=False,
                              min_gross_exposure=0.2, max_gross_exposure=1.0)
        alerts_path = str(tmp_path / "alerts_log.csv")
        _, gross_exposure = compute_target_weights(["A", "B", "C"], prices, cfg,
                                                     portfolio="p1", alerts_log_path=alerts_path)
        assert gross_exposure == pytest.approx(cfg.max_gross_exposure)
        assert read_recent_alerts(portfolio="p1", log_path=alerts_path) == []


class TestIBKRConnectionRetry:
    """
    place_orders_ibkr() retries the CONNECTION phase (before any order
    is sent) but must NEVER retry order submission itself, a disconnect
    after an order was actually sent but before its confirmation arrived could
    otherwise cause a duplicate order on retry, a much worse outcome than
    failing the run cleanly. This test confirms the connection retry count and
    that a fully-failed connection returns an empty dict (no orders attempted)
    rather than raising or hanging.
    """

    def test_connection_retries_then_fails_cleanly(self, monkeypatch):
        from ibapi.client import EClient
        import momentum_trading.execution.live_signal as ls

        call_count = {"n": 0}

        def flaky_connect(self, host, port, clientId):
            call_count["n"] += 1
            raise ConnectionRefusedError("simulated connection failure")

        monkeypatch.setattr(EClient, "connect", flaky_connect)
        result = ls.place_orders_ibkr({"SPY": {"action": "BUY", "shares": 1}}, port=9999)

        assert call_count["n"] == 3  # exactly 3 attempts, not unlimited retries
        assert result == {}          # no orders were submitted, unambiguous failure


def _install_fake_ibkr(monkeypatch, submission_log):
    """
    Shared mock harness for place_orders_ibkr() tests, bypasses the real
    threaded message loop entirely (connect()/run() become synchronous no-ops) and
    makes every placeOrder() call fill instantly, recording (action, symbol, shares)
    so tests can assert on submission order and sizing without a real/mocked
    multi-second IBKR round trip.
    """
    from ibapi.client import EClient

    def fake_connect(self, host, port, clientId):
        self.nextValidId(1)

    def fake_run(self):
        pass

    def fake_place_order(self, orderId, contract, order):
        submission_log.append((order.action, contract.symbol, order.totalQuantity))
        self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

    def fake_disconnect(self):
        pass

    monkeypatch.setattr(EClient, "connect", fake_connect)
    monkeypatch.setattr(EClient, "run", fake_run)
    monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
    monkeypatch.setattr(EClient, "disconnect", fake_disconnect)


class TestFractionalOrderFlooring:
    """
    IBKR does not support fractional EQUITY/ETF share orders via the API under any
    circumstances, confirmed empirically (error 10243) even after correctly setting
    cashQty per IBKR's own official sample code: cashQty only authorizes fractional fills
    for forex/CASH-pair orders, not STK contracts. place_orders_ibkr() floors fractional
    share counts to whole shares at the submission boundary as the only way to actually
    place an order, these tests confirm that flooring (and the drop-if-zero case).
    """

    def test_fractional_order_floors_to_whole_shares(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 5.9094}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == [("BUY", "BUY1", 5)]

    def test_fractional_order_flooring_to_zero_is_dropped(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 0.9094}}
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == []  # never submitted, 0 whole shares isn't a valid order
        assert any("floors to 0 whole shares" in r.message for r in caplog.records)

    def test_fractional_order_flooring_to_zero_is_recorded_in_results(self, monkeypatch, tmp_path):
        # A ticker dropped for flooring to 0 whole shares never gets a real IBKR orderId, so
        # _collect_results() alone would silently omit it entirely, place_orders_ibkr()
        # tracks it separately (dropped_orders) and merges it back in, so callers building the
        # rebalance summary email's "What Actually Happened" column can still see it.
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 0.9094}}
        result = ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert result == {"BUY1": {"status": "DROPPED_FRACTIONAL", "filled": 0.0, "avg_fill_price": 0.0}}

    def test_whole_share_order_is_unaffected(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == [("BUY", "BUY1", 5)]


class TestBidAskSpreadGate:
    """
    max_bid_ask_spread_pct's pre-trade gate in place_orders_ibkr() (Epic 6 of the layered
    risk-management plan, "Liquidity/Slippage Monitor", Nice-to-Have tier). Mocks
    fetch_bid_ask_spread() directly (its own real IBKR reqMktData() call can't be exercised in
    this test suite, same disclosed limitation as place_orders_ibkr() itself, only the pure
    compute_spread_pct() half is unit-tested independently), matching this file's existing
    "mock the I/O boundary, test the wiring" pattern.
    """

    def test_wide_spread_ticker_is_dropped_and_excluded_from_submission(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)
        monkeypatch.setattr(ls, "fetch_bid_ask_spread",
                             lambda ticker, port, **k: {"bid": 99.0, "ask": 101.0, "spread_pct": 0.02})

        orders = {"WIDE1": {"action": "BUY", "shares": 5}}
        result = ls.place_orders_ibkr(orders, port=9999, max_bid_ask_spread_pct=0.01,
                                       alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == []  # never reached real order submission
        assert result == {"WIDE1": {"status": "DROPPED_WIDE_SPREAD", "filled": 0.0, "avg_fill_price": 0.0}}

    def test_normal_spread_ticker_submits_as_before(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)
        monkeypatch.setattr(ls, "fetch_bid_ask_spread",
                             lambda ticker, port, **k: {"bid": 99.95, "ask": 100.05, "spread_pct": 0.001})

        orders = {"TIGHT1": {"action": "BUY", "shares": 5}}
        result = ls.place_orders_ibkr(orders, port=9999, max_bid_ask_spread_pct=0.01,
                                       alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == [("BUY", "TIGHT1", 5)]
        assert result["TIGHT1"]["status"] == "Filled"

    def test_default_none_never_calls_the_spread_fetch_at_all(self, monkeypatch, tmp_path):
        # Regression: max_bid_ask_spread_pct=None (the default) must be byte-identical to
        # before this feature existed, zero new IBKR calls, every pre-existing call site
        # (no max_bid_ask_spread_pct passed) is unaffected.
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)
        calls = []
        monkeypatch.setattr(ls, "fetch_bid_ask_spread", lambda ticker, port, **k: calls.append(ticker))

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert calls == []
        assert submission_log == [("BUY", "BUY1", 5)]

    def test_none_quote_result_does_not_block_the_order(self, monkeypatch, tmp_path):
        # fetch_bid_ask_spread() returns None on a timeout/no-usable-quote (e.g. no real-time
        # market-data subscription), treated as "couldn't check," not as "spread is wide,"
        # the order still submits rather than being blocked by an unrelated data-feed gap.
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)
        monkeypatch.setattr(ls, "fetch_bid_ask_spread", lambda ticker, port, **k: None)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        result = ls.place_orders_ibkr(orders, port=9999, max_bid_ask_spread_pct=0.01,
                                       alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert submission_log == [("BUY", "BUY1", 5)]
        assert result["BUY1"]["status"] == "Filled"


class TestExtendedHoursOrders:
    """
    IBKR/exchanges reject plain MKT orders outside regular trading hours (error 201, "Exchange
    is closed"), and MKT orders never work outside RTH at all, confirmed against IBKR's own
    TWS API docs; only LMT orders with outsideRth=True do. allow_extended_hours=True switches
    place_orders_ibkr() to that combination; these tests confirm the order actually gets built
    that way, that the buffer direction is correct for BUY vs. SELL, and that a ticker with no
    reference price falls back to a regular MKT order instead of submitting unpriced.
    """

    def _install_fake_ibkr_capturing_order(self, monkeypatch, captured):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            captured.append({
                "symbol": contract.symbol, "action": order.action,
                "orderType": order.orderType, "outsideRth": order.outsideRth,
                "lmtPrice": order.lmtPrice, "totalQuantity": order.totalQuantity,
            })
            self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

    def test_extended_hours_buy_sets_lmt_outside_rth_with_higher_buffer(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_order(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              allow_extended_hours=True)

        order = captured[0]
        assert order["orderType"] == "LMT"
        assert order["outsideRth"] is True
        assert order["lmtPrice"] == pytest.approx(100.5, abs=0.001)  # +0.5% buffer, favors fill

    def test_extended_hours_sell_uses_lower_buffer(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_order(monkeypatch, captured)

        orders = {"SELL1": {"action": "SELL", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"SELL1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              allow_extended_hours=True)

        order = captured[0]
        assert order["orderType"] == "LMT"
        assert order["outsideRth"] is True
        assert order["lmtPrice"] == pytest.approx(99.5, abs=0.001)  # -0.5% buffer, favors fill

    def test_extended_hours_without_reference_price_falls_back_to_mkt(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_order(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(orders, port=9999,  # no expected_prices
                                  alerts_log_path=str(tmp_path / "alerts_log.csv"),
                                  allow_extended_hours=True)

        order = captured[0]
        assert order["orderType"] == "MKT"
        assert order["outsideRth"] is False
        assert any("no reference price is available" in r.message for r in caplog.records)

    def test_extended_hours_disabled_by_default(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_order(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"))
        # allow_extended_hours not passed, must default to off, unaffected behavior

        order = captured[0]
        assert order["orderType"] == "MKT"
        assert order["outsideRth"] is False


class TestBrokerStopLossBracket:
    """
    attach_broker_stop_loss (Epic 2 of the cross-portfolio-sell-prevention plan, belt-and-
    suspenders alongside the Python-side auto_execute_stop_loss check): when set, each BUY
    submits a real IBKR bracket, parent BUY (transmit=False) + child STP SELL (parentId linked,
    transmit=True, stop price = reference_price * (1 - stop_loss_pct)). Only the parent oid is
    tracked in the fill-poll wait set, a resting protective stop correctly stays non-terminal
    indefinitely, that's the whole point.
    """

    def _install_fake_ibkr_capturing_orders(self, monkeypatch, captured, fill_children=False):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            captured.append({
                "orderId": orderId, "symbol": contract.symbol, "action": order.action,
                "orderType": order.orderType, "transmit": order.transmit,
                "parentId": order.parentId, "auxPrice": order.auxPrice,
                "totalQuantity": order.totalQuantity, "outsideRth": order.outsideRth,
                "tif": order.tif,
            })
            # A resting child STP naturally stays non-terminal (Submitted), matching real
            # broker behavior, only the parent (or a plain, non-bracket order) fills instantly.
            if order.parentId and not fill_children:
                self.orderStatus(orderId, "Submitted", 0, order.totalQuantity, 0.0)
            else:
                self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

    def test_bracket_parent_not_transmitted_child_is_and_linked_by_parent_id(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert len(captured) == 2
        parent, child = captured[0], captured[1]
        assert parent["action"] == "BUY" and parent["transmit"] is False
        assert child["action"] == "SELL" and child["orderType"] == "STP"
        assert child["transmit"] is True
        assert child["parentId"] == parent["orderId"]
        assert child["totalQuantity"] == parent["totalQuantity"] == 5

    def test_child_stop_price_computed_from_stop_loss_pct(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        child = captured[1]
        assert child["auxPrice"] == pytest.approx(88.0, abs=0.01)  # 100 * (1 - 0.12)

    def test_disabled_by_default_is_a_single_plain_order(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"))
        # attach_broker_stop_loss not passed, must default to off, byte-identical single order.

        assert len(captured) == 1
        assert captured[0]["transmit"] is True

    def test_per_ticker_disabled_override_skips_the_bracket(self, monkeypatch, tmp_path):
        # generate_orders() stashes the RESOLVED per-ticker stop_loss_pct (None when disabled
        # via ticker_risk_overrides) onto each order; place_orders_ibkr() must honor that even
        # when attach_broker_stop_loss is on for the rest of the portfolio.
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5, "stop_loss_pct": None}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert len(captured) == 1  # no child STP attached
        assert captured[0]["transmit"] is True  # plain, unprotected BUY

    def test_per_ticker_custom_pct_override_is_used_for_the_bracket(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5, "stop_loss_pct": 0.25}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert len(captured) == 2
        # 100 * (1 - 0.25) = 75.0, the ticker's OWN override, not the portfolio-wide 0.12.
        assert captured[1]["auxPrice"] == pytest.approx(75.0, abs=0.01)

    def test_missing_reference_price_falls_back_to_plain_unprotected_buy(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(orders, port=9999,  # no expected_prices
                                  alerts_log_path=str(tmp_path / "alerts_log.csv"),
                                  attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert len(captured) == 1  # no child, BUY still submitted
        assert captured[0]["transmit"] is True
        assert any("no reference price" in r.message for r in caplog.records)

    def test_resting_child_stop_excluded_from_fill_poll_wait_and_result_surfaced(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        result = ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                                       alerts_log_path=str(tmp_path / "alerts_log.csv"),
                                       attach_broker_stop_loss=True, stop_loss_pct=0.12,
                                       fill_poll_timeout=2.0)

        # Poll loop returned promptly (didn't wait the full timeout for the non-terminal
        # resting child), and the parent BUY is correctly reported as Filled.
        assert result["BUY1"]["status"] == "Filled"
        assert result["BUY1"]["stop_order_id"] == captured[1]["orderId"]

    def test_extended_hours_bracket_sets_outside_rth_on_both_legs(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12,
                              allow_extended_hours=True)

        parent, child = captured[0], captured[1]
        assert parent["outsideRth"] is True
        assert child["outsideRth"] is True

    def test_every_order_carries_explicit_tif_day_except_bracket_child_gtc(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}, "SELL1": {"action": "SELL", "shares": 3}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0, "SELL1": 50.0},
                              alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        by_action_type = {(o["action"], o["orderType"]): o for o in captured}
        assert by_action_type[("SELL", "MKT")]["tif"] == "DAY"    # the plain rebalance SELL
        assert by_action_type[("BUY", "MKT")]["tif"] == "DAY"     # the bracket's parent BUY
        assert by_action_type[("SELL", "STP")]["tif"] == "GTC"    # the bracket's protective child

    def test_plain_order_without_bracket_also_carries_explicit_tif_day(self, monkeypatch, tmp_path):
        # Fix 3: every order (bracket or not) now carries an EXPLICIT tif, no longer relying on
        # the account's own implicit preset (previously observed defaulting to DAY via IBKR's
        # own informational code 10349).
        import momentum_trading.execution.live_signal as ls
        captured = []
        self._install_fake_ibkr_capturing_orders(monkeypatch, captured)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert captured[0]["tif"] == "DAY"


class TestOffHoursSubmissionWarning:
    """
    place_orders_ibkr()'s proactive off-hours log line, gated by
    is_outside_all_trading_windows() (already unit-tested in isolation above), monkeypatched
    directly here so this doesn't depend on the real wall clock.
    """

    def _install_fake_ibkr(self, monkeypatch):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

    def test_warns_when_outside_all_trading_windows(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        self._install_fake_ibkr(monkeypatch)
        monkeypatch.setattr(ls, "is_outside_all_trading_windows", lambda **k: True)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                                  alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert any("outside all trading windows" in r.message for r in caplog.records)

    def test_no_warning_during_trading_windows(self, monkeypatch, tmp_path, caplog):
        import momentum_trading.execution.live_signal as ls
        self._install_fake_ibkr(monkeypatch)
        monkeypatch.setattr(ls, "is_outside_all_trading_windows", lambda **k: False)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(orders, port=9999, expected_prices={"BUY1": 100.0},
                                  alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert not any("outside all trading windows" in r.message for r in caplog.records)


class TestCancelRestingStopBeforeSell:
    """
    attach_broker_stop_loss's cancel-before-sell mechanism: a resting protective STP for a
    ticker THIS run is about to SELL (rebalance rotation, stop-loss check, or time-stop, all
    funnel through place_orders_ibkr()) must be cancelled first, or the broker's own triggered
    stop and this app's rebalance-driven sell could both try to sell the same shares.
    Broker-truth-based (reqAllOpenOrders(), not a locally-cached order ID), since the run that
    PLACED the bracket and the run that later decides to EXIT are almost always different
    process invocations/client connections.
    """

    def _install_fake_ibkr_with_resting_orders(self, monkeypatch, resting_orders, cancelled):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_req_all_open_orders(self):
            for o in resting_orders:
                self.openOrder(o["orderId"], type("C", (), {"symbol": o["symbol"]})(),
                                type("O", (), {"action": o["action"], "orderType": o["orderType"]})(),
                                None)
            self.openOrderEnd()

        def fake_cancel_order(self, orderId):
            cancelled.append(orderId)

        def fake_place_order(self, orderId, contract, order):
            self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "reqAllOpenOrders", fake_req_all_open_orders)
        monkeypatch.setattr(EClient, "cancelOrder", fake_cancel_order)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

    def test_resting_stop_for_a_sold_ticker_is_cancelled_before_the_sell(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        cancelled = []
        self._install_fake_ibkr_with_resting_orders(
            monkeypatch,
            [{"orderId": 42, "symbol": "SELL1", "action": "SELL", "orderType": "STP"}],
            cancelled,
        )

        orders = {"SELL1": {"action": "SELL", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"),
                              attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert cancelled == [42]

    def test_sell_for_ticker_with_no_resting_stop_is_unaffected(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        cancelled = []
        self._install_fake_ibkr_with_resting_orders(monkeypatch, [], cancelled)

        orders = {"SELL1": {"action": "SELL", "shares": 5}}
        result = ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"),
                                       attach_broker_stop_loss=True, stop_loss_pct=0.12)

        assert cancelled == []
        assert result["SELL1"]["status"] == "Filled"

    def test_disabled_by_default_never_calls_req_all_open_orders(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        from ibapi.client import EClient
        cancelled = []
        self._install_fake_ibkr_with_resting_orders(monkeypatch, [], cancelled)
        calls = []
        monkeypatch.setattr(EClient, "reqAllOpenOrders", lambda self: calls.append(1))

        orders = {"SELL1": {"action": "SELL", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))
        # attach_broker_stop_loss not passed, must default to off, zero extra IBKR round trip.

        assert calls == []


class TestInformationalOrderErrorDoesNotCorruptStatus:
    """
    IBKR error 10349 ("Order TIF was set to DAY based on order preset") carries a real
    orderId but is not a failure, confirmed empirically against a real paper account
    (orders carrying this exact code went on to fill seconds later with a real
    execDetails/commissionReport). Before this fix, place_orders_ibkr()'s error() callback
    unconditionally overwrote the order's tracked status to "ERROR: ..." for ANY error
    callback matching that orderId, and the poll loop treats status.startswith("ERROR") as
    terminal, so an order that was actually fine (or still pending) got misreported as
    rejected and the code stopped watching it too early.
    """

    def test_informational_error_does_not_mark_order_as_failed(self, monkeypatch, tmp_path):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            # Simulate IBKR sending the informational TIF notice BEFORE the real fill,
            # exactly the ordering observed in the real log that exposed this bug.
            self.error(orderId, 10349, "Order TIF was set to DAY based on order preset.")
            self.orderStatus(orderId, "Filled", order.totalQuantity, 0, 100.0)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

        import momentum_trading.execution.live_signal as ls
        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        results = ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert results["BUY1"]["status"] == "Filled"  # not "ERROR: Order TIF was set to DAY..."

    def test_genuine_error_still_marks_order_as_failed(self, monkeypatch, tmp_path):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            self.error(orderId, 10268, "The 'EtradeOnly' order attribute is not supported.")

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "placeOrder", fake_place_order)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

        import momentum_trading.execution.live_signal as ls
        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        results = ls.place_orders_ibkr(orders, port=9999, alerts_log_path=str(tmp_path / "alerts_log.csv"))

        assert results["BUY1"]["status"].startswith("ERROR")


class TestSellsBeforeBuys:
    """
    place_orders_ibkr() must submit and confirm ALL sells before
    submitting any buy, a buy submitted before its funding sell clears can be
    rejected on a cash account, or silently rely on margin buying power this code
    never checks. Mirrors the backtest engine's explicit sells-first/buys-second
    structure (momentum_backtest.py's run_risk_managed_backtest), closing a real
    backtest/live divergence found while validating a user question about partial
    trade sizing.
    """

    def test_sells_submitted_and_confirmed_before_any_buy(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {
            "BUY1": {"action": "BUY", "shares": 5},
            "SELL1": {"action": "SELL", "shares": 3},
            "BUY2": {"action": "BUY", "shares": 2},
            "SELL2": {"action": "SELL", "shares": 4},
        }
        results = ls.place_orders_ibkr(orders, port=9999,
                                        alerts_log_path=str(tmp_path / "alerts_log.csv"))

        actions_in_order = [a for a, _, _ in submission_log]
        last_sell_index = len(actions_in_order) - 1 - actions_in_order[::-1].index("SELL")
        first_buy_index = actions_in_order.index("BUY")
        assert last_sell_index < first_buy_index, f"a BUY was submitted before a SELL: {submission_log}"
        assert {(a, t) for a, t, _ in submission_log} == {
            ("BUY", "BUY1"), ("SELL", "SELL1"), ("BUY", "BUY2"), ("SELL", "SELL2"),
        }
        assert all(r["status"] == "Filled" for r in results.values())

    def test_sells_only_never_waits_on_buy_phase(self, monkeypatch, tmp_path):
        # No buys at all, must not error or hang on an empty buy phase.
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"SELL1": {"action": "SELL", "shares": 3}}
        results = ls.place_orders_ibkr(orders, port=9999,
                                        alerts_log_path=str(tmp_path / "alerts_log.csv"))
        assert results["SELL1"]["status"] == "Filled"


class TestCashAwareBuySizing:
    """
    After sells clear, BUYs are checked against real available
    cash via available_cash_fn (injected here so no real IBKR account-summary round
    trip is needed). Default behavior is warn-only, submit as computed, let IBKR's
    own fill/reject be the backstop; auto_reduce_on_insufficient_cash additionally
    scales BUY sizes down (floored to whole shares) to fit.
    """

    def test_warn_only_submits_full_size_despite_shortfall(self, monkeypatch, caplog, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)
        alerts_path = str(tmp_path / "alerts_log.csv")

        orders = {"BUY1": {"action": "BUY", "shares": 20}}  # 20 * $100 = $2000 requested
        with caplog.at_level("WARNING"):
            ls.place_orders_ibkr(
                orders, port=9999, expected_prices={"BUY1": 100.0},
                auto_reduce_on_insufficient_cash=False,
                available_cash_fn=lambda: 1000.0,  # only $1000 available
                portfolio="p1", alerts_log_path=alerts_path,
            )
        buy_calls = [(t, s) for a, t, s in submission_log if a == "BUY"]
        assert buy_calls == [("BUY1", 20)]  # submitted at FULL size, unreduced
        assert any("INSUFFICIENT CASH" in r.message for r in caplog.records)

        # INSUFFICIENT_CASH must also land in the alert log.
        rows = read_recent_alerts(portfolio="p1", log_path=alerts_path)
        assert len(rows) == 1
        assert rows[0]["alert_type"] == "INSUFFICIENT_CASH"
        assert rows[0]["severity"] == "WARNING"

    def test_auto_reduce_scales_buys_to_fit(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        # Two buys totaling $2000 requested, only $1000 available -> scale factor 0.5
        orders = {
            "BUY1": {"action": "BUY", "shares": 10},   # $1000 @ $100
            "BUY2": {"action": "BUY", "shares": 10},   # $1000 @ $100
        }
        ls.place_orders_ibkr(
            orders, port=9999, expected_prices={"BUY1": 100.0, "BUY2": 100.0},
            auto_reduce_on_insufficient_cash=True,
            available_cash_fn=lambda: 1000.0,
            alerts_log_path=str(tmp_path / "alerts_log.csv"),
        )
        buy_calls = {t: s for a, t, s in submission_log if a == "BUY"}
        assert buy_calls == {"BUY1": 5, "BUY2": 5}  # each scaled by 0.5 -> 5 shares

    def test_auto_reduce_drops_orders_that_floor_to_zero(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        # $10 available, BUY1 needs $1000 -> scale factor 0.01 -> floors to 0 shares
        orders = {"BUY1": {"action": "BUY", "shares": 1}}
        ls.place_orders_ibkr(
            orders, port=9999, expected_prices={"BUY1": 1000.0},
            auto_reduce_on_insufficient_cash=True,
            available_cash_fn=lambda: 10.0,
            alerts_log_path=str(tmp_path / "alerts_log.csv"),
        )
        buy_calls = [(t, s) for a, t, s in submission_log if a == "BUY"]
        assert buy_calls == []  # dropped entirely, never submitted

    def test_auto_reduce_drop_is_recorded_in_results(self, monkeypatch, tmp_path):
        # Same drop-to-zero-after-scaling case as above, but confirming the ticker still
        # appears in the returned results dict (not silently omitted) so the rebalance
        # summary email's "What Actually Happened" column can report it.
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 1}}
        result = ls.place_orders_ibkr(
            orders, port=9999, expected_prices={"BUY1": 1000.0},
            auto_reduce_on_insufficient_cash=True,
            available_cash_fn=lambda: 10.0,
            alerts_log_path=str(tmp_path / "alerts_log.csv"),
        )
        assert result == {"BUY1": {"status": "DROPPED_INSUFFICIENT_CASH", "filled": 0.0, "avg_fill_price": 0.0}}

    def test_no_shortfall_submits_unchanged_regardless_of_flag(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        orders = {"BUY1": {"action": "BUY", "shares": 5}}  # $500 @ $100, well within budget
        ls.place_orders_ibkr(
            orders, port=9999, expected_prices={"BUY1": 100.0},
            auto_reduce_on_insufficient_cash=True,
            available_cash_fn=lambda: 10000.0,
            alerts_log_path=str(tmp_path / "alerts_log.csv"),
        )
        buy_calls = [(t, s) for a, t, s in submission_log if a == "BUY"]
        assert buy_calls == [("BUY1", 5)]

    def test_missing_expected_prices_skips_cash_check_entirely(self, monkeypatch, tmp_path):
        import momentum_trading.execution.live_signal as ls
        submission_log = []
        _install_fake_ibkr(monkeypatch, submission_log)

        def boom():
            raise AssertionError("available_cash_fn must not be called with no price to check against")

        orders = {"BUY1": {"action": "BUY", "shares": 5}}
        ls.place_orders_ibkr(orders, port=9999, expected_prices=None,
                              auto_reduce_on_insufficient_cash=True, available_cash_fn=boom,
                              alerts_log_path=str(tmp_path / "alerts_log.csv"))
        buy_calls = [(t, s) for a, t, s in submission_log if a == "BUY"]
        assert buy_calls == [("BUY1", 5)]  # proceeds unchanged, cash check never ran


class TestAccountValueTag:
    """
    get_ibkr_account_value()'s tag parameter must actually
    control which accountSummary tag is read, this is what lets the cash-aware
    buy sizing above reuse the function for "AvailableFunds" instead of only ever
    reading "NetLiquidation".
    """

    def test_reads_the_requested_tag_not_always_net_liquidation(self, monkeypatch):
        from ibapi.client import EClient
        import momentum_trading.execution.live_signal as ls

        def fake_connect(self, host, port, clientId):
            pass  # accountSummary/accountSummaryEnd fire from reqAccountSummary below

        def fake_run(self):
            pass

        def fake_req_account_summary(self, reqId, group, tags):
            # Simulate IBKR reporting several tags for this account, only one of
            # which matches what was actually requested.
            self.accountSummary(reqId, "DU123", "NetLiquidation", "50000.00", "USD")
            self.accountSummary(reqId, "DU123", "AvailableFunds", "12345.67", "USD")
            self.accountSummaryEnd(reqId)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "reqAccountSummary", fake_req_account_summary)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

        result = ls.get_ibkr_account_value(port=9999, tag="AvailableFunds")
        assert result == 12345.67

    def test_default_tag_is_net_liquidation_backward_compatible(self, monkeypatch):
        from ibapi.client import EClient
        import momentum_trading.execution.live_signal as ls

        def fake_connect(self, host, port, clientId):
            pass

        def fake_run(self):
            pass

        def fake_req_account_summary(self, reqId, group, tags):
            self.accountSummary(reqId, "DU123", "NetLiquidation", "50000.00", "USD")
            self.accountSummary(reqId, "DU123", "AvailableFunds", "12345.67", "USD")
            self.accountSummaryEnd(reqId)

        def fake_disconnect(self):
            pass

        monkeypatch.setattr(EClient, "connect", fake_connect)
        monkeypatch.setattr(EClient, "run", fake_run)
        monkeypatch.setattr(EClient, "reqAccountSummary", fake_req_account_summary)
        monkeypatch.setattr(EClient, "disconnect", fake_disconnect)

        result = ls.get_ibkr_account_value(port=9999)  # no tag=, must default to NetLiquidation
        assert result == 50000.00
