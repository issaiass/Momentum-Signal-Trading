"""
tests/core/test_audit_log.py

core/audit_log.py is new shared code, used only by the
new alert log — log_orders()/log_command_attempt()'s own
hash-chain implementations are untouched and covered by their own existing
tests (TestHashChainAuditLog in test_governance.py,
TestEmailCommandLogging in test_email_commands.py). These tests confirm the
extracted helper reproduces that same proven behavior (genesis seed,
chaining, tamper detection) and that log_alert()/read_recent_alerts() work
correctly on top of it.
"""
import csv

import pytest

from momentum_trading.core.audit_log import (
    append_hash_chained_row, log_alert, read_recent_alerts, ALERTS_LOG_HEADER,
)
from momentum_trading.execution.live_signal import verify_log_integrity


class TestAppendHashChainedRow:
    """
    verify_log_integrity() (live_signal.py) is already generic over the
    "last column = hash, GENESIS seed" convention — these tests confirm
    append_hash_chained_row() produces logs that convention accepts, without
    duplicating a second verification implementation here.
    """
    def test_first_row_seeds_from_genesis(self, tmp_path):
        path = str(tmp_path / "log.csv")
        row_hash = append_hash_chained_row(path, ["a", "b"], ["x", "y"])
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["a", "b"]  # header written exactly as given, no extra hash column
        assert rows[1] == ["x", "y", row_hash]

    def test_writes_header_only_once(self, tmp_path):
        path = str(tmp_path / "log.csv")
        append_hash_chained_row(path, ["a", "b"], ["x", "y"])
        append_hash_chained_row(path, ["a", "b"], ["p", "q"])
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["a", "b"]
        assert len(rows) == 3  # header + 2 data rows

    def test_chained_log_verifies_clean(self, tmp_path):
        path = str(tmp_path / "log.csv")
        append_hash_chained_row(path, ["a", "b"], ["x", "y"])
        append_hash_chained_row(path, ["a", "b"], ["p", "q"])
        result = verify_log_integrity(path)
        assert result["valid"] is True
        assert result["rows_checked"] == 2

    def test_tampered_chained_log_is_detected(self, tmp_path):
        path = str(tmp_path / "log.csv")
        append_hash_chained_row(path, ["a", "b"], ["x", "y"])
        append_hash_chained_row(path, ["a", "b"], ["p", "q"])

        with open(path) as f:
            rows = list(csv.reader(f))
        rows[1][0] = "tampered"
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(rows)

        result = verify_log_integrity(path)
        assert result["valid"] is False
        assert result["first_bad_row"] == 1


class TestLogAlert:
    """
    log_alert() is the function every wired call site
    calls — these confirm its schema and hash-chain compatibility directly,
    independent of any specific call site.
    """
    def test_writes_expected_schema(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "SPY down 12%", log_path=path)
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ALERTS_LOG_HEADER
        assert rows[1][1:5] == ["portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "SPY down 12%"]

    def test_multiple_alerts_chain_and_verify(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "SPY down 12%", log_path=path)
        log_alert("ALL", "TICKER_OVERLAP", "WARNING", "SPY in portfolio1 and portfolio2", log_path=path)
        result = verify_log_integrity(path)
        assert result["valid"] is True
        assert result["rows_checked"] == 2

    def test_appends_to_existing_file_without_duplicating_header(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "msg1", log_path=path)
        log_alert("portfolio1", "TIME_STOP_TRIGGERED", "CRITICAL", "msg2", log_path=path)
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ALERTS_LOG_HEADER
        assert len(rows) == 3


class TestReadRecentAlerts:
    """
    Backs the ALERTS_REPORT email command — must never raise
    (a missing/empty log is a normal, expected state, not an error), must
    filter correctly by portfolio, and must respect `limit` and return
    newest-first (most useful ordering for a quick status email).
    """
    def test_missing_log_returns_empty_list(self, tmp_path):
        path = str(tmp_path / "does_not_exist.csv")
        assert read_recent_alerts(portfolio="ALL", limit=10, log_path=path) == []

    def test_header_only_log_returns_empty_list(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(ALERTS_LOG_HEADER)
        assert read_recent_alerts(portfolio="ALL", limit=10, log_path=path) == []

    def test_all_returns_every_portfolio(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "m1", log_path=path)
        log_alert("portfolio2", "TIME_STOP_TRIGGERED", "CRITICAL", "m2", log_path=path)
        log_alert("ALL", "TICKER_OVERLAP", "WARNING", "m3", log_path=path)
        rows = read_recent_alerts(portfolio="ALL", limit=10, log_path=path)
        assert len(rows) == 3

    def test_filters_to_specific_portfolio(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "m1", log_path=path)
        log_alert("portfolio2", "TIME_STOP_TRIGGERED", "CRITICAL", "m2", log_path=path)
        rows = read_recent_alerts(portfolio="portfolio1", limit=10, log_path=path)
        assert len(rows) == 1
        assert rows[0]["portfolio"] == "portfolio1"

    def test_limit_is_respected(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        for i in range(5):
            log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", f"m{i}", log_path=path)
        rows = read_recent_alerts(portfolio="ALL", limit=2, log_path=path)
        assert len(rows) == 2

    def test_returns_newest_first(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "oldest", log_path=path)
        log_alert("portfolio1", "TIME_STOP_TRIGGERED", "CRITICAL", "newest", log_path=path)
        rows = read_recent_alerts(portfolio="ALL", limit=10, log_path=path)
        assert rows[0]["message"] == "newest"
        assert rows[1]["message"] == "oldest"
