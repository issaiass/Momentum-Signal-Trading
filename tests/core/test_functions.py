"""
tests/core/test_functions.py

Covers core/functions.py's get_bulk_prices() per-ticker vendor-fallback fix (Epic 6,
"Stale-Price Reporting + Live Price-Vendor Priority" plan), and fetch_with_retry()'s
retry-with-backoff + rate-limit pacing (Epic 10, "API Resilience for Price-Vendor Fetches"
plan). No prior pytest coverage existed for this module at all before Epic 6 (only exercised via
notebooks), scoped to these specific fixes, not a retroactive audit of the rest of the module.
"""
import json

import pandas as pd
import pytest
from urllib.error import HTTPError, URLError

import momentum_trading.core.functions as fn


def _make_df(price_col, value=100.0):
    dates = pd.bdate_range("2024-01-01", periods=5)
    return pd.DataFrame({price_col: [value] * len(dates)}, index=dates)


class TestGetBulkPricesVendorFallback:
    """
    Real, confirmed bug found via Epic 6's real production verification, not synthetic: the
    vendor is auto-detected from ONE ticker only, but a real API key can fail for a SUBSET of
    tickers under that same vendor (confirmed directly against a real .env FMP key: it returned
    `402 Payment Required` for some symbols, e.g. ADI/ARM/ASML, while succeeding for others,
    e.g. AAPL/AMD, in the exact same batch). The old code passed a FIXED source per ticker with
    no per-ticker fallback, silently dropping any ticker whose fetch failed under the
    batch-detected vendor, which could make the returned DataFrame missing configured tickers
    entirely, crashing several frames away downstream (resolve_strategy_scores()'s
    daily_prices[list(tickers)]) with a KeyError that doesn't obviously point back to a vendor
    402 at a glance.
    """

    def test_ticker_failing_under_detected_vendor_falls_back_not_dropped(self, monkeypatch):
        def fake_get_stock_prices(symbol, start_date, end_date, fmp_api_key=None,
                                   eodhd_api_key=None, source=None):
            if symbol == "A":
                if source in ("FMP", None):
                    return _make_df("adjClose")
                raise ValueError(f"{source} failed for A")
            if symbol == "B":
                if source == "FMP":
                    raise ValueError("FMP failed for B: HTTP Error 402: Payment Required")
                if source in ("EOD", None):
                    return _make_df("adjusted_close")
                raise ValueError(f"{source} failed for B")
            raise ValueError(f"unexpected symbol {symbol}")

        monkeypatch.setattr(fn, "get_stock_prices", fake_get_stock_prices)

        df = fn.get_bulk_prices(["A", "B"], "2024-01-01", "2024-01-10",
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key",
                                 request_pacing_seconds=0)

        assert set(df.columns) == {"A", "B"}  # B must not be silently dropped
        assert not df["B"].isna().all()

    def test_all_tickers_succeed_under_detected_vendor_is_unaffected(self, monkeypatch):
        # Baseline: when nothing fails, behavior is unchanged, no fallback ever triggers.
        calls = []

        def fake_get_stock_prices(symbol, start_date, end_date, fmp_api_key=None,
                                   eodhd_api_key=None, source=None):
            calls.append((symbol, source))
            return _make_df("adjClose")

        monkeypatch.setattr(fn, "get_stock_prices", fake_get_stock_prices)

        df = fn.get_bulk_prices(["A", "B"], "2024-01-01", "2024-01-10",
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key",
                                 request_pacing_seconds=0)

        assert set(df.columns) == {"A", "B"}
        # exactly one attempt per ticker (plus the one-off first-ticker vendor-detection probe)
        assert calls.count(("B", "FMP")) == 1

    def test_explicit_source_caller_gets_no_fallback_cascade(self, monkeypatch):
        # A caller that explicitly pins source='FMP' on get_bulk_prices() itself (not
        # auto-detected) must keep the strict single-vendor contract: no cascade, a failing
        # ticker is still dropped, same as before this fix.
        def fake_get_stock_prices(symbol, start_date, end_date, fmp_api_key=None,
                                   eodhd_api_key=None, source=None):
            if symbol == "A":
                return _make_df("adjClose")
            if symbol == "B":
                raise ValueError("FMP failed for B: HTTP Error 402: Payment Required")
            raise ValueError(f"unexpected symbol {symbol}")

        monkeypatch.setattr(fn, "get_stock_prices", fake_get_stock_prices)

        df = fn.get_bulk_prices(["A", "B"], "2024-01-01", "2024-01-10",
                                 source="FMP", fmp_api_key="fmp-key", eodhd_api_key="eod-key",
                                 request_pacing_seconds=0)

        assert set(df.columns) == {"A"}  # B dropped, no cascade for an explicit source

    def test_all_vendors_failing_for_one_ticker_drops_only_that_ticker(self, monkeypatch):
        def fake_get_stock_prices(symbol, start_date, end_date, fmp_api_key=None,
                                   eodhd_api_key=None, source=None):
            if symbol == "A":
                return _make_df("adjClose")
            if symbol == "B":
                raise ValueError(f"{source} failed for B")
            raise ValueError(f"unexpected symbol {symbol}")

        monkeypatch.setattr(fn, "get_stock_prices", fake_get_stock_prices)

        df = fn.get_bulk_prices(["A", "B"], "2024-01-01", "2024-01-10",
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key",
                                 request_pacing_seconds=0)

        assert set(df.columns) == {"A"}  # B genuinely unavailable everywhere, correctly dropped


