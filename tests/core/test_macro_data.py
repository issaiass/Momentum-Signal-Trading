"""
tests/core/test_macro_data.py

Covers core/macro_data.py -- FRED-sourced Fed Funds Rate / CPI, gated on FRED_API_KEY being
set, plus its file cache. Network calls are never made in this suite -- _fetch_fred_series() is
monkeypatched at the module level rather than mocking urlopen, matching the approach in
tests/core/test_fundamentals.py.

Run with: pytest tests/core/test_macro_data.py -v
"""
import json
import time

from momentum_trading.core import macro_data


FAKE_FFR = {"value": 3.63, "date": "2026-06-01"}
FAKE_CPI = {"value": 332.568, "date": "2026-06-01"}


class TestFetchMacroIndicators:
    def test_returns_empty_dict_when_no_api_key(self):
        assert macro_data.fetch_macro_indicators(None) == {}

    def test_returns_empty_dict_when_api_key_is_empty_string(self):
        assert macro_data.fetch_macro_indicators("") == {}

    def test_does_not_attempt_network_call_when_key_unset(self, monkeypatch):
        monkeypatch.setattr(
            macro_data, "_fetch_fred_series",
            lambda series_id, key: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        assert macro_data.fetch_macro_indicators(None) == {}

    def test_both_series_fetched_and_returned(self, monkeypatch):
        def fake_fetch(series_id, key):
            return FAKE_FFR if series_id == "FEDFUNDS" else FAKE_CPI

        monkeypatch.setattr(macro_data, "_fetch_fred_series", fake_fetch)
        result = macro_data.fetch_macro_indicators("fred-key")
        assert result == {"fed_funds_rate": FAKE_FFR, "cpi": FAKE_CPI}

    def test_one_series_failing_does_not_block_the_other(self, monkeypatch):
        def fake_fetch(series_id, key):
            if series_id == "FEDFUNDS":
                raise ValueError("FRED outage")
            return FAKE_CPI

        monkeypatch.setattr(macro_data, "_fetch_fred_series", fake_fetch)
        result = macro_data.fetch_macro_indicators("fred-key")
        assert result == {"cpi": FAKE_CPI}

    def test_both_series_failing_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(
            macro_data, "_fetch_fred_series",
            lambda series_id, key: (_ for _ in ()).throw(ValueError("FRED outage")),
        )
        assert macro_data.fetch_macro_indicators("fred-key") == {}

    def test_missing_value_marker_is_treated_as_no_observation(self, monkeypatch):
        # FRED uses "." for not-yet-released observations -- _fetch_fred_series() should
        # return None for that series rather than raising or fabricating a value.
        def fake_fetch(series_id, key):
            return None if series_id == "FEDFUNDS" else FAKE_CPI

        monkeypatch.setattr(macro_data, "_fetch_fred_series", fake_fetch)
        result = macro_data.fetch_macro_indicators("fred-key")
        assert result == {"cpi": FAKE_CPI}


class TestCache:
    def test_cache_miss_fetches_and_writes_cache_file(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "macro_cache.json")
        data = {"fed_funds_rate": FAKE_FFR, "cpi": FAKE_CPI}
        monkeypatch.setattr(macro_data, "fetch_macro_indicators", lambda key: data)

        result = macro_data.get_cached_or_fetch_macro_indicators("fred-key", cache_path=cache_path)
        assert result == data
        with open(cache_path) as f:
            cache = json.load(f)
        assert cache["data"] == data

    def test_cache_hit_within_max_age_skips_refetch(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "macro_cache.json")
        data = {"fed_funds_rate": FAKE_FFR, "cpi": FAKE_CPI}
        with open(cache_path, "w") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)

        monkeypatch.setattr(
            macro_data, "fetch_macro_indicators",
            lambda key: (_ for _ in ()).throw(AssertionError("should not refetch on cache hit")),
        )
        result = macro_data.get_cached_or_fetch_macro_indicators("fred-key", cache_path=cache_path)
        assert result == data

    def test_cache_expired_beyond_max_age_refetches(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "macro_cache.json")
        stale_timestamp = time.time() - (31 * 86400)  # default max_age_days=30
        old_data = {"fed_funds_rate": FAKE_FFR}
        with open(cache_path, "w") as f:
            json.dump({"fetched_at": stale_timestamp, "data": old_data}, f)

        new_data = {"fed_funds_rate": FAKE_FFR, "cpi": FAKE_CPI}
        monkeypatch.setattr(macro_data, "fetch_macro_indicators", lambda key: new_data)
        result = macro_data.get_cached_or_fetch_macro_indicators("fred-key", cache_path=cache_path)
        assert result == new_data

    def test_failed_fetch_does_not_poison_cache(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "macro_cache.json")
        monkeypatch.setattr(macro_data, "fetch_macro_indicators", lambda key: {})

        result = macro_data.get_cached_or_fetch_macro_indicators("fred-key", cache_path=cache_path)
        assert result == {}
        import os
        assert not os.path.isfile(cache_path)

    def test_corrupt_cache_file_is_treated_as_empty_not_a_crash(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "macro_cache.json")
        with open(cache_path, "w") as f:
            f.write("not valid json{{{")

        data = {"fed_funds_rate": FAKE_FFR, "cpi": FAKE_CPI}
        monkeypatch.setattr(macro_data, "fetch_macro_indicators", lambda key: data)
        result = macro_data.get_cached_or_fetch_macro_indicators("fred-key", cache_path=cache_path)
        assert result == data
