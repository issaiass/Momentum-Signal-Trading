"""
core/audit_log.py

Shared hash-chain append logic, extracted because the new
alerts log would otherwise be a THIRD independent copy of the same
pattern already implemented twice, live_signal.py's log_orders() (trade log)
and email_commands.py's log_command_attempt() (email command log). Those two
existing, working, already-tested implementations are left untouched; this is
used only by the new alert log, not a retrofit of stable code.

Convention (matches both existing implementations exactly): each row's last
column is its hash, computed over the previous row's hash plus this row's other
fields, seeded with the literal string "GENESIS" for the first row. A plain CSV
a script can still freely rewrite isn't tamper-PROOF, but this makes tampering
DETECTABLE (recomputed hashes won't match), same trade-off already accepted
for the trade log and email command log.

Verification: live_signal.py's verify_log_integrity() is already generic over
this exact convention (last column = hash, GENESIS seed) and works unchanged
against any log written by append_hash_chained_row(), reused as-is, not
duplicated here.
"""
import csv
import hashlib
import os
import time
from datetime import datetime

from .paths import logs_dir


def acquire_log_lock(log_path: str, timeout: float = 15.0, stale_after: float = 10.0) -> str:
    """
    A portable, dependency-free mutual-exclusion lock for the read-last-hash-then-append
    critical section every hash-chained log shares: this file's append_hash_chained_row()
    (alert log, signal rankings log), execution/live_signal.py's log_orders() (trade log), and
    interfaces/email_commands.py's log_command_attempt() (email command log), all four import
    this same helper rather than each inventing their own.

    Fixes a real, confirmed race: two processes writing to the SAME log file close together
    (e.g. two manual --force-rebalance invocations run back-to-back, or scheduled cron
    overlapping a long-running manual run) can each read the same "last row hash" before
    either has written, producing two rows chained from the SAME predecessor instead of one
    chained after the other, a broken link verify_log_integrity() correctly flags. Confirmed
    directly, not theoretical: two `docker exec ... daily-runner --force-rebalance` invocations
    run seconds apart produced exactly this break in a real trade log.

    Implemented as an exclusively-created sentinel file (log_path + ".lock"), portable across
    POSIX and Windows via os.open()'s O_CREAT | O_EXCL atomicity (no new dependency, matching
    this project's existing "no new deps for a lock/marker" precedent, daily_runner.py's
    rebalance-in-progress marker uses the same atomic-file philosophy). A lock file older than
    stale_after seconds is treated as abandoned (its holder crashed before releasing) and is
    force-reclaimed; the real critical section here is a handful of small file I/O calls that
    should complete in well under a second, so this is a generous, conservative margin, not a
    tight one. Raises TimeoutError if the overall timeout elapses without acquiring (sustained
    contention well beyond what a crashed holder alone would explain), rather than blocking
    forever. Returns the lock file path, pass it to release_log_lock() when done.

    Contended O_CREAT | O_EXCL raises FileExistsError on POSIX but can raise PermissionError
    on Windows instead (confirmed directly, not documented behavior assumed: a real concurrency
    test under real thread contention on this project's actual Windows dev environment hit
    PermissionError, not FileExistsError), both mean the same thing here, "someone else holds
    it right now," and are handled identically.
    """
    lock_path = log_path + ".lock"
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock_path
        except (FileExistsError, PermissionError):
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                continue  # lock file vanished between the failed create and this check, retry
            if age > stale_after:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass  # another process already reclaimed it, retry our own create
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for log lock: {lock_path}")
            time.sleep(0.05)


def release_log_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


def _last_row_hash(path: str) -> str:
    if not os.path.isfile(path):
        return "GENESIS"
    try:
        with open(path, "r") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:  # header only, or empty
            return "GENESIS"
        return rows[-1][-1]
    except Exception:
        return "GENESIS"


