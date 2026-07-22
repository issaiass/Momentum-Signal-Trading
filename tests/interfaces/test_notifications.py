"""
tests/test_notifications.py

Covers the categorized email notification system: CRITICAL cannot be
filtered (the whole point of the category), STANDARD/PERIODIC respect
config.yaml, and HTML/chart generation degrades gracefully rather than
crashing when data is missing. No actual SMTP send is tested (would require
a real or mocked mail server), these test the filtering LOGIC and content
generation, which is where a real bug would most likely hide.

Run with: pytest tests/test_notifications.py -v
"""
import pandas as pd
import pytest

from momentum_trading.interfaces.notifications import (
    NotificationCategory, should_send, send_action_email,
    build_rebalance_summary_html, build_monthly_report_html, build_daily_report_html,
    build_comparison_bar_chart, build_signal_universe_html, build_no_action_summary_html,
)


class TestCategoryFiltering:
    """
    The core safety property of this whole module: CRITICAL notifications
    (stop-loss executions, circuit-breaker trips) must be UNSUPPRESSABLE,
    even by a config that explicitly tries to disable them. If this test
    ever fails, it means a config typo or mistake could silently hide an
    alert that matters most.
    """

    def test_critical_ignores_config_attempting_to_disable_it(self):
        assert should_send(NotificationCategory.CRITICAL, {"send_critical": False}) is True
        assert should_send(NotificationCategory.CRITICAL, {}) is True

    def test_standard_respects_explicit_config(self):
        assert should_send(NotificationCategory.STANDARD, {"send_standard": False}) is False
        assert should_send(NotificationCategory.STANDARD, {"send_standard": True}) is True

    def test_periodic_respects_explicit_config(self):
        assert should_send(NotificationCategory.PERIODIC, {"send_periodic": False}) is False
        assert should_send(NotificationCategory.PERIODIC, {"send_periodic": True}) is True

    def test_warning_respects_explicit_config(self):
        # Unlike CRITICAL, the multi-portfolio capital-safety warnings ARE
        # meant to be filterable, they're review-when-convenient risk signals, not
        # run-blocking failures.
        assert should_send(NotificationCategory.WARNING, {"send_warning": False}) is False
        assert should_send(NotificationCategory.WARNING, {"send_warning": True}) is True

    def test_unconfigured_defaults_to_sending(self):
        # Absence of a notifications: block in config.yaml should not silently
        # suppress everything, default to "send" so a missing config section
        # doesn't accidentally go dark.
        assert should_send(NotificationCategory.STANDARD, {}) is True
        assert should_send(NotificationCategory.PERIODIC, {}) is True
        assert should_send(NotificationCategory.WARNING, {}) is True

    def test_daily_defaults_to_NOT_sending_when_unconfigured(self):
        # The one deliberate exception: DAILY defaults to OFF, unlike every other filterable
        # category, a real recurring compute/inbox-volume cost that must be an explicit
        # opt-in, including for a config.yaml predating this feature that never mentions
        # send_daily at all (not just one that explicitly sets it false).
        assert should_send(NotificationCategory.DAILY, {}) is False

    def test_daily_respects_explicit_config(self):
        assert should_send(NotificationCategory.DAILY, {"send_daily": True}) is True
        assert should_send(NotificationCategory.DAILY, {"send_daily": False}) is False


class TestSendActionEmail:
    def test_filtered_notification_returns_false_without_attempting_smtp(self, monkeypatch):
        # Confirms a filtered STANDARD notification short-circuits before
        # ever touching SMTP config/connection, verified indirectly by not
        # requiring any SMTP env vars to be set for this test to pass.
        result = send_action_email(NotificationCategory.STANDARD, "test", "<p>x</p>", {"send_standard": False})
        assert result is False

    def test_unconfigured_smtp_returns_false_not_raises(self, monkeypatch):
        for var in ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO_EMAIL"]:
            monkeypatch.delenv(var, raising=False)
        result = send_action_email(NotificationCategory.CRITICAL, "test", "<p>x</p>")
        assert result is False  # not sent, but doesn't crash


