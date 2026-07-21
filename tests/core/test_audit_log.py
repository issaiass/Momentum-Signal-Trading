"""
tests/core/test_audit_log.py

core/audit_log.py is new shared code, used only by the
new alert log, log_orders()/log_command_attempt()'s own
hash-chain implementations are untouched and covered by their own existing
tests (TestHashChainAuditLog in test_governance.py,
TestEmailCommandLogging in test_email_commands.py). These tests confirm the
extracted helper reproduces that same proven behavior (genesis seed,
chaining, tamper detection) and that log_alert()/read_recent_alerts() work
correctly on top of it.
"""
import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from momentum_trading.core.audit_log import (
    append_hash_chained_row, log_alert, read_recent_alerts, ALERTS_LOG_HEADER,
    acquire_log_lock, release_log_lock,
)
from momentum_trading.execution.live_signal import verify_log_integrity


class TestAppendHashChainedRow:
    """
    verify_log_integrity() (live_signal.py) is already generic over the
    "last column = hash, GENESIS seed" convention, these tests confirm
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

    def test_concurrent_writers_do_not_corrupt_the_chain(self, tmp_path):
        # Reproduces the real, confirmed race (two `daily-runner --force-rebalance`
        # invocations run close together corrupted a real trade log's hash chain):
        # many threads appending to the SAME log_path at once, forced to start together via a
        # Barrier to maximize contention. Before acquire_log_lock() existed, this reliably
        # broke the chain (two writers reading the same stale prev_hash); now every row must
        # still verify, in SOME order, no forks.
        path = str(tmp_path / "log.csv")
        n_writers = 12
        barrier = threading.Barrier(n_writers)

        def write_one(i):
            barrier.wait()
            append_hash_chained_row(path, ["a", "b"], [f"row{i}", "y"])

        with ThreadPoolExecutor(max_workers=n_writers) as pool:
            list(pool.map(write_one, range(n_writers)))

        result = verify_log_integrity(path)
        assert result["valid"] is True
        assert result["rows_checked"] == n_writers
        with open(path) as f:
            rows = list(csv.reader(f))
        assert len(rows) == n_writers + 1  # header + one row per writer, none lost


class TestAcquireLogLock:
    """
    The portable exclusive-create lock backing append_hash_chained_row()/log_orders()/
    log_command_attempt()'s shared critical section.
    """

    def test_acquire_then_release_allows_reacquire(self, tmp_path):
        log_path = str(tmp_path / "log.csv")
        lock_path = acquire_log_lock(log_path)
        assert os.path.isfile(lock_path)
        release_log_lock(lock_path)
        assert not os.path.isfile(lock_path)
        # A second acquire after release must succeed immediately, not hang.
        lock_path2 = acquire_log_lock(log_path, timeout=1.0)
        release_log_lock(lock_path2)

    def test_second_acquire_blocks_until_first_releases(self, tmp_path):
        log_path = str(tmp_path / "log.csv")
        lock_path = acquire_log_lock(log_path)
        released_at = {}

        def release_after_delay():
            time.sleep(0.2)
            released_at["time"] = time.monotonic()
            release_log_lock(lock_path)

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        start = time.monotonic()
        acquire_log_lock(log_path, timeout=5.0)
        acquired_at = time.monotonic()
        releaser.join()

        assert acquired_at >= released_at["time"]  # genuinely waited, didn't just get lucky
        assert acquired_at - start >= 0.15  # real delay, not immediate

    def test_stale_lock_is_reclaimed_without_waiting_full_timeout(self, tmp_path):
        log_path = str(tmp_path / "log.csv")
        lock_path = log_path + ".lock"
        os.makedirs(tmp_path, exist_ok=True)
        with open(lock_path, "w") as f:
            f.write("")
        stale_mtime = time.time() - 3600  # 1 hour old, way past any real crash-recovery window
        os.utime(lock_path, (stale_mtime, stale_mtime))

        start = time.monotonic()
        reclaimed = acquire_log_lock(log_path, timeout=10.0, stale_after=10.0)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0  # reclaimed promptly, did not wait out the 10s timeout
        release_log_lock(reclaimed)

    def test_timeout_raises_if_never_released(self, tmp_path):
        log_path = str(tmp_path / "log.csv")
        acquire_log_lock(log_path)  # never released
        with pytest.raises(TimeoutError):
            acquire_log_lock(log_path, timeout=0.3, stale_after=999.0)


class TestLogAlert:
    """
    log_alert() is the function every wired call site
    calls, these confirm its schema and hash-chain compatibility directly,
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

    def test_explicit_sender_is_used_as_is(self, tmp_path):
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "msg1", log_path=path,
                   sender="explicit@example.com")
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[0] == ALERTS_LOG_HEADER
        assert rows[1][ALERTS_LOG_HEADER.index("sender")] == "explicit@example.com"

    def test_default_sender_resolves_from_smtp_user_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMTP_USER", "bot@gmail.com")
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "msg1", log_path=path)
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[1][ALERTS_LOG_HEADER.index("sender")] == "bot@gmail.com"

    def test_default_sender_blank_when_smtp_user_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SMTP_USER", raising=False)
        path = str(tmp_path / "alerts_log.csv")
        log_alert("portfolio1", "STOP_LOSS_TRIGGERED", "CRITICAL", "msg1", log_path=path)
        with open(path) as f:
            rows = list(csv.reader(f))
        assert rows[1][ALERTS_LOG_HEADER.index("sender")] == ""


class TestReadRecentAlerts:
    """
    Backs the ALERTS_REPORT email command, must never raise
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
