"""
tests/test_daily_runner.py

Covers the CLI/operational layer: config.yaml schema validation, idempotent
same-day rebalance locking, email alert fallback behavior, and the
portfolio-level circuit breaker's persistence-across-runs logic.

Run with: pytest tests/test_daily_runner.py -v
See TESTING.md for fixture explanations and how to interpret a failure.
"""
import csv
import os
import pandas as pd
import yaml
import pytest

import momentum_trading.daily_runner as daily_runner
from momentum_trading.backtest.momentum_backtest import BacktestConfig
from momentum_trading.daily_runner import (
    load_config, validate_config_schema, already_ran_today, mark_ran_today, send_alert_email,
    check_and_handle_time_stops, check_and_handle_stop_losses, resolve_total_values, check_ticker_overlap,
    has_run_on_or_after,
)
from momentum_trading.core.audit_log import read_recent_alerts


class TestConfigSchemaValidation:
    """
    validate_config_schema() exists specifically to catch a bad config.yaml
    BEFORE it's used to build a BacktestConfig deep inside the rebalance loop,
    these tests confirm each specific mistake (empty tickers, a
    custom_weights key that doesn't match the ticker list, weights summing
    over 1.0, a negative dollar value) produces a clear, field-specific error
    naming the offending portfolio, rather than a generic crash or, worse,
    a silently-wrong sizing decision.
    """

    def test_valid_config_passes(self, sample_config_dict):
        validate_config_schema(sample_config_dict, "test.yaml")  # should not raise

    def test_missing_portfolios_key_raises(self):
        with pytest.raises(ValueError, match="portfolios"):
            validate_config_schema({}, "test.yaml")

    def test_empty_tickers_raises(self):
        bad = {"portfolios": {"p1": {"tickers": []}}}
        with pytest.raises(ValueError, match="tickers"):
            validate_config_schema(bad, "test.yaml")

    def test_custom_weight_for_unknown_ticker_raises(self):
        # A stale custom_weights entry (referencing a ticker no longer in the
        # portfolio) would otherwise be silently ignored, this forces the
        # mismatch to be caught and fixed rather than quietly producing a
        # different allocation than intended.
        bad = {"portfolios": {"p1": {"tickers": ["SPY"], "custom_weights": {"QQQ": 0.5}}}}
        with pytest.raises(ValueError, match="not in this portfolio"):
            validate_config_schema(bad, "test.yaml")

    def test_custom_weights_over_one_raises(self):
        # Weights summing above 1.0 would imply leverage the user probably
        # didn't intend to configure explicitly.
        bad = {"portfolios": {"p1": {"tickers": ["SPY", "QQQ"],
                                       "custom_weights": {"SPY": 0.8, "QQQ": 0.8}}}}
        with pytest.raises(ValueError, match="sum to"):
            validate_config_schema(bad, "test.yaml")

    def test_negative_total_value_raises(self):
        bad = {"portfolios": {"p1": {"tickers": ["SPY"], "total_value": -100}}}
        with pytest.raises(ValueError, match="total_value"):
            validate_config_schema(bad, "test.yaml")

    def test_multiple_null_total_values_is_now_valid(self):
        # total_value: null means "an equal share of the account remainder", multiple null
        # portfolios split that remainder equally (resolve_total_values()), no longer an
        # ambiguous/rejected configuration, this must pass schema validation cleanly.
        ok = {"portfolios": {
            "p1": {"tickers": ["SPY"], "total_value": None},
            "p2": {"tickers": ["QQQ"], "total_value": None},
        }}
        validate_config_schema(ok, "test.yaml")  # should not raise

    def test_single_null_total_value_is_valid(self):
        ok = {"portfolios": {
            "p1": {"tickers": ["SPY"], "total_value": None},
            "p2": {"tickers": ["QQQ"], "total_value": 500.0},
        }}
        validate_config_schema(ok, "test.yaml")  # should not raise

    def test_non_bool_send_warning_raises(self):
        # send_warning: "false" is a truthy non-empty string in
        # Python, would otherwise silently mean "send" via default truthiness, the
        # opposite of what someone writing that value almost certainly intended. This
        # field gates whether a real capital-safety risk reaches you by email at all,
        # so a bad value here must fail loudly, not silently do the wrong thing.
        bad = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "notifications": {"send_warning": "false"}}
        with pytest.raises(ValueError, match="send_warning"):
            validate_config_schema(bad, "test.yaml")

    def test_bool_send_warning_is_valid(self):
        ok_true = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "notifications": {"send_warning": True}}
        ok_false = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "notifications": {"send_warning": False}}
        validate_config_schema(ok_true, "test.yaml")   # should not raise
        validate_config_schema(ok_false, "test.yaml")  # should not raise

    def test_missing_notifications_section_is_valid(self):
        ok = {"portfolios": {"p1": {"tickers": ["SPY"]}}}
        validate_config_schema(ok, "test.yaml")  # should not raise

    def test_non_bool_send_email_command_feedback_raises(self):
        # Same YAML-truthiness footgun as send_warning above, this field gates the
        # ACCEPTED/REJECTED/ERROR reply emails for email-commanded remote actions.
        bad = {"portfolios": {"p1": {"tickers": ["SPY"]}},
               "notifications": {"send_email_command_feedback": "false"}}
        with pytest.raises(ValueError, match="send_email_command_feedback"):
            validate_config_schema(bad, "test.yaml")

    def test_bool_send_email_command_feedback_is_valid(self):
        ok_true = {"portfolios": {"p1": {"tickers": ["SPY"]}},
                   "notifications": {"send_email_command_feedback": True}}
        ok_false = {"portfolios": {"p1": {"tickers": ["SPY"]}},
                    "notifications": {"send_email_command_feedback": False}}
        validate_config_schema(ok_true, "test.yaml")   # should not raise
        validate_config_schema(ok_false, "test.yaml")  # should not raise

    def test_missing_account_wide_max_drawdown_pct_is_valid(self):
        # 0.0 (disabled) is the implicit default when the field is absent, an existing
        # config.yaml without this field must load exactly as before this feature existed.
        ok = {"portfolios": {"p1": {"tickers": ["SPY"]}}}
        validate_config_schema(ok, "test.yaml")  # should not raise

    def test_account_wide_max_drawdown_pct_in_range_is_valid(self):
        ok = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "account_wide_max_drawdown_pct": 0.25}
        validate_config_schema(ok, "test.yaml")  # should not raise

    def test_account_wide_max_drawdown_pct_out_of_range_raises(self):
        bad_negative = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "account_wide_max_drawdown_pct": -0.1}
        with pytest.raises(ValueError, match="account_wide_max_drawdown_pct"):
            validate_config_schema(bad_negative, "test.yaml")
        # >= 1.0 would mean "halt only after losing 100%+", can never trigger, disables the
        # feature while looking configured, same reasoning as max_portfolio_drawdown_pct's
        # existing BacktestConfig validation.
        bad_too_high = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "account_wide_max_drawdown_pct": 1.0}
        with pytest.raises(ValueError, match="account_wide_max_drawdown_pct"):
            validate_config_schema(bad_too_high, "test.yaml")

    def test_account_wide_max_drawdown_pct_wrong_type_raises(self):
        bad = {"portfolios": {"p1": {"tickers": ["SPY"]}}, "account_wide_max_drawdown_pct": "0.2"}
        with pytest.raises(ValueError, match="account_wide_max_drawdown_pct"):
            validate_config_schema(bad, "test.yaml")