class TestHTMLGeneration:
    """
    Content-generation functions must produce something coherent even from
    edge-case inputs (empty orders, missing comparison data), a report that
    crashes on a rebalance day with zero trades would be worse than one that
    just renders an empty table.
    """

    def test_rebalance_summary_includes_action_and_ticker(self):
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift"},
                  "QQQ": {"action": "HOLD", "shares": 0, "reason": "no drift"}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "SPY" in html and "BUY" in html
        assert "QQQ" in html and "HOLD" in html

    def test_rebalance_summary_handles_empty_orders(self):
        html = build_rebalance_summary_html("portfolio1", {})
        assert "portfolio1" in html  # doesn't crash, still identifies the portfolio

    def test_rebalance_summary_shows_money_invested_columns(self):
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift",
                           "money_invested": 600.0, "pct_money_invested": 0.6},
                  "QQQ": {"action": "HOLD", "shares": 0, "reason": "no drift",
                           "money_invested": 400.0, "pct_money_invested": 0.4}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "Money Invest" in html and "% Money Invest" in html
        assert "$600.00" in html and "60.0%" in html
        assert "$400.00" in html and "40.0%" in html

    def test_rebalance_summary_shows_transaction_amount_column(self):
        # Epic 3: a full-exit SELL shows $0.00 Money Invest (correct, target allocation) but a
        # real, non-zero Transaction $ (the actual dollar amount sold), the distinction that
        # motivated this column existing at all.
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift",
                           "money_invested": 600.0, "pct_money_invested": 0.6,
                           "transaction_amount": 550.0},
                  "OLD": {"action": "SELL", "shares": 5, "reason": "drift",
                           "money_invested": 0.0, "pct_money_invested": 0.0,
                           "transaction_amount": 500.0}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "Transaction $" in html
        assert "$550.00" in html
        assert "$0.00" in html and "$500.00" in html

    def test_rebalance_summary_missing_transaction_amount_defaults_to_zero(self):
        orders = {"SPY": {"action": "HOLD", "shares": 0, "reason": "no drift"}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "Transaction $" in html
        assert "$0.00" in html

    def test_rebalance_summary_capital_header_sums_money_invested(self):
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift",
                           "money_invested": 600.0, "pct_money_invested": 0.6},
                  "QQQ": {"action": "HOLD", "shares": 0, "reason": "no drift",
                           "money_invested": 400.0, "pct_money_invested": 0.4}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "Capital allocated this rebalance" in html
        assert "$1,000.00" in html

    def test_rebalance_summary_missing_money_invested_defaults_to_zero(self):
        # An order dict from a call site that doesn't set money_invested (shouldn't happen via
        # generate_orders() anymore, but defensively) must not crash the email builder.
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift"}}
        html = build_rebalance_summary_html("portfolio1", orders)
        assert "$0.00" in html

    def test_rebalance_summary_dry_run_shows_no_order_sent(self):
        # dry_run=True (no --live): a BUY/SELL order was decided but never sent to a
        # broker, so the new column should say so rather than imply a real outcome.
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift"}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=True)
        assert "Dry-run" in html

    def test_rebalance_summary_hold_shows_no_outcome(self):
        # HOLD never reaches place_orders_ibkr() at all, the outcome column should
        # be a neutral placeholder, not "no order sent" (which implies one was intended).
        orders = {"QQQ": {"action": "HOLD", "shares": 0, "reason": "no drift"}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "—" in html

    def test_rebalance_summary_shows_real_fill(self):
        # execution/live_signal.py's run() merges fill_status/fill_price/fill_shares
        # onto each order after a live place_orders_ibkr() call, confirm the email
        # surfaces the REAL fill, not just the intended action.
        orders = {"SPY": {"action": "BUY", "shares": 2, "reason": "drift",
                           "fill_status": "Filled", "fill_price": 601.23, "fill_shares": 2.0}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "Filled 2 @ $601.23" in html

    def test_rebalance_summary_shows_dropped_fractional_order(self):
        # place_orders_ibkr() now tracks orders dropped for flooring to 0 whole shares
        # (IBKR has no fractional equity API support) separately via dropped_orders,
        # since they never get a real IBKR orderId, confirm that surfaces here too.
        orders = {"GLD": {"action": "BUY", "shares": 0.4, "reason": "drift",
                           "fill_status": "DROPPED_FRACTIONAL", "fill_price": 0.0, "fill_shares": 0.0}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "Dropped" in html and "0 whole shares" in html

    def test_rebalance_summary_shows_dropped_insufficient_cash(self):
        orders = {"XLK": {"action": "BUY", "shares": 5, "reason": "drift",
                           "fill_status": "DROPPED_INSUFFICIENT_CASH", "fill_price": 0.0, "fill_shares": 0.0}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "insufficient cash" in html

    def test_rebalance_summary_shows_rejected_order(self):
        orders = {"XLE": {"action": "BUY", "shares": 3, "reason": "drift",
                           "fill_status": "ERROR: Order rejected", "fill_price": 0.0, "fill_shares": 0.0}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "Rejected" in html and "Order rejected" in html

    def test_rebalance_summary_shows_still_open_order(self):
        # fill_poll_timeout expired before a terminal status arrived, e.g. a limit
        # order still working outside RTH. Should read as "still open", not "filled".
        orders = {"XLF": {"action": "BUY", "shares": 4, "reason": "drift",
                           "fill_status": "PreSubmitted", "fill_price": 0.0, "fill_shares": 0.0}}
        html = build_rebalance_summary_html("portfolio1", orders, dry_run=False)
        assert "Still open" in html and "PreSubmitted" in html

    def test_monthly_report_includes_comparison_when_available(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=3, freq="ME"),
            "total_value": [1000, 1050, 1100], "cash": [0, 0, 0], "unrealized_pnl": [0, 50, 100],
        })
        comparison = {"portfolio_cumulative_return": 0.10, "benchmark_cumulative_return": 0.05,
                      "outperformance": 0.05, "n_periods": 2}
        html, chart, _ = build_monthly_report_html("portfolio1", snap, comparison)
        assert "Outperformance" in html
        assert chart is not None  # matplotlib is available in this test environment

    def test_monthly_report_handles_missing_comparison_gracefully(self):
        # comparison with an "error" key (e.g. no snapshot log yet) should not
        # crash the report, it should just omit that section.
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, chart, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "portfolio1" in html  # doesn't crash

    def test_monthly_report_handles_empty_snapshot(self):
        html, chart, _ = build_monthly_report_html("portfolio1", pd.DataFrame(), {"error": "no data"})
        assert "portfolio1" in html
        assert chart is None  # can't chart with no data, should be None not an exception

    def test_monthly_report_includes_real_pnl_when_provided(self):
        # real_pnl comes from execution/live_signal.py's measure_live_performance(),
        # distinct from the snapshot-based unrealized_pnl already in the report.
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        real_pnl = {"realized_pnl": 40.0, "unrealized_pnl": 90.0, "total_pnl": 130.0,
                    "trade_count": 2, "total_return_pct": 0.13}
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"}, real_pnl)
        assert "Actual P&L" in html
        assert "130.00" in html
        assert "+13.00%" in html

    def test_monthly_report_omits_real_pnl_section_when_not_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "Actual P&L" not in html

    def test_monthly_report_includes_strategy_stats_when_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        since_inception = {
            "inception_date": pd.Timestamp("2026-01-01"), "total_return": 0.05, "cagr": 0.12,
            "max_drawdown": -0.03, "std_dev": 0.10, "sharpe_ratio": None, "sortino_ratio": None,
        }
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, since_inception=since_inception,
        )
        assert "Strategy Performance (Since Inception)" in html
        assert "+5.00%" in html  # total_return
        assert "Not enough history yet" in html  # sharpe/sortino both None

    def test_monthly_report_omits_strategy_stats_when_not_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "Strategy Performance (Since Inception)" not in html

    def test_monthly_report_includes_technical_indicators_when_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        indicators = {"SPY": {"sma_20": 500.0, "rsi_14": 55.5}, "QQQ": {}}  # QQQ: no history yet
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, indicators=indicators,
        )
        assert "Technical Indicators" in html
        assert "SPY" in html
        assert "QQQ" not in html  # empty indicator dict, omitted, not shown blank

    def test_monthly_report_omits_indicators_section_when_none_have_data(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, indicators={"SPY": {}},
        )
        assert "Technical Indicators" not in html

    def test_monthly_report_includes_fundamentals_when_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        fundamentals = {
            "SPY": {"pe_ratio": 25.0, "peg_ratio": 1.5, "roe": 0.3,
                     "debt_to_equity": 1.1, "current_ratio": 0.9},
            "QQQ": {},  # no fundamentals access from either vendor
        }
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, fundamentals=fundamentals,
        )
        assert "Fundamental Indicators" in html
        assert "SPY" in html
        assert "QQQ" not in html  # empty dict, omitted, not shown blank

    def test_monthly_report_omits_fundamentals_section_when_not_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "Fundamental Indicators" not in html

    def test_monthly_report_omits_fundamentals_section_when_none_have_data(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, fundamentals={"SPY": {}},
        )
        assert "Fundamental Indicators" not in html

    def test_monthly_report_includes_macro_when_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        macro = {
            "fed_funds_rate": {"value": 3.63, "date": "2026-06-01"},
            "cpi": {"value": 332.568, "date": "2026-06-01"},
        }
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, macro=macro,
        )
        assert "Macro Context" in html
        assert "Fed Funds Rate" in html
        assert "CPI" in html

    def test_monthly_report_omits_macro_section_when_not_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "Macro Context" not in html

    def test_monthly_report_omits_macro_section_when_empty_dict(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"}, macro={})
        assert "Macro Context" not in html

    def test_monthly_report_includes_position_performance_when_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        position_performance = {
            "SPY": {
                "entry_date": pd.Timestamp("2026-01-05"), "entry_price": 500.0,
                "current_price": 550.0, "shares": 10.0, "return_pct": 0.10,
                "market_value": 5500.0,
            },
        }
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, position_performance=position_performance,
        )
        assert "Position Performance" in html
        assert "SPY" in html
        assert "2026-01-05" in html
        assert "+10.00%" in html

    def test_monthly_report_shows_unknown_for_undeterminable_entry_date(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        position_performance = {
            "SPY": {
                "entry_date": None, "entry_price": 500.0, "current_price": 550.0,
                "shares": 10.0, "return_pct": 0.10, "market_value": 5500.0,
            },
        }
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, position_performance=position_performance,
        )
        assert "Unknown" in html

    def test_monthly_report_omits_position_performance_section_when_not_provided(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "Position Performance" not in html

    def test_monthly_report_omits_position_performance_section_when_empty_dict(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_monthly_report_html(
            "portfolio1", snap, {"error": "no data"}, position_performance={},
        )
        assert "Position Performance" not in html


class TestBuildNoActionSummaryHtml:
    """
    Sent when a rebalance ran to completion but produced zero orders. Epic 5 of the
    "Rebalance Reporting Clarity & Selection-Logic Fixes" plan adds picks_were_empty, so "no
    tickers passed selection" (e.g. every ticker filtered by the liquidity filter) reads
    distinctly from a routine "drift too small to trade" skip, previously indistinguishable.
    """

    def test_default_shows_generic_no_changes_message(self):
        html = build_no_action_summary_html("portfolio1")
        assert "portfolio1" in html
        assert "no order changes" in html
        assert "no tickers passed selection" not in html

    def test_picks_were_empty_shows_distinct_message(self):
        html = build_no_action_summary_html("portfolio1", picks_were_empty=True)
        assert "portfolio1" in html
        assert "no tickers passed selection" in html
        assert "holding cash" in html
        assert "NO_ELIGIBLE_TICKERS" in html

    def test_picks_were_empty_false_is_byte_identical_to_default(self):
        assert build_no_action_summary_html("portfolio1") == build_no_action_summary_html(
            "portfolio1", picks_were_empty=False)


class TestBuildSignalUniverseHtml:
    """
    The second rebalance-email table (Table 2): the FULL ranked universe, selected AND
    watchlist tickers, distinct from build_rebalance_summary_html()'s Table 1 which stays
    scoped to `orders` (real decisions) only.
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

    def test_every_universe_ticker_renders(self):
        html = build_signal_universe_html(self._universe(), self._orders(), top_n=1, strategy_type="momentum")
        assert "A" in html and "B" in html

    def test_watchlist_row_shows_zeroed_money_and_na_stop_loss(self):
        html = build_signal_universe_html(self._universe(), self._orders(), top_n=1, strategy_type="momentum")
        assert "WATCHLIST" in html
        assert "Watchlist / Reserve" in html
        # B's row: $0.00 money invested, 0.0% share, N/A stop-loss (not present in orders at all).
        assert html.count("$0.00") >= 1

    def test_selected_row_shows_real_values(self):
        html = build_signal_universe_html(self._universe(), self._orders(), top_n=1, strategy_type="momentum")
        assert "$500.00" in html
        assert "$90.00 (estimated)" in html  # BUY stop-loss price is always an estimate
        assert "Top 1 (Selected)" in html

    def test_lookback_return_header_unqualified_for_base_score_strategy(self):
        html = build_signal_universe_html(self._universe(), self._orders(), top_n=1, strategy_type="momentum")
        assert "Lookback Return (%)" in html
        assert "composite/residual/blended" not in html

    def test_lookback_return_header_footnoted_for_composite_strategy(self):
        html = build_signal_universe_html(self._universe(), self._orders(), top_n=1, strategy_type="residual_momentum")
        assert "composite/residual/blended" in html

    def test_both_tables_can_render_in_one_email_body(self):
        orders = self._orders()
        table1 = build_rebalance_summary_html("portfolio1", orders)
        table2 = build_signal_universe_html(self._universe(), orders, top_n=1, strategy_type="momentum")
        combined = table1 + table2
        assert "Rebalance Summary" in combined
        assert "Full Signal Universe" in combined

    def test_excluded_variants_render_with_distinct_action_and_reason(self):
        universe = {
            "A": {"rank": 1, "signal_score": 0.15, "close_price": 100.0, "selection_status": "Top 1 (Selected)"},
            "B": {"rank": 2, "signal_score": 0.05, "close_price": 50.0, "selection_status": "Watchlist / Reserve"},
            "E": {"rank": None, "signal_score": -0.10, "close_price": 30.0, "selection_status": "Excluded (Negative Momentum)"},
            "F": {"rank": None, "signal_score": 0.20, "close_price": 20.0, "selection_status": "Excluded (Illiquid)"},
        }
        orders = self._orders()
        html = build_signal_universe_html(universe, orders, top_n=1, strategy_type="momentum")
        # Action column shows the generic "EXCLUDED" value for both variants...
        assert html.count(">EXCLUDED<") == 2
        # ...while the Selection Status column (and Reason, since these were never in `orders`)
        # still carries the specific reason.
        assert "Excluded (Negative Momentum)" in html
        assert "Excluded (Illiquid)" in html
        # WATCHLIST is untouched for the still-positive, merely-outranked ticker.
        assert ">WATCHLIST<" in html

    def test_rows_render_in_momentum_rank_order(self):
        # Deliberately out-of-order dict insertion (C rank=1 last, A rank=2 first, B unranked),
        # rows must render sorted by rank ascending, unranked trailing by score desc.
        universe = {
            "A": {"rank": 2, "signal_score": 0.05, "close_price": 50.0, "selection_status": "Watchlist / Reserve"},
            "B": {"rank": None, "signal_score": 0.20, "close_price": 30.0, "selection_status": "Watchlist / Reserve"},
            "C": {"rank": 1, "signal_score": 0.15, "close_price": 100.0, "selection_status": "Top 1 (Selected)"},
            "D": {"rank": None, "signal_score": 0.02, "close_price": 10.0, "selection_status": "Watchlist / Reserve"},
        }
        html = build_signal_universe_html(universe, {}, top_n=1, strategy_type="momentum")
        import re
        tickers_in_order = re.findall(r"<tr><td style='padding:4px 8px;'>(\w+)</td>", html)
        assert tickers_in_order == ["C", "A", "B", "D"]


class TestBuildComparisonBarChart:
    def test_returns_png_bytes_for_valid_window_data(self):
        window_data = {
            "1 Month": {"portfolio": 0.05, "benchmark": 0.03},
            "3 Month": {"portfolio": 0.10, "benchmark": 0.08},
            "as_of_date": "2026-07-01",
        }
        result = build_comparison_bar_chart(window_data, "Test Chart")
        assert result is not None
        assert result[:8] == b"\x89PNG\r\n\x1a\n"  # PNG file signature

    def test_returns_none_for_no_plottable_windows(self):
        assert build_comparison_bar_chart({"as_of_date": "2026-07-01", "error": "x"}, "Empty") is None

    def test_returns_none_for_empty_dict(self):
        assert build_comparison_bar_chart({}, "Empty") is None


class TestBuildDailyReportHtml:
    """build_daily_report_html() is a thin wrapper over the same _build_report_html() the
    monthly report uses, these tests just confirm the daily-specific framing (label, cadence
    wording) and that it accepts daily_window_comparison()-shaped data correctly."""

    def test_daily_report_has_daily_label_not_monthly(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-07-01", periods=1, freq="D"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, _, _ = build_daily_report_html("portfolio1", snap, {"error": "no data"})
        assert "Daily Report: portfolio1" in html
        assert "Monthly Report" not in html

    def test_daily_report_charts_short_windows(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-07-01", periods=2, freq="D"),
            "total_value": [1000, 1010], "cash": [1000, 1010], "unrealized_pnl": [0, 0],
        })
        window_comparison = {
            "1 Day": {"portfolio": 0.01, "benchmark": 0.005},
            "as_of_date": "2026-07-02",
        }
        html, _, comparison_chart = build_daily_report_html(
            "portfolio1", snap, {"error": "no data"}, window_comparison=window_comparison,
        )
        assert comparison_chart is not None
        assert comparison_chart[:8] == b"\x89PNG\r\n\x1a\n"
        assert 'cid:comparison_chart' in html