class TestFetchWithRetry:
    """
    fetch_with_retry() (Epic 10, "API Resilience for Price-Vendor Fetches" plan): retries only
    genuinely transient failures (HTTPError 429/5xx, URLError, OSError), not a bad API key/no
    access (any other 4xx) or a genuine "no data" ValueError, since retrying those wastes time
    on a failure a retry can't fix. backoff_seconds is passed tiny in every test below so the
    suite stays fast, no need to monkeypatch time.sleep here (unlike the vendor-closure tests
    below, which can't override fetch_with_retry()'s own hardcoded call args).
    """

    def test_retries_http_429_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise HTTPError("http://x", 429, "Too Many Requests", None, None)
            return "ok"

        assert fn.fetch_with_retry(flaky, max_attempts=3, backoff_seconds=0.001) == "ok"
        assert len(calls) == 3

    def test_retries_http_503_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise HTTPError("http://x", 503, "Service Unavailable", None, None)
            return "ok"

        assert fn.fetch_with_retry(flaky, max_attempts=3, backoff_seconds=0.001) == "ok"
        assert len(calls) == 2

    def test_retries_urlerror_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise URLError("connection reset")
            return "ok"

        assert fn.fetch_with_retry(flaky, max_attempts=3, backoff_seconds=0.001) == "ok"
        assert len(calls) == 2

    def test_retries_oserror_then_succeeds(self):
        # Covers yfinance/requests-style network failures, which don't raise urllib exceptions
        # at all (ConnectionError/TimeoutError are OSError subclasses in Python 3).
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionError("connection refused")
            return "ok"

        assert fn.fetch_with_retry(flaky, max_attempts=3, backoff_seconds=0.001) == "ok"
        assert len(calls) == 2

    def test_does_not_retry_http_403(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise HTTPError("http://x", 403, "Forbidden", None, None)

        with pytest.raises(HTTPError):
            fn.fetch_with_retry(always_fails, max_attempts=3, backoff_seconds=0.001)
        assert len(calls) == 1

    def test_does_not_retry_http_402(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise HTTPError("http://x", 402, "Payment Required", None, None)

        with pytest.raises(HTTPError):
            fn.fetch_with_retry(always_fails, max_attempts=3, backoff_seconds=0.001)
        assert len(calls) == 1

    def test_does_not_retry_plain_value_error(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise ValueError("No data returned")

        with pytest.raises(ValueError):
            fn.fetch_with_retry(always_fails, max_attempts=3, backoff_seconds=0.001)
        assert len(calls) == 1

    def test_reraises_last_exception_after_exhausting_attempts(self):
        calls = []

        def always_fails():
            calls.append(1)
            raise URLError("still down")

        with pytest.raises(URLError):
            fn.fetch_with_retry(always_fails, max_attempts=3, backoff_seconds=0.001)
        assert len(calls) == 3


class TestVendorClosuresUseRetry:
    """
    Confirms each of get_stock_prices()'s three vendor closures actually routes its network
    call through fetch_with_retry() (Epic 10), not just that the helper exists unused elsewhere.
    Monkeypatches time.sleep (not backoff_seconds, which these closures don't expose to a
    caller) to keep the suite fast despite fetch_with_retry()'s hardcoded default backoff.
    """

    def test_fmp_closure_retries_transient_http_error(self, monkeypatch):
        monkeypatch.setattr(fn.time, "sleep", lambda s: None)
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps([{"date": "2024-01-02", "close": 100.0, "adjClose": 100.0}]).encode()

        def fake_urlopen(url, context=None):
            calls.append(url)
            if len(calls) < 3:
                raise HTTPError(url, 503, "Service Unavailable", None, None)
            return FakeResponse()

        monkeypatch.setattr(fn, "urlopen", fake_urlopen)
        df = fn.get_stock_prices("AAPL", "2024-01-01", "2024-01-10",
                                  fmp_api_key="key", source="FMP")
        assert not df.empty
        assert len(calls) >= 3  # at least the 3 primary-OHLCV attempts before success

    def test_eodhd_closure_retries_transient_http_error(self, monkeypatch):
        monkeypatch.setattr(fn.time, "sleep", lambda s: None)
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps([{"date": "2024-01-02", "close": 100.0}]).encode()

        def fake_urlopen(url, context=None):
            calls.append(url)
            if len(calls) < 2:
                raise HTTPError(url, 429, "Too Many Requests", None, None)
            return FakeResponse()

        monkeypatch.setattr(fn, "urlopen", fake_urlopen)
        df = fn.get_stock_prices("AAPL", "2024-01-01", "2024-01-10",
                                  eodhd_api_key="key", source="EOD")
        assert not df.empty
        assert len(calls) == 2

    def test_yf_closure_retries_transient_connection_error(self, monkeypatch):
        monkeypatch.setattr(fn.time, "sleep", lambda s: None)
        calls = []
        real_df = pd.DataFrame({"Close": [100.0]}, index=pd.bdate_range("2024-01-02", periods=1))

        def fake_download(*args, **kwargs):
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionError("connection refused")
            return real_df

        monkeypatch.setattr(fn.yf, "download", fake_download)
        df = fn.get_stock_prices("AAPL", "2024-01-01", "2024-01-10", source="yf")
        assert not df.empty
        assert len(calls) == 2


class TestGetBulkPricesPacing:
    """
    request_pacing_seconds (Epic 10): a small delay between successive per-ticker fetches in
    get_bulk_prices()'s loop, to avoid tripping a vendor's rate limit on a larger portfolio (a
    real portfolio in this project's own config.yaml has 58 tickers). Not applied before the
    first request or during the earlier vendor-auto-detection probe.
    """

    def _fake_get_stock_prices(self, symbol, start_date, end_date, fmp_api_key=None,
                                eodhd_api_key=None, source=None):
        return _make_df("adjClose")

    def test_sleeps_between_tickers_by_default(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr(fn.time, "sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(fn, "get_stock_prices", self._fake_get_stock_prices)

        fn.get_bulk_prices(["A", "B", "C"], "2024-01-01", "2024-01-10", fmp_api_key="key")

        # 3 tickers -> 2 inter-ticker sleeps (none before the first ticker in the loop).
        assert sleep_calls == [0.25, 0.25]

    def test_zero_pacing_is_a_no_op(self, monkeypatch):
        sleep_calls = []
        monkeypatch.setattr(fn.time, "sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(fn, "get_stock_prices", self._fake_get_stock_prices)

        fn.get_bulk_prices(["A", "B", "C"], "2024-01-01", "2024-01-10", fmp_api_key="key",
                            request_pacing_seconds=0)

        assert sleep_calls == []
