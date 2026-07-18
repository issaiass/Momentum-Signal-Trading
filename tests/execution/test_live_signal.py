"""
tests/test_live_signal.py

Covers the live-trading order logic: order generation (BUY/SELL/HOLD sizing
and rounding), real FIFO P&L measurement from a trade log, and multi-portfolio
orchestration. Nothing here connects to a real broker -- IBKR-dependent
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

from momentum_trading.backtest.momentum_backtest import BacktestConfig
import momentum_trading.execution.live_signal as live_signal
from momentum_trading.execution.live_signal import (
    generate_orders, log_orders, measure_live_performance, run_multi_portfolio, get_top_etfs,
    compute_aggregate_drift, derive_entry_date, compute_target_weights,
)
from momentum_trading.core.audit_log import read_recent_alerts


class TestGetTopEtfs:
    """
    get_top_etfs() is where BacktestConfig.top_n actually takes effect -- it's
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
        # top_n=3 should be exactly the 3 lowest ranks: QQQ(1), XLK(2), SPY(3) --
        # not an arbitrary/unordered subset.
        picks = get_top_etfs(self._ranks(), top_n=3)
        assert set(picks) == {"QQQ", "XLK", "SPY"}

    def test_top_n_larger_than_universe_returns_whole_universe(self):
        # Mirrors daily_runner.py's min(cfg.top_n, len(tickers)) clamp being
        # unnecessary in practice -- nsmallest() degrades gracefully on its own.
        picks = get_top_etfs(self._ranks(), top_n=10)
        assert len(picks) == 5


class TestGenerateOrders:
    """
    generate_orders() is where target weights become concrete BUY/SELL/HOLD
    decisions with real share counts -- bugs here directly translate to wrong
    trades, so these tests focus on the boundary behaviors most likely to be
    wrong: direction (buy vs sell), the min-trade-size cost filter, and
    whole-vs-fractional share rounding (a real source of confusion since the
    backtest engine and this live path must round identically or their
    results silently diverge).
    """

    def test_produces_buy_and_sell(self):
        # Confirms direction is correct in both directions simultaneously
        # (SPY/QQQ need to shrink, XLK needs to grow) -- a sign error here
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
        # rather than executing them anyway -- this is the mechanism that
        # keeps turnover/commission drag down on small accounts.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1000.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"SPY": 1.0}, gross_exposure=1.0,
            total_value=100.0, latest_prices={"SPY": 550.0}, cfg=cfg,
        )
        assert orders["SPY"]["action"] == "HOLD"

    def test_fractional_shares_when_enabled(self):
        # allow_fractional_shares=True should size to a real fraction of a
        # share (1000/220=4.5454...), not silently floor to a whole number --
        # confirms the flag actually changes behavior, not just accepted syntax.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0, allow_fractional_shares=True)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["shares"] == pytest.approx(4.5454, abs=1e-3)

    def test_whole_shares_by_default(self):
        # The default (allow_fractional_shares=False) must floor to a whole
        # int, matching the backtest engine's default rounding -- if this ever
        # returned a float by mistake, downstream integer-assuming code
        # (e.g. IBKR order quantity formatting) could behave unexpectedly.
        cfg = BacktestConfig(drift_threshold=0.0, min_trade_size=1.0)
        orders = generate_orders(
            current_holdings={}, target_weights={"XLK": 1.0}, gross_exposure=1.0,
            total_value=1000.0, latest_prices={"XLK": 220.0}, cfg=cfg,
        )
        assert orders["XLK"]["shares"] == 4
        assert isinstance(orders["XLK"]["shares"], int)


