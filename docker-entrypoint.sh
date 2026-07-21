#!/bin/sh
set -e

# Cron schedule times are runtime-configurable via DAILY_RUNNER_CRON /
# RISK_MONITOR_CRON (docker-compose.yml / .env) instead of baked into the image at
# build time, changing the schedule now only needs a container recreate
# (`docker compose up -d`), not a full rebuild. Defaults match the original fixed
# schedule this replaced (daily-runner 9:35am ET weekdays, risk_monitor hourly
# 9am-4pm ET weekdays).
DAILY_RUNNER_CRON="${DAILY_RUNNER_CRON:-35 9 * * 1-5}"
# RISK_MONITOR_CRON's shipped default (hourly, 9am-4pm ET) is a generic catch-all. Recommended,
# regime-specific alternatives, both chosen to avoid known noisy windows (see
# docs/DEPLOYMENT.md's "Recommended risk_monitor.py timing", docs/RISK_CONSTRAINTS.md's
# "Stop-Loss Width" for the matching stop_loss_pct guidance):
#   Short-term/weekly portfolios (config.yaml holding_period < 1): check twice daily,
#     10:00 AM + 3:30 PM ET, skips the open's first-30-min "shakeout" noise, the 3:30 check
#     captures the day's near-final trend ahead of the close.
#   Long-term/monthly portfolios (holding_period >= 1): check once, at the close,
#     RISK_MONITOR_CRON="45 15 * * 1-5" (3:45pm ET), closing prices set the official daily
#     trend, checking only there avoids reacting to intraday whipsaws that reverse before the
#     bell.
# This single value applies to EVERY portfolio in RISK_MONITOR_PORTFOLIOS below (see the loop
# that generates one crontab line per name, all sharing this same schedule) -- it cannot give
# two portfolios two different schedules, nor give one portfolio two different times in a day
# (cron's comma syntax, e.g. "0,30 10,15 * * 1-5", fires at the CROSS-PRODUCT of both fields:
# 10:00, 10:30, 15:00, 15:30, not just the two times you actually want). A container mixing a
# short-term and a long-term portfolio needs a host-level cron/Task Scheduler `docker exec`
# workaround for the one that doesn't match this container's own schedule, see
# docs/DEPLOYMENT.md's "Recommended risk_monitor.py timing" section for the copy-pasteable
# recipe (drop that portfolio from RISK_MONITOR_PORTFOLIOS below, add the extra check(s) as a
# separate host-level `docker exec momentum-signal python -m momentum_trading.risk.risk_monitor
# --portfolio <name>` cron/Task Scheduler entry instead).
RISK_MONITOR_CRON="${RISK_MONITOR_CRON:-0 9-16 * * 1-5}"

# Space-separated portfolio names to independently risk-monitor, one cron
# entry each, config.yaml supports any number of portfolios (daily-runner already
# loops over all of them), but risk_monitor.py previously covered a single
# hardcoded "portfolio1" regardless of how many were configured, silently leaving
# additional portfolios with no automated oversight. Defaults to the original
# single-portfolio behavior if unset; must match names under config.yaml's
# portfolios: key, kept as an explicit env var (not auto-discovered from
# config.yaml) so a bad/incomplete config can never prevent cron from starting,
# same reasoning as risk_monitor.py's --portfolio flag being explicit per entry.
#
# To monitor more than one portfolio, set this in .env (NOT here, this default only
# ever covers a single portfolio):
#   RISK_MONITOR_PORTFOLIOS=portfolio1 portfolio2 portfolio3
# One risk_monitor.py cron entry (and one log file) gets generated per name listed.
#
# Momentum/stop-loss strategy parameters (holding_period, lookback_period, stop_loss_pct,
# auto_execute_stop_loss, attach_broker_stop_loss, etc.) are NOT set here or anywhere in this
# file, they're per-portfolio fields in config.yaml's default_risk/risk_overrides. Recommended
# starting points per regime (see config.example.yaml's own comments and
# docs/RISK_CONSTRAINTS.md's "Recommended Config Presets"/"Stop-Loss Width" sections):
#   Short-term/weekly (holding_period: 0.25): stop_loss_pct: 0.10, tighter control, cuts
#     downside rapidly for a noisier, faster-rotating signal.
#   Long-term/monthly (holding_period: 1): stop_loss_pct: 0.15-0.20, room to breathe through
#     normal pullbacks; still a FIXED stop from entry, not a trailing stop, no trailing-stop
#     mechanism exists in this codebase today, see that "Stop-Loss Width" section for why.
RISK_MONITOR_PORTFOLIOS="${RISK_MONITOR_PORTFOLIOS:-portfolio1}"

