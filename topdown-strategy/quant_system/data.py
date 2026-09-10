"""
공용 데이터 로더
====================================================
yfinance 일봉 OHLCV를 내려받아 정리된 DataFrame으로 반환한다.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf


def fetch_ohlcv(ticker: str, period: str = "5y", interval: str = "1d") -> pd.DataFrame:
    """yfinance에서 일봉 OHLCV를 내려받는다.

    Parameters
    ----------
    ticker : str
        조회할 티커 심볼.
    period : str
        yfinance period 문자열 (예: "5y", "2y").
    interval : str
        데이터 간격. 기본 일봉.

    Returns
    -------
    pd.DataFrame
        columns=[Open, High, Low, Close, Volume], index=DatetimeIndex, 결측 제거됨.
    """
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()
