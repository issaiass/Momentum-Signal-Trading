"""
functions.py
This file contains the functions needed form the Digital Hub Insights LLC course:
Building a Momentum-Based Investment Startegy

Copyright 2025 Digital Hub Insights LLC. No unauthorized reporduction of this code is permitted
without expressed written permission. All rights reserved.

Version 3.0. 
December 23, 2025

Version 3 incorprates different data sources for the retrieval of price data.

"""

import numpy as np
import pandas as pd
import pandas_datareader as pdr
import os
import json
import ssl
from urllib.request import urlopen
import pandas_market_calendars as mcal
from datetime import datetime
from dateutil.relativedelta import relativedelta
from pandas.tseries.offsets import BDay, BQuarterEnd, BMonthEnd, YearEnd, MonthEnd, QuarterEnd
import collections
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.colors import TwoSlopeNorm
import seaborn as sns


ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

def get_stock_prices(
    symbol: str,
    start_date: str,
    end_date: str,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None,
    source: str | None = None
) -> pd.DataFrame:
    """
    Download historical price data for a single symbol.
    
    Supports three data sources: FMP, EODHD, and Yahoo Finance. When no source
    is specified, the function attempts each source in order (FMP → EODHD → yf)
    until one succeeds. When a source is explicitly specified, only that source
    is attempted and an error is raised if it fails.

    Parameters
    ----------
    symbol : str
        Ticker symbol (e.g., 'SPY', 'AAPL').
    start_date : str
        Start date in 'YYYY-MM-DD' format.
    end_date : str
        End date in 'YYYY-MM-DD' format.
    fmp_api_key : str | None, optional
        API key for Financial Modeling Prep. Required if using FMP.
    eodhd_api_key : str | None, optional
        API key for EODHD. Required if using EODHD.
    source : str | None, optional
        Data source to use: 'FMP', 'EOD', 'yf', or None for auto-fallback.
        Default is None.

    Returns
    -------
    pd.DataFrame
        Historical prices with datetime index. Column names vary by source:
        - FMP: adjClose, open, high, low, close, volume
        - EODHD: adjusted_close, open, high, low, close, volume
        - Yahoo Finance: Adj Close, Open, High, Low, Close, Volume

    Raises
    ------
    ValueError
        If an explicit source is specified but the API key is missing,
        if an explicit source fails, or if all sources fail in auto-fallback mode.

    Examples
    --------
    >>> # Auto-fallback mode
    >>> df = get_stock_prices('AAPL', '2023-01-01', '2023-12-31', 
    ...                       fmp_api_key='your_key')
    
    >>> # Explicit source
    >>> df = get_stock_prices('AAPL', '2023-01-01', '2023-12-31', source='yf')
    """
    
    # -------------------------------------------------------------------------
    # Helper function: Fetch from FMP
    # -------------------------------------------------------------------------
    def _fetch_fmp() -> pd.DataFrame:
        """Fetch data from Financial Modeling Prep API.

        Uses FMP's `/stable/` endpoints, not the legacy `/api/v3/` path this function used
        before -- FMP shut down every `/api/v3/` endpoint 2025-08-31, and it now returns 403
        "Legacy Endpoint" regardless of subscription tier (confirmed by live testing against a
        real key; see CLAUDE.md's core/ notes). Two `/stable/` calls are needed to match the
        old response shape: `/historical-price-eod/full` for raw OHLCV (needed by
        execution/live_signal.py's fetch_ohlcv_for_tickers(), which requires plain
        open/high/low/close/volume for technical indicators) and
        `/historical-price-eod/dividend-adjusted` for `adjClose` (needed by this module's
        get_bulk_prices(), whose momentum-ranking price series must be dividend-adjusted --
        unadjusted close would distort rankings around ex-dividend dates for dividend-paying
        ETFs). The response is a flat list, unlike `/api/v3/`'s `{"historical": [...]}` wrapper.
        """
        if not fmp_api_key:
            raise ValueError("FMP API key not provided")

        ohlcv_url = (
            f"https://financialmodelingprep.com/stable/historical-price-eod/full"
            f"?symbol={symbol}&from={start_date}&to={end_date}&apikey={fmp_api_key}"
        )
        response = urlopen(ohlcv_url, context=ssl_context)
        prices = json.loads(response.read().decode("utf-8"))

        # Check for valid response
        if not prices:
            raise ValueError(f"No data returned from FMP for {symbol}")

        df = pd.DataFrame(prices).set_index('date').sort_index()
        df.index = pd.to_datetime(df.index)

        # Dividend-adjusted close -- best-effort second call. Non-fatal if it fails: the raw
        # OHLCV above still stands, and get_bulk_prices() already falls back to unadjusted
        # 'close' when 'adjClose' is absent from the frame.
        try:
            adj_url = (
                f"https://financialmodelingprep.com/stable/historical-price-eod/dividend-adjusted"
                f"?symbol={symbol}&from={start_date}&to={end_date}&apikey={fmp_api_key}"
            )
            adj_response = urlopen(adj_url, context=ssl_context)
            adj_prices = json.loads(adj_response.read().decode("utf-8"))
            if adj_prices:
                adj_df = pd.DataFrame(adj_prices).set_index('date')[['adjClose']]
                adj_df.index = pd.to_datetime(adj_df.index)
                df = df.join(adj_df, how='left')
        except Exception:
            pass

        return df
    
    # -------------------------------------------------------------------------
    # Helper function: Fetch from EODHD
    # -------------------------------------------------------------------------
    def _fetch_eodhd() -> pd.DataFrame:
        """Fetch data from EODHD API."""
        if not eodhd_api_key:
            raise ValueError("EODHD API key not provided")
        
        url = (
            f"https://eodhistoricaldata.com/api/eod/{symbol}"
            f"?from={start_date}&to={end_date}"
            f"&period=d&api_token={eodhd_api_key}&fmt=json"
        )
        
        response = urlopen(url, context=ssl_context)
        data = response.read().decode("utf-8")
        prices = json.loads(data)
        
        # Check for valid response
        if not prices:
            raise ValueError(f"No data returned from EODHD for {symbol}")
        
        df = pd.DataFrame(prices).set_index('date').sort_index()
        df.index = pd.to_datetime(df.index)
        
        return df
    
    # -------------------------------------------------------------------------
    # Helper function: Fetch from Yahoo Finance
    # -------------------------------------------------------------------------
    def _fetch_yf() -> pd.DataFrame:
        """Fetch data from Yahoo Finance."""
        df = yf.download(
            symbol, 
            start=start_date, 
            end=end_date, 
            progress=False,
            auto_adjust=False
        )
        
        # Check for valid response
        if df.empty:
            raise ValueError(f"No data returned from Yahoo Finance for {symbol}")
        
        # Handle multi-level column index from newer yfinance versions
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        
        return df
    
    # -------------------------------------------------------------------------
    # Explicit source mode
    # -------------------------------------------------------------------------
    if source is not None:
        source = source.upper() if source.lower() != 'yf' else 'yf'
        
        # Validate API key is present for the specified source
        if source == 'FMP' and not fmp_api_key:
            raise ValueError("source='FMP' specified but no FMP API key provided")
        elif source == 'EOD' and not eodhd_api_key:
            raise ValueError("source='EOD' specified but no EODHD API key provided")
        
        try:
            if source == 'FMP':
                df = _fetch_fmp()
            elif source == 'EOD':
                df = _fetch_eodhd()
            elif source == 'yf':
                df = _fetch_yf()
            else:
                raise ValueError(f"Invalid source '{source}'. Use 'FMP', 'EOD', or 'yf'")
            
            print(f"Data retrieved from {source} for {symbol}")
            return df
            
        except Exception as e:
            raise ValueError(f"{source} failed for {symbol}: {e}")
    
    # -------------------------------------------------------------------------
    # Auto-fallback mode: FMP → EODHD → yf
    # -------------------------------------------------------------------------
    
    # Try FMP
    if fmp_api_key:
        try:
            df = _fetch_fmp()
            print(f"Data retrieved from FMP for {symbol}")
            return df
        except Exception as e:
            print(f"FMP failed for {symbol}, trying EODHD...")
    
    # Try EODHD
    if eodhd_api_key:
        try:
            df = _fetch_eodhd()
            print(f"Data retrieved from EODHD for {symbol}")
            return df
        except Exception as e:
            print(f"EODHD failed for {symbol}, trying Yahoo Finance...")
    
    # Try Yahoo Finance
    try:
        df = _fetch_yf()
        print(f"Data retrieved from Yahoo Finance for {symbol}")
        return df
    except Exception as e:
        raise ValueError(f"All data sources failed for {symbol}. Last error: {e}")


