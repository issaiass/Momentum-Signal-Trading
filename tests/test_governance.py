"""
tests/test_governance.py

Covers institutional-governance features: risk-quantification tools
(VaR/CVaR, scenario shocks), a pre-trade capacity/market-impact check, a
tamper-evident hash-chained audit log, the independent (read-only)
risk_monitor.py process, and the config-approval gate required before --live
will run. These are process/oversight safeguards — none of them validate
whether the underlying momentum strategy itself is profitable; see
TestCrashProtection in test_momentum_backtest.py for the closest thing to
strategy-behavior testing, and TESTING.md for what this suite can and can't
tell you.

Run with: pytest tests/test_governance.py -v
"""
import csv
import os
import numpy as np
import pandas as pd
import pytest
import yaml

import momentum_trading.core.functions_quant_extensions as fnx
from momentum_trading.execution.live_signal import log_orders, verify_log_integrity
from momentum_trading.backtest.momentum_backtest import BacktestConfig
from momentum_trading.risk.risk_monitor import (
    compute_realized_and_open_pnl, write_halt_flag, load_initial_capital,
)


class TestVaRCVaR:
    """
    CVaR (average loss in the tail) must always be at least as bad as VaR (the
    tail threshold itself) by definition — if a change to the calculation
    ever violated that, the numbers would be actively misleading rather than
    just imprecise.
    """
    def test_cvar_worse_than_var(self):
        np.random.seed(3)
        returns = pd.Series(np.random.normal(0.005, 0.04, 200))
        result = fnx.historical_var_cvar(returns, confidence=0.95)
        assert result["cvar_pct"] >= result["var_pct"]

    def test_dollar_conversion(self):
        returns = pd.Series(np.random.normal(0, 0.02, 100))
        result = fnx.historical_var_cvar(returns, portfolio_value=10000)
        assert result["var_dollar"] == pytest.approx(result["var_pct"] * 10000)


class TestScenarioShock:
    """
    scenario_shock() applies deterministic hypothetical returns to current
    weights — this test confirms the weighted-sum math is exactly right
    (verifiable by hand: 0.4*-0.10 + 0.3*-0.20 + 0.3*0 = -0.10), and that an
    unshocked holding (GLD here) correctly contributes zero.
    """
    def test_shock_math_is_correct(self):
        result = fnx.scenario_shock(
            current_weights={"SPY": 0.4, "XLK": 0.3, "GLD": 0.3},
            shock_returns={"SPY": -0.10, "XLK": -0.20},
            portfolio_value=1000.0,
        )
        assert result["total_shock_pct"] == pytest.approx(-0.10)
        assert result["total_shock_dollar"] == pytest.approx(-100.0)
        assert result["per_ticker_impact"]["GLD"]["shock_applied"] == 0.0


class TestCapacityCheck:
    """
    check_capacity() flags positions sized too large relative to average
    daily volume (market-impact risk). Confirms it correctly distinguishes a
    thin ticker (10% of ADV, over a 5% limit) from a liquid one (~0% of ADV) —
    a check that never fires would be silently useless; a check that always
    fires would be silently ignored. Both directions matter.
    """
    def test_thin_ticker_flagged_liquid_not(self):
        dates = pd.bdate_range("2026-01-01", "2026-03-01")
        prices = pd.DataFrame({"THIN": 50.0, "LIQUID": 200.0}, index=dates)
        volume = pd.DataFrame({"THIN": 1000, "LIQUID": 5_000_000}, index=dates)
        result = fnx.check_capacity(
            {"THIN": 5000.0, "LIQUID": 5000.0}, volume, prices, dates[-1], max_pct_of_adv=0.05,
        )
        assert bool(result["THIN"]["flagged"]) is True
        assert bool(result["LIQUID"]["flagged"]) is False


