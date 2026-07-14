"""
tests/test_architecture.py

Epic 17-18: tests that specifically probe the RESTRUCTURE itself, not the
strategy logic (which the rest of the suite already covers). These exist to
catch regressions in the package boundaries, path resolution, and the
circuit-breaker extraction -- the kind of bug that a pure logic test would
never catch (e.g. the "works when run from the project root but breaks from
anywhere else" class of bug found and fixed during this restructure).
"""
import subprocess
import sys
import importlib

import pytest


class TestPackageImportability:
    """Every module must import cleanly as part of the installed package -- a
    broken relative import in any single module would otherwise only surface
    at runtime, in whatever code path happens to exercise it."""

    @pytest.mark.parametrize("module_path", [
        "momentum_trading",
        "momentum_trading.core.functions",
        "momentum_trading.core.functions_quant_extensions",
        "momentum_trading.core.paths",
        "momentum_trading.backtest.momentum_backtest",
        "momentum_trading.execution.live_signal",
        "momentum_trading.risk.circuit_breaker",
        "momentum_trading.risk.risk_monitor",
        "momentum_trading.interfaces.notifications",
        "momentum_trading.interfaces.email_commands",
        "momentum_trading.daily_runner",
    ])
    def test_module_imports_cleanly(self, module_path):
        importlib.import_module(module_path)  # raises on any import error


class TestNoStaleFlatImports:
    """
    Guards against regressing back to the old flat-file import style
    (e.g. `from live_signal import X` instead of
    `from momentum_trading.execution.live_signal import X`) sneaking back in
    via a future edit -- this was found and fixed once already during this
    restructure (a local import inside daily_runner.main() was missed by the
    first pass of automated rewrites).
    """

    def test_no_bare_flat_imports_in_source(self):
        import ast
        import pathlib

        src_root = pathlib.Path(importlib.import_module("momentum_trading").__file__).parent
        flat_names = {"functions", "functions_quant_extensions", "momentum_backtest",
                      "live_signal", "notifications", "email_commands", "risk_monitor"}
        offenders = []

        for py_file in src_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in flat_names:
                            offenders.append(f"{py_file.relative_to(src_root)}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module in flat_names and (node.level or 0) == 0:
                        offenders.append(f"{py_file.relative_to(src_root)}:{node.lineno}: from {node.module} import ...")

        assert not offenders, "Found stale flat-style imports:\n" + "\n".join(offenders)


class TestPathResolutionAcrossWorkingDirectories:
    """
    THE core regression this restructure needed to fix: before core.paths
    existed, LOCK_DIR and other state paths were bare relative strings that
    only worked if the process's CWD happened to be the project root. These
    tests confirm path resolution is correct regardless of where the
    interpreter is invoked from -- run as a subprocess from a genuinely
    different working directory, not just monkeypatched within the same process.
    """

    def test_project_root_resolves_correctly_from_elsewhere(self, tmp_path):
        script = (
            "import momentum_trading.core.paths as paths\n"
            "print(paths.PROJECT_ROOT)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),  # deliberately NOT the project root
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        resolved_root = result.stdout.strip()
        # Should NOT resolve to the unrelated tmp_path we launched from
        assert resolved_root != str(tmp_path)
        # Should contain a real pyproject.toml (proves it found the actual project root)
        import pathlib
        assert (pathlib.Path(resolved_root) / "pyproject.toml").exists()

    def test_lock_dir_resolves_to_project_data_dir_from_elsewhere(self, tmp_path):
        script = (
            "import momentum_trading.risk.circuit_breaker as cb\n"
            "print(cb.LOCK_DIR)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(tmp_path),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        lock_dir = result.stdout.strip()
        assert lock_dir != str(tmp_path / "data")  # not a stray dir in the wrong place
        assert lock_dir.endswith("/data") or lock_dir.endswith("\\data")

    def test_env_override_takes_precedence(self, tmp_path):
        script = (
            "import momentum_trading.core.paths as paths\n"
            "print(paths.PROJECT_ROOT)\n"
        )
        import os
        env = os.environ.copy()
        env["MOMENTUM_TRADING_ROOT"] = str(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(tmp_path)


class TestCircuitBreakerExtraction:
    """
    Epic 18, Story 18.1: the circuit breaker logic was extracted from
    daily_runner.py into risk/circuit_breaker.py, with alerting
    dependency-injected (alert_fn) instead of imported directly, specifically
    to avoid a risk->interfaces import cycle. These tests confirm the
    extraction preserved behavior AND that the decoupling actually works
    (the module functions correctly with no alert_fn at all).
    """

    def test_check_circuit_breaker_works_without_alert_fn(self, tmp_path, monkeypatch):
        import momentum_trading.risk.circuit_breaker as cb
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        monkeypatch.setattr(cb, "LOCK_DIR", tmp_path / "data")
        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.20)

        # no alert_fn passed at all -- must not raise even when it trips
        assert cb.check_circuit_breaker("p", 1000.0, cfg) is False
        assert cb.check_circuit_breaker("p", 700.0, cfg) is True  # trips, no alert_fn, no crash

    def test_check_circuit_breaker_calls_injected_alert_fn_on_trip(self, tmp_path, monkeypatch):
        import momentum_trading.risk.circuit_breaker as cb
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        monkeypatch.setattr(cb, "LOCK_DIR", tmp_path / "data")
        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.20)

        calls = []
        def fake_alert(subject, body):
            calls.append((subject, body))

        cb.check_circuit_breaker("p", 1000.0, cfg, alert_fn=fake_alert)
        cb.check_circuit_breaker("p", 700.0, cfg, alert_fn=fake_alert)

        assert len(calls) == 1
        assert "CIRCUIT BREAKER TRIPPED" in calls[0][0]

    def test_risk_module_has_no_dependency_on_interfaces_module(self):
        # Static check: risk/circuit_breaker.py must never IMPORT anything
        # from interfaces/ -- that's precisely the coupling this extraction
        # was designed to avoid (see module docstring). Checked via AST on
        # actual import statements, not text search, since the docstring
        # itself legitimately mentions "interfaces" while explaining why.
        import ast
        import pathlib

        cb_path = pathlib.Path(
            importlib.import_module("momentum_trading.risk.circuit_breaker").__file__
        )
        tree = ast.parse(cb_path.read_text(), filename=str(cb_path))
        import_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                import_modules.append(node.module)
            elif isinstance(node, ast.Import):
                import_modules.extend(alias.name for alias in node.names)

        assert not any("interfaces" in m for m in import_modules), \
            f"risk/circuit_breaker.py imports from interfaces/: {import_modules}"

    def test_daily_runner_reexports_match_risk_module(self):
        # daily_runner.py imports these names from risk.circuit_breaker -- confirm
        # they're literally the same function objects, not accidentally
        # shadowed/redefined copies that could silently diverge.
        import momentum_trading.daily_runner as dr
        import momentum_trading.risk.circuit_breaker as cb

        assert dr.check_circuit_breaker is cb.check_circuit_breaker
        assert dr.resume_trading is cb.resume_trading
        assert dr.get_effective_max_drawdown_pct is cb.get_effective_max_drawdown_pct
        assert dr.LOCK_DIR is cb.LOCK_DIR


class TestPackageInstallability:
    """Confirms the package is genuinely pip-installable and the console-script
    entry point is registered, not just importable from a source checkout."""

    def test_console_script_entry_point_registered(self):
        result = subprocess.run(
            [sys.executable, "-c", "from importlib.metadata import entry_points; "
             "eps = entry_points(group='console_scripts'); "
             "print('daily-runner' in [ep.name for ep in eps])"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"
