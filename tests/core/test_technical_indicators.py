"""
tests/core/test_technical_indicators.py

Covers core/technical_indicators.py -- hand-rolled rather than depending on the pandas-ta
package (which hard-pins numba==0.61.2, incompatible with this project's pandas>=3.0.3 under
`uv sync`'s full dependency resolution, confirmed by direct attempt). These tests pin the
standard formulas against hand-verifiable known values (a monotonically rising price series
should show RSI=100, a flat series should show SMA/EMA equal to the constant price) plus
boundary/sanity checks (RSI and ADX must stay within [0, 100], Bollinger bands must be ordered
lower < mid < upper) so a future edit that subtly breaks one of these (e.g. reordering ADX's
three smoothing passes, per its own docstring warning) gets caught.

Run with: pytest tests/core/test_technical_indicators.py -v
"""
import numpy as np
import pandas as pd
import pytest

from momentum_trading.core.technical_indicators import (
    sma, ema, rsi, macd, atr, bollinger_bands, rolling_std, adx, vwap, obv,
    compute_latest_indicators,
)


def _synthetic_ohlcv(n=100, seed=0):
    rng = np.random.default_rng(seed)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)),
                       index=pd.date_range("2026-01-01", periods=n, freq="B"))
    high = close + rng.random(n)
    low = close - rng.random(n)
    volume = pd.Series(rng.integers(1000, 5000, n), index=close.index)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})


class TestKnownValues:
    """Hand-verifiable cases -- a constant or monotonic series has an unambiguous correct
    indicator value, independent of any implementation detail."""

    def test_sma_of_constant_series_equals_the_constant(self):
        close = pd.Series([100.0] * 30)
        assert sma(close, window=10).iloc[-1] == pytest.approx(100.0)

    def test_ema_of_constant_series_equals_the_constant(self):
        close = pd.Series([100.0] * 30)
        assert ema(close, span=10).iloc[-1] == pytest.approx(100.0)

    def test_rsi_of_strictly_rising_series_is_100(self):
        # Every period is a gain, zero losses -- RSI's definition makes this exactly 100
        # (avg_loss == 0 => RS => infinity => RSI => 100), not just "high".
        close = pd.Series(range(1, 31), dtype=float)
        assert rsi(close, period=14).iloc[-1] == pytest.approx(100.0)

    def test_rsi_of_strictly_falling_series_is_0(self):
        close = pd.Series(range(30, 0, -1), dtype=float)
        assert rsi(close, period=14).iloc[-1] == pytest.approx(0.0)

    def test_obv_accumulates_volume_on_up_days_only(self):
        # 3 up days then 1 down day: OBV = +v1 +v2 +v3 -v4 (starts implicitly at 0 on day 1
        # since diff() is NaN there, treated as 0 by np.sign(...).fillna(0)).
        close = pd.Series([10.0, 11.0, 12.0, 13.0, 12.0])
        volume = pd.Series([100, 200, 300, 400, 500])
        result = obv(close, volume)
        assert result.iloc[-1] == pytest.approx(200 + 300 + 400 - 500)

    def test_bollinger_bands_ordering(self):
        ohlcv = _synthetic_ohlcv()
        bb = bollinger_bands(ohlcv["close"])
        assert bb["lower"].iloc[-1] < bb["mid"].iloc[-1] < bb["upper"].iloc[-1]


class TestBoundaries:
    """RSI and ADX are both defined on [0, 100] by construction -- any implementation bug in
    the smoothing/ratio math tends to produce values outside this range, so a violation here
    is a strong, cheap signal something is wrong."""

    def test_rsi_stays_within_0_100(self):
        ohlcv = _synthetic_ohlcv(seed=1)
        r = rsi(ohlcv["close"]).dropna()
        assert (r >= 0).all() and (r <= 100).all()

    def test_adx_stays_within_0_100(self):
        ohlcv = _synthetic_ohlcv(seed=2)
        a = adx(ohlcv["high"], ohlcv["low"], ohlcv["close"]).dropna()
        assert (a >= 0).all() and (a <= 100).all()

    def test_atr_is_never_negative(self):
        ohlcv = _synthetic_ohlcv(seed=3)
        a = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"]).dropna()
        assert (a >= 0).all()


class TestMacd:
    def test_macd_histogram_equals_macd_minus_signal(self):
        ohlcv = _synthetic_ohlcv(seed=4)
        result = macd(ohlcv["close"])
        diff = (result["histogram"] - (result["macd"] - result["signal"])).dropna()
        assert (diff.abs() < 1e-9).all()


class TestComputeLatestIndicators:
    def test_returns_empty_dict_for_insufficient_history(self):
        # MACD needs 26 periods -- anything shorter must not produce a dict full of NaNs.
        short_ohlcv = _synthetic_ohlcv(n=10)
        assert compute_latest_indicators(short_ohlcv) == {}

    def test_returns_empty_dict_for_empty_dataframe(self):
        assert compute_latest_indicators(pd.DataFrame()) == {}

    def test_returns_all_expected_keys_with_enough_history(self):
        ohlcv = _synthetic_ohlcv(n=60, seed=5)
        result = compute_latest_indicators(ohlcv)
        expected_keys = {
            "sma_20", "ema_20", "adx_14", "rsi_14", "macd", "macd_signal", "macd_histogram",
            "atr_14", "bollinger_upper", "bollinger_mid", "bollinger_lower", "std_dev_20",
            "vwap", "obv",
        }
        assert set(result.keys()) == expected_keys
        assert all(not pd.isna(v) for v in result.values())