class TestLoadConfig:
    """
    load_config() is the full pipeline: read YAML -> schema-validate ->
    build a BacktestConfig per portfolio. These tests confirm the happy path
    works end-to-end from a real file on disk (not just an in-memory dict,
    which TestConfigSchemaValidation covers), and that an invalid
    risk_override surfaces the PORTFOLIO NAME in the error, with multiple
    portfolios in one config.yaml, a generic "invalid config" error would be
    much harder to act on than one that says which portfolio is broken.
    """

    def test_load_valid_yaml_file(self, tmp_path, sample_config_dict):
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(sample_config_dict, f)

        result = load_config(str(path))
        assert "portfolio1" in result["portfolios_resolved"]
        assert result["portfolios_resolved"]["portfolio1"]["tickers"] == ["SPY", "QQQ", "XLK"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_invalid_risk_override_raises_with_portfolio_name(self, tmp_path):
        bad = {
            "portfolios": {"p1": {"tickers": ["SPY"], "risk_overrides": {"stop_loss_pct": 5.0}}}
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(bad, f)
        with pytest.raises(ValueError, match="p1"):
            load_config(str(path))

    def test_top_n_independent_per_portfolio(self, tmp_path):
        # Confirms top_n is genuinely per-portfolio, not a single value
        # shared across a config.yaml with several portfolios, four portfolios,
        # four different top_n values, each resolved independently via
        # risk_overrides on top of one shared default_risk.top_n.
        cfg = {
            "default_risk": {"holding_period": 1, "top_n": 3},   # portfolio1 uses this as-is
            "portfolios": {
                # Explicit total_value on every portfolio here, unrelated to what
                # this test checks (top_n independence), but required since more than
                # one portfolio with total_value: null (unset defaults to null) in
                # the same config.yaml is forbidden.
                "portfolio1": {"tickers": ["SPY", "QQQ", "XLK"], "total_value": 1000.0},
                "portfolio2": {"tickers": ["XLF", "XLE", "GLD", "TLT"], "total_value": 1000.0,
                               "risk_overrides": {"top_n": 10}},
                "portfolio3": {"tickers": ["SPY", "QQQ", "XLK", "XLF", "XLE"], "total_value": 1000.0,
                               "risk_overrides": {"top_n": 40}},
                "portfolio4": {"tickers": ["GLD", "TLT", "BIL"], "total_value": 1000.0,
                               "risk_overrides": {"top_n": 5}},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)

        resolved = load_config(str(path))["portfolios_resolved"]
        expected = {"portfolio1": 3, "portfolio2": 10, "portfolio3": 40, "portfolio4": 5}
        for name, expected_top_n in expected.items():
            assert resolved[name]["cfg"].top_n == expected_top_n, (
                f"{name}: expected top_n={expected_top_n}, got {resolved[name]['cfg'].top_n}"
            )


    def test_lookback_period_independent_per_portfolio(self, tmp_path):
        # Mirrors test_top_n_independent_per_portfolio, confirms lookback_period
        # (the trailing-months momentum ranking window) resolves per-portfolio via
        # risk_overrides on top of one shared default_risk.lookback_period, the same
        # way every other BacktestConfig field already does.
        cfg = {
            "default_risk": {"holding_period": 1, "lookback_period": 12},
            "portfolios": {
                "portfolio1": {"tickers": ["SPY", "QQQ", "XLK"], "total_value": 1000.0},
                "portfolio2": {"tickers": ["XLF", "XLE", "GLD", "TLT"], "total_value": 1000.0,
                               "risk_overrides": {"lookback_period": 6}},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)

        resolved = load_config(str(path))["portfolios_resolved"]
        assert resolved["portfolio1"]["cfg"].lookback_period == 12
        assert resolved["portfolio2"]["cfg"].lookback_period == 6


class TestApplyStrategyTypePreset:
    """
    apply_strategy_type_preset() (Epic 1 of the selectable-momentum-strategy plan): selecting a
    strategy_type auto-configures the underlying fields it maps to, UNLESS the portfolio's own
    config already set that specific field explicitly, which always wins. Tested both as a pure
    function directly and end-to-end through load_config() (the actual call site).
    """

    def test_dual_momentum_implies_absolute_momentum_and_regime_filter(self):
        from momentum_trading.daily_runner import apply_strategy_type_preset
        result = apply_strategy_type_preset({"strategy_type": "dual_momentum"})
        assert result["use_absolute_momentum"] is True
        assert result["use_regime_filter"] is True

    def test_explicit_field_value_overrides_the_preset(self):
        from momentum_trading.daily_runner import apply_strategy_type_preset
        result = apply_strategy_type_preset({
            "strategy_type": "dual_momentum", "use_absolute_momentum": False,
        })
        assert result["use_absolute_momentum"] is False  # explicit value wins over the preset
        assert result["use_regime_filter"] is True        # untouched field still gets the preset

    def test_volatility_scaled_momentum_implies_inverse_vol_sizing(self):
        from momentum_trading.daily_runner import apply_strategy_type_preset
        result = apply_strategy_type_preset({"strategy_type": "volatility_scaled_momentum"})
        assert result["sizing_method"] == "inverse_vol"

    def test_correlation_weighted_momentum_implies_correlation_penalty(self):
        from momentum_trading.daily_runner import apply_strategy_type_preset
        result = apply_strategy_type_preset({"strategy_type": "correlation_weighted_momentum"})
        assert result["use_correlation_penalty"] is True

    def test_momentum_and_relative_momentum_and_unset_are_all_no_ops(self):
        from momentum_trading.daily_runner import apply_strategy_type_preset
        base = {"top_n": 7}
        for merged in (
            {**base, "strategy_type": "momentum"},
            {**base, "strategy_type": "relative_momentum"},
            dict(base),  # no strategy_type key at all
        ):
            result = apply_strategy_type_preset(merged)
            assert result == merged  # byte-identical, no fields added

    def test_load_config_wires_the_preset_end_to_end(self, tmp_path):
        cfg = {
            "portfolios": {
                "p1": {"tickers": ["SPY", "QQQ"], "total_value": 1000.0,
                       "risk_overrides": {"strategy_type": "dual_momentum"}},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)

        resolved = load_config(str(path))["portfolios_resolved"]
        assert resolved["p1"]["cfg"].use_absolute_momentum is True
        assert resolved["p1"]["cfg"].use_regime_filter is True

    def test_load_config_respects_explicit_override_alongside_preset(self, tmp_path):
        cfg = {
            "portfolios": {
                "p1": {"tickers": ["SPY", "QQQ"], "total_value": 1000.0,
                       "risk_overrides": {"strategy_type": "dual_momentum",
                                          "use_absolute_momentum": False}},
            },
        }
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)

        resolved = load_config(str(path))["portfolios_resolved"]
        assert resolved["p1"]["cfg"].use_absolute_momentum is False


class TestIdempotency:
    """
    Guards against a duplicate same-day rebalance (e.g. a cron retry firing
    twice, or a manual run overlapping a scheduled one), without this,
    the system could place the same intended trade twice.
    """

    def test_lock_lifecycle(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")

        tag = "test_portfolio"
        assert already_ran_today(tag) is False
        mark_ran_today(tag)
        assert already_ran_today(tag) is True

    def test_as_of_checks_a_specific_past_date_not_today(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")

        import datetime as dt
        tag = "test_portfolio"
        past_date = dt.date(2026, 1, 2)

        # No lock exists for that past date yet, and today's own lock is unrelated.
        assert already_ran_today(tag, as_of=past_date) is False
        mark_ran_today(tag)  # marks TODAY, not past_date
        assert already_ran_today(tag, as_of=past_date) is False  # still false, unaffected

        # Directly write a lock file for the past date, as if it HAD run that day.
        (tmp_path / "data" / f"last_run_{tag}_20260102.lock").write_text("x")
        assert already_ran_today(tag, as_of=past_date) is True


class TestHasRunOnOrAfter:
    """
    Backs the missed-rebalance-day check's "has this period already been handled somehow"
    question. Deliberately a RANGE check, not an exact-date match: a manual --force-rebalance
    catch-up marks TODAY's own date, never the missed period's original target date, so an
    exact-date check would keep warning forever even after the user follows the warning's own
    suggested remedy. This must clear the moment ANY lock exists on or after the missed date.
    """

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        (tmp_path / "data").mkdir(exist_ok=True)

    def test_no_locks_at_all_is_false(self, tmp_path, monkeypatch):
        import datetime as dt
        self._isolate(tmp_path, monkeypatch)
        assert has_run_on_or_after("rebalance_p1", dt.date(2026, 7, 1)) is False

    def test_only_older_lock_is_false(self, tmp_path, monkeypatch):
        import datetime as dt
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / "data" / "last_run_rebalance_p1_20260601.lock").write_text("x")
        assert has_run_on_or_after("rebalance_p1", dt.date(2026, 7, 1)) is False

    def test_exact_date_lock_is_true(self, tmp_path, monkeypatch):
        import datetime as dt
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / "data" / "last_run_rebalance_p1_20260701.lock").write_text("x")
        assert has_run_on_or_after("rebalance_p1", dt.date(2026, 7, 1)) is True

    def test_later_catch_up_lock_is_true(self, tmp_path, monkeypatch):
        # The exact scenario this exists for: missed on 2026-07-01, manually caught up via
        # --force-rebalance on 2026-07-19, which marks 20260719, not 20260701.
        import datetime as dt
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / "data" / "last_run_rebalance_p1_20260719.lock").write_text("x")
        assert has_run_on_or_after("rebalance_p1", dt.date(2026, 7, 1)) is True

    def test_different_portfolio_tag_is_ignored(self, tmp_path, monkeypatch):
        import datetime as dt
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / "data" / "last_run_rebalance_p2_20260719.lock").write_text("x")
        assert has_run_on_or_after("rebalance_p1", dt.date(2026, 7, 1)) is False


class TestRebalanceInProgressMarker:
    """
    Written immediately before run() is called for a rebalance, deleted immediately after
    (success or a handled exception). A marker still present on a LATER run means a previous
    process crashed mid-rebalance, purely a visibility signal (see the WARNING wiring), it does
    not block or change the current run, the diff-based order generation already makes a retry
    safe on its own. Written atomically (temp file + os.replace()), not the plain write_text()
    this project's other flag files use, since this one specifically exists to be readable
    reliably even by a concurrently-running process (risk_monitor.py's independent hourly cron
    can overlap daily_runner.py's run in the default Docker setup, confirmed by reading
    docker-entrypoint.sh, though risk_monitor.py itself never actually reads this file, it stays
    independent).
    """

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        return daily_runner

    def test_write_then_clear_lifecycle(self, tmp_path, monkeypatch):
        daily_runner = self._isolate(tmp_path, monkeypatch)
        path = daily_runner._rebalance_in_progress_marker_path("p1")
        assert not path.exists()

        daily_runner._write_rebalance_in_progress_marker("p1")
        assert path.exists()
        assert path.read_text().strip()  # a real timestamp, not empty

        daily_runner._clear_rebalance_in_progress_marker("p1")
        assert not path.exists()

    def test_clear_is_safe_when_no_marker_exists(self, tmp_path, monkeypatch):
        # No marker was ever written (e.g. a crash happened before the write itself), clearing
        # must not raise.
        daily_runner = self._isolate(tmp_path, monkeypatch)
        daily_runner._clear_rebalance_in_progress_marker("p1")  # should not raise

    def test_write_leaves_no_stray_temp_file(self, tmp_path, monkeypatch):
        # Atomic write via temp-file + os.replace(): the intermediate .tmp file must not survive.
        daily_runner = self._isolate(tmp_path, monkeypatch)
        daily_runner._write_rebalance_in_progress_marker("p1")
        marker = daily_runner._rebalance_in_progress_marker_path("p1")
        tmp = marker.with_suffix(marker.suffix + ".tmp")
        assert not tmp.exists()

    def test_different_portfolios_get_independent_markers(self, tmp_path, monkeypatch):
        daily_runner = self._isolate(tmp_path, monkeypatch)
        daily_runner._write_rebalance_in_progress_marker("p1")
        assert daily_runner._rebalance_in_progress_marker_path("p1").exists()
        assert not daily_runner._rebalance_in_progress_marker_path("p2").exists()


class TestClassifyOrphanedTickers:
    """
    A ticker currently held but NOT in this portfolio's configured tickers: list is either
    (a) confirmed_orphaned: THIS portfolio's own trade log shows an open BUY history for it
    (removed from config after being legitimately held here), safe to price/reconcile, or
    (b) unrecognized: not confirmed, could belong to a SIBLING portfolio sharing the same real
    IBKR account (the documented multi-portfolio ticker-leakage scenario), must NOT be
    auto-priced or auto-traded. Reuses derive_entry_date() (already used by
    check_and_handle_time_stops()), does not invent a new distinguishing mechanism.
    """

    def _write_log(self, tmp_path, rows):
        path = tmp_path / "log.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares"])
            w.writerows(rows)
        return str(path)

    def test_ticker_in_configured_universe_is_excluded_entirely(self, tmp_path):
        log_path = self._write_log(tmp_path, [])
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"SPY": 10.0}, ["SPY", "QQQ"], log_path,
        )
        assert confirmed == [] and unrecognized == []

    def test_held_ticker_with_open_history_is_confirmed_orphaned(self, tmp_path):
        log_path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "OLD", "BUY", 5]])
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"OLD": 5.0}, ["SPY", "QQQ"], log_path,
        )
        assert confirmed == ["OLD"]
        assert unrecognized == []

    def test_held_ticker_with_no_history_is_unrecognized(self, tmp_path):
        log_path = self._write_log(tmp_path, [])  # empty log, no history at all
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"BIL": 20.0}, ["SPY", "QQQ"], log_path,
        )
        assert confirmed == []
        assert unrecognized == ["BIL"]

    def test_held_ticker_that_is_currently_flat_in_the_log_is_unrecognized(self, tmp_path):
        # A fully-closed position (per THIS portfolio's own log) but somehow still shows real
        # shares held (e.g. a manual TWS trade re-opened it): fails in the SAFE direction,
        # not confirmed, left untouched, per derive_entry_date()'s own documented semantics.
        log_path = self._write_log(tmp_path, [
            ["2026-01-05T09:35:00", "OLD", "BUY", 5],
            ["2026-02-02T09:35:00", "OLD", "SELL", 5],
        ])
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"OLD": 3.0}, ["SPY", "QQQ"], log_path,
        )
        assert confirmed == []
        assert unrecognized == ["OLD"]

    def test_no_orphaned_tickers_when_all_holdings_are_configured(self, tmp_path):
        log_path = self._write_log(tmp_path, [])
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"SPY": 10.0, "QQQ": 5.0}, ["SPY", "QQQ"], log_path,
        )
        assert confirmed == [] and unrecognized == []

    def test_mix_of_confirmed_and_unrecognized(self, tmp_path):
        log_path = self._write_log(tmp_path, [["2026-01-05T09:35:00", "OLD", "BUY", 5]])
        confirmed, unrecognized = daily_runner._classify_orphaned_tickers(
            {"SPY": 10.0, "OLD": 5.0, "BIL": 20.0}, ["SPY"], log_path,
        )
        assert confirmed == ["OLD"]
        assert unrecognized == ["BIL"]


