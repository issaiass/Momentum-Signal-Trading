"""
core/technical_indicators.py

Pure price/volume-derived technical indicators for the email reports (monthly/daily
performance reports, interfaces/notifications.py) -- no execution or I/O side effects, per
core/'s architecture rule. Operates on a single ticker's daily OHLCV DataFrame (columns:
open/high/low/close/volume, as returned by core/functions.py's get_bulk_prices()).

Hand-rolled rather than depending on the `pandas-ta` package: pandas-ta 0.4.71b0 hard-pins
numba==0.61.2, which requires numpy<2.3 -- incompatible with this project's pandas>=3.0.3 (which
requires numpy>=2.3.3 on Python 3.14+), making `uv sync` fail to resolve at all across this
project's supported Python range. Confirmed by direct attempt: pandas-ta imports and computes
correctly in isolation, but breaks the project's lockfile resolution project-wide. These formulas
are standard and stable (Wilder's smoothing for RSI/ATR/ADX, matching the conventional
definitions), so hand-rolling avoids the dependency risk entirely.

All indicators only need daily bars (no intraday data available in this project) -- VWAP/OBV
below are the standard cumulative-since-window-start adaptation, not true intraday VWAP, since
there's no intraday data to reset against.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int = 20) -> pd.Series:
    return close.rolling(window).mean()


def ema(close: pd.Series, span: int = 20) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI -- exponential smoothing with alpha=1/period, the standard definition."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Returns a DataFrame with columns macd, signal, histogram."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    return pd.DataFrame({
        "macd": macd_line, "signal": signal_line, "histogram": macd_line - signal_line,
    })


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR -- exponential smoothing of true range, alpha=1/period."""
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def bollinger_bands(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Returns a DataFrame with columns mid, upper, lower."""
    mid = sma(close, window)
    std = close.rolling(window).std()
    return pd.DataFrame({"mid": mid, "upper": mid + num_std * std, "lower": mid - num_std * std})


def rolling_std(close: pd.Series, window: int = 20) -> pd.Series:
    """Rolling standard deviation of daily returns -- the 'Standard Deviation' volatility
    indicator, distinct from Bollinger's price-level std used for the bands themselves."""
    return close.pct_change().rolling(window).std()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ADX (trend strength, direction-agnostic). The fiddliest of these formulas --
    directional movement is computed BEFORE smoothing, then +DI/-DI are derived from the
    smoothed values, then DX is smoothed again into ADX. Get any of these three smoothing
    passes in the wrong order and the result silently diverges from the standard definition."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    tr = _true_range(high, low, close)
    smoothed_tr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_di = 100 * smoothed_plus_dm / smoothed_tr
    minus_di = 100 * smoothed_minus_dm / smoothed_tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Cumulative VWAP over the provided window -- there's no intraday data in this project to
    reset a true session VWAP against, so this is volume-weighted average price since the start
    of whatever OHLCV window was passed in, not a single-session figure."""
    typical_price = (high + low + close) / 3
    return (typical_price * volume).cumsum() / volume.cumsum()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    return (np.sign(close.diff()).fillna(0) * volume).cumsum()


def compute_latest_indicators(ohlcv: pd.DataFrame) -> dict:
    """
    Computes every indicator above on a single ticker's OHLCV DataFrame (columns:
    open/high/low/close/volume) and returns only the MOST RECENT value of each -- reports show
    a current snapshot per held position, not a full time series. Returns {} if there isn't
    enough history for the longest-window indicator (26-period MACD) to have a real value yet,
    rather than returning a dict full of NaNs.
    """
    if ohlcv.empty or len(ohlcv) < 26:
        return {}

    close, high, low, volume = ohlcv["close"], ohlcv["high"], ohlcv["low"], ohlcv["volume"]
    macd_df = macd(close)
    bb_df = bollinger_bands(close)

    return {
        "sma_20": sma(close).iloc[-1],
        "ema_20": ema(close).iloc[-1],
        "adx_14": adx(high, low, close).iloc[-1],
        "rsi_14": rsi(close).iloc[-1],
        "macd": macd_df["macd"].iloc[-1],
        "macd_signal": macd_df["signal"].iloc[-1],
        "macd_histogram": macd_df["histogram"].iloc[-1],
        "atr_14": atr(high, low, close).iloc[-1],
        "bollinger_upper": bb_df["upper"].iloc[-1],
        "bollinger_mid": bb_df["mid"].iloc[-1],
        "bollinger_lower": bb_df["lower"].iloc[-1],
        "std_dev_20": rolling_std(close).iloc[-1],
        "vwap": vwap(high, low, close, volume).iloc[-1],
        "obv": obv(close, volume).iloc[-1],
    }
