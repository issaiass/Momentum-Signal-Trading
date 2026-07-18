"""
tests/test_docker_entrypoint.py

docker-entrypoint.sh generates /etc/cron.d/momentum-cron at CONTAINER
START from env vars (DAILY_RUNNER_CRON, RISK_MONITOR_CRON, RISK_MONITOR_PORTFOLIOS)
instead of baking a fixed schedule into the image at build time. These tests run the
REAL script via subprocess (not a reimplementation of its logic) — the same
philosophy as test_architecture.py's path-resolution tests — with two things
overridden purely for testability:
  - CRONTAB_PATH points at a temp file instead of /etc/cron.d/momentum-cron, which
    doesn't exist/isn't writable outside the real container.
  - `cron` and `crontab` are stubbed no-op executables on PATH, since the real
    binaries aren't installed on a dev/CI machine and the script's `set -e` would
    otherwise abort before writing anything.

Skipped entirely if `sh` isn't on PATH (e.g. a bare Windows machine with no Git Bash/
WSL) — this script only ever actually runs inside the Linux container, so that's a
reasonable, narrow skip condition, not a gap in real coverage.
"""
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="requires a POSIX shell (sh) on PATH")


@pytest.fixture
def stub_bin_dir(tmp_path):
    """A directory on PATH with no-op `cron`/`crontab` stubs, standing in for the
    real binaries that only exist inside the actual container image."""
    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir()
    for name in ("cron", "crontab"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def run_entrypoint(stub_bin_dir, tmp_path, extra_env=None):
    crontab_path = tmp_path / "momentum-cron"
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CRONTAB_PATH"] = str(crontab_path)
    for key in ("DAILY_RUNNER_CRON", "RISK_MONITOR_CRON", "RISK_MONITOR_PORTFOLIOS"):
        env.pop(key, None)  # start from a clean slate regardless of the caller's real .env
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(["sh", str(ENTRYPOINT)], capture_output=True, text=True, env=env, timeout=15)
    assert result.returncode == 0, f"entrypoint failed: {result.stderr}"
    return crontab_path.read_text()


class TestDefaultSchedule:
    """No env vars set at all — must match the original hardcoded schedule exactly,
    since this is what every existing deployment gets on upgrade."""

    def test_default_produces_single_portfolio1_line(self, stub_bin_dir, tmp_path):
        crontab = run_entrypoint(stub_bin_dir, tmp_path)
        lines = crontab.strip().splitlines()
        assert len(lines) == 2  # daily-runner + exactly one risk_monitor line
        assert lines[0] == "35 9 * * 1-5 cd /app && daily-runner >> /app/logs/daily_$(date +%Y%m%d).log 2>&1"
        assert "--portfolio portfolio1 " in lines[1]
        assert lines[1].startswith("0 9-16 * * 1-5 ")

    def test_date_expression_stays_literal(self, stub_bin_dir, tmp_path):
        # Regression guard: $(date ...) must NOT be evaluated when the crontab is
        # generated — it has to stay literal so cron's own shell evaluates it fresh
        # every time the job fires, not once at container startup.
        crontab = run_entrypoint(stub_bin_dir, tmp_path)
        assert "$(date +%Y%m%d)" in crontab


class TestConfigurableSchedule:
    def test_custom_cron_times_are_used(self, stub_bin_dir, tmp_path):
        crontab = run_entrypoint(stub_bin_dir, tmp_path, extra_env={
            "DAILY_RUNNER_CRON": "15 8 * * 1-5",
            "RISK_MONITOR_CRON": "*/30 9-16 * * 1-5",
        })
        lines = crontab.strip().splitlines()
        assert lines[0].startswith("15 8 * * 1-5 ")
        assert lines[1].startswith("*/30 9-16 * * 1-5 ")


class TestMultiPortfolioRiskMonitoring:
    """RISK_MONITOR_PORTFOLIOS controls how many risk_monitor.py cron
    entries get generated — previously always exactly one, hardcoded to
    "portfolio1", regardless of how many portfolios config.yaml actually defined."""

    def test_single_portfolio_default(self, stub_bin_dir, tmp_path):
        crontab = run_entrypoint(stub_bin_dir, tmp_path)
        risk_monitor_lines = [l for l in crontab.strip().splitlines() if "risk_monitor" in l]
        assert len(risk_monitor_lines) == 1

    def test_multiple_portfolios_get_one_line_each(self, stub_bin_dir, tmp_path):
        crontab = run_entrypoint(stub_bin_dir, tmp_path, extra_env={
            "RISK_MONITOR_PORTFOLIOS": "portfolio1 portfolio2 portfolio3",
        })
        risk_monitor_lines = [l for l in crontab.strip().splitlines() if "risk_monitor" in l]
        assert len(risk_monitor_lines) == 3
        for name in ("portfolio1", "portfolio2", "portfolio3"):
            assert any(f"--portfolio {name} " in l for l in risk_monitor_lines)

    def test_each_portfolio_gets_its_own_log_file(self, stub_bin_dir, tmp_path):
        # Sharing one log file across portfolios would interleave/garble concurrent
        # output — each entry must write to a distinct, portfolio-named log path.
        crontab = run_entrypoint(stub_bin_dir, tmp_path, extra_env={
            "RISK_MONITOR_PORTFOLIOS": "portfolio1 portfolio2",
        })
        assert "risk_monitor_portfolio1_$(date" in crontab
        assert "risk_monitor_portfolio2_$(date" in crontab

    def test_daily_runner_line_present_regardless_of_portfolio_count(self, stub_bin_dir, tmp_path):
        crontab = run_entrypoint(stub_bin_dir, tmp_path, extra_env={
            "RISK_MONITOR_PORTFOLIOS": "portfolio1 portfolio2 portfolio3 portfolio4",
        })
        daily_lines = [l for l in crontab.strip().splitlines() if "daily-runner" in l]
        assert len(daily_lines) == 1