class TestMeasureLivePerformance:
    """
    measure_live_performance() computes REAL money math (FIFO realized/
    unrealized P&L) directly from the trade log CSV -- this is what an
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

    def test_missing_log_raises(self, tmp_path):
        # Should fail loudly (no log = no data to measure) rather than
        # silently returning zero P&L, which could be mistaken for "no
        # activity" instead of "the file path is wrong."
        with pytest.raises(FileNotFoundError):
            measure_live_performance("2026-01-01", "2026-03-01", log_path=str(tmp_path / "nonexistent.csv"))

    def test_dry_run_filter_excludes_the_other_mode(self, tmp_path):
        # log_orders() writes both dry-run and live rows to the SAME file -- without
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


class TestRunMultiPortfolio:
    """
    run_multi_portfolio() must keep each portfolio's signal, sizing, and
    trade log fully independent -- these tests confirm both the current
    dict-based input shape (with per-portfolio custom_weights) and the older
    plain-list shape (kept for backward compatibility) work, and that
    separate log files are actually created per portfolio, not merged.
    """

    def _mock_fetch(self, tickers, lookback_days=400, fmp_api_key=None, eodhd_api_key=None):
        # Deterministic (seeded) synthetic price panel -- isolates this test
        # from network access and from real-vendor data changing over time.
        dates = pd.bdate_range("2025-01-01", "2026-07-09")
        rng = np.random.default_rng(1)
        data = {t: np.cumprod(1 + rng.normal(0.0005, 0.01, len(dates))) * 100 for t in tickers}
        return pd.DataFrame(data, index=dates)

    def test_dict_shape_with_custom_weights(self, monkeypatch, tmp_path):
        # Confirms two portfolios with DIFFERENT settings (one algorithmic,
        # one hand-specified weights) both run correctly in the same call and
        # log to separate files -- the core "multiple portfolios, same
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
    skip -- same formula, extracted as a pure function so it's directly
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
        # current value to the drift sum -- 200 / 1000 = 0.20.
        drift = compute_aggregate_drift(
            target_dollar={}, current_value={"A": 200.0}, total_value=1000.0,
        )
        assert drift == pytest.approx(0.20)

    def test_zero_total_value_returns_zero_not_divide_error(self):
        assert compute_aggregate_drift({"A": 100.0}, {"A": 50.0}, 0.0) == 0.0


class TestDeriveEntryDate:
    """
    Live-side equivalent of the backtest's entry_dates
    tracking -- entry date must persist across partial adds/trims and reset
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


class TestCorrelationSpikeScaling:
    """
    use_correlation_spike_regime's live-trading equivalent --
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
    is sent) but must NEVER retry order submission itself -- a disconnect
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
    Shared mock harness for place_orders_ibkr() tests -- bypasses the real
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
    circumstances -- confirmed empirically (error 10243) even after correctly setting
    cashQty per IBKR's own official sample code: cashQty only authorizes fractional fills
    for forex/CASH-pair orders, not STK contracts. place_orders_ibkr() floors fractional
    share counts to whole shares at the submission boundary as the only way to actually
    place an order -- these tests confirm that flooring (and the drop-if-zero case).
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

        assert submission_log == []  # never submitted -- 0 whole shares isn't a valid order
        assert any("floors to 0 whole shares" in r.message for r in caplog.records)

    def test_fractional_order_flooring_to_zero_is_recorded_in_results(self, monkeypatch, tmp_path):
        # A ticker dropped for flooring to 0 whole shares never gets a real IBKR orderId, so
        # _collect_results() alone would silently omit it entirely -- place_orders_ibkr()
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


class TestExtendedHoursOrders:
    """
    IBKR/exchanges reject plain MKT orders outside regular trading hours (error 201, "Exchange
    is closed") -- and MKT orders never work outside RTH at all, confirmed against IBKR's own
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
        # allow_extended_hours not passed -- must default to off, unaffected behavior

        order = captured[0]
        assert order["orderType"] == "MKT"
        assert order["outsideRth"] is False


class TestInformationalOrderErrorDoesNotCorruptStatus:
    """
    IBKR error 10349 ("Order TIF was set to DAY based on order preset") carries a real
    orderId but is not a failure -- confirmed empirically against a real paper account
    (orders carrying this exact code went on to fill seconds later with a real
    execDetails/commissionReport). Before this fix, place_orders_ibkr()'s error() callback
    unconditionally overwrote the order's tracked status to "ERROR: ..." for ANY error
    callback matching that orderId, and the poll loop treats status.startswith("ERROR") as
    terminal -- so an order that was actually fine (or still pending) got misreported as
    rejected and the code stopped watching it too early.
    """

    def test_informational_error_does_not_mark_order_as_failed(self, monkeypatch, tmp_path):
        from ibapi.client import EClient

        def fake_connect(self, host, port, clientId):
            self.nextValidId(1)

        def fake_run(self):
            pass

        def fake_place_order(self, orderId, contract, order):
            # Simulate IBKR sending the informational TIF notice BEFORE the real fill --
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
    submitting any buy -- a buy submitted before its funding sell clears can be
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
        # No buys at all -- must not error or hang on an empty buy phase.
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
    trip is needed). Default behavior is warn-only -- submit as computed, let IBKR's
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
    control which accountSummary tag is read -- this is what lets the cash-aware
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

        result = ls.get_ibkr_account_value(port=9999)  # no tag= -- must default to NetLiquidation
        assert result == 50000.00
