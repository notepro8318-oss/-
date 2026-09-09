"""
[2단계] 주도 섹터 선정 (Sector Momentum)
====================================================
- 대상 ETF: QQQ, SOXX, IGV, XLF, XLE, XLV, XLY, XLI
- 조건: 1주일(5거래일), 1개월(20거래일) 수익률을 계산하여
        두 기간 모두 수익률 상위 1~2위에 해당하는 ETF를 '주도 섹터'로 추출한다.
"""

import pandas as pd

from config import SECTOR_ETFS, SECTOR_RET_SHORT, SECTOR_RET_LONG, TOP_SECTOR_COUNT
from market import fetch_price_history


def _period_return(close: pd.Series, lookback: int) -> float:
    """N거래일 전 종가 대비 현재 종가 수익률(%)."""
    if len(close) <= lookback:
        return float("nan")
    return float((close.iloc[-1] / close.iloc[-1 - lookback] - 1.0) * 100.0)


def rank_sectors(etf_list=None) -> pd.DataFrame:
    """
    섹터 ETF별 1주일/1개월 수익률과 순위를 계산한다.

    Returns
    -------
    pd.DataFrame: index=ticker, columns=[ret_1w, ret_1m, rank_1w, rank_1m]
    """
    etf_list = etf_list or SECTOR_ETFS
    rows = []
    for etf in etf_list:
        df = fetch_price_history(etf, period="3mo")
        if df.empty:
            continue
        close = df["Close"]
        rows.append({
            "ticker": etf,
            "ret_1w": _period_return(close, SECTOR_RET_SHORT),
            "ret_1m": _period_return(close, SECTOR_RET_LONG),
        })

    result = pd.DataFrame(rows).set_index("ticker")
    result["rank_1w"] = result["ret_1w"].rank(ascending=False, method="min")
    result["rank_1m"] = result["ret_1m"].rank(ascending=False, method="min")
    return result.sort_values("rank_1m")


def select_leading_sectors(top_n: int = TOP_SECTOR_COUNT) -> list:
    """1주일/1개월 수익률 모두 상위 top_n에 드는 '주도 섹터' 티커 리스트를 반환한다."""
    ranked = rank_sectors()
    leaders = ranked[(ranked["rank_1w"] <= top_n) & (ranked["rank_1m"] <= top_n)]
    return leaders.index.tolist()


if __name__ == "__main__":
    ranked = rank_sectors()
    print("[섹터 모멘텀 순위]")
    print(ranked.round(2))

    leaders = select_leading_sectors()
    print(f"\n[주도 섹터] (1주일/1개월 모두 상위 {TOP_SECTOR_COUNT}위 이내): {leaders}")