class TestComputeScopedPositionsValue:
    """
    Backs the total_value drift warning. Deliberately an EXPLICIT set intersection against
    this portfolio's own tickers (+ confirmed_orphaned), not the pre-existing positions_value/
    write_portfolio_snapshot() computation's implicit (price-availability-only) scoping, which
    was found to double-count a ticker legitimately shared between two portfolios under the
    documented TICKER OVERLAP scenario. This must NOT reproduce that bug.
    """

    def test_sums_only_configured_and_confirmed_orphaned_tickers(self):
        current_positions = {
            "SPY": {"shares": 2.0}, "OLD": {"shares": 5.0}, "FOREIGN": {"shares": 100.0},
        }
        latest_prices = {"SPY": 500.0, "OLD": 20.0, "FOREIGN": 10.0}
        value = daily_runner._compute_scoped_positions_value(
            current_positions, latest_prices, ["SPY"], ["OLD"],
        )
        # SPY (2*500) + OLD (5*20) = 1100, FOREIGN excluded (not configured, not confirmed orphaned)
        assert value == pytest.approx(1100.0)

    def test_missing_price_is_excluded_not_a_crash(self):
        current_positions = {"SPY": {"shares": 2.0}, "OLD": {"shares": 5.0}}
        latest_prices = {"SPY": 500.0}  # OLD has no price this run
        value = daily_runner._compute_scoped_positions_value(
            current_positions, latest_prices, ["SPY"], ["OLD"],
        )
        assert value == pytest.approx(1000.0)

    def test_no_double_counting_a_ticker_shared_between_two_portfolios(self):
        # The exact scenario the pre-existing positions_value computation gets wrong: a ticker
        # legitimately configured in BOTH portfolio1 and portfolio2 (TICKER OVERLAP). Each
        # portfolio's OWN scoped computation must independently see the SAME full share count
        # (this function doesn't attempt to split ownership, that's a separate, harder problem),
        # but a portfolio that does NOT configure the shared ticker must get ZERO for it,
        # unlike the pre-existing computation's implicit scoping.
        current_positions = {"XLF": {"shares": 10.0}}
        latest_prices = {"XLF": 50.0}
        # portfolio1 configures XLF:
        value_p1 = daily_runner._compute_scoped_positions_value(
            current_positions, latest_prices, ["XLF"], [],
        )
        # portfolio2 does NOT configure XLF, and it's not confirmed-orphaned for portfolio2 either:
        value_p2 = daily_runner._compute_scoped_positions_value(
            current_positions, latest_prices, ["QQQ"], [],
        )
        assert value_p1 == pytest.approx(500.0)
        assert value_p2 == pytest.approx(0.0)

    def test_empty_positions_returns_zero(self):
        assert daily_runner._compute_scoped_positions_value({}, {}, ["SPY"], []) == 0.0


