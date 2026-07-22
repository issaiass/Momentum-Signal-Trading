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


def compute_retention_window_days(lookback_period: float, holding_period: float) -> int:
    """
    Retention window backing time-based log rotation (see rotate_hash_chained_log()). Implements
    3 * (lookback_period + holding_period) while reusing this codebase's EXISTING month/
    week-quarter conventions, the same ones execution/live_signal.py's
    resolve_momentum_scores()/compute_required_lookback_days() already established, instead of
    inventing a new day-per-month approximation. The regime (weekly vs. monthly) is determined
    ONCE from holding_period, exactly like resolve_momentum_scores() already does, NOT
    independently per field, a monthly lookback_period under a weekly holding_period is still
    expressed on the week-scale (CLAUDE.md's documented "same week-scale as its rebalance
    cadence, not mixed months/weeks" rule): holding_period < 1 (weekly regime), both periods
    convert via round(period * 4) weeks * 7 days; otherwise (monthly regime), both convert via
    round(period) * 31 days. Summed, then multiplied by 3. See docs/LOG_RETENTION.md.
    """
    if holding_period < 1:
        def _period_days(period: float) -> int:
            return max(1, round(period * 4)) * 7
    else:
        def _period_days(period: float) -> int:
            return round(period) * 31

    return 3 * (_period_days(lookback_period) + _period_days(holding_period))


def _atomic_write_csv(path: str, header_row: list, data_rows_out: list) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header_row)
        writer.writerows(data_rows_out)
    os.replace(tmp_path, path)


def _unique_archive_path(log_path: str) -> str:
    suffix = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive_path = f"{log_path}.archive_{suffix}.csv"
    while os.path.isfile(archive_path):
        suffix += "_1"
        archive_path = f"{log_path}.archive_{suffix}.csv"
    return archive_path


def _rotate_log(log_path: str, cutoff_date: datetime, timestamp_col: str, rechain: bool) -> dict:
    """
    Shared split-and-archive mechanics for rotate_hash_chained_log()/rotate_plain_log() (see
    those functions' own docstrings for the full behavior contract). `rechain` selects whether
    each output file's row_hash column (last field) gets recomputed from a fresh "GENESIS" seed
    (hash-chained logs) or left as plain data with no hash column at all (rechain=False, e.g.
    data/portfolio_snapshot_<portfolio>.csv, which has no row_hash column to begin with).
    """
    if not os.path.isfile(log_path):
        return {"rotated": False}
    lock_path = acquire_log_lock(log_path)
    try:
        with open(log_path, "r", newline="") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return {"rotated": False}
        header, data_rows = rows[0], rows[1:]
        try:
            ts_idx = header.index(timestamp_col)
        except ValueError:
            return {"rotated": False}

        archive_rows, keep_rows = [], []
        for row in data_rows:
            parsed = None
            if len(row) > ts_idx:
                try:
                    parsed = datetime.fromisoformat(row[ts_idx])
                except ValueError:
                    parsed = None
            (archive_rows if parsed is not None and parsed < cutoff_date else keep_rows).append(row)

        if not archive_rows:
            return {"rotated": False}

        def _rechain(data_rows_in):
            prev_hash = "GENESIS"
            out = []
            for row in data_rows_in:
                fields = row[:-1]
                row_hash = _compute_row_hash(prev_hash, fields)
                out.append(fields + [row_hash])
                prev_hash = row_hash
            return out

        archive_path = _unique_archive_path(log_path)
        _atomic_write_csv(archive_path, header, _rechain(archive_rows) if rechain else archive_rows)
        _atomic_write_csv(log_path, header, _rechain(keep_rows) if rechain else keep_rows)

        return {"rotated": True, "archived_rows": len(archive_rows),
                "kept_rows": len(keep_rows), "archive_path": archive_path}
    finally:
        release_log_lock(lock_path)


def rotate_hash_chained_log(log_path: str, cutoff_date: datetime,
                             timestamp_col: str = "timestamp") -> dict:
    """
    Time-based log rotation for any hash-chained log written by append_hash_chained_row() (this
    module's alert log and live_signal.py's signal rankings log) or the bespoke-but-identical-
    convention writers (live_signal.py's log_orders() trade log, email_commands.py's
    log_command_attempt() email command log): rows with timestamp_col strictly before
    cutoff_date are moved to a new sibling "<log_path>.archive_<run_timestamp>.csv" file, never
    deleted, see docs/LOG_RETENTION.md.

    Both the archive file and the rewritten active log_path get a FRESHLY recomputed row_hash
    chain, independently re-seeded from "GENESIS", so live_signal.py's verify_log_integrity()
    (unchanged, already hardcodes prev_hash="GENESIS" for row 1 of any file) keeps working
    correctly on both resulting files with zero changes to that function. The archive file's
    row_hash values will therefore differ from what was originally written in the live file,
    expected and harmless: tamper-evidence going FORWARD from the moment of rotation is what
    matters, not preserving the exact original hash bytes across a legitimate, logged
    administrative operation.

    No-ops (returns {"rotated": False}) when the file doesn't exist, is empty/header-only, has
    no timestamp_col, or nothing is older than cutoff_date, the common case on most days. A row
    whose timestamp_col can't be parsed is conservatively KEPT (never archived), rotation should
    never be the reason a malformed-but-real row silently disappears.

    Guarded by the SAME acquire_log_lock()/release_log_lock() critical section as every append
    to this file (see that function's own docstring for the real concurrent-write race it
    fixes), so rotation can never race a concurrent writer into a corrupt or mis-chained file.
    Both output files are written atomically (temp file + os.replace()), matching
    daily_runner.py's own atomic-marker-write precedent, so a crash mid-rotation can't leave
    either file half-written.
    """
    return _rotate_log(log_path, cutoff_date, timestamp_col, rechain=True)


def rotate_plain_log(log_path: str, cutoff_date: datetime, timestamp_col: str) -> dict:
    """
    Time-based rotation for a plain (non-hash-chained) time-series CSV, e.g.
    data/portfolio_snapshot_<portfolio>.csv (execution/live_signal.py's
    write_portfolio_snapshot(), whose date column is named "date", not "timestamp", pass
    timestamp_col="date"). Same archive-not-delete semantics, atomic writes, and
    acquire_log_lock()/release_log_lock() discipline as rotate_hash_chained_log(), just without
    any hash chain to preserve or recompute, this file has no row_hash column at all. See
    docs/LOG_RETENTION.md.
    """
    return _rotate_log(log_path, cutoff_date, timestamp_col, rechain=False)


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
