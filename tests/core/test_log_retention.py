"""
tests/core/test_log_retention.py

Time-based log retention (core/audit_log.py's compute_retention_window_days()/
rotate_hash_chained_log()/rotate_plain_log(), execution/live_signal.py's
read_trade_log_with_archives(), daily_runner.py's apply_portfolio_log_retention()/
apply_shared_log_retention()). See docs/LOG_RETENTION.md.
"""
import csv
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

from momentum_trading.core.audit_log import (
    compute_retention_window_days, rotate_hash_chained_log, rotate_plain_log,
    append_hash_chained_row, log_alert, ALERTS_LOG_HEADER,
)
from momentum_trading.execution.live_signal import (
    verify_log_integrity, read_trade_log_with_archives, measure_live_performance,
    log_orders,
)


class TestComputeRetentionWindowDays:
    """
    Matches the SAME month/week-quarter conventions execution/live_signal.py's
    compute_required_lookback_days() already established, not a new approximation.
    """
    def test_monthly_regime_matches_31_days_per_month_convention(self):
        # lookback=12, holding=1 (monthly regime, both >= 1): 3 * (12*31 + 1*31) = 3 * 403 = 1209
        assert compute_retention_window_days(12, 1) == 3 * (12 * 31 + 1 * 31)

    def test_weekly_regime_matches_week_quarter_convention(self):
        # holding_period < 1 -> the WHOLE call is in the weekly regime (matches
        # resolve_momentum_scores()'s "regime decided once by holding_period" convention, not
        # decided independently per field): both periods convert via round(period*4) weeks * 7.
        # lookback=1: round(1*4)=4 weeks -> 28 days. holding=0.25: round(0.25*4)=1 week -> 7 days.
        assert compute_retention_window_days(1, 0.25) == 3 * (28 + 7)

    def test_monthly_lookback_under_weekly_holding_still_uses_week_scale(self):
        # A monthly-sized lookback_period (e.g. 3) under a weekly holding_period must still be
        # interpreted on the week-scale, not months, matching CLAUDE.md's documented rule.
        weekly_regime_days = compute_retention_window_days(3, 0.25)
        monthly_regime_days = compute_retention_window_days(3, 1)
        assert weekly_regime_days != monthly_regime_days
        assert weekly_regime_days == 3 * (round(3 * 4) * 7 + round(0.25 * 4) * 7)

    def test_result_scales_with_larger_periods(self):
        small = compute_retention_window_days(3, 1)
        large = compute_retention_window_days(12, 1)
        assert large > small