class TestAlertFallback:
    """
    If SMTP isn't configured, an alert must still be VISIBLE (as an ERROR log
    line) rather than silently disappearing, a failed unattended run with
    no notification at all is the worst-case outcome this test guards against.
    """

    def test_unconfigured_smtp_does_not_raise(self, monkeypatch):
        for var in ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO_EMAIL"]:
            monkeypatch.delenv(var, raising=False)
        send_alert_email("Test", "body")  # should log an error, not raise


class TestCircuitBreaker:
    """
    The circuit breaker's most important, least obvious behavioral
    requirement is that it does NOT auto-resume after a drawdown recovers,
    a human must explicitly clear it. This is deliberate: a brief recovery
    during a volatile period shouldn't silently re-enable trading without
    review. test_trips_on_drawdown_and_persists_despite_recovery is the test
    that would catch a regression of exactly that behavior.
    """

    def test_trips_on_drawdown_and_persists_despite_recovery(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        # Tripping/resuming the breaker also calls log_alert(), which resolves via
        # the module-global ALERTS_LOG_PATH, isolate it so this test doesn't
        # write into the real project's logs/alerts_log.csv.
        monkeypatch.setattr(circuit_breaker, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))
        from momentum_trading.daily_runner import check_circuit_breaker, resume_trading
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.20)
        assert check_circuit_breaker("p", 1000.0, cfg) is False
        assert check_circuit_breaker("p", 1200.0, cfg) is False  # new peak
        assert check_circuit_breaker("p", 950.0, cfg) is True    # -20.8% from peak, trips
        assert check_circuit_breaker("p", 1300.0, cfg) is True   # still halted despite recovery
        resume_trading("p")
        assert check_circuit_breaker("p", 1300.0, cfg) is False  # cleared after explicit resume

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        # Confirms max_portfolio_drawdown_pct=0.0 (the default) genuinely
        # disables the feature, an existing config.yaml without this field
        # set should NOT unexpectedly start halting trades.
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import check_circuit_breaker
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        cfg = BacktestConfig()  # max_portfolio_drawdown_pct=0.0 by default
        assert check_circuit_breaker("p", 100.0, cfg) is False  # -90% drawdown, but disabled

    def test_externally_written_halt_flag_is_respected_even_with_breaker_disabled(self, tmp_path, monkeypatch):
        # REGRESSION for a real, pre-existing bug found while building the account-wide breaker
        # (Epic 4 of the layered risk-management plan): check_circuit_breaker()'s early return
        # ("both config breakers disabled, nothing to check") used to skip the halt_path.exists()
        # check ENTIRELY, meaning a halt flag written by an EXTERNAL source (risk_monitor.py's
        # write_halt_flag(), an email-commanded PAUSE, or the new account-wide breaker) was
        # SILENTLY IGNORED by daily_runner.py's rebalance gate whenever this specific portfolio's
        # OWN max_portfolio_drawdown_pct/max_dollar_drawdown were left at their shipped defaults
        # (0.0/None), which is the common case. This made risk_monitor.py's entire documented
        # purpose ("writes a halt flag file daily_runner.py checks and respects") silently
        # ineffective for any portfolio that hadn't separately opted into its own drawdown
        # breaker. Confirmed by direct reproduction before this fix (check_circuit_breaker()
        # returned False despite the flag file existing on disk).
        monkeypatch.chdir(tmp_path)
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        circuit_breaker.LOCK_DIR.mkdir(exist_ok=True)
        from momentum_trading.risk.circuit_breaker import check_circuit_breaker, _halt_flag_path
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        _halt_flag_path("p1").write_text("2026-01-01T00:00:00 | risk_monitor.py: some halt reason")
        cfg = BacktestConfig()  # both breakers at their default-disabled values
        assert check_circuit_breaker("p1", 1000.0, cfg) is True


class TestComputeAccountWideDrawdown:
    """
    compute_account_wide_drawdown() (Epic 4 of the layered risk-management plan, "Drawdown
    Circuit Breaker", Recommended tier, account-wide variant): a pure hand-verifiable formula,
    same shape as check_circuit_breaker()'s existing per-portfolio drawdown_pct/drawdown_dollar
    math, no I/O.
    """

    def test_hand_computed_drawdown(self):
        from momentum_trading.risk.circuit_breaker import compute_account_wide_drawdown
        result = compute_account_wide_drawdown(current_account_value=8000.0, peak_account_equity=10000.0)
        assert result["drawdown_pct"] == pytest.approx(-0.20)
        assert result["drawdown_dollar"] == pytest.approx(2000.0)

    def test_no_drawdown_when_at_peak(self):
        from momentum_trading.risk.circuit_breaker import compute_account_wide_drawdown
        result = compute_account_wide_drawdown(current_account_value=10000.0, peak_account_equity=10000.0)
        assert result["drawdown_pct"] == pytest.approx(0.0)
        assert result["drawdown_dollar"] == pytest.approx(0.0)

    def test_zero_peak_does_not_divide_by_zero(self):
        from momentum_trading.risk.circuit_breaker import compute_account_wide_drawdown
        result = compute_account_wide_drawdown(current_account_value=0.0, peak_account_equity=0.0)
        assert result["drawdown_pct"] == pytest.approx(0.0)
        assert result["drawdown_dollar"] == pytest.approx(0.0)


