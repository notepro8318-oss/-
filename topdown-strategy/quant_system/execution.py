"""
[Module 4a] ExecutionEngine
====================================================
눌림목(Trigger A) / 돌파(Trigger B) 진입 트리거와 ATR 기반 동적 손절/트레일링
스탑을 계산한다.
"""

from __future__ import annotations

import pandas as pd

from qs_config import (
    PULLBACK_BAND_UPPER, PULLBACK_BAND_LOWER, PULLBACK_VOLUME_RATIO, PULLBACK_VOLUME_MA,
    BREAKOUT_LOOKBACK, BREAKOUT_VOLUME_MA, BREAKOUT_VOLUME_RATIO,
    BREAKOUT_ATR_PERIOD, BREAKOUT_CANDLE_ATR_RATIO,
    ATR_PERIOD, STOP_INITIAL_PCT, STOP_ATR_MULT, BREAKEVEN_ATR_TRIGGER, TRAIL_ATR_MULT,
    SIDEWAYS_PARTIAL_ATR_TRIGGER, SIDEWAYS_PARTIAL_FRACTION,
    STOP_ATR_MULT_V2, RUNNER_EXIT_MA_PERIOD,
)


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """ATR(period) = TrueRange의 단순이동평균."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


class ExecutionEngine:
    """진입 트리거 판정 및 손절/트레일링/분할익절 상태를 관리하는 엔진."""

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """진입 트리거 계산에 필요한 MA20/거래량 SMA/ATR/박스권 고점을 계산한다."""
        d = df.copy()
        d["MA20"] = d["Close"].rolling(20).mean()
        d["VolMA20"] = d["Volume"].rolling(PULLBACK_VOLUME_MA).mean()
        d["VolMA50"] = d["Volume"].rolling(BREAKOUT_VOLUME_MA).mean()
        d["ATR14"] = compute_atr(d, BREAKOUT_ATR_PERIOD)
        d["Box20High"] = d["High"].shift(1).rolling(BREAKOUT_LOOKBACK).max()
        d["MA50Runner"] = d["Close"].rolling(RUNNER_EXIT_MA_PERIOD).mean()
        return d

    def check_trigger_a(self, row: pd.Series, prev_row: pd.Series) -> bool:
        """Trigger A(눌림목): 20일선 지지 테스트 + 거래량 급감 + 전일 고가 돌파."""
        if pd.isna(row["MA20"]) or pd.isna(row["VolMA20"]) or pd.isna(prev_row["High"]):
            return False
        ma20 = row["MA20"]
        touches_support = (row["Low"] <= ma20 * PULLBACK_BAND_UPPER) and (row["Low"] >= ma20 * PULLBACK_BAND_LOWER)
        volume_dried_up = row["Volume"] < PULLBACK_VOLUME_RATIO * row["VolMA20"]
        confirms_up = row["Close"] > prev_row["High"]
        return bool(touches_support and volume_dried_up and confirms_up)

    def check_trigger_b(self, row: pd.Series) -> bool:
        """Trigger B(돌파): 20일 박스권 상향 돌파 + 거래량 폭발 + 강한 양봉."""
        if pd.isna(row["Box20High"]) or pd.isna(row["VolMA50"]) or pd.isna(row["ATR14"]):
            return False
        breaks_box = row["Close"] > row["Box20High"]
        volume_explodes = row["Volume"] >= BREAKOUT_VOLUME_RATIO * row["VolMA50"]
        strong_candle = (row["Close"] - row["Open"]) >= BREAKOUT_CANDLE_ATR_RATIO * row["ATR14"]
        return bool(breaks_box and volume_explodes and strong_candle)

    def entry_signal(self, df: pd.DataFrame, regime: str) -> dict | None:
        """국면별로 허용된 트리거를 검사해 진입 신호를 반환한다.

        - BULL: Trigger A, Trigger B 모두 허용
        - SIDEWAYS: Trigger A만 허용 (Trigger B 금지)
        - BEAR: 신규 진입 없음
        """
        if regime == "BEAR":
            return None

        d = self.compute_indicators(df)
        if len(d) < 2:
            return None
        row, prev_row = d.iloc[-1], d.iloc[-2]

        if self.check_trigger_a(row, prev_row):
            return {"type": "PULLBACK", "entry_price": float(row["Close"]),
                    "ma20": float(row["MA20"]), "atr": float(row["ATR14"]) if not pd.isna(row["ATR14"]) else None}

        if regime == "BULL" and self.check_trigger_b(row):
            return {"type": "BREAKOUT", "entry_price": float(row["Close"]),
                    "ma20": float(row["MA20"]), "atr": float(row["ATR14"])}

        return None

    def initial_stop(self, entry_price: float, ma20_at_entry: float, atr: float | None = None) -> float:
        """StopPrice = min(MA20_entry, EntryPrice - 1*ATR).

        눌림목(Trigger A) 진입은 정의상 MA20에 바짝 붙어 체결되므로, MA20을 그대로
        손절가로 쓰면 손절폭이 1~1.5% 수준으로 지나치게 좁아져 정상적인 일중
        변동성에도 쉽게 스탑아웃된다. ATR 기준 최소 폭(1*ATR)을 함께 반영해
        MA20과 ATR-스탑 중 더 낮은(=더 여유 있는) 가격을 손절가로 사용한다.
        ATR을 알 수 없을 때만 EntryPrice*(1-STOP_INITIAL_PCT)를 대체 하한으로 쓴다.
        """
        if atr and atr > 0:
            atr_stop = entry_price - STOP_ATR_MULT * atr
            return min(ma20_at_entry, atr_stop)
        return min(ma20_at_entry, entry_price * (1 - STOP_INITIAL_PCT))

    def update_trailing_stop(self, entry_price: float, current_stop: float, peak_price: float,
                              current_close: float, atr: float) -> tuple[float, float]:
        """보유 중 매일 호출: 본전 상향 + 3*ATR 트레일링 스탑을 갱신한다.

        Returns
        -------
        (new_stop, new_peak)
        """
        new_peak = max(peak_price, current_close)
        new_stop = current_stop

        if atr and atr > 0:
            if (current_close - entry_price) >= BREAKEVEN_ATR_TRIGGER * atr:
                new_stop = max(new_stop, entry_price)
            trail_stop = new_peak - TRAIL_ATR_MULT * atr
            new_stop = max(new_stop, trail_stop)

        return new_stop, new_peak

    def sideways_partial_target_hit(self, entry_price: float, current_close: float, atr: float) -> bool:
        """SIDEWAYS 국면 분할 익절: 1.5*ATR 도달 여부."""
        if not atr or atr <= 0:
            return False
        return (current_close - entry_price) >= SIDEWAYS_PARTIAL_ATR_TRIGGER * atr

    @property
    def partial_exit_fraction(self) -> float:
        return SIDEWAYS_PARTIAL_FRACTION

    def initial_stop_v2(self, entry_price: float, atr: float) -> float:
        """[v2 Runner 전략] StopPrice = EntryPrice - 2.5*ATR14 (MA20 하한 없이 순수 ATR 기반)."""
        return entry_price - STOP_ATR_MULT_V2 * atr