def get_bulk_prices(
    tickers: list,
    start_date: str,
    end_date: str,
    frequency: str = 'D',
    source: str | None = None,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None
) -> pd.DataFrame:
    """
    Fetch adjusted close prices for a list of tickers between specified dates.
    
    When no source is specified, the function uses the first ticker to determine
    the best available data source (FMP → EODHD → yf), then uses that source
    for all remaining tickers to ensure consistent column formatting.

    Parameters
    ----------
    tickers : list of str
        List of ticker symbols for which prices are to be fetched.
    start_date : str
        The start date for fetching prices in 'YYYY-MM-DD' format.
    end_date : str
        The end date for fetching prices in 'YYYY-MM-DD' format.
    frequency : str, optional
        The frequency for data resampling:
        - 'D' for daily (default)
        - 'W' for weekly (Friday close)
        - 'M' for monthly
    source : str | None, optional
        Data source to use: 'FMP', 'EOD', 'yf', or None for auto-detection.
        Default is None.
    fmp_api_key : str | None, optional
        API key for Financial Modeling Prep. Required if using FMP.
    eodhd_api_key : str | None, optional
        API key for EODHD. Required if using EODHD.

    Returns
    -------
    pd.DataFrame
        A DataFrame with dates as index and adjusted close prices for each ticker.

    Raises
    ------
    ValueError
        If a source is specified but the required API key is missing,
        if no valid data source can be determined, or if all tickers fail.

    Examples
    --------
    >>> # Auto-detect source using first ticker
    >>> df = get_bulk_prices(['AAPL', 'MSFT', 'GOOGL'], 
    ...                      '2023-01-01', '2023-12-31',
    ...                      fmp_api_key='your_key')
    
    >>> # Explicit source with monthly resampling
    >>> df = get_bulk_prices(['SPY', 'BND'], 
    ...                      '2020-01-01', '2023-12-31',
    ...                      frequency='M', source='yf')
    """
    
    # Ensure tickers is a list
    if not isinstance(tickers, list):
        tickers = [tickers]
    
    if not tickers:
        raise ValueError("Tickers list cannot be empty")
    
    # -------------------------------------------------------------------------
    # Validate API key if source is explicitly specified
    # -------------------------------------------------------------------------
    if source is not None:
        source_check = source.upper() if source.lower() != 'yf' else 'yf'
        
        if source_check == 'FMP' and not fmp_api_key:
            raise ValueError("source='FMP' specified but no FMP API key provided")
        elif source_check == 'EOD' and not eodhd_api_key:
            raise ValueError("source='EOD' specified but no EODHD API key provided")
    
    # -------------------------------------------------------------------------
    # Determine source using first ticker (if not explicitly specified)
    # -------------------------------------------------------------------------
    if source is None:
        test_ticker = tickers[0]
        print(f"Determining best data source using {test_ticker}...")
        
        # Try FMP
        if fmp_api_key:
            try:
                _ = get_stock_prices(
                    test_ticker, start_date, end_date,
                    fmp_api_key=fmp_api_key,
                    source='FMP'
                )
                source_used = 'FMP'
            except Exception:
                source_used = None
        else:
            source_used = None
        
        # Try EODHD if FMP failed
        if source_used is None and eodhd_api_key:
            try:
                _ = get_stock_prices(
                    test_ticker, start_date, end_date,
                    eodhd_api_key=eodhd_api_key,
                    source='EOD'
                )
                source_used = 'EOD'
            except Exception:
                source_used = None
        
        # Try Yahoo Finance if both failed
        if source_used is None:
            try:
                _ = get_stock_prices(
                    test_ticker, start_date, end_date,
                    source='yf'
                )
                source_used = 'yf'
            except Exception:
                raise ValueError(f"No data source available for {test_ticker}")
        
        print(f"Using {source_used} for all tickers")
    else:
        source_used = source.upper() if source.lower() != 'yf' else 'yf'
    
    # -------------------------------------------------------------------------
    # Determine adjusted close column name based on source
    # -------------------------------------------------------------------------
    if source_used == 'FMP':
        price_col = 'adjClose'
    elif source_used == 'EOD':
        price_col = 'adjusted_close'
    else:  # yf
        price_col = 'Adj Close'
    
    # -------------------------------------------------------------------------
    # Fetch prices for all tickers
    # -------------------------------------------------------------------------
    price_frames = []
    
    for ticker in tickers:
        try:
            df = get_stock_prices(
                ticker, start_date, end_date,
                fmp_api_key=fmp_api_key,
                eodhd_api_key=eodhd_api_key,
                source=source_used
            )
            
            # Extract adjusted close column, fallback to close if not available
            if price_col in df.columns:
                price_series = df[price_col]
            elif 'close' in df.columns:
                price_series = df['close']
            elif 'Close' in df.columns:
                price_series = df['Close']
            else:
                print(f"Warning: No price column found for {ticker}, skipping")
                continue
            
            price_frames.append(price_series.rename(ticker))
        
        except Exception as e:
            print(f"Error retrieving data for {ticker}: {e}")
    
    # Check if any data was retrieved
    if not price_frames:
        raise ValueError("No data retrieved for any ticker")
    
    # -------------------------------------------------------------------------
    # Combine all tickers into one DataFrame
    # -------------------------------------------------------------------------
    df_prices = pd.concat(price_frames, axis=1)
    df_prices.index = pd.to_datetime(df_prices.index)
    df_prices.index = df_prices.index.rename('date')
    
    # -------------------------------------------------------------------------
    # Optional resampling
    # -------------------------------------------------------------------------
    if frequency == 'W':
        df_prices = df_prices.resample('W-FRI').last()
    elif frequency == 'M':
        df_prices = df_prices.resample('M').last()
    
    return df_prices.sort_index()
    
    
