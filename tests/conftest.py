"""
tests/conftest.py

Shared fixtures for the test suite. Synthetic price panels are clearly synthetic
(seeded, documented), these are for testing CODE MECHANICS (order generation,
config validation, P&L math), not for validating the strategy's real performance.

NOTE: no sys.path manipulation needed here anymore, the package is
installed editable (`pip install -e .`), so `import momentum_trading...` works
from any working directory, same as a real installed dependency would.
"""
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synthetic_daily_prices():
    """Deterministic (seeded) synthetic price panel for 5 tickers, ~2.5 years."""
    np.random.seed(0)
    tickers = ["SPY", "QQQ", "XLK", "XLF", "XLE"]
    dates = pd.bdate_range("2018-01-01", "2020-06-30")
    data = {t: np.cumprod(1 + np.random.normal(0.0004, 0.012, len(dates))) * 100 for t in tickers}
    return pd.DataFrame(data, index=dates)


@pytest.fixture
def synthetic_daily_highs(synthetic_daily_prices):
    """
    Epic 2, "Institutional Risk-Management Features" plan (ATR-Based Trailing Stop): a
    deterministic high panel matching synthetic_daily_prices' own shape/seed exactly (same
    tickers/date index), high = close + a small positive offset, sufficient for atr() to
    compute a real, non-degenerate true-range series without needing a separate synthetic
    price-generation process.
    """
    return synthetic_daily_prices + 0.5


@pytest.fixture
def synthetic_daily_lows(synthetic_daily_prices):
    """Epic 2 counterpart to synthetic_daily_highs above: low = close - a small positive offset."""
    return synthetic_daily_prices - 0.5


@pytest.fixture
def synthetic_monthly_picks(synthetic_daily_prices):
    """Simple top-2-by-3-month-momentum picks, matching the pattern used elsewhere in this project."""
    close = synthetic_daily_prices
    month_ends = close.resample("ME").last().index
    picks = {}
    for d in month_ends:
        window = close.loc[:d].tail(63)
        if len(window) < 20:
            continue
        mom = window.iloc[-1] / window.iloc[0] - 1
        picks[d] = list(mom.sort_values(ascending=False).index[:2])
    return pd.Series(picks)


@pytest.fixture
def sample_config_dict():
    """A minimal, valid config.yaml-shaped dict for schema validation tests."""
    return {
        "default_risk": {"holding_period": 1, "stop_loss_pct": 0.12},
        "portfolios": {
            "portfolio1": {
                "tickers": ["SPY", "QQQ", "XLK"],
                "custom_weights": None,
                "total_value": 1000.0,
            }
        },
    }
