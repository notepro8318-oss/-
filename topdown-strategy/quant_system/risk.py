"""
[Module 4b] RiskManager
====================================================
고정 비율 리스크(Fixed Fractional) 방식으로 포지션 사이즈(주식 수)를 계산한다.
"""

from __future__ import annotations

import math

from qs_config import RISK_PCT, POSITION_CAP_PCT, MIN_DPS_FLOOR_PCT, REGIME_EXPOSURE


class RiskManager:
    """자산/손절폭 기반으로 매수 수량과 포트폴리오 노출을 결정하는 엔진."""

    def __init__(self, risk_pct: float = RISK_PCT, cap_pct: float = POSITION_CAP_PCT,
                 min_dps_floor_pct: float = MIN_DPS_FLOOR_PCT) -> None:
        self.risk_pct = risk_pct
        self.cap_pct = cap_pct
        self.min_dps_floor_pct = min_dps_floor_pct

    def position_size(self, equity: float, entry_price: float, stop_price: float,
                       regime_multiplier: float = 1.0) -> dict:
        """고정 비율 리스크 포지션 사이징.

        RiskAmount = Equity * Risk%
        DPS        = EntryPrice - StopPrice  (<=0 이면 0.01 * EntryPrice로 대체)
        Shares_risk = floor(RiskAmount / DPS)
        Shares_cap  = floor(Equity * Cap% / EntryPrice)
        Shares_final = min(Shares_risk, Shares_cap) * regime_multiplier 적용

        Returns
        -------
        dict: {shares, risk_amount, dps, position_value}
        """
        risk_amount = equity * self.risk_pct * regime_multiplier
        dps = entry_price - stop_price
        if dps <= 0:
            dps = self.min_dps_floor_pct * entry_price

        shares_risk = math.floor(risk_amount / dps) if dps > 0 else 0
        shares_cap = math.floor((equity * self.cap_pct * regime_multiplier) / entry_price) if entry_price > 0 else 0
        shares = max(0, min(shares_risk, shares_cap))

        return {
            "shares": shares,
            "risk_amount": risk_amount,
            "dps": dps,
            "position_value": shares * entry_price,
        }

    @staticmethod
    def regime_multiplier(regime: str) -> float:
        """국면별 포트폴리오 노출 승수 (BULL=1.0, SIDEWAYS=0.4, BEAR=0.0)."""
        return REGIME_EXPOSURE.get(regime, 0.0)
