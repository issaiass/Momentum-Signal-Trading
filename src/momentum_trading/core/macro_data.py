"""
core/macro_data.py

Macro indicators (Fed Funds Rate, CPI) for the email reports' "Macro Context" section --
portfolio-independent (one fetch covers every portfolio in a run, not per-ticker like
core/technical_indicators.py / core/fundamentals.py).

Confirmed via live testing against a real API key during development: FRED (the St. Louis
Fed's own API, https://fred.stlouisfed.org) is the source -- neither FMP nor EODHD's
already-used endpoints in this project cover macro series at all. Requires a NEW, free
FRED_API_KEY (signup at https://fred.stlouisfed.org/docs/api/api_key.html, no cost) -- if unset,
the whole macro section is simply omitted, the same "opt-in by not configuring it" pattern
already used for email-commanded remote actions (inactive unless all four IMAP env vars are set).

series_id=FEDFUNDS -> Fed Funds Rate (monthly). series_id=CPIAUCSL -> CPI, Consumer Price Index
for All Urban Consumers (monthly). Both confirmed returning real data via
https://api.stlouisfed.org/fred/series/observations?series_id=...&api_key=...&file_type=json
during development.
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

MACRO_CACHE_PATH = str(data_dir() / "macro_cache.json")

_FRED_SERIES = {"fed_funds_rate": "FEDFUNDS", "cpi": "CPIAUCSL"}


def _fetch_fred_series(series_id: str, fred_api_key: str) -> dict | None:
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={fred_api_key}&file_type=json"
        f"&sort_order=desc&limit=1"
    )
    data = json.loads(urlopen(url, context=ssl_context, timeout=15).read())
    observations = data.get("observations", [])
    if not observations:
        return None
    latest = observations[0]
    try:
        value = float(latest["value"])
    except (KeyError, ValueError):
        return None  # FRED uses "." for missing/not-yet-released values
    return {"value": value, "date": latest["date"]}


def fetch_macro_indicators(fred_api_key: str | None) -> dict:
    """
    {"fed_funds_rate": {"value": ..., "date": ...}, "cpi": {"value": ..., "date": ...}} --
    latest observation of each. Returns {} (never raises) if fred_api_key is falsy -- checked
    BEFORE any network attempt, so an unconfigured key costs nothing every run -- or if both
    FRED calls fail. Each series is fetched independently: one failing doesn't block the other
    (e.g. a transient FRED outage on one series shouldn't hide data that's actually available).
    """
    if not fred_api_key:
        return {}

    result = {}
    for key, series_id in _FRED_SERIES.items():
        try:
            observation = _fetch_fred_series(series_id, fred_api_key)
            if observation is not None:
                result[key] = observation
        except Exception:
            pass
    return result


def get_cached_or_fetch_macro_indicators(
    fred_api_key: str | None, max_age_days: int = 30, cache_path: str = MACRO_CACHE_PATH,
) -> dict:
    """
    File-cached wrapper around fetch_macro_indicators() -- Fed Funds Rate and CPI are both
    released roughly monthly, so refetching on every report run (especially the opt-in DAILY
    report) would waste calls for data that hasn't changed. A failed/empty fetch does NOT get
    cached, so a transient FRED outage or a not-yet-configured FRED_API_KEY doesn't block
    retrying (and picking up real data) on a later run once you've added a key.
    """
    cache: dict = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            cache = {}

    if cache and (time.time() - cache.get("fetched_at", 0)) < max_age_days * 86400:
        return cache["data"]

    data = fetch_macro_indicators(fred_api_key)
    if data:
        cache = {"fetched_at": time.time(), "data": data}
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    return data
