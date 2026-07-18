# Email-Commanded Remote Actions

> **Security-sensitive feature. Read this entire document before enabling it.**

## What this is

A trusted trader can send simple, structured commands via email that `daily_runner.py` picks
up and applies on its next scheduled run. Entirely **opt-in**, inactive unless you set four
specific environment variables (below).

## Security model (the important part)

- **Single trusted sender.** Only emails from the exact address in `TRUSTED_SENDER_EMAIL` are
  ever parsed. This is enforced twice: the IMAP search itself only fetches mail `FROM` that
  address (so unrelated inbox mail is never touched, logged, replied to, or marked read), and
  `parse_command()` re-checks the sender again before parsing.
- **Self-generated emails are never treated as commands, and neither is a reply to one.** This
  matters most if `TRUSTED_SENDER_EMAIL` is the same address as `ALERT_TO_EMAIL`/`IMAP_USER` (see
  "Strongly recommended" note below), since every outbound alert/reply then lands right back in
  the polled inbox as new mail "from" the trusted sender. Two guards, together, stop that from
  cascading:
  1. Every alert/reply/report this system sends carries an `X-Momentum-Trading-Bot` header; the
     poller skips anything carrying it. This alone stops the bot from ever replying to a message
     *it just sent itself*.
  2. Every outbound subject is also prefixed `[momentum-trading]`; the poller separately skips
     any inbound email whose subject already contains that marker. This catches the case the
     header check structurally cannot: a **human** replying to the bot's own reply. A
     human-authored reply is a brand-new message your mail client builds, it never carries the
     bot's custom header, but it does keep the subject prefix, since mail clients preserve/extend
     subject lines on reply. Without this second guard, that human reply would be treated as a new
     command attempt, get rejected again, and trigger another reply, doubling `Re: Re:` in the
     subject each round.

  Neither guard suppresses the **first** rejection reply for a genuine non-command email from the
  trusted sender (e.g. ordinary correspondence you send from that same address), that reply is
  still sent, by design (see "Fail-safe" below). Only the cascade is prevented.
- **No open-ended parameter changes.** `ADJUST_PARAM` can only touch a small, hard-coded
  allowlist (`stop_loss_pct`, `max_position_weight`, `top_n`), each with hard numeric bounds.
  Nothing else is settable this way, everything else requires editing `config.yaml` directly,
  going through the existing `metadata.approved_by`/`approved_date` gate.
- **Fail-safe.** Any email that doesn't match the trusted sender, or that does but doesn't
  parse as a valid recognized command, is ignored. The bot continues running on its **current**
  configuration, it never partially applies a malformed command and never crashes.
- **Confirmation replies.** Every parsed attempt (accepted or rejected) gets an email reply
  explaining what happened and why.
- **High-impact actions require manual follow-through.** `LIQUIDATE` and `ADJUST_PARAM` are
  parsed and validated, but **not auto-applied**, they're logged and alerted so a human
  reviews and applies them deliberately. Only `PAUSE`, `RESUME`, and `SKIP_NEXT_REBALANCE` are
  fully automatic, and all three reuse mechanisms you already trust (the circuit-breaker halt
  flag, a one-time skip flag).

## Setup

Set these environment variables (same `.env`/native-env-var convention as SMTP settings in
`DEPLOYMENT.md`):

```
IMAP_HOST=imap.gmail.com
IMAP_USER=your-bot-inbox@gmail.com
IMAP_PASS=your_app_password
TRUSTED_SENDER_EMAIL=trader@yourdomain.com
```

**Strongly recommended:** use a dedicated inbox for `IMAP_USER`, not your primary email. This
inbox's password grants read access to whatever commands arrive there, treat it like any
other credential with write-adjacent power over the bot. Using the same address for everything
(`IMAP_USER`/`TRUSTED_SENDER_EMAIL`/`SMTP_USER`/`ALERT_TO_EMAIL`) works too and cannot loop, the
two guards above ("Self-generated emails are never treated as commands...") make that structurally
safe, not just "safe in practice." But it isn't noise-free: since the IMAP search fetches every
unread message `FROM` the trusted sender regardless of content, ordinary correspondence you send
from that same address (replying to an unrelated email thread, for example) will still get one
"not a recognized command" reply per message, expected, correct fail-safe behavior, not a bug,
but a dedicated inbox avoids that noise entirely since it never receives unrelated personal mail
in the first place. `daily_runner.py` also logs a one-time warning at startup if it detects
`TRUSTED_SENDER_EMAIL == IMAP_USER`, precisely so this tradeoff isn't invisible.