def compound_growth_index(returns, frequency=None, exchange='NYSE', index_start=100):
    '''
    Returns a compound growth index from a pandas series or DataFrame
    Note: Returns are supplied in percent format, not decimal! The function will divide by 100.

    Parameters
    ----------
    returns : pd.Series or pd.DataFrame
        Series of periodic returns
    frequency : str, optional
        Frequency of the data: 'H' for hourly, 'D' for daily, 'M' for monthly
        If None, will attempt to infer from the data
    exchange : str, optional
        Exchange calendar to use for trading days. The default is 'NYSE'.
    index_start : int, optional
        Starting level of the indexed series. The default is 100.

    Returns
    -------
    indexed_rets : pd.DataFrame
        Indexed return series
    '''

    # pd.concat([pd.DataFrame(data), df], ignore_index=True)

    # Convert Series to DataFrame if necessary
    if isinstance(returns, pd.Series):
        returns = returns.to_frame()

    # Infer frequency if not provided
    if not frequency:
        frequency = pd.infer_freq(returns.index)

    # Get appropriate calendar
    start_date = returns.index[0]
    end_date = returns.index[-1]

    # Handle different frequencies
    if frequency == 'H':
        # For hourly data, we'll set the first index at one hour before the first return
        first_time = returns.index[0] - pd.Timedelta(hours=1)
        first_row = pd.DataFrame([[index_start] * len(returns.columns)],
                                 columns=returns.columns,
                                 index=[first_time])
    elif frequency == 'M':
        # For monthly data, set first index to one day before the first return
        first_row = pd.DataFrame([[index_start] * len(returns.columns)],
                                 columns=returns.columns,
                                 index=[returns.index[0] - relativedelta(days=1)])
    elif frequency == 'D':
        # For daily data, use the exchange calendar to find the previous trading day
        cal = mcal.get_calendar(exchange).schedule(
            (start_date - pd.Timedelta(days=5)).date(),
            end_date
        )

        z = 1
        dt = (returns.index[0] - BDay(1)).date()
        while dt not in cal.index:
            if pd.to_datetime(dt) <= pd.to_datetime(cal.index[0]):
                break
            dt = (dt - BDay(z)).date()

        first_row = pd.DataFrame([[index_start] * len(returns.columns)],
                                 columns=returns.columns,
                                 index=[dt])
    else:
        # For any other frequency, set first index to one period before first return
        # This is a fallback and might need adjustment for specific frequencies
        first_row = pd.DataFrame([[index_start] * len(returns.columns)],
                                 columns=returns.columns,
                                 index=[returns.index[0] - pd.Timedelta(days=1)])

    # Calculate indexed returns (assuming returns are in percent format)
    # Convert from percentage to decimal by dividing by 100
    # returns_decimal = returns / 100
    indexed_rets = (1 + returns).cumprod() * index_start

    # Concatenate the first row with the calculated index values
    indexed_rets = pd.concat([first_row, indexed_rets])
    indexed_rets.columns = returns.columns

    # Ensure datetime format for index
    indexed_rets.index = pd.to_datetime(indexed_rets.index)

    return indexed_rets
    

def rolling_return_stats(rolling_returns, benchmark):
    """
    Calculate statistics for rolling returns compared to a benchmark.

    Args:
    rolling_returns (pd.DataFrame): A DataFrame where each column represents the rolling returns of an asset.
    benchmark (str): The column name of the benchmark asset to compare against.

    Returns:
    pd.DataFrame: A DataFrame containing the percentage of periods each asset's return
                  was greater than the benchmark, the maximum return, and the minimum return.

    Example:
    >>> data = {
        'Fund1': [0.1, 0.2, 0.15, 0.22, 0.18],
        'Fund2': [0.12, 0.18, 0.17, 0.21, 0.19],
        'Benchmark': [0.11, 0.19, 0.16, 0.2, 0.17]
    }
    >>> rolling_returns = pd.DataFrame(data)
    >>> stats = rolling_return_stats(rolling_returns, 'Benchmark')
    >>> print(stats)
    """

    # Initialize an empty DataFrame to store statistics
    df_stats = pd.DataFrame()

    # Iterate over each column in the DataFrame to compute statistics
    for col in rolling_returns.columns:
        # Compute the percentage of periods where the return of the column is greater than the benchmark
        percent_greater = (rolling_returns[col] > rolling_returns[benchmark]).sum() / rolling_returns.shape[0]

        # Store the computed percentage in df_stats, multiplying by 100 to convert to percentage and rounding to 2 decimal places
        df_stats.loc[col, '% Periods Greater Than Benchmark'] = np.round(percent_greater * 100, 2)

    # Compute and store the maximum return for each column, rounded to 2 decimal places
    df_stats['Maximum Return (%)'] = rolling_returns.max().round(2)

    # Compute and store the minimum return for each column, rounded to 2 decimal places
    df_stats['Minimum Return (%)'] = rolling_returns.min().round(2)

    return df_stats


