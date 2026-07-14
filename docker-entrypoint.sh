#!/bin/sh
set -e

# Epic 20: cron schedule times are runtime-configurable via DAILY_RUNNER_CRON /
# RISK_MONITOR_CRON (docker-compose.yml / .env) instead of baked into the image at
# build time -- changing the schedule now only needs a container recreate
# (`docker compose up -d`), not a full rebuild. Defaults match the original fixed
# schedule this replaced (daily-runner 9:35am ET weekdays, risk_monitor hourly
# 9am-4pm ET weekdays).
DAILY_RUNNER_CRON="${DAILY_RUNNER_CRON:-35 9 * * 1-5}"
RISK_MONITOR_CRON="${RISK_MONITOR_CRON:-0 9-16 * * 1-5}"

# Epic 22: space-separated portfolio names to independently risk-monitor, one cron
# entry each -- config.yaml supports any number of portfolios (daily-runner already
# loops over all of them), but risk_monitor.py previously covered a single
# hardcoded "portfolio1" regardless of how many were configured, silently leaving
# additional portfolios with no automated oversight. Defaults to the original
# single-portfolio behavior if unset; must match names under config.yaml's
# portfolios: key, kept as an explicit env var (not auto-discovered from
# config.yaml) so a bad/incomplete config can never prevent cron from starting --
# same reasoning as Epic 19 keeping --portfolio explicit per entry.
#
# To monitor more than one portfolio, set this in .env (NOT here -- this default only
# ever covers a single portfolio):
#   RISK_MONITOR_PORTFOLIOS=portfolio1 portfolio2 portfolio3
# One risk_monitor.py cron entry (and one log file) gets generated per name listed.
RISK_MONITOR_PORTFOLIOS="${RISK_MONITOR_PORTFOLIOS:-portfolio1}"

# Overridable so tests/test_docker_entrypoint.py can point this at a temp file instead
# of requiring /etc/cron.d to exist/be writable outside the real container.
CRONTAB_PATH="${CRONTAB_PATH:-/etc/cron.d/momentum-cron}"

# Single quotes around the log-path segments keep $(date ...) UNevaluated here --
# it must land literally in the crontab file so cron's own shell evaluates it fresh
# every time the job actually fires, not once now at container startup.
{
  echo "$DAILY_RUNNER_CRON"' cd /app && daily-runner >> /app/logs/daily_$(date +%Y%m%d).log 2>&1'
  for p in $RISK_MONITOR_PORTFOLIOS; do
    echo "$RISK_MONITOR_CRON"' cd /app && python -m momentum_trading.risk.risk_monitor --portfolio '"$p"' >> /app/logs/risk_monitor_'"$p"'_$(date +%Y%m%d).log 2>&1'
  done
} > "$CRONTAB_PATH"
chmod 0644 "$CRONTAB_PATH"
crontab "$CRONTAB_PATH"

exec cron -f