class TestAccountWideCircuitBreaker:
    """
    check_account_wide_drawdown_breaker(), the halting wiring built on top of
    compute_account_wide_drawdown() above. Distinct from TestCircuitBreaker's per-portfolio
    breaker: this evaluates the SUM of every portfolio's resolved capital against ONE
    account-wide peak, and when tripped, halts EVERY portfolio at once (writes each one's own
    circuit_breaker_halted_<name>.flag, the exact file the existing per-portfolio rebalance
    gate already checks, no new gating code path needed downstream).
    """

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(circuit_breaker, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))
        monkeypatch.setattr(daily_runner, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        from momentum_trading.daily_runner import check_account_wide_drawdown_breaker
        # 0.0 = disabled, matches max_portfolio_drawdown_pct's exact convention, must never
        # halt anything even under a severe drawdown when left at its default.
        tripped = check_account_wide_drawdown_breaker(["p1", "p2"], 100.0, max_drawdown_pct=0.0)
        assert tripped is False
        assert not (tmp_path / "data" / "circuit_breaker_halted_p1.flag").exists()
        assert not (tmp_path / "data" / "circuit_breaker_halted_p2.flag").exists()

    def test_breach_halts_every_portfolio(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        from momentum_trading.daily_runner import check_account_wide_drawdown_breaker

        assert check_account_wide_drawdown_breaker(["p1", "p2"], 10000.0, max_drawdown_pct=0.20) is False
        assert check_account_wide_drawdown_breaker(["p1", "p2"], 12000.0, max_drawdown_pct=0.20) is False  # new peak
        tripped = check_account_wide_drawdown_breaker(["p1", "p2"], 9000.0, max_drawdown_pct=0.20)  # -25% from peak
        assert tripped is True
        assert (tmp_path / "data" / "circuit_breaker_halted_p1.flag").exists()
        assert (tmp_path / "data" / "circuit_breaker_halted_p2.flag").exists()

    def test_resuming_each_portfolio_individually_clears_its_own_halt(self, tmp_path, monkeypatch):
        # Per the plan: resuming reuses the EXISTING per-portfolio resume_trading(), no new
        # resume mechanism, since the account-wide breaker writes to the SAME halt-flag file
        # check_circuit_breaker()/resume_trading() already manage.
        self._isolate(tmp_path, monkeypatch)
        from momentum_trading.daily_runner import check_account_wide_drawdown_breaker
        from momentum_trading.risk.circuit_breaker import resume_trading

        check_account_wide_drawdown_breaker(["p1", "p2"], 10000.0, max_drawdown_pct=0.20)
        check_account_wide_drawdown_breaker(["p1", "p2"], 7000.0, max_drawdown_pct=0.20)
        assert (tmp_path / "data" / "circuit_breaker_halted_p1.flag").exists()
        resume_trading("p1")
        assert not (tmp_path / "data" / "circuit_breaker_halted_p1.flag").exists()
        assert (tmp_path / "data" / "circuit_breaker_halted_p2.flag").exists()  # untouched

    def test_alert_fn_called_with_every_affected_portfolio_named(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        from momentum_trading.daily_runner import check_account_wide_drawdown_breaker

        calls = []
        check_account_wide_drawdown_breaker(["p1", "p2"], 10000.0, max_drawdown_pct=0.20)
        check_account_wide_drawdown_breaker(["p1", "p2"], 7000.0, max_drawdown_pct=0.20,
                                             alert_fn=lambda subject, body: calls.append((subject, body)))
        assert len(calls) == 1
        subject, body = calls[0]
        assert "p1" in body and "p2" in body


class TestMaxDrawdownEmailOverride:
    """
    SET_MAX_DRAWDOWN's core safety property, the
    override can only TIGHTEN the effective breaker threshold, never loosen
    it, enforced at the point of USE (get_effective_max_drawdown_pct), not
    at email-parse time. A regression here would mean a malformed or
    misconfigured override could accidentally make the bot LESS safe, which
    would defeat the entire point of this feature.
    """

    def test_no_override_uses_configured_value(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import get_effective_max_drawdown_pct

        assert get_effective_max_drawdown_pct("p", 0.20) == 0.20

    def test_tighter_override_takes_effect(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import get_effective_max_drawdown_pct, _max_drawdown_override_path

        (tmp_path / "data").mkdir(exist_ok=True)
        _max_drawdown_override_path("p").write_text("0.10")
        assert get_effective_max_drawdown_pct("p", 0.20) == 0.10

    def test_looser_override_is_ignored(self, tmp_path, monkeypatch):
        # THE critical test: an override requesting a LOOSER threshold than
        # config must be ignored, the configured (tighter) value wins.
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        from momentum_trading.daily_runner import get_effective_max_drawdown_pct, _max_drawdown_override_path

        (tmp_path / "data").mkdir(exist_ok=True)
        _max_drawdown_override_path("p").write_text("0.50")  # looser than config
        assert get_effective_max_drawdown_pct("p", 0.20) == 0.20  # config wins, not 0.50

    def test_tighter_override_trips_breaker_earlier_than_loose_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        import momentum_trading.daily_runner as daily_runner
        import momentum_trading.risk.circuit_breaker as circuit_breaker
        monkeypatch.setattr(circuit_breaker, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        # Tripping the breaker also calls log_alert(), isolate ALERTS_LOG_PATH too.
        monkeypatch.setattr(circuit_breaker, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))
        from momentum_trading.daily_runner import check_circuit_breaker, _max_drawdown_override_path
        from momentum_trading.backtest.momentum_backtest import BacktestConfig

        (tmp_path / "data").mkdir(exist_ok=True)
        _max_drawdown_override_path("p").write_text("0.05")  # very tight override
        cfg = BacktestConfig(max_portfolio_drawdown_pct=0.50)  # loose config

        assert check_circuit_breaker("p", 1000.0, cfg) is False  # sets peak
        # -6% breaches the tight 5% override, even though config allows up to 50%
        assert check_circuit_breaker("p", 940.0, cfg) is True


class TestTimeStops:
    """
    Live-trading equivalent of the backtest's
    max_holding_days, independent of and in addition to the price-based
    stop-loss (check_and_handle_stop_losses), sharing its auto_execute_stop_loss
    flag rather than a second config field.
    """

    @pytest.fixture(autouse=True)
    def _isolate_alerts_log(self, tmp_path, monkeypatch):
        # check_and_handle_time_stops() now also calls
        # log_alert(), which resolves its path via the module-global
        # ALERTS_LOG_PATH (imported from core.audit_log, resolved once at
        # import time into logs_dir()/alerts_log.csv, same pattern as every
        # other daily_runner.py path constant), without patching it, these
        # tests would write TIME_STOP_TRIGGERED rows into the real project's
        # logs/alerts_log.csv instead of tmp_path.
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))

    def _write_log(self, tmp_path, rows):
        path = tmp_path / "trade_log.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "ticker", "action", "shares"])
            w.writerows(rows)
        return str(path)

    def test_disabled_when_max_holding_days_is_none(self, tmp_path):
        trade_log = self._write_log(tmp_path, [["2020-01-01T09:35:00", "XLK", "BUY", 10]])
        cfg = BacktestConfig(max_holding_days=None)
        flagged = check_and_handle_time_stops(
            tickers=["XLK"], current_positions={"XLK": {"shares": 10}},
            latest_prices={"XLK": 100.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(tmp_path / "out.csv"), trade_log_path=trade_log,
        )
        assert flagged == []

    def test_not_flagged_when_held_less_than_max_holding_days(self, tmp_path):
        recent = pd.Timestamp.now() - pd.Timedelta(days=5)
        trade_log = self._write_log(tmp_path, [[recent.isoformat(), "XLK", "BUY", 10]])
        cfg = BacktestConfig(max_holding_days=30)
        flagged = check_and_handle_time_stops(
            tickers=["XLK"], current_positions={"XLK": {"shares": 10}},
            latest_prices={"XLK": 100.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(tmp_path / "out.csv"), trade_log_path=trade_log,
        )
        assert flagged == []

    def test_flagged_only_when_auto_execute_disabled(self, tmp_path):
        old = pd.Timestamp.now() - pd.Timedelta(days=40)
        trade_log = self._write_log(tmp_path, [[old.isoformat(), "XLK", "BUY", 10]])
        out_path = tmp_path / "out.csv"
        cfg = BacktestConfig(max_holding_days=30, auto_execute_stop_loss=False)
        flagged = check_and_handle_time_stops(
            tickers=["XLK"], current_positions={"XLK": {"shares": 10}},
            latest_prices={"XLK": 100.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(out_path), trade_log_path=trade_log,
        )
        assert flagged == ["XLK"]
        assert not out_path.exists()  # flag-only: no exit order was logged

    def test_auto_executed_when_enabled(self, tmp_path):
        old = pd.Timestamp.now() - pd.Timedelta(days=40)
        trade_log = self._write_log(tmp_path, [[old.isoformat(), "XLK", "BUY", 10]])
        out_path = tmp_path / "out.csv"
        cfg = BacktestConfig(max_holding_days=30, auto_execute_stop_loss=True)
        flagged = check_and_handle_time_stops(
            tickers=["XLK"], current_positions={"XLK": {"shares": 10}},
            latest_prices={"XLK": 100.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(out_path), trade_log_path=trade_log, portfolio="p1",
        )
        assert flagged == ["XLK"]
        logged = pd.read_csv(out_path)
        assert logged.iloc[0]["action"] == "SELL"
        assert logged.iloc[0]["ticker"] == "XLK"

        # TIME_STOP_TRIGGERED must land in the alert log,
        # tagged with the portfolio it actually fired for.
        rows = read_recent_alerts(portfolio="p1", log_path=str(tmp_path / "data" / "alerts_log.csv"))
        assert len(rows) == 1
        assert rows[0]["alert_type"] == "TIME_STOP_TRIGGERED"
        assert rows[0]["severity"] == "CRITICAL"

    def test_flat_position_not_flagged(self, tmp_path):
        # A ticker with shares=0 in current_positions (fully exited) must never
        # be flagged, regardless of what the trade log says about its history.
        old = pd.Timestamp.now() - pd.Timedelta(days=40)
        trade_log = self._write_log(tmp_path, [
            [old.isoformat(), "XLK", "BUY", 10],
            [(old + pd.Timedelta(days=1)).isoformat(), "XLK", "SELL", 10],
        ])
        cfg = BacktestConfig(max_holding_days=30)
        flagged = check_and_handle_time_stops(
            tickers=["XLK"], current_positions={"XLK": {"shares": 0}},
            latest_prices={"XLK": 100.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(tmp_path / "out.csv"), trade_log_path=trade_log,
        )
        assert flagged == []


class TestStopLossCheck:
    """
    check_and_handle_stop_losses() had no dedicated test
    at all before now (only exercised indirectly). Added alongside the
    log_alert() wiring to confirm STOP_LOSS_TRIGGERED actually
    lands in the alert log with the right portfolio tag, not just that the
    ticker gets flagged.
    """

    @pytest.fixture(autouse=True)
    def _isolate_alerts_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))

    def test_triggered_position_is_flagged_and_alert_logged(self, tmp_path):
        cfg = BacktestConfig(stop_loss_pct=0.10)
        flagged = check_and_handle_stop_losses(
            tickers=["XLK"],
            current_positions={"XLK": {"shares": 10, "avg_entry_price": 100.0}},
            latest_prices={"XLK": 85.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(tmp_path / "out.csv"), portfolio="p1",
        )
        assert flagged == ["XLK"]
        rows = read_recent_alerts(portfolio="p1", log_path=str(tmp_path / "data" / "alerts_log.csv"))
        assert len(rows) == 1
        assert rows[0]["alert_type"] == "STOP_LOSS_TRIGGERED"
        assert rows[0]["severity"] == "CRITICAL"

    def test_not_flagged_within_tolerance(self, tmp_path):
        cfg = BacktestConfig(stop_loss_pct=0.10)
        flagged = check_and_handle_stop_losses(
            tickers=["XLK"],
            current_positions={"XLK": {"shares": 10, "avg_entry_price": 100.0}},
            latest_prices={"XLK": 95.0}, cfg=cfg, dry_run=True, ibkr_port=7497,
            log_path=str(tmp_path / "out.csv"), portfolio="p1",
        )
        assert flagged == []
        assert read_recent_alerts(portfolio="p1", log_path=str(tmp_path / "data" / "alerts_log.csv")) == []


class TestAlertsReportEmailCommand:
    """
    ALERTS_REPORT is handled specially in
    check_and_apply_email_commands(), BEFORE the normal per-portfolio
    targets loop, since PORTFOLIO here means "filter to this portfolio's
    alerts" (a query), not "apply this action to these portfolios" like every
    other command. These tests exercise the full path: a canned parsed
    command (poll_and_process_commands mocked out, no real IMAP) ->
    read_recent_alerts() -> send_alert_email() reply body.
    """

    @pytest.fixture(autouse=True)
    def _isolate_alerts_log(self, tmp_path, monkeypatch):
        # Without this, check_and_apply_email_commands()'s ALERTS_REPORT handler
        # would read the REAL project's logs/alerts_log.csv (via the module-global
        # ALERTS_LOG_PATH) instead of an empty tmp path, e.g. test_alerts_report_
        # no_alerts_replies_gracefully asserts "No alerts recorded", which only
        # holds if this is genuinely isolated from whatever real alerts exist.
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path / "data")
        monkeypatch.setattr(daily_runner, "ALERTS_LOG_PATH", str(tmp_path / "data" / "alerts_log.csv"))

    def _configure_email_env(self, monkeypatch):
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("IMAP_USER", "bot@example.com")
        monkeypatch.setenv("IMAP_PASS", "secret")
        monkeypatch.setenv("TRUSTED_SENDER_EMAIL", "trader@example.com")

    def _parsed(self, body):
        from momentum_trading.interfaces.email_commands import parse_command
        result = parse_command("trader@example.com", "trader@example.com", body)
        assert result.success is True
        return result

    def test_alerts_report_replies_with_matching_rows_only(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)
        from momentum_trading.core.audit_log import log_alert
        alerts_path = str(tmp_path / "data" / "alerts_log.csv")
        log_alert("p1", "STOP_LOSS_TRIGGERED", "CRITICAL", "SPY down 12%", log_path=alerts_path)
        log_alert("p2", "TIME_STOP_TRIGGERED", "CRITICAL", "QQQ held too long", log_path=alerts_path)

        parsed = self._parsed("ACTION: ALERTS_REPORT\nPORTFOLIO: p1")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [parsed])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1", "p2"], ibkr_port=7497, dry_run=True)

        assert len(sent) == 1
        subject, body = sent[0]
        assert "ALERTS_REPORT" in subject
        assert "STOP_LOSS_TRIGGERED" in body
        assert "TIME_STOP_TRIGGERED" not in body  # filtered to p1 only

    def test_alerts_report_all_includes_every_portfolio(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)
        from momentum_trading.core.audit_log import log_alert
        alerts_path = str(tmp_path / "data" / "alerts_log.csv")
        log_alert("p1", "STOP_LOSS_TRIGGERED", "CRITICAL", "SPY down 12%", log_path=alerts_path)
        log_alert("p2", "TIME_STOP_TRIGGERED", "CRITICAL", "QQQ held too long", log_path=alerts_path)

        parsed = self._parsed("ACTION: ALERTS_REPORT\nPORTFOLIO: ALL")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [parsed])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1", "p2"], ibkr_port=7497, dry_run=True)

        assert len(sent) == 1
        _, body = sent[0]
        assert "STOP_LOSS_TRIGGERED" in body
        assert "TIME_STOP_TRIGGERED" in body

    def test_alerts_report_no_alerts_replies_gracefully(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)

        parsed = self._parsed("ACTION: ALERTS_REPORT\nPORTFOLIO: p1")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [parsed])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1"], ibkr_port=7497, dry_run=True)

        assert len(sent) == 1
        _, body = sent[0]
        assert "No alerts recorded" in body

    def test_unknown_portfolio_skipped_without_reply(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)

        parsed = self._parsed("ACTION: ALERTS_REPORT\nPORTFOLIO: nonexistent")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [parsed])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1"], ibkr_port=7497, dry_run=True)

        assert sent == []


class TestSendEmailCommandFeedbackFlag:
    """
    notifications.send_email_command_feedback gates the ACCEPTED/REJECTED/ERROR reply
    EMAIL only (default true, matches this feature's pre-existing always-on behavior).
    poll_and_process_commands() is mocked out here (as in TestAlertsReportEmailCommand
    above), the audit-log-always-writes guarantee is a property of that unmocked
    function itself, see tests/interfaces/test_email_commands.py.
    """

    def _configure_email_env(self, monkeypatch):
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("IMAP_USER", "bot@example.com")
        monkeypatch.setenv("IMAP_PASS", "secret")
        monkeypatch.setenv("TRUSTED_SENDER_EMAIL", "trader@example.com")

    def _parsed_status(self):
        from momentum_trading.interfaces.email_commands import parse_command
        return parse_command("trader@example.com", "trader@example.com",
                              "ACTION: STATUS\nPORTFOLIO: p1")

    def test_default_true_sends_reply_email(self, monkeypatch):
        self._configure_email_env(monkeypatch)
        monkeypatch.setattr(daily_runner, "poll_and_process_commands",
                             lambda *a, **k: [self._parsed_status()])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1"], ibkr_port=7497, dry_run=True)

        assert len(sent) == 1
        assert "STATUS" in sent[0][0]

    def test_flag_false_suppresses_reply_email(self, monkeypatch):
        self._configure_email_env(monkeypatch)
        monkeypatch.setattr(daily_runner, "poll_and_process_commands",
                             lambda *a, **k: [self._parsed_status()])
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(
            ["p1"], ibkr_port=7497, dry_run=True, send_email_command_feedback=False,
        )

        assert sent == []

    def test_flag_false_does_not_block_command_application(self, tmp_path, monkeypatch):
        # PAUSE (not dry-run) still writes the halt flag even with feedback suppressed,
        # the flag only gates the reply EMAIL, never the underlying action.
        self._configure_email_env(monkeypatch)
        from momentum_trading.interfaces.email_commands import parse_command
        parsed = parse_command("trader@example.com", "trader@example.com",
                                "ACTION: PAUSE\nPORTFOLIO: p1")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [parsed])
        monkeypatch.setattr(daily_runner, "send_alert_email", lambda *a, **k: None)
        halt_path = tmp_path / "halt_p1.flag"
        monkeypatch.setattr(daily_runner, "_halt_flag_path", lambda name: halt_path)
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path)

        daily_runner.check_and_apply_email_commands(
            ["p1"], ibkr_port=7497, dry_run=False, send_email_command_feedback=False,
        )

        assert halt_path.exists()