# Overridable so tests/test_docker_entrypoint.py can point this at a temp file instead
# of requiring /etc/cron.d to exist/be writable outside the real container.
CRONTAB_PATH="${CRONTAB_PATH:-/etc/cron.d/momentum-cron}"

# Single quotes around the log-path segments keep $(date ...) UNevaluated here,
# it must land literally in the crontab file so cron's own shell evaluates it fresh
# every time the job actually fires, not once now at container startup.
#
# IMPORTANT: DAILY_RUNNER_CRON only controls how often this container ATTEMPTS a run (leave it
# at the daily-weekday default below for every cadence, daily/weekly/monthly alike), the
# actual rebalance cadence is set via config.yaml's holding_period, which daily-runner itself
# self-gates on via is_rebalance_day(). Changing DAILY_RUNNER_CRON to fire less than daily would
# also silently stop the daily stop-loss/time-stop checks, which run independently of
# holding_period. See docs/DEPLOYMENT.md's "Choosing a rebalance cadence" for worked examples.
{
  echo "$DAILY_RUNNER_CRON"' cd /app && daily-runner >> /app/logs/daily_$(date +%Y%m%d).log 2>&1' # dry run
  # echo "$DAILY_RUNNER_CRON"' cd /app && daily-runner --live --port 7497 >> /app/logs/daily_$(date +%Y%m%d).log 2>&1' # trading (paper)
  # echo "$DAILY_RUNNER_CRON"' cd /app && daily-runner --live --confirm-live-trading --port 7496 >> /app/logs/daily_$(date +%Y%m%d).log 2>&1' # trading (live account)  
  # To go live, replace the line above with (paper: --port 7497, real money also needs
  # --confirm-live-trading with --port 7496), see docs/DEPLOYMENT.md "Going live for real":
  #   echo "$DAILY_RUNNER_CRON"' cd /app && daily-runner --live --port 7497 >> /app/logs/daily_$(date +%Y%m%d).log 2>&1'
  # daily-runner's --port now defaults to the IBKR_PORT env var (set in .env) when omitted,
  # so if IBKR_PORT is already correct there, an explicit --port above isn't strictly required,
  # it's kept in the example for clarity and because it still overrides IBKR_PORT if both are set.
  #
  # --live and --confirm-live-trading, unlike --port (and unlike DAILY_RUNNER_CRON/IBKR_HOST/
  # IBKR_PORT above), are DELIBERATELY NOT env-var-driven, this was considered and explicitly
  # rejected, not an oversight. An env var toggle would let real-money trading get enabled by a
  # plain .env edit + `docker compose up -d`, no script edit or rebuild, removing the
  # intentional friction that requires a deliberate code change (this file) plus
  # `docker compose up -d --build` before any real order can ever be placed. Same reasoning as
  # dry-run being the unflagged default and --confirm-live-trading being a second, separate flag
  # in daily_runner.py itself, see CLAUDE.md's "Safety defaults that are load-bearing" and
  # docs/DEPLOYMENT.md's "Going live for real".
  for p in $RISK_MONITOR_PORTFOLIOS; do
    echo "$RISK_MONITOR_CRON"' cd /app && python -m momentum_trading.risk.risk_monitor --portfolio '"$p"' >> /app/logs/risk_monitor_'"$p"'_$(date +%Y%m%d).log 2>&1'
  done
} > "$CRONTAB_PATH"
chmod 0644 "$CRONTAB_PATH"
crontab "$CRONTAB_PATH"

exec cron -f
