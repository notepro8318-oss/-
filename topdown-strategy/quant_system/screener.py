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
    MOM_12_1_START, MOM_12_1_END, ROC_63_LOOKBACK, ROC_21_LOOKBACK,
    MOM_WEIGHT_121, MOM_WEIGHT_ROC63, MOM_WEIGHT_ROC21,
    COMPOSITE_WEIGHT_MOMENTUM, COMPOSITE_WEIGHT_NEAR_HIGH, COMPOSITE_WEIGHT_MRS,
    TOP_STOCK_COUNT,
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


class MomentumRanker:
    """[v2] Hard AND 필터 대신 12-1M 모멘텀 + 신고가근접도 + MRS를 가중 합산한
    Cross-Sectional 랭킹으로 매주 상위 종목을 선정하는 엔진."""

    def __init__(self, top_n: int = TOP_STOCK_COUNT,
                 mom_start: int = MOM_12_1_START, mom_end: int = MOM_12_1_END,
                 roc63: int = ROC_63_LOOKBACK, roc21: int = ROC_21_LOOKBACK,
                 lookback_52w: int = LOOKBACK_52W, mrs_lookback: int = MRS_LOOKBACK) -> None:
        self.top_n = top_n
        self.mom_start = mom_start
        self.mom_end = mom_end
        self.roc63 = roc63
        self.roc21 = roc21
        self.lookback_52w = lookback_52w
        self.mrs_lookback = mrs_lookback

    def compute_snapshot(self, stock_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> dict | None:
        """한 종목의 최근 거래일 기준 모멘텀/52주위치/MRS 스냅샷을 계산한다."""
        df = stock_df.copy()
        close = df["Close"]
        if len(close) <= self.mom_start:
            return None

        mom_121 = (close.shift(self.mom_end) - close.shift(self.mom_start)) / close.shift(self.mom_start)
        roc_63 = close.pct_change(self.roc63)
        roc_21 = close.pct_change(self.roc21)
        high_52w = df["High"].rolling(self.lookback_52w, min_periods=60).max()

        bench_close = benchmark_df["Close"].reindex(df.index).ffill()
        ratio = close / bench_close
        mrs = (ratio / ratio.rolling(self.mrs_lookback, min_periods=60).mean() - 1.0) * 100.0

        last = -1
        if pd.isna(mom_121.iloc[last]) or pd.isna(high_52w.iloc[last]) or pd.isna(mrs.iloc[last]):
            return None

        return {
            "close": float(close.iloc[last]),
            "mom_121": float(mom_121.iloc[last]),
            "roc_63": float(roc_63.iloc[last]) if not pd.isna(roc_63.iloc[last]) else 0.0,
            "roc_21": float(roc_21.iloc[last]) if not pd.isna(roc_21.iloc[last]) else 0.0,
            "near_high_ratio": float(close.iloc[last] / high_52w.iloc[last]),
            "mrs": float(mrs.iloc[last]),
        }

    def rank_candidates(self, candidates: dict[str, pd.DataFrame], benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """후보 종목들의 CompositeScore를 계산해 오름차순(우수 순)으로 정렬한다.

        MomentumScore = 0.5*Rank(Mom_12_1) + 0.3*Rank(ROC_63) + 0.2*Rank(ROC_21)
        CompositeScore = 0.4*Rank(MomentumScore) + 0.3*Rank(신고가근접도) + 0.3*Rank(MRS)
        """
        rows = []
        for ticker, df in candidates.items():
            snap = self.compute_snapshot(df, benchmark_df)
            if snap is None:
                continue
            snap["ticker"] = ticker
            rows.append(snap)

        if not rows:
            return pd.DataFrame()

        result = pd.DataFrame(rows).set_index("ticker")
        result["rank_mom121"] = result["mom_121"].rank(ascending=False, method="min")
        result["rank_roc63"] = result["roc_63"].rank(ascending=False, method="min")
        result["rank_roc21"] = result["roc_21"].rank(ascending=False, method="min")
        result["momentum_score"] = (
            MOM_WEIGHT_121 * result["rank_mom121"]
            + MOM_WEIGHT_ROC63 * result["rank_roc63"]
            + MOM_WEIGHT_ROC21 * result["rank_roc21"]
        )

        result["rank_momentum"] = result["momentum_score"].rank(ascending=True, method="min")
        result["rank_near_high"] = result["near_high_ratio"].rank(ascending=False, method="min")
        result["rank_mrs"] = result["mrs"].rank(ascending=False, method="min")

        result["composite_score"] = (
            COMPOSITE_WEIGHT_MOMENTUM * result["rank_momentum"]
            + COMPOSITE_WEIGHT_NEAR_HIGH * result["rank_near_high"]
            + COMPOSITE_WEIGHT_MRS * result["rank_mrs"]
        )
        return result.sort_values("composite_score")

    def select_top_stocks(self, candidates: dict[str, pd.DataFrame], benchmark_df: pd.DataFrame) -> list[str]:
        """CompositeScore 상위 top_n 종목의 티커 리스트를 반환한다."""
        ranked = self.rank_candidates(candidates, benchmark_df)
        if ranked.empty:
            return []
        return ranked.head(self.top_n).index.tolist()