class TestEmailCommandApplyTimeErrorIsolation:
    """
    An exception while APPLYING an already-ACCEPTED command (e.g. a file-write failure)
    must be isolated to just that command: logged as its own ERROR row (not silently
    merged into REJECTED, and not just a log stream line), optionally emailed, and must
    NOT abort every OTHER command queued in the same batch. Before this, any apply-time
    exception propagated out of the whole per-command loop, silently skipping every
    command after the one that failed.
    """

    def _configure_email_env(self, monkeypatch):
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("IMAP_USER", "bot@example.com")
        monkeypatch.setenv("IMAP_PASS", "secret")
        monkeypatch.setenv("TRUSTED_SENDER_EMAIL", "trader@example.com")

    def test_one_command_failing_to_apply_does_not_abort_the_rest(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)
        from momentum_trading.interfaces.email_commands import parse_command
        cmd1 = parse_command("trader@example.com", "trader@example.com", "ACTION: PAUSE\nPORTFOLIO: p1")
        cmd2 = parse_command("trader@example.com", "trader@example.com", "ACTION: PAUSE\nPORTFOLIO: p2")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [cmd1, cmd2])

        def _flag_path(name):
            if name == "p1":
                raise OSError("disk full")
            return tmp_path / f"halt_{name}.flag"
        monkeypatch.setattr(daily_runner, "_halt_flag_path", _flag_path)
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path)

        logged = []
        monkeypatch.setattr(daily_runner, "log_command_attempt",
                             lambda sender, result, **kw: logged.append((sender, result, kw)))
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(["p1", "p2"], ibkr_port=7497, dry_run=False)

        # p2's halt flag WAS written, despite p1 failing first in the same batch.
        assert (tmp_path / "halt_p2.flag").exists()
        # p1's failure was recorded as its own ERROR row, not silently swallowed.
        assert len(logged) == 1
        assert logged[0][2].get("outcome") == "ERROR"
        assert "disk full" in logged[0][2].get("reason", "")
        # And (default flag = true) an error email was sent specifically for it.
        assert any("APPLY ERROR" in subj for subj, _ in sent)

    def test_apply_error_reply_suppressed_when_feedback_flag_false(self, tmp_path, monkeypatch):
        self._configure_email_env(monkeypatch)
        from momentum_trading.interfaces.email_commands import parse_command
        cmd1 = parse_command("trader@example.com", "trader@example.com", "ACTION: PAUSE\nPORTFOLIO: p1")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [cmd1])
        monkeypatch.setattr(daily_runner, "_halt_flag_path",
                             lambda name: (_ for _ in ()).throw(OSError("disk full")))
        monkeypatch.setattr(daily_runner, "LOCK_DIR", tmp_path)
        logged = []
        monkeypatch.setattr(daily_runner, "log_command_attempt",
                             lambda sender, result, **kw: logged.append((sender, result, kw)))
        sent = []
        monkeypatch.setattr(daily_runner, "send_alert_email",
                             lambda subject, body: sent.append((subject, body)))

        daily_runner.check_and_apply_email_commands(
            ["p1"], ibkr_port=7497, dry_run=False, send_email_command_feedback=False,
        )

        # Still logged (the audit trail is unconditional)...
        assert len(logged) == 1
        assert logged[0][2].get("outcome") == "ERROR"
        # ...but no error email was sent.
        assert sent == []


