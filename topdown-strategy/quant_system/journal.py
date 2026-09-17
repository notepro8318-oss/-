"""
[Module 5] TradeJournal
====================================================
사용자가 직접 입력한 매매(매매일/매수가/수량)를 CSV로 저장하고, v2 Runner
전략의 매도 시퀀스(손절 -> TP1 -> TP2 -> Runner)를 현재가 기준으로 재현해
현재 어느 단계인지, 그리고 가장 최근 거래일에 새로 발생한 이벤트가 있는지를
판정한다.
"""

from __future__ import annotations

import os

import pandas as pd

from qs_config import STOP_ATR_MULT_V2, TP1_PCT, TP1_FRACTION, TP2_PCT, TP2_FRACTION
from data import fetch_ohlcv
from execution import ExecutionEngine

JOURNAL_CSV_PATH = os.path.join(os.path.dirname(__file__), "trade_journal.csv")
JOURNAL_COLUMNS = ["ticker", "entry_date", "entry_price", "shares", "memo"]

_execution = ExecutionEngine()

STAGE_LABELS = {
    "HOLDING": "보유중 (TP1 대기)",
    "TP1_DONE": "TP1 완료 (TP2 대기, 손절가 본전 상향)",
    "TP2_DONE": "TP2 완료 (잔여 물량 Runner 추세추종중)",
    "EXITED": "전량 청산 완료",
}
EXIT_REASON_LABELS = {
    "STOP": "초기 손절",
    "BREAKEVEN_STOP": "본전 손절",
    "RUNNER": "Runner(MA50 이탈) 청산",
}


def load_journal() -> pd.DataFrame:
    """저장된 매매일지를 불러온다. 파일이 없으면 빈 DataFrame을 반환한다."""
    if os.path.exists(JOURNAL_CSV_PATH):
        df = pd.read_csv(JOURNAL_CSV_PATH)
        df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
        return df
    return pd.DataFrame(columns=JOURNAL_COLUMNS)


def save_journal(df: pd.DataFrame) -> None:
    """매매일지를 CSV로 저장한다."""
    df.to_csv(JOURNAL_CSV_PATH, index=False)


def compute_trade_status(ticker: str, entry_date, entry_price: float) -> dict:
    """진입일 이후 종가 흐름을 따라가며 v2 매도 시퀀스(손절/TP1/TP2/Runner)를 재현한다.

    Returns
    -------
    dict
        stage, exit_reason, stop_initial/tp1_price/tp2_price, current_stop,
        remaining_frac(잔여 비중), last_date/last_close, pnl_pct, events(발생 이력),
        new_event_today(가장 최근 거래일에 새로 발생한 이벤트 여부) 등을 담은 dict.
        오류 시 {"error": "..."}.
    """
    df = fetch_ohlcv(ticker, period="5y")
    if df.empty:
        return {"error": f"{ticker} 시세 데이터를 불러올 수 없습니다."}

    entry_ts = pd.Timestamp(entry_date)
    ind = _execution.compute_indicators(df)
    valid_idx = ind.index[ind.index >= entry_ts]
    if len(valid_idx) == 0:
        return {"error": "매매일 이후 거래일 데이터가 없습니다 (미래 날짜이거나 상장폐지 여부를 확인하세요)."}

    entry_idx = valid_idx[0]
    atr_entry = ind.loc[entry_idx, "ATR14"]
    if pd.isna(atr_entry):
        after = ind.loc[entry_idx:, "ATR14"].dropna()
        if after.empty:
            return {"error": "ATR 계산에 필요한 데이터가 부족합니다 (상장 초기 종목일 수 있습니다)."}
        atr_entry = float(after.iloc[0])
    else:
        atr_entry = float(atr_entry)

    stop_initial = entry_price - STOP_ATR_MULT_V2 * atr_entry
    tp1_price = entry_price * (1 + TP1_PCT)
    tp2_price = entry_price * (1 + TP2_PCT)

    stage = "HOLDING"
    current_stop = stop_initial
    remaining_frac = 1.0
    exit_reason = None
    exit_date = None
    exit_price = None
    events: list[tuple] = []

    path = ind.loc[entry_idx:]
    for date, row in path.iterrows():
        close = float(row["Close"])

        if stage == "HOLDING":
            if close <= current_stop:
                stage, exit_reason, exit_date, exit_price = "EXITED", "STOP", date, close
                events.append((date, f"🔴 손절 청산 — 종가 {close:.2f} ≤ 손절가 {current_stop:.2f}"))
                break
            if close >= tp1_price:
                stage = "TP1_DONE"
                remaining_frac -= TP1_FRACTION
                current_stop = entry_price
                events.append((date, f"🟢 TP1 도달(+{TP1_PCT*100:.0f}%) — 물량 {TP1_FRACTION*100:.0f}% 익절, "
                                      f"손절가 본전({entry_price:.2f})으로 상향"))

        elif stage == "TP1_DONE":
            if close <= current_stop:
                stage, exit_reason, exit_date, exit_price = "EXITED", "BREAKEVEN_STOP", date, close
                events.append((date, f"🟡 본전 손절 청산 — 종가 {close:.2f} ≤ {current_stop:.2f}"))
                break
            if close >= tp2_price:
                stage = "TP2_DONE"
                remaining_frac -= TP2_FRACTION
                events.append((date, f"🟢 TP2 도달(+{TP2_PCT*100:.0f}%) — 물량 {TP2_FRACTION*100:.0f}% 추가 익절, "
                                      f"잔여 {remaining_frac*100:.0f}% Runner 추세추종 전환"))

        elif stage == "TP2_DONE":
            ma50 = row["MA50Runner"]
            if not pd.isna(ma50) and close < ma50:
                stage, exit_reason, exit_date, exit_price = "EXITED", "RUNNER", date, close
                events.append((date, f"🔵 Runner 청산 — 종가 {close:.2f} < MA50 {ma50:.2f}"))
                break

    last_date = path.index[-1]
    last_close = float(path.iloc[-1]["Close"])
    pnl_pct = (last_close / entry_price - 1.0) * 100.0

    return {
        "stage": stage,
        "exit_reason": exit_reason,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "atr_entry": atr_entry,
        "stop_initial": stop_initial,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "current_stop": current_stop,
        "remaining_frac": remaining_frac,
        "last_date": last_date,
        "last_close": last_close,
        "pnl_pct": pnl_pct,
        "events": events,
        "new_event_today": bool(events) and events[-1][0] == last_date,
    }
