"""
[4단계] 매수 타점 및 리스크 관리 (Entry & Risk Management)
====================================================
매수 타점(둘 중 하나 만족 시 매수 시그널):
  (A) 눌림목: 당일 저가가 MA20을 터치(<=)하고, 종가는 MA20을 지켜냄(종가 > MA20)
  (B) 돌파: 종가가 최근 20일 박스권 고점을 강하게 상회(돌파)

리스크 관리(손익비 2:1):
  - 손절: 매수가 대비 -5% 또는 종가가 MA20 이탈 시
  - 익절: 매수가 대비 +10%
  - 포지션 사이징: 1회 진입 시 총 자본의 2% 이하만 리스크에 노출
"""

import pandas as pd

from config import (
    STOCK_MA_SHORT, BOX_BREAKOUT_LOOKBACK, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT, RISK_PER_TRADE_PCT,
)


def check_entry_signal(df: pd.DataFrame) -> dict:
    """
    3단계를 통과한 종목의 일봉 df(MA20 포함)를 받아 4단계 매수 타점을 판별한다.

    Returns
    -------
    dict: {
        "signal": bool,
        "type": "PULLBACK" | "BREAKOUT" | None,
        "entry_price": float,
        "stop_loss": float,
        "take_profit": float,
    }
    """
    d = df.copy()
    d["Box20High"] = d["High"].shift(1).rolling(BOX_BREAKOUT_LOOKBACK).max()  # 당일 제외 직전 20일 고점
    d = d.dropna(subset=["MA20", "Box20High"])
    if d.empty:
        return {"signal": False, "type": None}

    last = d.iloc[-1]
    close = float(last["Close"])
    low = float(last["Low"])
    ma20 = float(last["MA20"])
    box_high = float(last["Box20High"])

    # (A) 눌림목: 저가가 MA20 터치, 종가는 MA20 사수
    is_pullback = (low <= ma20) and (close > ma20)

    # (B) 박스권 돌파: 종가가 직전 20일 고점을 상향 돌파
    is_breakout = close > box_high

    if is_pullback:
        entry_type = "PULLBACK"
    elif is_breakout:
        entry_type = "BREAKOUT"
    else:
        return {"signal": False, "type": None}

    entry_price = close
    stop_loss = entry_price * (1 - STOP_LOSS_PCT)
    # MA20 이탈도 손절 조건이므로, MA20이 -5% 손절선보다 더 타이트하면 MA20을 손절선으로 사용
    stop_loss = max(stop_loss, ma20) if ma20 < entry_price else stop_loss
    take_profit = entry_price * (1 + TAKE_PROFIT_PCT)

    return {
        "signal": True,
        "type": entry_type,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


def calc_position_size(capital: float, entry_price: float, stop_loss: float,
                        risk_pct: float = RISK_PER_TRADE_PCT) -> dict:
    """
    자금 관리: 1회 진입 시 총 자본의 risk_pct(기본 2%) 이하만 리스크에 노출되도록
    포지션 사이즈(주식 수)를 계산한다.

        risk_amount = capital * risk_pct
        shares = risk_amount / (entry_price - stop_loss)
    """
    risk_amount = capital * risk_pct
    per_share_risk = entry_price - stop_loss
    if per_share_risk <= 0:
        return {"shares": 0, "risk_amount": risk_amount, "position_value": 0.0}

    shares_by_risk = int(risk_amount // per_share_risk)
    # 손절폭이 매우 타이트하면 리스크 기준 수량이 보유 현금을 초과할 수 있으므로
    # 레버리지 없이 매수 가능한 최대 수량으로 한 번 더 제한한다.
    shares_by_cash = int(capital // entry_price)
    shares = max(0, min(shares_by_risk, shares_by_cash))
    position_value = shares * entry_price

    return {
        "shares": shares,
        "risk_amount": risk_amount,
        "position_value": position_value,
    }