class TestSameInboxWarning:
    """
    TRUSTED_SENDER_EMAIL == IMAP_USER is a fully supported, common setup (see
    docs/EMAIL_COMMANDS.md), but it means ordinary correspondence from that address gets
    treated as a failed command attempt and replied to once (by design, see
    tests/interfaces/test_email_commands.py::TestReplyCascadeGuard for why this can no longer
    cascade). This visibility warning exists so that tradeoff isn't silent, it must fire when
    the addresses match and stay silent otherwise.
    """

    def _configure_email_env(self, monkeypatch, imap_user, trusted_sender):
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("IMAP_USER", imap_user)
        monkeypatch.setenv("IMAP_PASS", "secret")
        monkeypatch.setenv("TRUSTED_SENDER_EMAIL", trusted_sender)

    def test_warns_when_trusted_sender_matches_imap_user(self, monkeypatch, caplog):
        self._configure_email_env(monkeypatch, "bot@example.com", "BOT@example.com")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [])

        with caplog.at_level("WARNING", logger="daily_runner"):
            daily_runner.check_and_apply_email_commands(["p1"], ibkr_port=7497, dry_run=True)

        assert any("same address as IMAP_USER" in r.message for r in caplog.records)

    def test_no_warning_when_addresses_differ(self, monkeypatch, caplog):
        self._configure_email_env(monkeypatch, "bot-inbox@example.com", "trader@example.com")
        monkeypatch.setattr(daily_runner, "poll_and_process_commands", lambda *a, **k: [])

        with caplog.at_level("WARNING", logger="daily_runner"):
            daily_runner.check_and_apply_email_commands(["p1"], ibkr_port=7497, dry_run=True)

        assert not any("same address as IMAP_USER" in r.message for r in caplog.records)


class TestTestEmailFlag:
    """
    --test-email must exit immediately based on run_email_diagnostics()'s result,
    before any config.yaml loading or portfolio logic runs, it's a pure email-setup check,
    usable even with no config.yaml present at all.
    """

    def _run_main_with_args(self, monkeypatch, args, diagnostics_result):
        import momentum_trading.interfaces.email_diagnostics as email_diagnostics
        monkeypatch.setattr(email_diagnostics, "run_email_diagnostics", lambda: diagnostics_result)
        monkeypatch.setattr("sys.argv", ["daily-runner"] + args)

        def _fail_if_called(*a, **k):
            raise AssertionError("load_config() should not be called when --test-email is passed")
        monkeypatch.setattr(daily_runner, "load_config", _fail_if_called)

        with pytest.raises(SystemExit) as exc_info:
            daily_runner.main()
        return exc_info.value.code

    def test_success_exits_zero(self, monkeypatch):
        assert self._run_main_with_args(monkeypatch, ["--test-email"], True) == 0

    def test_failure_exits_one(self, monkeypatch):
        assert self._run_main_with_args(monkeypatch, ["--test-email"], False) == 1


