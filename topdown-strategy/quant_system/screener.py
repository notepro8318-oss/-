"""
[Module 3] StockScreener
====================================================
정배열(Stage 2) / 52주 신고가-저가 위치 / 맨스필드 상대강도(MRS) 3개 조건을
AND로 검증해 종목을 압축한다.
"""

from __future__ import annotations

import pandas as pd

from config import (
    STOCK_MA_SHORT, STOCK_MA_MID, STOCK_MA_LONG,
    MA_LONG_TREND_LOOKBACK, MA_LONG_TREND_MIN_PCT,
    LOOKBACK_52W, NEAR_52W_HIGH_RATIO, ABOVE_52W_LOW_RATIO, MRS_LOOKBACK,
)


class StockScreener:
    """정배열/52주 위치/맨스필드 RS 조건으로 종목을 스크리닝하는 엔진."""

    def __init__(self, ma_short: int = STOCK_MA_SHORT, ma_mid: int = STOCK_MA_MID,
                 ma_long: int = STOCK_MA_LONG, trend_lookback: int = MA_LONG_TREND_LOOKBACK,
                 trend_min_pct: float = MA_LONG_TREND_MIN_PCT, lookback_52w: int = LOOKBACK_52W,
                 near_high_ratio: float = NEAR_52W_HIGH_RATIO,
                 above_low_ratio: float = ABOVE_52W_LOW_RATIO,
                 mrs_lookback: int = MRS_LOOKBACK) -> None:
        self.ma_short = ma_short
        self.ma_mid = ma_mid
        self.ma_long = ma_long
        self.trend_lookback = trend_lookback
        self.trend_min_pct = trend_min_pct
        self.lookback_52w = lookback_52w
        self.near_high_ratio = near_high_ratio
        self.above_low_ratio = above_low_ratio
        self.mrs_lookback = mrs_lookback

    def compute_indicators(self, stock_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """스크리닝에 필요한 이동평균/52주 고저/MRS 지표를 계산한다."""
        df = stock_df.copy()
        df["MA_S"] = df["Close"].rolling(self.ma_short).mean()
        df["MA_M"] = df["Close"].rolling(self.ma_mid).mean()
        df["MA_L"] = df["Close"].rolling(self.ma_long).mean()
        df["MA_L_prev"] = df["MA_L"].shift(self.trend_lookback)
        df["High52W"] = df["High"].rolling(self.lookback_52w, min_periods=60).max()
        df["Low52W"] = df["Low"].rolling(self.lookback_52w, min_periods=60).min()

        bench_close = benchmark_df["Close"].reindex(df.index).ffill()
        ratio = df["Close"] / bench_close
        df["MRS"] = (ratio / ratio.rolling(self.mrs_lookback, min_periods=60).mean() - 1.0) * 100.0
        return df

    def passes_alignment(self, row: pd.Series) -> bool:
        """정배열(Minervini Stage 2) + MA120 상승 지속성 조건."""
        close, ma_s, ma_m, ma_l = row["Close"], row["MA_S"], row["MA_M"], row["MA_L"]
        if pd.isna(ma_l) or pd.isna(row["MA_L_prev"]):
            return False
        aligned = close > ma_s > ma_m > ma_l
        trend_pct = (ma_l - row["MA_L_prev"]) / row["MA_L_prev"] * 100.0
        return bool(aligned and trend_pct >= self.trend_min_pct)

    def passes_52w_position(self, row: pd.Series) -> bool:
        """52주 신고가 근접(-15% 이내) + 저점 대비 +30% 이상 상승."""
        if pd.isna(row["High52W"]) or pd.isna(row["Low52W"]) or row["Low52W"] <= 0:
            return False
        near_high = (row["Close"] / row["High52W"]) >= self.near_high_ratio
        above_low = (row["Close"] / row["Low52W"]) >= self.above_low_ratio
        return bool(near_high and above_low)

    def passes_mrs(self, row: pd.Series) -> bool:
        """맨스필드 상대강도(MRS) > 0."""
        return bool(not pd.isna(row["MRS"]) and row["MRS"] > 0)

    def screen(self, stock_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> dict | None:
        """가장 최근 거래일 기준으로 3개 조건을 모두 통과하는지 검사한다.

        Returns
        -------
        dict | None
            통과 시 지표 스냅샷 dict, 미통과 시 None.
        """
        df = self.compute_indicators(stock_df, benchmark_df)
        df = df.dropna(subset=["MA_L", "MA_L_prev", "High52W", "Low52W"])
        if df.empty:
            return None

        row = df.iloc[-1]
        if self.passes_alignment(row) and self.passes_52w_position(row) and self.passes_mrs(row):
            return {
                "close": float(row["Close"]),
                "ma20": float(row["MA_S"]),
                "ma60": float(row["MA_M"]),
                "ma120": float(row["MA_L"]),
                "high_52w": float(row["High52W"]),
                "low_52w": float(row["Low52W"]),
                "mrs": float(row["MRS"]),
            }
        return None