class TestHashChainAuditLog:
    """
    The trade log's hash-chain exists to make tampering DETECTABLE (not
    prevent it at the OS level — a plain CSV can always be edited). These
    tests confirm both directions: an untouched log verifies clean, and a
    log with even one altered field (a price, in this case) is caught and
    the exact bad row is identified — a hash-chain that can't actually
    detect tampering would be false security.
    """
    def test_untampered_log_is_valid(self, tmp_path):
        path = str(tmp_path / "log.csv")
        cfg = BacktestConfig()
        log_orders({"SPY": {"action": "BUY", "shares": 2, "reason": "t"}}, {"SPY": 550.0}, True, path=path, cfg=cfg)
        log_orders({"QQQ": {"action": "SELL", "shares": 1, "reason": "t2"}}, {"QQQ": 480.0}, True, path=path, cfg=cfg)
        result = verify_log_integrity(path)
        assert result["valid"] is True
        assert result["rows_checked"] == 2

    def test_tampered_log_is_detected(self, tmp_path):
        path = str(tmp_path / "log.csv")
        cfg = BacktestConfig()
        log_orders({"SPY": {"action": "BUY", "shares": 2, "reason": "t"}}, {"SPY": 550.0}, True, path=path, cfg=cfg)
        log_orders({"QQQ": {"action": "SELL", "shares": 1, "reason": "t2"}}, {"QQQ": 480.0}, True, path=path, cfg=cfg)

        with open(path) as f:
            rows = list(csv.reader(f))
        rows[1][4] = "9999.99"  # tamper with a price field
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(rows)

        result = verify_log_integrity(path)
        assert result["valid"] is False
        assert result["first_bad_row"] == 1


class TestRiskMonitor:
    """
    risk_monitor.py is deliberately a SEPARATE, independently-implemented FIFO
    calculation from measure_live_performance() in live_signal.py — if a bug
    ever affected one, it should not also blind the other. These tests confirm
    its P&L math independently and that it can write the halt flag file
    daily_runner.py checks (the actual cross-process integration is tested
    manually, not in this unit suite — see TESTING.md).
    """
    def test_computes_fifo_realized_pnl(self, tmp_path):
        path = tmp_path / "log.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares", "price", "reason", "dry_run"])
            w.writerow(["2026-01-05T09:35:00", "XLK", "BUY", 5, 200.0, "entry", True])
            w.writerow(["2026-02-02T09:35:00", "XLK", "SELL", 5, 150.0, "exit", True])
        result = compute_realized_and_open_pnl(str(path))
        assert result["realized_pnl"] == pytest.approx(-250.0)

    def test_write_halt_flag_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.risk.risk_monitor as risk_monitor
        monkeypatch.setattr(risk_monitor, "LOCK_DIR", tmp_path / "data")
        write_halt_flag("testp", "test reason")
        assert (tmp_path / "data" / "circuit_breaker_halted_testp.flag").exists()

    def test_load_initial_capital_reads_total_value_from_config(self, tmp_path):
        # risk_monitor.py's --initial-capital falls back to
        # config.yaml's portfolios.<name>.total_value when omitted on the CLI —
        # this is the independent, minimal YAML read that makes that possible.
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"portfolios": {"portfolio1": {"total_value": 2500.0}}}))
        assert load_initial_capital("portfolio1", str(path)) == 2500.0

    def test_load_initial_capital_returns_none_for_null_total_value(self, tmp_path):
        # total_value: null means "pull live from IBKR" in config.yaml's own
        # convention — not usable as a static monitor baseline, so this must
        # surface as None (caller turns that into a clear error), not 0 or a crash.
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"portfolios": {"portfolio1": {"total_value": None}}}))
        assert load_initial_capital("portfolio1", str(path)) is None

    def test_load_initial_capital_returns_none_for_unknown_portfolio(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({"portfolios": {"portfolio1": {"total_value": 1000.0}}}))
        assert load_initial_capital("portfolio2", str(path)) is None

    def test_load_initial_capital_returns_none_for_missing_file(self, tmp_path):
        assert load_initial_capital("portfolio1", str(tmp_path / "does_not_exist.yaml")) is None


class TestConfigApprovalGate:
    """
    Confirms config.example.yaml — the template everyone copies from —
    ships with approval fields explicitly null, so a fresh config.yaml can
    never accidentally pass the --live approval gate without a human
    deliberately filling those fields in.
    """
    def test_config_example_has_null_approval_by_default(self):
        with open("config.example.yaml") as f:
            raw = yaml.safe_load(f)
        metadata = raw.get("metadata", {})
        assert metadata.get("approved_by") is None
        assert metadata.get("approved_date") is None