Note also: every reply (confirmation or rejection) is sent to `ALERT_TO_EMAIL` specifically,
not conditionally to whichever address triggered it, since `daily_runner.py`'s reply wiring
reuses `send_alert_email()`, which always targets `ALERT_TO_EMAIL`. In the common same-address
setup this is the same inbox anyway; if you split addresses, replies always land in
`ALERT_TO_EMAIL`'s inbox specifically, not the sender's.

## Command syntax

Send a plain-text email with one command per message, using this line-based format:

```
ACTION: <command>
PORTFOLIO: <portfolio_name or ALL>
```

### Supported commands

| Command | Auto-applied? | Extra fields | Example |
|---|---|---|---|
| `PAUSE` | Yes (only in `--live` mode, see note below) | N/A | `ACTION: PAUSE`<br>`PORTFOLIO: portfolio1` |
| `RESUME` | Yes (only in `--live` mode) | N/A | `ACTION: RESUME`<br>`PORTFOLIO: portfolio1` |
| `SKIP_NEXT_REBALANCE` | Yes (only in `--live` mode) | N/A | `ACTION: SKIP_NEXT_REBALANCE`<br>`PORTFOLIO: portfolio1` |
| `STATUS` | Yes, read-only, always applies | N/A | `ACTION: STATUS`<br>`PORTFOLIO: portfolio1` |
| `SET_MAX_DRAWDOWN` | Yes (only in `--live` mode), **tightening-only** | `VALUE:` (fraction 0-1) | `ACTION: SET_MAX_DRAWDOWN`<br>`PORTFOLIO: portfolio1`<br>`VALUE: 0.10` |
| `TRIGGER_REPORT` | Parsed, logged | N/A | `ACTION: TRIGGER_REPORT`<br>`PORTFOLIO: portfolio1` |
| `LIQUIDATE` | **No, manual only** | `CONFIRM: I confirm liquidation` (exact phrase, case-insensitive) | `ACTION: LIQUIDATE`<br>`PORTFOLIO: portfolio1`<br>`CONFIRM: I confirm liquidation` |
| `ADJUST_PARAM` | **No, manual only** | `PARAM:` (allowlisted name), `VALUE:` (number, within bounds) | `ACTION: ADJUST_PARAM`<br>`PORTFOLIO: portfolio1`<br>`PARAM: stop_loss_pct`<br>`VALUE: 0.15` |
| `ALERTS_REPORT` | Yes, read-only, always applies | `LIMIT:` (optional, default 10, max 50) | `ACTION: ALERTS_REPORT`<br>`PORTFOLIO: portfolio1`<br>`LIMIT: 20` |

Use `PORTFOLIO: ALL` to apply to every portfolio in `config.yaml`. For `ADJUST_PARAM`, the
allowlist and its bounds are:

| `PARAM` | Bounds | Notes |
|---|---|---|
| `stop_loss_pct` | `0.01` – `0.50` | Fraction, e.g. `0.15` for 15% |
| `max_position_weight` | `0.05` – `1.00` | Fraction of portfolio in a single position |
| `top_n` | `1` – `50` | Number of top-ranked tickers to hold; same concentration lever as `config.yaml`'s `top_n` |

For `ALERTS_REPORT`, `PORTFOLIO` means "filter to this portfolio's alerts" (or `ALL` for every
portfolio, including cross-portfolio alerts like `TICKER_OVERLAP` that are themselves logged
under the pseudo-portfolio name `ALL`), a query filter, not "apply this action to these
portfolios" like the other commands. See `docs/ALERT_LOG.md` for what `alert_type`s exist.

**Important note on when commands actually apply:** `daily_runner.py` reuses its own
`dry_run = not args.live` state for email commands too, state-changing commands (PAUSE,
RESUME, SKIP_NEXT_REBALANCE, SET_MAX_DRAWDOWN) are parsed, validated, logged, and replied to
normally even when running without `--live`, but are only actually *applied* when the bot is
running in `--live` mode. This was a deliberate choice (not a separate new flag) to keep the
same safety semantics as the rest of the system: nothing touches real state unless you're
actually trading live. `STATUS` is read-only and always replies regardless of `--live`.