def calculate_rolling_returns(df_returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    Calculates rolling returns for each column in the given DataFrame.

    Args:
        df_returns (pd.DataFrame): DataFrame containing monthly returns in decimal form.
        window (int): Rolling window size (e.g., number of months).

    Returns:
        pd.DataFrame: DataFrame of rolling returns with the same structure as the input.
    """
    # Calculate rolling returns
    rolling_returns = df_returns.rolling(window=window).apply(
        lambda x: np.prod(1 + x) - 1, raw=False
    )
    return rolling_returns


def annualize_returns(returns, frequency='M'):
    '''
    Annualize set of returns based on any frequency.

    Parameters
    ----------
    returns : pd.Series or pd.DataFrame
        Series or DataFrame of periodic returns.
    frequency : str
        Frequency of the returns ('M' for monthly, 'D' for daily, 'Q' for quarterly, 'W' for weekly).

    Returns
    -------
    annualized_returns : pd.DataFrame
        Annualized return series.

    '''

    # Ensure that returns is treated as a DataFrame, even if it's a single Series
    if isinstance(returns, pd.Series):
        returns = returns.to_frame()

    # Define periods per year based on frequency
    if frequency == 'M':
        periods_per_year = 12
    elif frequency == 'D':
        periods_per_year = 252
    elif frequency == 'Q':
        periods_per_year = 4
    elif frequency == 'W':
        periods_per_year = 52
    elif frequency == 'H':  # hourly
        periods_per_year = 252 * 24
    else:
        raise ValueError("Invalid frequency. Use 'M', 'D', 'Q', 'H', or 'W'.")

    # Calculate compounded growth and annualized returns
    compounded_growth = (returns + 1).prod()
    n_periods = returns.shape[0]

    # Annualize returns based on frequency
    annualized_returns = compounded_growth ** (periods_per_year / n_periods) - 1

    # If the result is a scalar, convert to DataFrame
    if isinstance(annualized_returns, pd.Series):
        annualized_returns = annualized_returns.to_frame(name='Return')

    # Return as a percentage (multiply by 100) and round to 2 decimal places
    return annualized_returns.mul(100).round(2)


def annualize_vol(returns, frequency='M'):
    '''
    Annualize volatility of a set of returns based on any frequency.

    Parameters
    ----------
    returns : pd.Series or pd.DataFrame
        return series or dataframe
    frequency : str
        options are 'M', 'D', 'Q' for monthly, daily, or quarterly returns.

    Returns
    -------
    pd.DataFrame
        A DataFrame containing the annualized volatility as a percentage.
    '''

    # Define periods per year based on frequency
    if frequency == 'M':
        periods_per_year = 12
    elif frequency == 'D':
        periods_per_year = 252
    elif frequency == 'Q':
        periods_per_year = 4
    elif frequency == 'W':
        periods_per_year = 52
    elif frequency == 'H':  # hourly
        periods_per_year = 252 * 24
    else:
        raise ValueError("Invalid frequency. Use 'M', 'D', 'Q', 'H', or 'W'.")

    # Ensure that returns is treated as a DataFrame, even if it's a single Series
    if isinstance(returns, pd.Series):
        returns = returns.to_frame()

    # Calculate annualized volatility
    annualized_vol = (returns.std() * (periods_per_year ** 0.5))

    if isinstance(annualized_vol, pd.Series):
        annualized_vol = annualized_vol.to_frame(name='Standard Deviation')

    # Return as a DataFrame and ensure percentages are rounded
    return annualized_vol.mul(100).round(2)


def return_period_dates(start_date, end_date, frequency='M', exchange='NYSE'):
    """
    Determines the dates to be used for trailing returns calculations. The function
    adjusts the dates for the latest trading day if they fall on a holiday or weekend,
    based on the specified exchange's calendar.

    Parameters:
    start_date (datetime): The start date for calculating the period.
    end_date (datetime): The end date for calculating the period.
    frequency (str): The frequency of returns ('D' for daily, 'W' for weekly, 'M' for monthly). Defaults to 'M'.
    exchange (str): The stock exchange calendar used for checking trading days. Defaults to 'TSX'.

    Returns:
    dict: A dictionary with periods as keys and their respective adjusted dates as values.
    """
    if type(start_date) == str:
      dt_start = datetime.strptime(start_date, '%Y-%m-%d')
    else:
      dt_start = start_date

    if type(end_date) == str:
      dt_end = datetime.strptime(end_date, '%Y-%m-%d')
    else:
      dt_end = end_date

    cal = mcal.get_calendar(exchange).schedule(start_date, end_date)

    if frequency == 'M':
      past_dates_dict = {
          '1 Month': (dt_end - MonthEnd(1)),
          '3 Month': (dt_end - MonthEnd(3)),
          '6 Month': (dt_end - MonthEnd(6)),
          'YTD': (dt_end - YearEnd()),
          '1 Year': (dt_end - MonthEnd(12)),
          '3 Year': (dt_end - MonthEnd(36)),
          '5 Year': (dt_end - MonthEnd(60)),
          '10 Year': (dt_end - MonthEnd(120)),
          'Since Inception': (dt_start - MonthEnd(1))
      }

      holiday_check = False

    if frequency == 'D':

      past_dates_dict = {
          '1 Month': dt_end - relativedelta(months=1),
          '3 Month': dt_end - relativedelta(months=3),
          '6 Month': dt_end - relativedelta(months=6),
          'YTD': (dt_end - YearEnd()),
          '1 Year': dt_end - relativedelta(years=1),
          '3 Year': dt_end - relativedelta(years=3),
          '5 Year': dt_end - relativedelta(years=5),
          '10 Year': dt_end - relativedelta(years=10),
          'Since Inception': (dt_start - BDay())
      }

      holiday_check = True

    if frequency == 'W':
        past_dates_dict = {
          '1 Week': dt_end - relativedelta(weeks=1),
          '1 Month': dt_end - relativedelta(weeks=4),  # approximately 4 weeks
          '3 Months': dt_end - relativedelta(weeks=13),  # approximately 13 weeks
          '6 Months': dt_end - relativedelta(weeks=26),  # approximately 26 weeks
          '1 Year': dt_end - relativedelta(weeks=52),  # approximately 52 weeks
          '3 Years': dt_end - relativedelta(weeks=156),  # approximately 156 weeks
          '5 Years': dt_end - relativedelta(weeks=260),  # approximately 260 weeks
          '10 Years': dt_end - relativedelta(weeks=520),  # approximately 520 weeks
          'Since Inception': dt_start - BDay()  # the day before the start date
      }

        holiday_check = True

    # correct when the past dates fall on trading holidays or weekends
    if holiday_check:
      for key, value in past_dates_dict.items():
          z = 1
          dt = value
          while pd.to_datetime(dt) not in pd.to_datetime(cal.index):
              if dt < pd.to_datetime(cal.index[0]):
                  break
              dt = value - BDay(z)
              z += 1

          past_dates_dict[key] = dt
          try:
              past_dates_dict[key] = past_dates_dict[key]
          except:
              pass

    return past_dates_dict


def trailing_returns(returns, start_date, end_date, frequency='D', exchange='TSX',
                     annualize=True):
    """
    Calculates the trailing returns for a given DataFrame of returns.

    This function computes the trailing returns over specified periods, optionally annualizing
    the returns based on the given frequency. The returns are calculated from the start date to the
    end date for the specified frequency (monthly, weekly, or quarterly).

    Parameters:
    returns (pd.DataFrame): DataFrame containing return data for various investments.
    start_date (str or datetime): Start date for calculating trailing returns.
    end_date (str or datetime): End date for calculating trailing returns.
    frequency (str): Frequency for calculating returns ('M' for monthly, 'W' for weekly, 'Q' for quarterly). Defaults to 'D'.
    exchange (str): Exchange used for market calendar (currently unused in the function body).
    annualize (bool): Flag indicating whether to annualize the returns. Defaults to True.

    Returns:
    pd.DataFrame: DataFrame containing trailing returns for each specified period.
    """
    # convert dates to datetime format if required
    if isinstance(start_date, str):
        dt_start = datetime.strptime(start_date, '%Y-%m-%d')
    else:
        dt_start = start_date

    if isinstance(end_date, str):
        dt_end = datetime.strptime(end_date, '%Y-%m-%d')
    else:
        dt_end = end_date

    # ensure returns index is in datetime
    returns.index = pd.to_datetime(returns.index)

    # 1) Convert the returns to a compound growth index
    funds_cgi = compound_growth_index(returns, frequency=frequency)

    # 2) get the required previous dates for CGI start date of each performance
    # period.
    dt_start = funds_cgi.index[0]
    past_dates_dict = return_period_dates(dt_start, dt_end, frequency=frequency, exchange=exchange)

    if past_dates_dict['Since Inception'] != funds_cgi.index[0]:
        past_dates_dict['Since Inception'] = funds_cgi.index[0]

    # 3) Initialize empty dict to hold returns
    returns_dict = collections.defaultdict(dict)

    # 4) Cycle through past_dates_dict, calculate the return, and then add
    # to returns dict. Need loop and if statement for annualize.
    for key, value in past_dates_dict.items():
        for fund in funds_cgi:
            # get the ending value given the end date
            ev = funds_cgi[fund].loc[dt_end]

            # check if the time period exists for each fund
            if value < funds_cgi.index[0]:
                returns_dict[fund][key] = np.nan
                continue

            else:
                bv = funds_cgi[fund].loc[value]

            if annualize:

                if frequency == 'M':
                    num_months = (dt_end.year - value.year) * 12 + \
                                 (dt_end.month - value.month)

                    if num_months > 12:
                        ret = ((1 + ((ev / bv) - 1)) **
                               (1 / (num_months / 12)) - 1)

                        returns_dict[fund][key] = round(ret * 100, 2)

                    else:
                        returns_dict[fund][key] = round(((ev / bv) - 1) * 100, 2)

                elif frequency == 'D':
                    num_days = funds_cgi.index.get_loc(dt_end) - funds_cgi.index.get_loc(value)
                    if num_days > 252:
                        ret = ((1 + ((ev / bv) - 1)) **
                               (1 / (num_days / 252)) - 1)

                        returns_dict[fund][key] = round(ret * 100, 2)
                    else:
                        returns_dict[fund][key] = round(((ev / bv) - 1) * 100, 2)
            else:
                returns_dict[fund][key] = round(((ev / bv) - 1) * 100, 2)

    # create a dataframe and reorder the columns
    df_final = pd.DataFrame(returns_dict).T
    cols = [
        '1 Month',
        '3 Month',
        '6 Month',
        'YTD',
        '1 Year',
        '3 Year',
        '5 Year',
        '10 Year',
        'Since Inception'
    ]
    df_final = df_final[cols]

    return df_final


def calendar_returns(returns, frequency='D'):
    # 1) Convert the returns to a compound growth index
    funds_cgi = compound_growth_index(returns, frequency=frequency)

    # 3) find the cgi values from the respective dates
    # resample the cgi to last year-end for each year.
    funds_cgi.index = pd.to_datetime(funds_cgi.index)
    cgi_resampled = funds_cgi.resample('YE').last()

    # 4) calculate the pct change period over period
    df_ror = cgi_resampled.pct_change().dropna()

    # sort index
    df_ror = df_ror.sort_index(ascending=False)

    # 5) Change index to year only and
    df_ror.index = pd.to_datetime(df_ror.index, format='%Y-%m-%d').year

    return df_ror.T.mul(100).round(2)


def get_rf(
    symbol: str,
    start_date: str,
    end_date: str,
    fmp_api_key: str | None = None,
    eodhd_api_key: str | None = None,
    source: str | None = None,
    frequency: str = 'M'
) -> pd.DataFrame:
    """
    Fetch risk-free rate proxy data and calculate periodic returns.
    
    Downloads price data for a risk-free rate proxy (e.g., 'BIL' for T-Bills)
    and converts it to periodic returns at the specified frequency.

    Parameters
    ----------
    symbol : str
        Ticker symbol for risk-free rate proxy (e.g., 'BIL', 'SHV').
    start_date : str
        Start date in 'YYYY-MM-DD' format.
    end_date : str
        End date in 'YYYY-MM-DD' format.
    fmp_api_key : str | None, optional
        API key for Financial Modeling Prep. Required if using FMP.
    eodhd_api_key : str | None, optional
        API key for EODHD. Required if using EODHD.
    source : str | None, optional
        Data source to use: 'FMP', 'EOD', 'yf', or None for auto-fallback.
        Default is None.
    frequency : str, optional
        Return frequency: 'M' for monthly, 'Q' for quarterly, 'D' for daily.
        Default is 'M'.

    Returns
    -------
    pd.DataFrame
        DataFrame containing periodic returns for the risk-free rate proxy.

    Examples
    --------
    >>> # Monthly risk-free returns using auto-fallback
    >>> rf = get_rf('BIL', '2020-01-01', '2023-12-31', fmp_api_key='your_key')
    
    >>> # Quarterly returns using Yahoo Finance
    >>> rf = get_rf('SHV', '2020-01-01', '2023-12-31', source='yf', frequency='Q')
    """
    
    # -------------------------------------------------------------------------
    # Fetch price data
    # -------------------------------------------------------------------------
    df_rf = get_stock_prices(
        symbol, start_date, end_date,
        fmp_api_key=fmp_api_key,
        eodhd_api_key=eodhd_api_key,
        source=source
    )
    
    # -------------------------------------------------------------------------
    # Determine adjusted close column based on available columns
    # -------------------------------------------------------------------------
    if 'adjClose' in df_rf.columns:
        price_col = 'adjClose'
    elif 'adjusted_close' in df_rf.columns:
        price_col = 'adjusted_close'
    elif 'Adj Close' in df_rf.columns:
        price_col = 'Adj Close'
    elif 'close' in df_rf.columns:
        price_col = 'close'
    elif 'Close' in df_rf.columns:
        price_col = 'Close'
    else:
        raise ValueError(f"No price column found in data for {symbol}")
    
    df_rf = df_rf[[price_col]]
    
    # -------------------------------------------------------------------------
    # Resample to specified frequency
    # -------------------------------------------------------------------------
    df_rf.index = pd.to_datetime(df_rf.index)
    
    if frequency == 'M':
        df_rf = df_rf.resample('ME').last()
    elif frequency == 'Q':
        df_rf = df_rf.resample('QE').last()
    # Daily frequency requires no resampling
    
    # -------------------------------------------------------------------------
    # Calculate returns
    # -------------------------------------------------------------------------
    df_rf_returns = df_rf.pct_change().dropna()
    df_rf_returns.columns = [symbol]
    
    return df_rf_returns


def sharpe_ratio(returns, rf, start_date, end_date, frequency='D'):
    """
    Calculate and annualize the sharpe ratio for a series of investments
    :param fund_returns: dataframe of fund returns loaded from session state
    :param benchmark_returns: dataframe of benchmark returns loaded from session state
    :param rf_returns: dataframe of risk-free rate returns loaded from session state
    :param frequency: string, options = M, D, Q
    :return:
    """

    if frequency == 'M':
        periods_per_year = 12

    if frequency == 'D':
        periods_per_year = 252

    if frequency == 'Q':
        periods_per_year = 4

    if returns.shape[0] < periods_per_year:
        print('Insufficient performance history to calculate Sharpe Ratio')
        return None

    rf_returns = get_rf(rf, start_date, end_date)

    # calculate the average return
    avg_cash = float(rf_returns.mean().iloc[0])

    sharpe = (returns.mean().sub(avg_cash)).div(returns.std())

    sharpe_annualized = sharpe * (periods_per_year ** 0.5)

    return sharpe_annualized.round(2).to_frame(name='Sharpe Ratio')


def sortino_ratio(returns, target, frequency='D'):
    """
    Calculates the Sortino ratio for a given set of returns.

    The Sortino ratio is a modification of the Sharpe ratio that differentiates harmful volatility
    from total overall volatility by using the asset's standard deviation of negative asset returns,
    called downside deviation, instead of the total standard deviation of portfolio returns.

    Parameters:
    - returns (pd.DataFrame or pd.Series): Returns of the asset(s) for which the Sortino ratio is to be calculated.
    - target (float or pd.DataFrame or pd.Series): The target return. If a DataFrame or Series, the mean value is used.
    - frequency (str): The frequency of the returns data ('D' for daily, 'M' for monthly, 'Q' for quarterly).

    Returns:
    - pd.DataFrame: A DataFrame with the Sortino ratio for each asset.
    """

    # Determine the number of periods per year based on the frequency
    if frequency == 'M':
        periods_per_year = 12
    elif frequency == 'D':
        periods_per_year = 252
    elif frequency == 'Q':
        periods_per_year = 4

    # Check if there are enough data points to calculate the Sortino ratio
    if returns.shape[0] < periods_per_year:
        print('Insufficient performance history to calculate Sortino Ratio')
        return None

    # Determine the target rate
    if isinstance(target, pd.DataFrame) or isinstance(target, pd.Series):
        target = float(target.mean(axis=0))

    # Convert returns to a percentage basis
    returns = returns.div(100)

    # Calculate the average outperformance
    avg_outperformance = returns.mean(axis=0).subtract(target) * periods_per_year

    # Calculate downside deviation
    rets_below_target = returns[returns < target].fillna(0)
    df_sortino = avg_outperformance / (rets_below_target.std() * np.sqrt(periods_per_year))

    # Return the Sortino ratio as a DataFrame
    return df_sortino.round(2).to_frame(name='Sortino Ratio')


def max_drawdown(returns: pd.DataFrame):
    """
    Calculates the maximum drawdown for each financial instrument in a DataFrame.

    A drawdown is the peak-to-trough decline during a specific recorded period of an investment.
    This function computes the maximum drawdown, which is the maximum observed loss from a peak to
    a trough of a portfolio, before a new peak is attained. It is particularly useful for assessing
    the financial risk of an investment.

    Parameters:
    returns (pd.DataFrame): A DataFrame where each column represents a different financial instrument
                            and contains their respective monthly returns.

    Returns:
    pd.DataFrame: A DataFrame where each row corresponds to a financial instrument from the input,
                  and contains its maximum drawdown as a percentage.
    """
    # Initialize DataFrame to store maximum drawdowns
    max_drawdowns = pd.DataFrame(dtype=float, columns=returns.columns)

    # Calculate maximum drawdown for each column (financial instrument)
    for column in returns.columns:
        # Cumulative returns from the beginning of the time series
        cumulative_returns = (1 + returns[column]).cumprod()

        # Track the highest value (peak) reached so far in cumulative returns
        previous_peaks = cumulative_returns.cummax()

        # Calculate drawdowns as the percentage loss from the previous peak
        drawdowns = (cumulative_returns - previous_peaks) / previous_peaks.shift(1)

        # Identify the maximum drawdown
        max_drawdown = drawdowns.min()
        max_drawdowns.loc[0, column] = max_drawdown

    # Rename the index for clarity
    max_drawdowns.index = ['Maximum Drawdown']

    # Return the result, converting drawdowns to percentage format
    return np.round(max_drawdowns.mul(100), 2).T


def best_worst_periods(returns: pd.DataFrame, period: int):
    """
    Function to calculate the best and worst returns over the supplied period

    params:
    returns: pd.DataFrame, monthly returns
    period: int, number of months used in the calculation

    Returns: pd.DataFrame

    """

    # 1) Create a compound growth index of the supplied monthly returns
    df_cgi = compound_growth_index(returns, frequency='M')

    # 2) Calculate the pct_change based on the period
    df_pct_change = df_cgi.pct_change(period).dropna()

    # 3) determine the max and min periods, convert to dataframes and name the columns
    best_period = df_pct_change.max().to_frame(name=f'Best {period} Month Period')
    worst_period = df_pct_change.min().to_frame(name=f'Worst {period} Month Period')

    # 4) concat the two dfs
    df_final = pd.concat([best_period, worst_period], axis=1)

    return np.round(df_final.mul(100), 2)


def rolling_returns(prices, period, frequency=None, annualize=True):
    """
    Calculate rolling returns for a given DataFrame of prices over a specified period.

    This function computes the percentage change in prices over the number of periods specified,
    which represents the rolling return. If annualize is True, the rolling returns are annualized
    based on the inferred frequency of the data points in the 'prices' DataFrame.

    Parameters:
    ----------
    prices : pd.DataFrame
        A pandas DataFrame with datetime index representing the time series data of prices.

    period : int
        The number of periods over which the rolling return should be calculated. This should
        match the frequency of the data (e.g., if monthly data, and you want 3-month rolling returns,
        period should be 3).

    annualize : bool, optional (default=True)
        If True, annualizes the rolling returns based on the frequency of the data. For daily data,
        assumes 252 trading days in a year, and for monthly data, 12 months in a year.

    Returns:
    -------
    pd.DataFrame
        A DataFrame of the same shape as 'prices', containing the rolling returns over the
        specified period. If 'annualize' is True, these returns are annualized.

    Examples:
    --------
    >>> prices = pd.DataFrame({'AAPL': [100, 105, 110, 120, 115]})
    >>> rolling_returns(prices, 2)
        AAPL
    1  0.050
    2  0.048
    3  0.091
    4 -0.042

    >>> rolling_returns(prices, 2, annualize=False)
        AAPL
    1  0.050
    2  0.048
    3  0.091
    4 -0.042
    """

    if not frequency:
        # infer the frequency
        frequency = pd.infer_freq(prices.index)

    # calculate the rolling returns based on the period
    df_rolling = prices.pct_change(period).dropna()

    if annualize:
        match frequency:
            case 'D':
                periods_per_year = 252

            case 'M':
                periods_per_year = 12

        df_rolling = (1 + df_rolling) ** (periods_per_year / period) - 1

    return df_rolling


def tear_sheet(returns: pd.DataFrame, start_date: str, end_date: str,
               exchange: str = 'NYSE', frequency: str = 'D', annualize: bool = True,
               benchmark_col: str = 'SPY_Return') -> pd.DataFrame:
    """
    Generates a tear sheet containing key performance statistics for a given set of strategy returns.

    The tear sheet includes trailing returns, calendar returns, Sharpe ratio, Sortino ratio,
    maximum drawdown, best/worst periods, and the percentage of rolling 3-year periods
    where the strategy outperforms the benchmark.

    Parameters:
    ----------
    returns : pd.DataFrame
        A DataFrame containing the strategy returns and benchmark returns.
        Each column represents a different return series.

    start_date : pd.Timestamp
        The start date for the analysis period.

    end_date : pd.Timestamp
        The end date for the analysis period.

    exchange : str, optional
        The exchange code (default is 'NYSE').

    frequency : str, optional
        The frequency of the returns (default is 'D' for daily).

    annualize : bool, optional
        Whether to annualize the returns for calculations (default is True).

    benchmark_col : str, optional
        The column name of the benchmark returns (default is 'SPY_Return').

    Returns:
    -------
    pd.DataFrame
        A DataFrame containing various performance statistics for the strategy returns, including:
        - Trailing returns
        - Calendar returns
        - Sharpe and Sortino ratios
        - Maximum drawdown
        - Best/worst 12-month and 36-month periods
        - Percentage of 3-year rolling periods where the strategy outperforms the benchmark
    """

    # check if both start_date and end_date are supplied as strings or datetime
    if isinstance(start_date, str):
        start_date = pd.to_datetime(start_date)
    if isinstance(end_date, str):
        end_date = pd.to_datetime(end_date)

    # 1) Annualized Return
    df_annualized_return = annualize_returns(returns, frequency).T

    # 2) Annualized Volatility
    df_annualized_vol = annualize_vol(returns, frequency).T

    # 3) Trailing returns for the specified period
    df_trailing_returns = trailing_returns(returns, start_date, end_date, frequency, exchange, annualize).T

    # 4) Calendar year returns for each series
    df_calendar_returns = calendar_returns(returns, frequency).T

    # 5) Sharpe Ratio, using BIL (proxy for risk-free rate) as the benchmark
    df_sharpe = sharpe_ratio(returns, 'BIL', datetime.strftime(start_date - MonthEnd(), '%Y-%m-%d'),
                                datetime.strftime(end_date, '%Y-%m-%d'), frequency).T

    # 6) Sortino Ratio, assuming the target return is 0 (focus on downside risk)
    df_sortino = sortino_ratio(returns, 0, 'M').T

    # 7) Maximum drawdown for each series
    df_max_drawdown = max_drawdown(returns).T

    # 8) Best and Worst periods (12 months and 36 months)
    df_best_worst_12 = best_worst_periods(returns, 12).T
    df_best_worst_36 = best_worst_periods(returns, 36).T

    # 9) Percentage of rolling 3-year periods where the strategy outperforms the benchmark
    df_cgi = compound_growth_index(returns, frequency, exchange, 100)
    df_rolling_returns = rolling_returns(df_cgi, 36, frequency, annualize)
    df_stats = pd.DataFrame()

    # Iterate over each column in the DataFrame to compute statistics
    for col in df_rolling_returns.columns:
        # Calculate the percentage of rolling periods where the column's return exceeds the benchmark's return
        percent_greater = (df_rolling_returns[col] > df_rolling_returns[benchmark_col]).sum() / df_rolling_returns.shape[0]

        # Store the result in a new DataFrame, converting to percentage and rounding to 2 decimal places
        df_stats.loc[col, '% 3-Year Periods Greater Than Benchmark'] = np.round(percent_greater * 100, 2)

    # Concatenate all statistics into a final summary table
    df_summary = pd.concat([df_annualized_return, df_annualized_vol, df_trailing_returns, df_calendar_returns,
                            df_sharpe, df_sortino, df_max_drawdown, df_best_worst_12, df_best_worst_36, df_stats.T],
                            axis=0)

    return df_summary
    

def cumulative_return_graph(returns, start_date, end_date):
    """
    Generates a line graph showing the cumulative return of investments over time.

    This function calculates the cumulative return from a DataFrame of periodic returns
    and plots it as a percentage. The cumulative return is the total change in the investment
    value from the beginning of the time series.

    Parameters:
    returns (pd.DataFrame): A DataFrame where each column represents a different investment
                            and contains their respective periodic returns.
    start_date (str): Start date of the time period to consider for the returns.
    end_date (str): End date of the time period to consider for the returns.

    Returns:
    None: The function outputs the graph directly and does not return any value.

    Note:
    The function assumes the input DataFrame's index represents dates and the columns represent
    different investment securities or portfolios.
    """

    # truncate the returns to the required timeframe
    returns = returns.loc[start_date:end_date]

    # Calculate cumulative returns in percentage
    df_cumulative = ((1 + returns).cumprod() - 1).mul(100)

    # Create a figure and axis for the plot
    fig, ax = plt.subplots(figsize=(10, 6))

    # Plot the cumulative returns using Seaborn's lineplot
    ax = sns.lineplot(data=df_cumulative, dashes=False)

    # Format the y-axis to show percentages without scientific notation
    ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
    ax.yaxis.get_major_formatter().set_scientific(False)
    ax.yaxis.get_major_formatter().set_useOffset(False)

    # Display minor gridlines for better readability
    ax.grid(visible=True, which='minor')

    # Set labels and title for the plot
    plt.xlabel('Date')
    plt.ylabel('Cumulative Return (%)')
    plt.title('Cumulative Percent Returns')

    # remove the frame around the axis
    ax.legend(frameon=False)

    # Add thousands separator for y-axis labels
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(format_thousands))

    # Apply layout adjustments and display the plot
    fig.tight_layout()
    plt.show()


def format_thousands(x, pos):
    """
    Formats y-axis in seaborn plots to thousands
    """
    return '{:,.0f}'.format(x)
    

def market_capture_ratio(returns: pd.DataFrame, start_date, end_date, frequency: str = 'M'):
    '''
    Calculate the up market capture ratio based on monthly returns
    '''

    df_final = pd.DataFrame()

    # convert to monthly
    if frequency == ' D':
        df_monthly = convert_to_monthly(returns, frequency='D')
    else:
        df_monthly = returns.copy()

    for i in range(0, len(df_monthly.columns), 2):
        # create a temp df with the long etf and its index
        df_temp = df_monthly.iloc[:, i:i + 2]  # STOPPED HERE. NEED TO ISOLATE THE COLUMNS
        # get the names of the long_etf and the index
        long_etf = df_temp.columns[0]
        index = df_temp.columns[1]
        up_market = (df_temp[df_temp[index]
                             >= 0]).sum(axis=0)

        up_ratio = (up_market / up_market.iloc[-1] * 100) \
            .round(2)

        up_ratio = up_ratio.to_frame(name='Up Market Capture Ratio')

        # 3) down ratio

        down_market = (df_temp[df_temp[index]
                               < 0]).sum(axis=0)

        down_ratio = (down_market / down_market.iloc[-1] * 100) \
            .round(2)

        down_ratio = down_ratio.to_frame(name='Down Market Capture Ratio')

        # create a final df
        df_ratio = pd.concat([up_ratio, down_ratio], axis=1)
        df_ratio.index = [long_etf, index]

        df_final = pd.concat([df_final, df_ratio])

    return df_final


def seasonal_heatmap(df_returns, ticker=None, absolute=True, benchmark=None, frequency='M'):
    """
    Create a heatmap of returns, supporting both absolute and relative returns.

    Parameters:
        df_returns (DataFrame): DataFrame containing returns.
            Index should be DateTimeIndex.
        ticker (str, optional): Ticker to highlight.
            Required if absolute=False and multiple columns exist.
        absolute (bool, default=True):
            - If True, show absolute returns
            - If False, show relative returns compared to benchmark
        benchmark (str, optional): Benchmark ticker for relative returns.
            Required if absolute=False and multiple columns exist.
        frequency (str, default='M'):
            Frequency to resample the data. 'M' for monthly, 'Q' for quarterly.

    Returns:
        None (displays the heatmap)

    Raises:
        ValueError: If parameters are inconsistent or invalid
    """
    # Validate inputs
    if len(df_returns.columns) > 1:
        if not absolute:
            if ticker is None or benchmark is None:
                raise ValueError("When absolute=False with multiple columns, "
                                 "both 'ticker' and 'benchmark' must be specified.")
            if ticker not in df_returns.columns or benchmark not in df_returns.columns:
                raise ValueError("Specified ticker or benchmark not found in DataFrame columns.")
    elif len(df_returns.columns) == 1:
        # If only one column, use that as ticker if not specified
        ticker = ticker or df_returns.columns[0]
        if not absolute and benchmark is None:
            raise ValueError("Benchmark must be specified for relative returns.")

    # Prepare data for visualization
    if frequency == 'M':
        if absolute:
            # Use first column if only one exists, otherwise use specified ticker
            column_to_use = ticker if ticker else df_returns.columns[0]
            pivot_df = df_returns.pivot_table(
                index=df_returns.index.year,
                columns=df_returns.index.month,
                values=column_to_use,
                aggfunc='sum'
            )
            pivot_df = pivot_df.reindex(columns=range(1, 13))
            x_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            title = f'{column_to_use} Returns Heatmap (%)' if ticker else 'Returns Heatmap (%)'
        else:
            # Relative returns calculation
            # Create a new DataFrame for relative returns
            relative_returns = pd.DataFrame({
                'relative_returns': df_returns[ticker] - df_returns[benchmark]
            })
            pivot_df = relative_returns.groupby([
                relative_returns.index.year,
                relative_returns.index.month
            ])['relative_returns'].sum().unstack()
            pivot_df = pivot_df.reindex(columns=range(1, 13))
            x_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            title = f'{ticker} Relative to {benchmark} Returns Heatmap (%)'

    elif frequency == 'Q':
        # Resample to quarterly returns
        if absolute:
            # Resample and use first column or specified ticker
            column_to_use = ticker if ticker else df_returns.columns[0]
            df_returns_qtr = df_returns.resample('Q')[column_to_use].sum()
            pivot_df = df_returns_qtr.groupby([
                df_returns_qtr.index.year,
                df_returns_qtr.index.quarter
            ]).first().unstack()
            pivot_df = pivot_df.reindex(columns=[1, 2, 3, 4])
            x_labels = ['Q1', 'Q2', 'Q3', 'Q4']
            title = f'{column_to_use} Returns Heatmap (%)' if ticker else 'Returns Heatmap (%)'
        else:
            # Relative quarterly returns
            df_returns_qtr = df_returns.resample('Q').sum()
            relative_returns = pd.DataFrame({
                'relative_returns': df_returns_qtr[ticker] - df_returns_qtr[benchmark]
            })
            pivot_df = relative_returns.groupby([
                relative_returns.index.year,
                relative_returns.index.quarter
            ])['relative_returns'].first().unstack()
            pivot_df = pivot_df.reindex(columns=[1, 2, 3, 4])
            x_labels = ['Q1', 'Q2', 'Q3', 'Q4']
            title = f'{ticker} Relative to {benchmark} Returns Heatmap (%)'

    else:
        raise ValueError("Invalid frequency. Choose 'M' for monthly or 'Q' for quarterly.")

    # Visualization setup
    cmap_colors = [(1, 0, 0),  # dark red
                   (1, 0.5, 0.5),  # light red
                   (0.5, 1, 0.5),  # light green
                   (0, 1, 0)]  # dark green
    cmap = sns.mpl.colors.ListedColormap(cmap_colors)

    # Find the maximum absolute value for normalization
    vmax = pivot_df.abs().max().max() * 100
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)

    # Create the heatmap
    fig, ax = plt.subplots(figsize=(12, 8))
    heatmap = sns.heatmap(pivot_df[::-1].mul(100).round(2),
                          cmap=cmap,
                          annot=True,
                          fmt=".2f",
                          linewidths=.5,
                          ax=ax,
                          norm=norm)  # Invert y-axis with [::-1]

    # Formatting
    ax.set_title(title)
    ax.set_xlabel('Month' if frequency == 'M' else 'Quarter')
    ax.set_ylabel('Year')
    ax.set_xticks(np.arange(len(x_labels)) + 0.5)
    ax.set_xticklabels(x_labels)
    ax.tick_params(axis='x', pad=10)  # Adjust the padding between ticks and labels
    plt.yticks(rotation=0)
    plt.show()