class TestResolveTotalValues:
    """
    total_value: null must mean "account value minus every
    OTHER portfolio's fixed total_value", the OLD behavior (each null portfolio
    independently pulling the FULL account value) silently double/triple-counted the
    same real capital across portfolios sharing one IBKR account. These tests use an
    injected account_value_fn so no real IBKR connection is needed.
    """

    def test_live_remainder_matches_hand_calculation(self):
        # $10,000 account, portfolio2 fixed at $2,500 -> portfolio1 (null) should
        # get exactly $7,500, not the full $10,000.
        portfolios = {
            "portfolio1": {"total_value": None, "tickers": ["SPY"]},
            "portfolio2": {"total_value": 2500.0, "tickers": ["XLF"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 10000.0)
        assert resolved == {"portfolio2": 2500.0, "portfolio1": 7500.0}

    def test_live_remainder_at_or_below_zero_raises(self):
        # Fixed portfolios already consume the whole (or more than the) account,
        # proceeding with zero/negative real capital must never happen silently.
        portfolios = {
            "portfolio1": {"total_value": None, "tickers": ["SPY"]},
            "portfolio2": {"total_value": 10000.0, "tickers": ["XLF"]},
        }
        with pytest.raises(ValueError, match="portfolio1"):
            resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 10000.0)

    def test_no_null_portfolio_leaves_fixed_values_untouched(self):
        portfolios = {
            "p1": {"total_value": 100.0, "tickers": ["SPY"]},
            "p2": {"total_value": 200.0, "tickers": ["QQQ"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 999999.0)
        assert resolved == {"p1": 100.0, "p2": 200.0}

    def test_dry_run_null_portfolio_gets_flat_placeholder_not_a_real_remainder(self):
        # Dry-run tests signal/order-generation LOGIC, not real capital math, the
        # null portfolio must get a simple flat placeholder, NOT reduced by other
        # portfolios' fixed total_value, so dry-run testing a config that's perfectly
        # valid live (e.g. a fixed portfolio bigger than $1000) doesn't spuriously fail.
        portfolios = {
            "portfolio1": {"total_value": None, "tickers": ["SPY"]},
            "portfolio2": {"total_value": 2500.0, "tickers": ["XLF"]},  # > the $1000 placeholder
        }
        resolved = resolve_total_values(portfolios, dry_run=True)
        assert resolved == {"portfolio2": 2500.0, "portfolio1": 1000.0}

    def test_dry_run_never_calls_account_value_fn(self):
        def boom():
            raise AssertionError("account_value_fn must not be called in dry-run")
        portfolios = {"p1": {"total_value": None, "tickers": ["SPY"]}}
        resolved = resolve_total_values(portfolios, dry_run=True, account_value_fn=boom)
        assert resolved == {"p1": 1000.0}

    def test_two_null_portfolios_split_the_remainder_equally(self):
        # $10,000 account, portfolio3 fixed at $2,500 -> remainder $7,500, split equally
        # between the two null portfolios -> $3,750 each, not $7,500 each (which would
        # double-count the same real capital) and not a KeyError for the second one.
        portfolios = {
            "portfolio1": {"total_value": None, "tickers": ["SPY"]},
            "portfolio2": {"total_value": None, "tickers": ["QQQ"]},
            "portfolio3": {"total_value": 2500.0, "tickers": ["XLF"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 10000.0)
        assert resolved == {
            "portfolio3": 2500.0, "portfolio1": 3750.0, "portfolio2": 3750.0,
        }

    def test_three_null_portfolios_no_fixed_split_the_whole_account(self):
        # No fixed portfolios at all, the full account value IS the remainder, split three ways.
        portfolios = {
            "p1": {"total_value": None, "tickers": ["SPY"]},
            "p2": {"total_value": None, "tickers": ["QQQ"]},
            "p3": {"total_value": None, "tickers": ["XLF"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 9000.0)
        assert resolved == {"p1": 3000.0, "p2": 3000.0, "p3": 3000.0}

    def test_resolved_capital_sums_to_exactly_the_account_value(self):
        # Explicit "sum <= real account value" guarantee (in fact ==, when any null
        # portfolio exists, the remainder is fully consumed, never left over or exceeded).
        portfolios = {
            "p1": {"total_value": None, "tickers": ["SPY"]},
            "p2": {"total_value": None, "tickers": ["QQQ"]},
            "p3": {"total_value": 1234.56, "tickers": ["XLF"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 50000.0)
        assert sum(resolved.values()) == pytest.approx(50000.0)

    def test_remainder_at_or_below_zero_with_multiple_nulls_raises_and_names_them(self):
        portfolios = {
            "portfolio1": {"total_value": None, "tickers": ["SPY"]},
            "portfolio2": {"total_value": None, "tickers": ["QQQ"]},
            "portfolio3": {"total_value": 10000.0, "tickers": ["XLF"]},
        }
        with pytest.raises(ValueError, match="portfolio1.*portfolio2|portfolio2.*portfolio1"):
            resolve_total_values(portfolios, dry_run=False, account_value_fn=lambda: 10000.0)

    def test_dry_run_multiple_null_portfolios_each_get_the_full_flat_placeholder(self):
        # Consistent with the existing single-null precedent: dry-run tests signal/order-
        # generation LOGIC, not real capital math, each null portfolio independently gets
        # $1000, NOT divided among them (dry-run deliberately doesn't model portfolios
        # competing for the same real capital).
        portfolios = {
            "p1": {"total_value": None, "tickers": ["SPY"]},
            "p2": {"total_value": None, "tickers": ["QQQ"]},
        }
        resolved = resolve_total_values(portfolios, dry_run=True)
        assert resolved == {"p1": 1000.0, "p2": 1000.0}


class TestCheckTickerOverlap:
    """
    Portfolios sharing a ticker on the same real IBKR account
    would each independently compute and submit orders against the same position,
    this is surfaced as a warning (not blocking, per explicit product decision), so
    it must correctly identify exactly which tickers and portfolios are involved.
    """

    def test_detects_overlap_and_names_portfolios(self):
        portfolios = {
            "p1": {"tickers": ["SPY", "XLF"]},
            "p2": {"tickers": ["XLF", "GLD"]},
        }
        overlap = check_ticker_overlap(portfolios)
        assert overlap == {"XLF": ["p1", "p2"]}

    def test_no_overlap_returns_empty(self):
        portfolios = {"p1": {"tickers": ["SPY"]}, "p2": {"tickers": ["QQQ"]}}
        assert check_ticker_overlap(portfolios) == {}

    def test_three_way_overlap_names_all_portfolios(self):
        portfolios = {
            "p1": {"tickers": ["GLD"]},
            "p2": {"tickers": ["GLD"]},
            "p3": {"tickers": ["GLD"]},
        }
        overlap = check_ticker_overlap(portfolios)
        assert overlap == {"GLD": ["p1", "p2", "p3"]}

    def test_matches_real_config_yaml_overlap(self):
        # Regression guard for the exact scenario found in the real config.yaml:
        # portfolio1 and portfolio2 share XLF/XLE/GLD/TLT.
        portfolios = {
            "portfolio1": {"tickers": ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLY", "XLP", "XLU", "GLD", "TLT", "BIL"]},
            "portfolio2": {"tickers": ["XLF", "XLE", "GLD", "TLT"]},
        }
        overlap = check_ticker_overlap(portfolios)
        assert set(overlap.keys()) == {"XLF", "XLE", "GLD", "TLT"}
        for names in overlap.values():
            assert set(names) == {"portfolio1", "portfolio2"}
