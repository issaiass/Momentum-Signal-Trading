"""
tests/core/test_fundamentals.py

Covers core/fundamentals.py, FMP-first, EODHD-fallback fetch of P/E, PEG, ROE,
Debt-to-Equity, Current Ratio for held tickers, plus its file cache. Network calls are never
made in this suite, _fetch_fmp_fundamentals()/_fetch_eodhd_fundamentals() are monkeypatched
at the module level rather than mocking urlopen, since the module's own docstring already
confirms (via live testing during development) exactly what each vendor's real response shape
looks like; these tests exercise the fallback/caching logic around that, not the HTTP layer.

Run with: pytest tests/core/test_fundamentals.py -v
"""
import json
import time

import pytest

from momentum_trading.core import fundamentals


FAKE_FMP_RESULT = {
    "pe_ratio": 34.1, "peg_ratio": 1.51, "current_ratio": 0.89,
    "debt_to_equity": 1.52, "roe": 1.52,
}
FAKE_EODHD_RESULT = {
    "pe_ratio": 30.0, "peg_ratio": 1.2, "roe": 1.1,
    "current_ratio": 0.9, "debt_to_equity": 1.4,
}


class TestFetchFundamentalsVendorFallback:
    def test_uses_fmp_when_fmp_key_provided_and_succeeds(self, monkeypatch):
        monkeypatch.setattr(fundamentals, "_fetch_fmp_fundamentals", lambda t, k: FAKE_FMP_RESULT)
        monkeypatch.setattr(
            fundamentals, "_fetch_eodhd_fundamentals",
            lambda t, k: (_ for _ in ()).throw(AssertionError("EODHD should not be called")),
        )
        result = fundamentals.fetch_fundamentals("AAPL", fmp_api_key="fmp-key", eodhd_api_key="eodhd-key")
        assert result == FAKE_FMP_RESULT

    def test_falls_back_to_eodhd_when_fmp_fails(self, monkeypatch):
        monkeypatch.setattr(
            fundamentals, "_fetch_fmp_fundamentals",
            lambda t, k: (_ for _ in ()).throw(ValueError("FMP down")),
        )
        monkeypatch.setattr(fundamentals, "_fetch_eodhd_fundamentals", lambda t, k: FAKE_EODHD_RESULT)
        result = fundamentals.fetch_fundamentals("AAPL", fmp_api_key="fmp-key", eodhd_api_key="eodhd-key")
        assert result == FAKE_EODHD_RESULT

    def test_returns_empty_dict_when_both_vendors_fail(self, monkeypatch):
        monkeypatch.setattr(
            fundamentals, "_fetch_fmp_fundamentals",
            lambda t, k: (_ for _ in ()).throw(ValueError("FMP down")),
        )
        monkeypatch.setattr(
            fundamentals, "_fetch_eodhd_fundamentals",
            lambda t, k: (_ for _ in ()).throw(ValueError("EODHD down")),
        )
        result = fundamentals.fetch_fundamentals("AAPL", fmp_api_key="fmp-key", eodhd_api_key="eodhd-key")
        assert result == {}

    def test_returns_empty_dict_when_no_api_keys_configured(self):
        assert fundamentals.fetch_fundamentals("AAPL", fmp_api_key=None, eodhd_api_key=None) == {}

    def test_skips_fmp_entirely_when_only_eodhd_key_provided(self, monkeypatch):
        monkeypatch.setattr(
            fundamentals, "_fetch_fmp_fundamentals",
            lambda t, k: (_ for _ in ()).throw(AssertionError("FMP should not be called")),
        )
        monkeypatch.setattr(fundamentals, "_fetch_eodhd_fundamentals", lambda t, k: FAKE_EODHD_RESULT)
        result = fundamentals.fetch_fundamentals("AAPL", fmp_api_key=None, eodhd_api_key="eodhd-key")
        assert result == FAKE_EODHD_RESULT


class TestCache:
    def test_cache_miss_fetches_and_writes_cache_file(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        monkeypatch.setattr(fundamentals, "fetch_fundamentals", lambda t, f, e: FAKE_FMP_RESULT)

        result = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert result == FAKE_FMP_RESULT
        with open(cache_path) as f:
            cache = json.load(f)
        assert cache["AAPL"]["data"] == FAKE_FMP_RESULT

    def test_cache_hit_within_max_age_skips_refetch(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"AAPL": {"fetched_at": time.time(), "data": FAKE_FMP_RESULT}}, f)

        monkeypatch.setattr(
            fundamentals, "fetch_fundamentals",
            lambda t, f, e: (_ for _ in ()).throw(AssertionError("should not refetch on cache hit")),
        )
        result = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert result == FAKE_FMP_RESULT

    def test_cache_expired_beyond_max_age_refetches(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        stale_timestamp = time.time() - (8 * 86400)  # 8 days ago, default max_age_days=7
        with open(cache_path, "w") as f:
            json.dump({"AAPL": {"fetched_at": stale_timestamp, "data": FAKE_FMP_RESULT}}, f)

        monkeypatch.setattr(fundamentals, "fetch_fundamentals", lambda t, f, e: FAKE_EODHD_RESULT)
        result = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert result == FAKE_EODHD_RESULT

    def test_failed_fetch_does_not_poison_cache(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        monkeypatch.setattr(fundamentals, "fetch_fundamentals", lambda t, f, e: {})

        result = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert result == {}
        import os
        assert not os.path.isfile(cache_path)

    def test_corrupt_cache_file_is_treated_as_empty_not_a_crash(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        with open(cache_path, "w") as f:
            f.write("not valid json{{{")

        monkeypatch.setattr(fundamentals, "fetch_fundamentals", lambda t, f, e: FAKE_FMP_RESULT)
        result = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert result == FAKE_FMP_RESULT

    def test_different_tickers_cached_independently(self, tmp_path, monkeypatch):
        cache_path = str(tmp_path / "fundamentals_cache.json")
        calls = {"AAPL": FAKE_FMP_RESULT, "MSFT": FAKE_EODHD_RESULT}
        monkeypatch.setattr(fundamentals, "fetch_fundamentals", lambda t, f, e: calls[t])

        aapl = fundamentals.get_cached_or_fetch_fundamentals(
            "AAPL", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        msft = fundamentals.get_cached_or_fetch_fundamentals(
            "MSFT", fmp_api_key="k", eodhd_api_key=None, cache_path=cache_path,
        )
        assert aapl == FAKE_FMP_RESULT
        assert msft == FAKE_EODHD_RESULT
