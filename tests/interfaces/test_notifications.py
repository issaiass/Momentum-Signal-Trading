"""
tests/test_notifications.py

Covers Epic 12's categorized email notification system: CRITICAL cannot be
filtered (the whole point of the category), STANDARD/PERIODIC respect
config.yaml, and HTML/chart generation degrades gracefully rather than
crashing when data is missing. No actual SMTP send is tested (would require
a real or mocked mail server) -- these test the filtering LOGIC and content
generation, which is where a real bug would most likely hide.

Run with: pytest tests/test_notifications.py -v
"""
import pandas as pd
import pytest

from momentum_trading.interfaces.notifications import (
    NotificationCategory, should_send, send_action_email,
    build_rebalance_summary_html, build_monthly_report_html,
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
        # Epic 27: unlike CRITICAL, the multi-portfolio capital-safety warnings ARE
        # meant to be filterable -- they're review-when-convenient risk signals, not
        # run-blocking failures.
        assert should_send(NotificationCategory.WARNING, {"send_warning": False}) is False
        assert should_send(NotificationCategory.WARNING, {"send_warning": True}) is True

    def test_unconfigured_defaults_to_sending(self):
        # Absence of a notifications: block in config.yaml should not silently
        # suppress everything -- default to "send" so a missing config section
        # doesn't accidentally go dark.
        assert should_send(NotificationCategory.STANDARD, {}) is True
        assert should_send(NotificationCategory.PERIODIC, {}) is True
        assert should_send(NotificationCategory.WARNING, {}) is True


class TestSendActionEmail:
    def test_filtered_notification_returns_false_without_attempting_smtp(self, monkeypatch):
        # Confirms a filtered STANDARD notification short-circuits before
        # ever touching SMTP config/connection -- verified indirectly by not
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
    edge-case inputs (empty orders, missing comparison data) -- a report that
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

    def test_monthly_report_includes_comparison_when_available(self):
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=3, freq="ME"),
            "total_value": [1000, 1050, 1100], "cash": [0, 0, 0], "unrealized_pnl": [0, 50, 100],
        })
        comparison = {"portfolio_cumulative_return": 0.10, "benchmark_cumulative_return": 0.05,
                      "outperformance": 0.05, "n_periods": 2}
        html, chart = build_monthly_report_html("portfolio1", snap, comparison)
        assert "Outperformance" in html
        assert chart is not None  # matplotlib is available in this test environment

    def test_monthly_report_handles_missing_comparison_gracefully(self):
        # comparison with an "error" key (e.g. no snapshot log yet) should not
        # crash the report -- it should just omit that section.
        snap = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=1, freq="ME"),
            "total_value": [1000], "cash": [1000], "unrealized_pnl": [0],
        })
        html, chart = build_monthly_report_html("portfolio1", snap, {"error": "no data"})
        assert "portfolio1" in html  # doesn't crash

    def test_monthly_report_handles_empty_snapshot(self):
        html, chart = build_monthly_report_html("portfolio1", pd.DataFrame(), {"error": "no data"})
        assert "portfolio1" in html
        assert chart is None  # can't chart with no data, should be None not an exception