### `SET_MAX_DRAWDOWN`: tightening-only, enforced at the point of use

The value you request is validated as a sane fraction (0, 1) at parse time, but the "can only
make the breaker MORE sensitive, never less" rule is enforced separately in
`daily_runner.py`'s `get_effective_max_drawdown_pct()`, the effective threshold used by the
circuit breaker is always `min(config.yaml's value, your override)`. If you email a *looser*
value than what's already configured, it's silently ignored (the tighter configured value
still wins), this can never be used to accidentally make the bot riskier.

## What happens to each command

- **PAUSE**, writes the same halt flag `check_circuit_breaker()` respects. Rebalancing stops
  for that portfolio until explicitly resumed (via `RESUME` email or `--resume-trading` CLI).
- **RESUME**, clears the halt flag, resets the peak-equity tracker (same as
  `daily_runner.py --resume-trading`).
- **SKIP_NEXT_REBALANCE**, one-time flag, consumed on the next rebalance attempt (doesn't
  persist beyond one cycle).
- **TRIGGER_REPORT / LIQUIDATE / ADJUST_PARAM**, parsed, validated, and logged/alerted, but
  require you to take the actual action yourself (send the report manually, place the
  liquidating trades yourself, or edit `config.yaml`'s `risk_overrides` with the validated
  value).
- **ALERTS_REPORT**, read-only, replies immediately (even in dry-run) with the most
  recent `LIMIT` rows (default 10, capped at 50) from `logs/alerts_log.csv`, filtered to the
  requested portfolio (or every portfolio, for `ALL`). See `docs/ALERT_LOG.md`.

## Audit logging and duplicate protection

- **Every parsed attempt** (accepted or rejected) is logged to a dedicated, hash-chained audit
  trail at `logs/email_commands_log.csv`, same tamper-evident pattern as the trade log
  (`live_signal.py`'s `log_orders()`), verifiable with `verify_log_integrity()`.
- **Message-ID deduplication**: each processed email's RFC `Message-ID` is recorded to
  `data/processed_command_ids.txt` and skipped on future polls, protects against the same
  command being applied twice if the IMAP server's `\Seen` flag doesn't persist correctly
  between poll cycles. (This one stays in `data/`, not `logs/`, it's dedup state the app reads
  back, not a human-readable audit log.)

## Testing before relying on this

- `email_commands.py`'s parsing/validation logic is covered by
  `tests/interfaces/test_email_commands.py` (36 tests, all passing), sender authentication, the
  `ADJUST_PARAM` allowlist (including `top_n`), `LIQUIDATE`'s confirmation phrase,
  `STATUS`/`SET_MAX_DRAWDOWN`/`ALERTS_REPORT` parsing, audit-log hash-chain integrity, and
  fail-safe behavior on malformed input are all verified. `SET_MAX_DRAWDOWN`'s tightening-only
  enforcement is covered separately in `tests/test_daily_runner.py::TestMaxDrawdownEmailOverride`;
  `ALERTS_REPORT`'s end-to-end read-and-reply path (mocked IMAP, no real mail server) is covered
  by `tests/test_daily_runner.py::TestAlertsReportEmailCommand`.
- Try the interactive `email_commands_walkthrough.ipynb` for a hands-on demo of every command
  and failure mode before enabling this against a real inbox.
- **The IMAP polling function (`poll_and_process_commands()`) has NOT been tested against a
  real mail server** in this project, no network access to a real inbox was available during
  development. Run `daily-runner --test-email` (see `DEPLOYMENT.md`'s "Verify before you trust
  it") for a real, live IMAP login check before relying on it, the same way you'd paper-trade
  before going live with real capital. It confirms the connection/credentials work; it doesn't
  send a real command through the full pipeline, for that, the interactive notebook below is
  still the safest first real test.

## Wiring reminder

`daily_runner.py` checks for commands once per run, before the per-portfolio rebalance loop,
via `check_and_apply_email_commands()`. If the four env vars aren't all set, this is a silent
no-op, you don't need to do anything to keep the feature disabled, it's off by default.
