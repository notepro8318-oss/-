"""
[1단계] 시장 추세 확인 (Market Filter)
====================================================
- 대상: SPY (또는 QQQ)
- 조건: 일봉 기준 MA20이 우상향(현재 MA20 > N일 전 MA20)하고,
        현재 종가가 MA20 위에 있을 때만 '매수 가능(Bull)' 상태로 판별한다.
- 역배열/하향 추세일 경우 매매 중단(Cash 100%) 신호를 반환한다.
"""

import yfinance as yf
import pandas as pd

from config import MARKET_INDEX, MARKET_MA_PERIOD, MARKET_MA_LOOKBACK


def fetch_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """yfinance로 일봉 OHLCV 데이터를 내려받는다."""
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        # 단일 티커라도 최신 yfinance는 MultiIndex 컬럼을 반환하는 경우가 있어 평탄화
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df


def check_market_trend(ticker: str = MARKET_INDEX) -> dict:
    """
    1단계 시장 필터를 계산한다.

    Returns
    -------
    dict: {
        "ticker": str,
        "is_bull": bool,       # 매매 가능 여부
        "close": float,
        "ma20": float,
        "ma20_prev": float,
    }
    """
    df = fetch_price_history(ticker, period="6mo")
    df["MA20"] = df["Close"].rolling(MARKET_MA_PERIOD).mean()
    df = df.dropna(subset=["MA20"])

    if len(df) <= MARKET_MA_LOOKBACK:
        raise ValueError(f"{ticker}: MA20 계산에 필요한 데이터가 부족합니다.")

    last = df.iloc[-1]
    prev = df.iloc[-1 - MARKET_MA_LOOKBACK]

    ma20_now = float(last["MA20"])
    ma20_prev = float(prev["MA20"])
    close_now = float(last["Close"])

    # 우상향(MA20 기울기 > 0) + 종가가 MA20 위 => 강세(Bull) 판정
    ma_rising = ma20_now > ma20_prev
    price_above_ma = close_now > ma20_now
    is_bull = bool(ma_rising and price_above_ma)

    return {
        "ticker": ticker,
        "is_bull": is_bull,
        "close": close_now,
        "ma20": ma20_now,
        "ma20_prev": ma20_prev,
    }


if __name__ == "__main__":
    result = check_market_trend()
    state = "BULL (매매 가능)" if result["is_bull"] else "BEAR/중립 (매매 중단, Cash 100%)"
    print(f"[시장필터] {result['ticker']} 종가={result['close']:.2f} "
          f"MA20={result['ma20']:.2f} (5일전 {result['ma20_prev']:.2f}) => {state}")
