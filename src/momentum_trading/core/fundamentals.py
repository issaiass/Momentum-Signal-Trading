"""
core/fundamentals.py

Fundamental indicators (P/E Ratio, PEG Ratio, ROE, Debt-to-Equity, Current Ratio) for the email
reports' "Fundamental Indicators" section, per currently-held ticker (same scope decision as
core/technical_indicators.py, held positions, not the whole configured universe).

Confirmed via live testing against real API keys during development, not guessed at:
  - FMP's LEGACY `/api/v3/` endpoints are dead, shut down 2025-08-31, every `/api/v3/` call
    now returns 403 "Legacy Endpoint" regardless of subscription (this also affected the
    EXISTING price-fetching in this project, core/functions.py's `_fetch_fmp()`, since
    migrated to `/stable/` as well). FMP's newer `/stable/` API works with the same key:
    `/stable/ratios` returns
    `priceToEarningsRatio`/`priceToEarningsGrowthRatio`/`currentRatio`/`debtToEquityRatio`
    directly; `returnOnEquity` (ROE) is NOT in that response and needs a second call to
    `/stable/key-metrics`.
  - EODHD's `/api/fundamentals/` endpoint returned "Only EOD data allowed for free users"
    against a free-tier key, fundamentals need a paid EODHD plan. Implemented below per
    EODHD's documented response shape (for when/if a paid plan is available), but NOT confirmed
    working the way the FMP path above was, treat the EODHD fallback as unverified until
    tested against a real paid-tier key.

Both paths return {} (never raise) on any failure, a ticker with no fundamentals access from
either vendor simply doesn't appear in the report section, the same graceful-degradation
contract core/technical_indicators.py's compute_latest_indicators() already uses.
"""

from __future__ import annotations

import json
import os
import ssl
import time
from urllib.request import urlopen

from .paths import data_dir

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

FUNDAMENTALS_CACHE_PATH = str(data_dir() / "fundamentals_cache.json")


def _fetch_fmp_fundamentals(ticker: str, fmp_api_key: str) -> dict:
    ratios_url = (
        f"https://financialmodelingprep.com/stable/ratios?symbol={ticker}&limit=1&apikey={fmp_api_key}"
    )
    ratios_resp = json.loads(urlopen(ratios_url, context=ssl_context, timeout=15).read())
    if not ratios_resp:
        raise ValueError(f"No ratios data returned from FMP for {ticker}")
    ratios = ratios_resp[0]

    result = {
        "pe_ratio": ratios.get("priceToEarningsRatio"),
        "peg_ratio": ratios.get("priceToEarningsGrowthRatio"),
        "current_ratio": ratios.get("currentRatio"),
        "debt_to_equity": ratios.get("debtToEquityRatio"),
        "roe": None,
    }

    # ROE isn't in /stable/ratios, a second, best-effort call. Non-fatal if it fails: the
    # other four fields still stand, matching the per-field graceful-degradation philosophy
    # used throughout this module (a partial result is better than none).
    try:
        metrics_url = (
            f"https://financialmodelingprep.com/stable/key-metrics?symbol={ticker}&limit=1&apikey={fmp_api_key}"
        )
        metrics_resp = json.loads(urlopen(metrics_url, context=ssl_context, timeout=15).read())
        if metrics_resp:
            result["roe"] = metrics_resp[0].get("returnOnEquity")
    except Exception:
        pass

    return result


def _fetch_eodhd_fundamentals(ticker: str, eodhd_api_key: str) -> dict:
    """See module docstring, NOT confirmed working (free-tier EODHD keys are blocked from
    fundamentals entirely). Balance-sheet field names below follow EODHD's public glossary but
    haven't been checked against a real response the way the FMP path above was."""
    exchange_ticker = ticker if "." in ticker else f"{ticker}.US"
    url = f"https://eodhistoricaldata.com/api/fundamentals/{exchange_ticker}?api_token={eodhd_api_key}&fmt=json"
    data = json.loads(urlopen(url, context=ssl_context, timeout=20).read())

    highlights = data.get("Highlights", {}) or {}
    yearly_balance_sheet = (data.get("Financials", {}) or {}).get("Balance_Sheet", {}).get("yearly", {}) or {}
    latest_bs = {}
    if yearly_balance_sheet:
        latest_date = max(yearly_balance_sheet.keys())
        latest_bs = yearly_balance_sheet.get(latest_date) or {}

    def _safe_ratio(numerator_key: str, denominator_key: str) -> float | None:
        try:
            numerator = float(latest_bs.get(numerator_key))
            denominator = float(latest_bs.get(denominator_key))
            return numerator / denominator if denominator else None
        except (TypeError, ValueError):
            return None

    return {
        "pe_ratio": highlights.get("PERatio"),
        "peg_ratio": highlights.get("PEGRatio"),
        "roe": highlights.get("ReturnOnEquityTTM"),
        "current_ratio": _safe_ratio("totalCurrentAssets", "totalCurrentLiabilities"),
        "debt_to_equity": _safe_ratio("shortLongTermDebtTotal", "totalStockholderEquity"),
    }


def fetch_fundamentals(
    ticker: str, fmp_api_key: str | None = None, eodhd_api_key: str | None = None,
) -> dict:
    """
    P/E Ratio, PEG Ratio, ROE, Debt-to-Equity, Current Ratio for one ticker, tries FMP first
    (matching core/functions.py's existing price-fetch vendor priority order), falls back to
    EODHD. Returns {} (never raises) if both vendors fail or no API key is configured at all,
    same contract as compute_latest_indicators() on insufficient data, so a ticker with no
    fundamentals access simply doesn't appear in the report section rather than breaking it.
    """
    if fmp_api_key:
        try:
            return _fetch_fmp_fundamentals(ticker, fmp_api_key)
        except Exception:
            pass
    if eodhd_api_key:
        try:
            return _fetch_eodhd_fundamentals(ticker, eodhd_api_key)
        except Exception:
            pass
    return {}


def get_cached_or_fetch_fundamentals(
    ticker: str, fmp_api_key: str | None = None, eodhd_api_key: str | None = None,
    max_age_days: int = 7, cache_path: str = FUNDAMENTALS_CACHE_PATH,
) -> dict:
    """
    File-cached wrapper around fetch_fundamentals(), fundamentals change quarterly at most, so
    refetching on every report run (especially the opt-in DAILY report, which could otherwise
    call this every single day) would waste API calls against likely-limited vendor quotas for
    no benefit. A failed fetch does NOT get written to the cache, only successful results
    persist, so a transient vendor outage doesn't block retrying on the very next run.
    """
    cache: dict = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            cache = {}

    entry = cache.get(ticker)
    if entry and (time.time() - entry.get("fetched_at", 0)) < max_age_days * 86400:
        return entry["data"]

    data = fetch_fundamentals(ticker, fmp_api_key, eodhd_api_key)
    if data:
        cache[ticker] = {"fetched_at": time.time(), "data": data}
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    return data
