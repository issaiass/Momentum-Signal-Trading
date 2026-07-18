"""
core/audit_log.py

Shared hash-chain append logic, extracted because the new
alerts log would otherwise be a THIRD independent copy of the same
pattern already implemented twice -- live_signal.py's log_orders() (trade log)
and email_commands.py's log_command_attempt() (email command log). Those two
existing, working, already-tested implementations are left untouched; this is
used only by the new alert log, not a retrofit of stable code.

Convention (matches both existing implementations exactly): each row's last
column is its hash, computed over the previous row's hash plus this row's other
fields, seeded with the literal string "GENESIS" for the first row. A plain CSV
a script can still freely rewrite isn't tamper-PROOF, but this makes tampering
DETECTABLE (recomputed hashes won't match) -- same trade-off already accepted
for the trade log and email command log.

Verification: live_signal.py's verify_log_integrity() is already generic over
this exact convention (last column = hash, GENESIS seed) and works unchanged
against any log written by append_hash_chained_row() -- reused as-is, not
duplicated here.
"""
import csv
import hashlib
import os
from datetime import datetime

from .paths import logs_dir


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
    """
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    file_exists = os.path.isfile(log_path)
    prev_hash = _last_row_hash(log_path)
    row_hash = _compute_row_hash(prev_hash, fields)

    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(header)
        writer.writerow(list(fields) + [row_hash])

    return row_hash


ALERTS_LOG_HEADER = ["timestamp", "portfolio", "alert_type", "severity", "message", "row_hash"]
ALERTS_LOG_PATH = str(logs_dir() / "alerts_log.csv")


def log_alert(portfolio: str, alert_type: str, severity: str, message: str,
              log_path: str = ALERTS_LOG_PATH) -> None:
    """
    Persistent, tamper-evident, queryable record of every
    alert/warning-worthy event -- stop-loss/time-stop triggers, circuit-breaker
    trips, ticker overlap, capital-allocation errors, and everything else that was
    previously ONLY a logger.warning()/logger.error() line (which only persists if
    something happens to redirect stdout to a file). Deliberately separate from
    data/live_trades_log_<name>.csv (BUY/SELL/HOLD decisions) and
    data/email_commands_log.csv (email command attempts) -- this is neither of
    those, it's the general "something worth knowing about happened" record.

    severity : one of "CRITICAL", "WARNING", "INFO" -- matches the existing
    NotificationCategory tiers where applicable, not a new taxonomy.

    This is purely additive alongside the existing logger.warning()/logger.error()
    and email-alert calls at each call site -- it does not replace or change either.
    """
    fields = [datetime.now().isoformat(), portfolio, alert_type, severity, message]
    append_hash_chained_row(log_path, ALERTS_LOG_HEADER, fields)


def read_recent_alerts(portfolio: str = "ALL", limit: int = 10,
                        log_path: str = ALERTS_LOG_PATH) -> list[dict]:
    """
    Backs the ALERTS_REPORT email command. Returns
    the most recent `limit` rows (newest first), optionally filtered to one
    portfolio -- "ALL" returns every row regardless of portfolio, including
    cross-portfolio alerts (e.g. TICKER_OVERLAP) that are themselves logged
    under the pseudo-portfolio name "ALL".

    A missing or header-only log returns an empty list rather than raising --
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
