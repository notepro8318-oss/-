"""
[3단계] 강세 종목 발굴 (Stock Selection / Relative Strength)
====================================================
- 대상: 2단계에서 선정된 주도 섹터 ETF의 대표 구성 종목(config.SECTOR_STOCKS)
- 필터링 조건:
    1) 정배열: 현재가 > MA20 > MA50 > MA200
    2) 신고가 근접: 현재가 >= 52주 신고가 * 0.9
    3) 상대강도(RS): 최근 1개월(20거래일) 수익률이 소속 섹터 ETF 수익률을 상회(Outperform)
"""

import pandas as pd

from config import (
    SECTOR_STOCKS, STOCK_MA_SHORT, STOCK_MA_MID, STOCK_MA_LONG,
    NEAR_52W_HIGH_RATIO, RS_LOOKBACK,
)
from market import fetch_price_history
from sector import _period_return


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA20"] = df["Close"].rolling(STOCK_MA_SHORT).mean()
    df["MA50"] = df["Close"].rolling(STOCK_MA_MID).mean()
    df["MA200"] = df["Close"].rolling(STOCK_MA_LONG).mean()
    df["High52W"] = df["Close"].rolling(252, min_periods=60).max()
    return df


def evaluate_stock(ticker: str, sector_ret_1m: float) -> dict | None:
    """
    개별 종목에 3단계 필터를 적용한다.
    조건을 모두 만족하지 못하면 None을 반환한다.
    """
    df = fetch_price_history(ticker, period="2y")
    if len(df) < STOCK_MA_LONG:
        return None  # 200일선 계산에 데이터 부족

    df = _compute_indicators(df).dropna(subset=["MA20", "MA50", "MA200", "High52W"])
    if df.empty:
        return None

    last = df.iloc[-1]
    close = float(last["Close"])
    ma20, ma50, ma200 = float(last["MA20"]), float(last["MA50"]), float(last["MA200"])
    high_52w = float(last["High52W"])

    # 조건 1: 정배열
    is_aligned = close > ma20 > ma50 > ma200

    # 조건 2: 52주 신고가 근접(-10% 이내)
    is_near_high = close >= high_52w * NEAR_52W_HIGH_RATIO

    # 조건 3: 상대강도(RS) - 종목의 1개월 수익률이 섹터 ETF 수익률을 상회
    stock_ret_1m = _period_return(df["Close"], RS_LOOKBACK)
    outperforms = (not pd.isna(stock_ret_1m)) and (not pd.isna(sector_ret_1m)) and (stock_ret_1m > sector_ret_1m)

    passed = bool(is_aligned and is_near_high and outperforms)
    if not passed:
        return None

    return {
        "ticker": ticker,
        "close": close,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "high_52w": high_52w,
        "stock_ret_1m": stock_ret_1m,
        "sector_ret_1m": sector_ret_1m,
        "df": df,  # 4단계 진입 로직에서 재사용
    }


def scan_leading_stocks(leading_sectors: list, sector_returns: dict) -> list:
    """
    2단계에서 뽑힌 주도 섹터들의 대표 종목을 순회하며 3단계 필터를 통과한 종목 리스트를 반환한다.

    Parameters
    ----------
    leading_sectors : list[str]  -- 주도 섹터 ETF 티커
    sector_returns : dict[str, float] -- {ETF: 1개월 수익률(%)}
    """
    candidates = []
    seen = set()
    for etf in leading_sectors:
        for ticker in SECTOR_STOCKS.get(etf, []):
            if ticker in seen:
                continue
            seen.add(ticker)
            result = evaluate_stock(ticker, sector_returns.get(etf, float("nan")))
            if result:
                result["sector"] = etf
                candidates.append(result)
    return candidates


if __name__ == "__main__":
    from sector import rank_sectors, select_leading_sectors

    ranked = rank_sectors()
    leaders = select_leading_sectors()
    sector_returns = ranked["ret_1m"].to_dict()

    print(f"[주도 섹터]: {leaders}")
    picks = scan_leading_stocks(leaders, sector_returns)
    print(f"\n[3단계 통과 종목] {len(picks)}개")
    for p in picks:
        print(f"  {p['ticker']:6s} 섹터={p['sector']:5s} 현재가={p['close']:.2f} "
              f"MA20/50/200={p['ma20']:.2f}/{p['ma50']:.2f}/{p['ma200']:.2f} "
              f"52W고점={p['high_52w']:.2f} 종목1M={p['stock_ret_1m']:.1f}% 섹터1M={p['sector_ret_1m']:.1f}%")
