"""
tests/core/test_functions.py

Covers core/functions.py's get_bulk_prices() per-ticker vendor-fallback fix (Epic 6,
"Stale-Price Reporting + Live Price-Vendor Priority" plan). No prior pytest coverage existed
for this module at all (only exercised via notebooks before this), scoped to the fix only, not
a retroactive audit of the rest of the module.
"""
import pandas as pd
import pytest

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
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key")

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
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key")

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
                                 source="FMP", fmp_api_key="fmp-key", eodhd_api_key="eod-key")

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
                                 fmp_api_key="fmp-key", eodhd_api_key="eod-key")

        assert set(df.columns) == {"A"}  # B genuinely unavailable everywhere, correctly dropped