class TestRotateHashChainedLog:
    def _seed_alert_log(self, path, rows_with_days_ago):
        """Writes hash-chained alert rows with explicit timestamps, oldest first."""
        for days_ago, message in rows_with_days_ago:
            ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
            fields = [ts, "portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", message, ""]
            append_hash_chained_row(path, ALERTS_LOG_HEADER, fields)

    def test_noop_when_file_does_not_exist(self, tmp_path):
        path = str(tmp_path / "does_not_exist.csv")
        result = rotate_hash_chained_log(path, datetime.now())
        assert result == {"rotated": False}

    def test_noop_when_nothing_old_enough(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        self._seed_alert_log(path, [(1, "recent")])
        result = rotate_hash_chained_log(path, datetime.now() - timedelta(days=30))
        assert result == {"rotated": False}
        with open(path) as f:
            assert len(list(csv.reader(f))) == 2  # header + the one row, untouched

    def test_splits_old_rows_into_archive_and_keeps_recent_in_active_file(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        self._seed_alert_log(path, [(100, "very old"), (50, "old"), (1, "recent")])
        cutoff = datetime.now() - timedelta(days=30)
        result = rotate_hash_chained_log(path, cutoff)

        assert result["rotated"] is True
        assert result["archived_rows"] == 2
        assert result["kept_rows"] == 1
        assert os.path.isfile(result["archive_path"])

        with open(path) as f:
            active_rows = list(csv.reader(f))
        assert len(active_rows) == 2  # header + 1 kept row
        assert active_rows[1][4] == "recent"

        with open(result["archive_path"]) as f:
            archive_rows = list(csv.reader(f))
        assert len(archive_rows) == 3  # header + 2 archived rows
        assert {r[4] for r in archive_rows[1:]} == {"very old", "old"}

    def test_both_resulting_files_independently_pass_verify_log_integrity(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        self._seed_alert_log(path, [(100, "very old"), (50, "old"), (1, "recent")])
        result = rotate_hash_chained_log(path, datetime.now() - timedelta(days=30))

        active_check = verify_log_integrity(path)
        archive_check = verify_log_integrity(result["archive_path"])
        assert active_check["valid"] is True
        assert active_check["rows_checked"] == 1
        assert archive_check["valid"] is True
        assert archive_check["rows_checked"] == 2

    def test_unparseable_timestamp_is_conservatively_kept_not_archived(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        append_hash_chained_row(path, ALERTS_LOG_HEADER,
                                 ["not-a-timestamp", "portfolio1", "X", "INFO", "m", ""])
        result = rotate_hash_chained_log(path, datetime.now() + timedelta(days=1))
        assert result == {"rotated": False}

    def test_concurrent_rotation_and_append_do_not_corrupt_the_chain(self, tmp_path):
        import threading
        path = str(tmp_path / "alerts_log.csv")
        self._seed_alert_log(path, [(100, "old")])
        barrier = threading.Barrier(2)

        def do_rotate():
            barrier.wait()
            rotate_hash_chained_log(path, datetime.now() - timedelta(days=30))

        def do_append():
            barrier.wait()
            log_alert("portfolio1", "TIME_STOP_TRIGGERED", "CRITICAL", "concurrent", log_path=path)

        t1 = threading.Thread(target=do_rotate)
        t2 = threading.Thread(target=do_append)
        t1.start(); t2.start()
        t1.join(); t2.join()

        result = verify_log_integrity(path)
        assert result["valid"] is True


class TestRotatePlainLog:
    def _write_snapshot_row(self, path, days_ago, total_value):
        file_exists = os.path.isfile(path)
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "total_value", "cash"])
            date_str = (datetime.now() - timedelta(days=days_ago)).date().isoformat()
            writer.writerow([date_str, total_value, "0"])

    def test_noop_when_nothing_old_enough(self, tmp_path):
        path = str(tmp_path / "snapshot.csv")
        self._write_snapshot_row(path, 1, 1000)
        result = rotate_plain_log(path, datetime.now() - timedelta(days=30), timestamp_col="date")
        assert result == {"rotated": False}

    def test_splits_and_has_no_hash_column(self, tmp_path):
        path = str(tmp_path / "snapshot.csv")
        self._write_snapshot_row(path, 100, 900)
        self._write_snapshot_row(path, 1, 1000)
        result = rotate_plain_log(path, datetime.now() - timedelta(days=30), timestamp_col="date")

        assert result["rotated"] is True
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["date", "total_value", "cash"]  # no row_hash column
        assert len(rows) == 2  # header + 1 kept row
        assert rows[1][1] == "1000"

        with open(result["archive_path"]) as f:
            archive_rows = list(csv.reader(f))
        assert archive_rows[0] == ["date", "total_value", "cash"]
        assert archive_rows[1][1] == "900"


class TestReadTradeLogWithArchives:
    HEADER = ["timestamp", "ticker", "action", "shares", "price", "reason", "rank",
              "signal_score", "money_invested", "pct_money_invested", "dry_run",
              "config_hash", "transaction_amount", "row_hash"]

    def _write_trade_row(self, path, days_ago, ticker, action, shares, price):
        file_exists = os.path.isfile(path)
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(self.HEADER)
            ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
            writer.writerow([ts, ticker, action, shares, price, "test", "", "", "", "",
                              True, "", 0.0, "dummyhash"])

    def test_no_archives_is_byte_identical_to_plain_read(self, tmp_path):
        path = str(tmp_path / "trades.csv")
        self._write_trade_row(path, 1, "AAPL", "BUY", 10, 100.0)
        df = read_trade_log_with_archives(path)
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "AAPL"

    def test_returns_none_when_nothing_exists(self, tmp_path):
        path = str(tmp_path / "does_not_exist.csv")
        assert read_trade_log_with_archives(path) is None

    def test_merges_active_and_archive_files_sorted_by_timestamp(self, tmp_path):
        path = str(tmp_path / "trades.csv")
        self._write_trade_row(path, 100, "AAPL", "BUY", 10, 100.0)
        self._write_trade_row(path, 1, "MSFT", "BUY", 5, 200.0)
        result = rotate_hash_chained_log(path, datetime.now() - timedelta(days=30))
        assert result["rotated"] is True

        df = read_trade_log_with_archives(path)
        assert len(df) == 2
        assert list(df["ticker"]) == ["AAPL", "MSFT"]  # sorted oldest-first

    def test_still_open_position_survives_rotation_for_fifo_pnl(self, tmp_path):
        """
        The core correctness guarantee: a BUY row for a position that's STILL OPEN today
        must not lose its cost basis just because it got archived away by rotation.
        """
        path = str(tmp_path / "trades.csv")
        self._write_trade_row(path, 100, "AAPL", "BUY", 10, 100.0)  # old BUY, position never sold
        self._write_trade_row(path, 1, "MSFT", "BUY", 5, 200.0)     # unrelated recent activity

        result = rotate_hash_chained_log(path, datetime.now() - timedelta(days=30))
        assert result["rotated"] is True
        assert result["archived_rows"] == 1  # the AAPL BUY moved out of the active file

        perf = measure_live_performance(
            "1970-01-01", pd.Timestamp.today().strftime("%Y-%m-%d"),
            latest_prices={"AAPL": 150.0, "MSFT": 210.0},
            log_path=path, dry_run=True,
        )
        assert perf["open_positions"]["AAPL"] == 10
        assert perf["open_position_avg_cost"]["AAPL"] == pytest.approx(100.0)
        assert perf["open_positions"]["MSFT"] == 5