def _compute_row_hash(prev_hash: str, row_fields: list) -> str:
    payload = prev_hash + "|" + "|".join(str(f) for f in row_fields)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def append_hash_chained_row(log_path: str, header: list[str], fields: list) -> str:
    """
    Appends one hash-chained row to log_path, writing the header first if the
    file doesn't exist yet. Returns the new row's hash (mainly useful for tests).

    The read-last-hash-then-append critical section is guarded by acquire_log_lock()/
    release_log_lock() (see that function's own docstring for the real race it fixes), so
    concurrent callers on the SAME log_path serialize instead of both chaining from the same
    stale prev_hash.
    """
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    lock_path = acquire_log_lock(log_path)
    try:
        file_exists = os.path.isfile(log_path)
        prev_hash = _last_row_hash(log_path)
        row_hash = _compute_row_hash(prev_hash, fields)

        with open(log_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)
            writer.writerow(list(fields) + [row_hash])

        return row_hash
    finally:
        release_log_lock(lock_path)


ALERTS_LOG_HEADER = ["timestamp", "portfolio", "alert_type", "severity", "message", "sender", "row_hash"]
ALERTS_LOG_PATH = str(logs_dir() / "alerts_log.csv")


def log_alert(portfolio: str, alert_type: str, severity: str, message: str,
              log_path: str = ALERTS_LOG_PATH, sender: str | None = None) -> None:
    """
    Persistent, tamper-evident, queryable record of every
    alert/warning-worthy event, stop-loss/time-stop triggers, circuit-breaker
    trips, ticker overlap, capital-allocation errors, and everything else that was
    previously ONLY a logger.warning()/logger.error() line (which only persists if
    something happens to redirect stdout to a file). Deliberately separate from
    data/live_trades_log_<name>.csv (BUY/SELL/HOLD decisions) and
    data/email_commands_log.csv (email command attempts), this is neither of
    those, it's the general "something worth knowing about happened" record.

    severity : one of "CRITICAL", "WARNING", "INFO", matches the existing
    NotificationCategory tiers where applicable, not a new taxonomy.

    sender : the outbound email account this alert would be/was notified from. Defaults
    to None, which resolves internally to os.environ.get("SMTP_USER", ""), the same
    pattern every other real SMTP call site in this project already uses (daily_runner.py,
    notifications.py, email_diagnostics.py, risk_monitor.py). This means none of this
    function's ~27 existing call sites need to pass anything new, the column
    self-populates from whatever SMTP_USER is configured for this run (blank "" if
    unset). This records the CONFIGURED sending account, not proof any specific alert
    was actually emailed, some severities/call sites never trigger an email at all.

    NOTE on schema evolution: this adds a 'sender' column (ALERTS_LOG_HEADER). If you
    have an existing logs/alerts_log.csv from before this change, its header/rows won't
    have this column (6 fields, not 7); appending new-schema rows to that file will
    misalign columns. Archive/rename it before your first run after upgrading, so a
    fresh file with the new header gets created, same remediation as log_orders()'s own
    schema-evolution note.

    This is purely additive alongside the existing logger.warning()/logger.error()
    and email-alert calls at each call site, it does not replace or change either.
    """
    if sender is None:
        sender = os.environ.get("SMTP_USER", "")
    fields = [datetime.now().isoformat(), portfolio, alert_type, severity, message, sender]
    append_hash_chained_row(log_path, ALERTS_LOG_HEADER, fields)


def read_recent_alerts(portfolio: str = "ALL", limit: int = 10,
                        log_path: str = ALERTS_LOG_PATH) -> list[dict]:
    """
    Backs the ALERTS_REPORT email command. Returns
    the most recent `limit` rows (newest first), optionally filtered to one
    portfolio, "ALL" returns every row regardless of portfolio, including
    cross-portfolio alerts (e.g. TICKER_OVERLAP) that are themselves logged
    under the pseudo-portfolio name "ALL".

    A missing or header-only log returns an empty list rather than raising,
    this backs a read-only report command that should never fail a run just
    because nothing has been logged yet.
    """
    if not os.path.isfile(log_path):
        return []
    with open(log_path, "r", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return []
    header, data_rows = rows[0], rows[1:]
    if portfolio != "ALL":
        data_rows = [r for r in data_rows if len(r) > 1 and r[1] == portfolio]
    data_rows = data_rows[-limit:]
    data_rows.reverse()
    return [dict(zip(header, r)) for r in data_rows]
