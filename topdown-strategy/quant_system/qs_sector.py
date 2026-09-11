"""
[Module 2] SectorRotationEngine
====================================================
GICS 11개 섹터 ETF의 주간 가중 복합 순위 점수를 산출하여 주도 섹터를 선정한다.
Score_Rank = 0.3 * Rank_1W + 0.7 * Rank_1M (낮을수록 우수)
"""

from __future__ import annotations

import pandas as pd

from qs_config import (
    SECTOR_ETFS, DEFENSIVE_ETFS, ROC_SHORT, ROC_LONG,
    RANK_WEIGHT_SHORT, RANK_WEIGHT_LONG, TOP_SECTOR_COUNT,
)
from data import fetch_ohlcv


class SectorRotationEngine:
    """섹터 ETF 모멘텀을 랭킹하여 국면별 주도 섹터를 선정하는 엔진."""

    def __init__(self, etfs: list[str] = None, defensive_etfs: list[str] = None,
                 roc_short: int = ROC_SHORT, roc_long: int = ROC_LONG,
                 w_short: float = RANK_WEIGHT_SHORT, w_long: float = RANK_WEIGHT_LONG,
                 top_n: int = TOP_SECTOR_COUNT) -> None:
        self.etfs = etfs or SECTOR_ETFS
        self.defensive_etfs = defensive_etfs or DEFENSIVE_ETFS
        self.roc_short = roc_short
        self.roc_long = roc_long
        self.w_short = w_short
        self.w_long = w_long
        self.top_n = top_n

    @staticmethod
    def _roc(close: pd.Series, lookback: int) -> float:
        """ROC_n = (Close_t - Close_{t-n}) / Close_{t-n}."""
        if len(close) <= lookback:
            return float("nan")
        return float((close.iloc[-1] - close.iloc[-1 - lookback]) / close.iloc[-1 - lookback])

    def score_sectors(self, price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """섹터별 ROC_5d/ROC_21d, 순위, 가중 복합 점수(Score_Rank)를 계산한다.

        Parameters
        ----------
        price_data : dict[str, pd.DataFrame]
            {ETF 티커: OHLCV DataFrame}

        Returns
        -------
        pd.DataFrame
            index=ticker, columns=[roc_5d, roc_21d, rank_1w, rank_1m, score].
            score 오름차순(우수 순) 정렬.
        """
        rows = []
        for ticker, df in price_data.items():
            if df is None or df.empty:
                continue
            close = df["Close"]
            rows.append({
                "ticker": ticker,
                "roc_5d": self._roc(close, self.roc_short),
                "roc_21d": self._roc(close, self.roc_long),
            })
        result = pd.DataFrame(rows).set_index("ticker")
        result["rank_1w"] = result["roc_5d"].rank(ascending=False, method="min")
        result["rank_1m"] = result["roc_21d"].rank(ascending=False, method="min")
        result["score"] = self.w_short * result["rank_1w"] + self.w_long * result["rank_1m"]
        return result.sort_values("score")

    def select_leading_sectors(self, price_data: dict[str, pd.DataFrame], regime: str) -> list[str]:
        """국면에 따라 주도 섹터를 선정한다.

        - BULL: 전체 11개 ETF 중 Score_Rank 상위 top_n
        - SIDEWAYS: 방어적 섹터 4개 중 Score_Rank 상위 top_n
        - BEAR: 빈 리스트(Cash 100%)
        """
        if regime == "BEAR":
            return []

        if regime == "SIDEWAYS":
            universe = {t: price_data[t] for t in self.defensive_etfs if t in price_data}
        else:  # BULL (및 그 외 안전 기본값)
            universe = price_data

        scored = self.score_sectors(universe)
        return scored.head(self.top_n).index.tolist()


if __name__ == "__main__":
    engine = SectorRotationEngine()
    data = {etf: fetch_ohlcv(etf, period="6mo") for etf in SECTOR_ETFS}

    ranked = engine.score_sectors(data)
    print("[섹터 복합 순위 점수]")
    print(ranked.round(4))

    for regime in ("BULL", "SIDEWAYS", "BEAR"):
        leaders = engine.select_leading_sectors(data, regime)
        print(f"\n[{regime}] 주도 섹터: {leaders}")
