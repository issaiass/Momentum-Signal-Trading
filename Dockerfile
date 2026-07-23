FROM python:3.12-slim

WORKDIR /app

# --- system deps: cron for internal scheduling, tzdata for correct market-hours timestamps ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=America/New_York
# Single source of truth for where config.yaml/data/logs live inside the
# container -- matches core/paths.py's env-var override, so path resolution never
# has to guess based on CWD.
ENV MOMENTUM_TRADING_ROOT=/app

# --- python deps + the package itself, via pyproject.toml ---
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

# config.yaml must exist before building (copy config.example.yaml -> config.yaml and edit first).
# This baked-in copy is only a FALLBACK for standalone `docker build`/`docker run` usage without
# compose; docker-compose.yml bind-mounts the host's real config.yaml over this same path, which
# always wins under normal (compose-based) operation, letting a host edit reach the running
# container on the next cron tick with no rebuild. See docs/DEPLOYMENT.md's "What needs what".
COPY config.example.yaml .
COPY config.yaml .

# --- persistent volume mount points: logs and trade history survive container restarts ---
RUN mkdir -p /app/logs /app/data
VOLUME ["/app/logs", "/app/data"]

# --- cron schedule: daily_runner.py daily at 9:35am ET (default), self-gates via
#     is_rebalance_day(). risk_monitor.py hourly during market hours (default) -- runs as a
#     SEPARATE cron entry so it keeps firing even if daily_runner.py's process hangs, but note
#     this still shares the same container/process space. For genuine segregation (a monitor
#     that survives even if the whole trading container is compromised), run risk_monitor.py in
#     a SEPARATE container/host instead -- see docs/DEPLOYMENT.md. This single-container
#     default is a convenience compromise, not full segregation.
#
#     Both entries use the installed console script / module invocation instead of a bare
#     `python daily_runner.py` -- the package is now pip-installed, not a loose file in /app.
#
#     daily-runner is intentionally invoked with no flags: dry-run is the safe default when
#     --live is omitted (there is no --dry-run flag -- passing one is an argparse error).
#     risk_monitor.py's --initial-capital is intentionally omitted too: it now falls back to
#     config.yaml's portfolios.<name>.total_value unless overridden here.
#
#     The actual crontab is generated at CONTAINER START (docker-entrypoint.sh), not
#     here at build time, from the DAILY_RUNNER_CRON/RISK_MONITOR_CRON env vars (see
#     docker-compose.yml / .env) -- so changing the schedule only needs a container recreate,
#     not a rebuild. Defaults there match what used to be hardcoded here.
#
#     config.yaml can define any number of portfolios (daily-runner already loops
#     over all of them) -- risk_monitor.py coverage for portfolios beyond the first is
#     controlled by RISK_MONITOR_PORTFOLIOS (space-separated names, one cron entry each),
#     also read at container start. ---
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# --- entrypoint generates /etc/cron.d/momentum-cron from env vars, then runs cron in the
#     foreground so the container stays alive ---
CMD ["/app/docker-entrypoint.sh"]
