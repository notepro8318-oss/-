"""
[Module 1] MarketRegimeDetector
====================================================
벤치마크 지수(SPY 등) 일봉을 기반으로 매 영업일 국면(BULL/SIDEWAYS/BEAR)을
2거래일 연속 확인 규칙으로 판정한다.
"""

from __future__ import annotations

import pandas as pd

from config import (
    MA_SHORT, MA_LONG, SLOPE_LOOKBACK, SLOPE_THRESHOLD, BAND_PCT,
    CONFIRM_DAYS, REGIME_EXPOSURE,
)


class MarketRegimeDetector:
    """벤치마크 데이터로부터 시장 국면을 분류하는 엔진."""

    def __init__(self, ma_short: int = MA_SHORT, ma_long: int = MA_LONG,
                 slope_lookback: int = SLOPE_LOOKBACK, slope_threshold: float = SLOPE_THRESHOLD,
                 band_pct: float = BAND_PCT, confirm_days: int = CONFIRM_DAYS) -> None:
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.slope_lookback = slope_lookback
        self.slope_threshold = slope_threshold
        self.band_pct = band_pct
        self.confirm_days = confirm_days

    def compute_indicators(self, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """MA20/MA200/Slope5d(MA20)를 계산해 지표 컬럼을 추가한다.

        Parameters
        ----------
        benchmark_df : pd.DataFrame
            columns=[Open, High, Low, Close, Volume]

        Returns
        -------
        pd.DataFrame
            원본 + [MA20, MA200, Slope5d] 컬럼이 추가된 DataFrame.
        """
        df = benchmark_df.copy()
        df["MA20"] = df["Close"].rolling(self.ma_short).mean()
        df["MA200"] = df["Close"].rolling(self.ma_long).mean()
        df["Slope5d"] = (df["MA20"] - df["MA20"].shift(self.slope_lookback)) / df["MA20"].shift(self.slope_lookback) * 100.0
        return df

    def _raw_regime(self, row: pd.Series) -> str:
        """2일 연속 확인 이전의 단일 거래일 원시(raw) 국면 판정."""
        close, ma20, ma200, slope = row["Close"], row["MA20"], row["MA200"], row["Slope5d"]
        if pd.isna(ma20) or pd.isna(ma200) or pd.isna(slope):
            return "SIDEWAYS"

        is_bull = (close >= ma20 * (1 + self.band_pct)) and (slope >= self.slope_threshold) and (close >= ma200)
        is_bear = (close <= ma20 * (1 - self.band_pct)) and (slope <= -self.slope_threshold) and (close < ma200)

        if is_bull:
            return "BULL"
        if is_bear:
            return "BEAR"
        return "SIDEWAYS"

    def classify(self, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """전체 기간에 대해 국면(Regime)과 노출 비중(Exposure)을 계산한다.

        2거래일 연속 BULL/BEAR raw 조건을 만족해야 해당 국면이 확정되며,
        그렇지 않으면 SIDEWAYS로 분류한다.

        Returns
        -------
        pd.DataFrame
            원본 + [MA20, MA200, Slope5d, RawRegime, Regime, Exposure] 컬럼.
        """
        df = self.compute_indicators(benchmark_df)
        df["RawRegime"] = df.apply(self._raw_regime, axis=1)

        confirmed = []
        prev_raw = None
        for raw in df["RawRegime"]:
            if raw in ("BULL", "BEAR") and raw == prev_raw:
                confirmed.append(raw)
            else:
                confirmed.append("SIDEWAYS")
            prev_raw = raw
        df["Regime"] = confirmed
        df["Exposure"] = df["Regime"].map(REGIME_EXPOSURE)
        return df

    def current_regime(self, benchmark_df: pd.DataFrame) -> dict:
        """가장 최근 거래일의 국면 스냅샷을 반환한다."""
        df = self.classify(benchmark_df)
        last = df.iloc[-1]
        return {
            "date": df.index[-1],
            "regime": last["Regime"],
            "exposure": last["Exposure"],
            "close": float(last["Close"]),
            "ma20": float(last["MA20"]),
            "ma200": float(last["MA200"]),
            "slope5d": float(last["Slope5d"]),
        }


if __name__ == "__main__":
    from data import fetch_ohlcv
    from config import BENCHMARK

    spy = fetch_ohlcv(BENCHMARK, period="2y")
    detector = MarketRegimeDetector()
    snapshot = detector.current_regime(spy)
    print(f"[{BENCHMARK}] {snapshot['date'].date()} Close={snapshot['close']:.2f} "
          f"MA20={snapshot['ma20']:.2f} MA200={snapshot['ma200']:.2f} "
          f"Slope5d={snapshot['slope5d']:.3f}% => Regime={snapshot['regime']} "
          f"(Exposure={snapshot['exposure']*100:.0f}%)")
