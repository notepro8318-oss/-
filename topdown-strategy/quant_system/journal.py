"""
[Module 5] TradeJournal — 룰 기반 매도 시그널 추적기
====================================================
사용자가 등록한 포지션(Trade)의 상태(phase/remaining_shares/current_stop/status)를
저장해 두고, 매수 후보 화면의 Runner 전략(손절 -> TP1 -> TP2 -> Runner)과 동일한
규칙으로 신규 시그널을 감지한다.

상태는 GitHub Contents API(공유 저장소, github_store.py)에 CSV로 저장되어
Streamlit Cloud(라이브 대시보드)와 GitHub Actions(장중/마감 후 폴링 스케줄러)가
동일한 매매일지를 공유한다. GITHUB_TOKEN이 없는 로컬 환경에서는 로컬 CSV로 폴백한다.

핵심 규칙 (entry_price 대비):
- STAGE_0: 저가 <= current_stop(=entry-2.5*ATR14) -> STOP, 전량 청산
           고가 >= entry*1.10 -> TP1 시그널(최초수량의 30%), current_stop=entry(본전), phase=STAGE_1
- STAGE_1: 저가 <= current_stop(본전) -> BREAKEVEN_STOP, 잔여 전량 청산
           고가 >= entry*1.20 -> TP2 시그널(최초수량의 30%), phase=STAGE_2(Runner)
- STAGE_2: 종가 < MA50 -> RUNNER_EXIT, 잔여 전량 청산

TP1/TP2 시그널은 phase/current_stop을 즉시 갱신하지만, remaining_shares는
사용자가 "체결 확인"을 눌러야 실제로 차감된다(실제 체결 수량이 다를 수 있으므로).
손절류 시그널(STOP/BREAKEVEN_STOP/RUNNER_EXIT)은 전량 청산이 전제이므로 감지 즉시
status=CLOSED, remaining_shares=0으로 자동 반영된다.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import date, datetime

import pandas as pd

from qs_config import STOP_ATR_MULT_V2, TP1_PCT, TP1_FRACTION, TP2_PCT, TP2_FRACTION
from data import fetch_ohlcv
from execution import ExecutionEngine
import github_store

_execution = ExecutionEngine()

_LOCAL_DIR = os.path.dirname(__file__)
TRADES_REPO_PATH = "topdown-strategy/quant_system/trade_journal.csv"
TRADES_LOCAL_PATH = os.path.join(_LOCAL_DIR, "trade_journal.csv")
SIGNALS_REPO_PATH = "topdown-strategy/quant_system/trade_signals.csv"
SIGNALS_LOCAL_PATH = os.path.join(_LOCAL_DIR, "trade_signals.csv")
EXITS_REPO_PATH = "topdown-strategy/quant_system/trade_exits.csv"
EXITS_LOCAL_PATH = os.path.join(_LOCAL_DIR, "trade_exits.csv")

TRADE_COLUMNS = [
    "id", "ticker", "asset_name", "entry_date", "entry_price",
    "initial_shares", "remaining_shares", "initial_stop", "current_stop",
    "atr14_at_entry", "status", "phase", "last_checked_date", "memo", "archived",
]
SIGNAL_COLUMNS = ["id", "trade_id", "ticker", "date", "type", "price", "suggested_shares",
                   "confirmed", "notified", "note"]
# History에서 수동으로 기록하는 매도 내역 (분할 매도 지원 — 포지션당 여러 건 가능)
EXIT_COLUMNS = ["id", "trade_id", "date", "price", "shares", "memo"]

PHASE_LABELS = {
    "STAGE_0": "STAGE_0 · 대기 (TP1 전)",
    "STAGE_1": "STAGE_1 · TP1 완료 (TP2 대기, 손절가 본전)",
    "STAGE_2": "STAGE_2 · TP2 완료 · Runner 추세추종",
}
SIGNAL_LABELS = {
    "TP1": f"TP1 익절 (+{TP1_PCT*100:.0f}%)",
    "TP2": f"TP2 익절 (+{TP2_PCT*100:.0f}%)",
    "STOP": "초기 손절",
    "BREAKEVEN_STOP": "본전 손절",
    "RUNNER_EXIT": "Runner 청산 (MA50 이탈)",
}


# ------------------------------------------------------------------
# 저장소 IO (GitHub Contents API, 미설정 시 로컬 CSV 폴백)
# ------------------------------------------------------------------
def _read_df(repo_path: str, local_path: str, columns: list[str]) -> pd.DataFrame:
    if github_store.is_configured():
        content, _ = github_store.get_file(repo_path)
        if content is None:
            return pd.DataFrame(columns=columns)
        df = pd.read_csv(io.StringIO(content))
    elif os.path.exists(local_path):
        df = pd.read_csv(local_path)
    else:
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def _write_df(df: pd.DataFrame, repo_path: str, local_path: str, message: str) -> None:
    csv_text = df.to_csv(index=False)
    if github_store.is_configured():
        _, sha = github_store.get_file(repo_path)
        github_store.put_file(repo_path, csv_text, message, sha=sha)
    else:
        df.to_csv(local_path, index=False)


def load_trades() -> pd.DataFrame:
    df = _read_df(TRADES_REPO_PATH, TRADES_LOCAL_PATH, TRADE_COLUMNS)
    if not df.empty:
        df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
        df["last_checked_date"] = pd.to_datetime(df["last_checked_date"]).dt.date
        for col in ["entry_price", "initial_shares", "remaining_shares",
                    "initial_stop", "current_stop", "atr14_at_entry"]:
            df[col] = pd.to_numeric(df[col])
        df["archived"] = df["archived"].fillna(False).astype(bool)
    return df


def save_trades(df: pd.DataFrame, message: str = "Update trade journal") -> None:
    _write_df(df, TRADES_REPO_PATH, TRADES_LOCAL_PATH, message)


def load_signals() -> pd.DataFrame:
    df = _read_df(SIGNALS_REPO_PATH, SIGNALS_LOCAL_PATH, SIGNAL_COLUMNS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["confirmed"] = df["confirmed"].fillna(False).astype(bool)
        # 기존(스키마 변경 전) 시그널은 notified 컬럼이 없었으므로, 이미 확인(confirmed)된
        # 시그널은 발송된 것으로 간주하고 나머지만 "미발송"으로 취급해 중복 알림을 막는다.
        df["notified"] = df["notified"].fillna(df["confirmed"]).astype(bool)
    return df


def save_signals(df: pd.DataFrame, message: str = "Update trade signals") -> None:
    _write_df(df, SIGNALS_REPO_PATH, SIGNALS_LOCAL_PATH, message)


def load_exits() -> pd.DataFrame:
    """History에서 기록한 매도 내역(분할 매도 포함)을 불러온다."""
    df = _read_df(EXITS_REPO_PATH, EXITS_LOCAL_PATH, EXIT_COLUMNS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["price"] = pd.to_numeric(df["price"])
        df["shares"] = pd.to_numeric(df["shares"]).astype(int)
    return df


def save_exits(df: pd.DataFrame, message: str = "Update trade exits") -> None:
    _write_df(df, EXITS_REPO_PATH, EXITS_LOCAL_PATH, message)


def add_exit(trade_id: str, exit_date, price: float, shares: int, memo: str = "") -> dict:
    """포지션에 매도 기록을 한 건 추가한다 (분할 매도는 여러 번 호출)."""
    exits_df = load_exits()
    new_row = {
        "id": uuid.uuid4().hex[:12], "trade_id": trade_id,
        "date": exit_date, "price": price, "shares": int(shares), "memo": memo.strip(),
    }
    new_df = pd.DataFrame([new_row])
    exits_df = new_df if exits_df.empty else pd.concat([exits_df, new_df], ignore_index=True)
    save_exits(exits_df, f"Add exit for trade {trade_id}: {shares}주 @ {price}")
    return new_row


def delete_exit(exit_id: str) -> None:
    """잘못 입력한 매도 기록을 삭제한다."""
    exits_df = load_exits()
    exits_df = exits_df[exits_df["id"] != exit_id].reset_index(drop=True)
    save_exits(exits_df, f"Delete exit {exit_id}")


# ------------------------------------------------------------------
# 진입 미리보기 / 등록
# ------------------------------------------------------------------
def preview_entry(ticker: str, entry_date, entry_price: float) -> dict:
    """티커/매매일/매수가만으로 ATR14·손절가·TP1/TP2 목표가를 즉시 계산한다."""
    df = fetch_ohlcv(ticker, period="5y")
    if df.empty:
        return {"error": f"{ticker} 시세 데이터를 불러올 수 없습니다."}

    entry_ts = pd.Timestamp(entry_date)
    ind = _execution.compute_indicators(df)
    valid_idx = ind.index[ind.index >= entry_ts]
    if len(valid_idx) == 0:
        return {"error": "매매일 이후 거래일 데이터가 없습니다 (미래 날짜이거나 데이터 지연일 수 있습니다)."}

    entry_idx = valid_idx[0]
    atr = ind.loc[entry_idx, "ATR14"]
    if pd.isna(atr):
        after = ind.loc[entry_idx:, "ATR14"].dropna()
        if after.empty:
            return {"error": "ATR 계산에 필요한 데이터가 부족합니다."}
        atr = float(after.iloc[0])
    else:
        atr = float(atr)

    initial_stop = entry_price - STOP_ATR_MULT_V2 * atr
    return {
        "entry_idx": entry_idx,
        "atr14": atr,
        "initial_stop": initial_stop,
        "tp1_price": entry_price * (1 + TP1_PCT),
        "tp2_price": entry_price * (1 + TP2_PCT),
    }


def add_trade(ticker: str, entry_date, entry_price: float, shares: int,
              memo: str = "", asset_name: str = "") -> dict:
    """미리보기 계산 결과로 신규 OPEN/STAGE_0 포지션을 등록한다."""
    preview = preview_entry(ticker, entry_date, entry_price)
    if "error" in preview:
        return preview

    trades_df = load_trades()
    entry_idx = preview["entry_idx"]
    new_row = {
        "id": uuid.uuid4().hex[:12],
        "ticker": ticker.strip().upper(),
        "asset_name": asset_name.strip(),
        "entry_date": entry_idx.date(),
        "entry_price": entry_price,
        "initial_shares": shares,
        "remaining_shares": shares,
        "initial_stop": preview["initial_stop"],
        "current_stop": preview["initial_stop"],
        "atr14_at_entry": preview["atr14"],
        "status": "OPEN",
        "phase": "STAGE_0",
        # 등록일 하루 전부터 스캔을 시작해 진입일 당일 트리거도 놓치지 않는다.
        "last_checked_date": entry_idx.date() - pd.Timedelta(days=1),
        "memo": memo.strip(),
        "archived": False,
    }
    new_df = pd.DataFrame([new_row])
    trades_df = new_df if trades_df.empty else pd.concat([trades_df, new_df], ignore_index=True)
    save_trades(trades_df, f"Add trade: {new_row['ticker']} {new_row['entry_date']}")
    return {"ok": True, "trade": new_row}


def archive_trade(trade_id: str) -> None:
    """포지션을 '매매일지 현황'에서 History로 옮긴다 (데이터는 보존, 추적/스캔 대상에서 제외)."""
    trades_df = load_trades()
    trades_df.loc[trades_df["id"] == trade_id, "archived"] = True
    save_trades(trades_df, f"Archive trade {trade_id}")


def restore_trade(trade_id: str) -> None:
    """History에서 '매매일지 현황'으로 다시 복원한다."""
    trades_df = load_trades()
    trades_df.loc[trades_df["id"] == trade_id, "archived"] = False
    save_trades(trades_df, f"Restore trade {trade_id}")


def delete_trade(trade_id: str) -> None:
    """포지션과 관련 시그널/매도기록을 완전히 삭제한다 (History에서 영구 삭제할 때 사용)."""
    trades_df = load_trades()
    trades_df = trades_df[trades_df["id"] != trade_id].reset_index(drop=True)
    save_trades(trades_df, f"Delete trade {trade_id}")
    signals_df = load_signals()
    signals_df = signals_df[signals_df["trade_id"] != trade_id].reset_index(drop=True)
    save_signals(signals_df, f"Delete signals for trade {trade_id}")
    exits_df = load_exits()
    exits_df = exits_df[exits_df["trade_id"] != trade_id].reset_index(drop=True)
    save_exits(exits_df, f"Delete exits for trade {trade_id}")


# ------------------------------------------------------------------
# 시그널 감지 (상태 머신)
# ------------------------------------------------------------------
def _make_signal(trade_id: str, ticker: str, sig_date, sig_type: str, price: float,
                  suggested_shares: int) -> dict:
    return {
        "id": uuid.uuid4().hex[:12], "trade_id": trade_id, "ticker": ticker,
        "date": sig_date.date() if hasattr(sig_date, "date") else sig_date,
        "type": sig_type, "price": round(float(price), 4),
        "suggested_shares": int(suggested_shares), "confirmed": False, "notified": False,
        "note": SIGNAL_LABELS.get(sig_type, sig_type),
    }


def _simulate_day(trade: dict, day_date, row: pd.Series) -> tuple[dict, list[dict]]:
    """하루치 High/Low/Close로 현재 phase의 트리거를 검사해 (갱신된 trade, 신규시그널들)을 반환.

    급등/갭상승 등으로 하루 만에 여러 단계(TP1->TP2, 심지어 Runner까지)를 동시에
    통과할 수 있으므로, 같은 날짜 안에서는 더 이상 전이가 없을 때까지 계속 검사한다.
    """
    signals: list[dict] = []
    if trade["status"] != "OPEN":
        return trade, signals

    entry_price = trade["entry_price"]
    initial_shares = trade["initial_shares"]
    high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])

    while trade["status"] == "OPEN":
        phase = trade["phase"]

        if phase == "STAGE_0":
            if low <= trade["current_stop"]:
                signals.append(_make_signal(trade["id"], trade["ticker"], day_date, "STOP",
                                             trade["current_stop"], trade["remaining_shares"]))
                trade["status"] = "CLOSED"
                trade["remaining_shares"] = 0
                break
            if high >= entry_price * (1 + TP1_PCT):
                suggested = round(initial_shares * TP1_FRACTION)
                signals.append(_make_signal(trade["id"], trade["ticker"], day_date, "TP1",
                                             entry_price * (1 + TP1_PCT), suggested))
                trade["current_stop"] = entry_price
                trade["phase"] = "STAGE_1"
                continue
            break

        if phase == "STAGE_1":
            if low <= trade["current_stop"]:
                signals.append(_make_signal(trade["id"], trade["ticker"], day_date, "BREAKEVEN_STOP",
                                             trade["current_stop"], trade["remaining_shares"]))
                trade["status"] = "CLOSED"
                trade["remaining_shares"] = 0
                break
            if high >= entry_price * (1 + TP2_PCT):
                suggested = round(initial_shares * TP2_FRACTION)
                signals.append(_make_signal(trade["id"], trade["ticker"], day_date, "TP2",
                                             entry_price * (1 + TP2_PCT), suggested))
                trade["phase"] = "STAGE_2"
                continue
            break

        if phase == "STAGE_2":
            ma50 = row.get("MA50Runner")
            if ma50 is not None and not pd.isna(ma50) and close < float(ma50):
                signals.append(_make_signal(trade["id"], trade["ticker"], day_date, "RUNNER_EXIT",
                                             close, trade["remaining_shares"]))
                trade["status"] = "CLOSED"
                trade["remaining_shares"] = 0
            break

        break

    return trade, signals


def scan_open_trades() -> list[dict]:
    """모든 OPEN 포지션을 마지막 확인일 다음날부터 최신 거래일까지 스캔해 신규 시그널을 감지/저장한다."""
    trades_df = load_trades()
    if trades_df.empty:
        return []
    signals_df = load_signals()

    all_new_signals: list[dict] = []
    any_trade_changed = False

    for i, row in trades_df.iterrows():
        if row["status"] != "OPEN" or row.get("archived", False):
            continue
        df = fetch_ohlcv(row["ticker"], period="5y")
        if df.empty:
            continue
        ind = _execution.compute_indicators(df)
        last_checked = pd.Timestamp(row["last_checked_date"])
        to_scan = ind.loc[ind.index > last_checked]
        if to_scan.empty:
            continue

        trade = row.to_dict()
        day_signals: list[dict] = []
        for day_date, day_row in to_scan.iterrows():
            trade, sigs = _simulate_day(trade, day_date, day_row)
            day_signals.extend(sigs)
            trade["last_checked_date"] = day_date.date()
            if trade["status"] == "CLOSED":
                break

        for col in TRADE_COLUMNS:
            trades_df.at[i, col] = trade[col]
        any_trade_changed = True

        if day_signals:
            new_sig_df = pd.DataFrame(day_signals)
            signals_df = new_sig_df if signals_df.empty else pd.concat([signals_df, new_sig_df], ignore_index=True)
            all_new_signals.extend(day_signals)

    if any_trade_changed:
        save_trades(trades_df, "Scan: update trade phase/status")
    if all_new_signals:
        save_signals(signals_df, f"Scan: {len(all_new_signals)} new signal(s)")

    return all_new_signals


def confirm_signal(signal_id: str) -> None:
    """시그널을 확인 처리한다. TP류는 잔여 수량을 실제로 차감한다."""
    signals_df = load_signals()
    match = signals_df[signals_df["id"] == signal_id]
    if match.empty:
        return
    sig = match.iloc[0]
    signals_df.loc[signals_df["id"] == signal_id, "confirmed"] = True
    save_signals(signals_df, f"Confirm signal {signal_id}")

    if sig["type"] in ("TP1", "TP2"):
        trades_df = load_trades()
        tmatch = trades_df.index[trades_df["id"] == sig["trade_id"]]
        if len(tmatch):
            idx = tmatch[0]
            remaining = max(0, int(trades_df.at[idx, "remaining_shares"]) - int(sig["suggested_shares"]))
            trades_df.at[idx, "remaining_shares"] = remaining
            save_trades(trades_df, f"Confirm fill: {sig['ticker']} {sig['type']}")


def get_unnotified_signals() -> pd.DataFrame:
    """아직 알림(Telegram 등)을 보내지 않은 시그널을 반환한다.

    시그널 감지(scan_open_trades)는 대시보드의 수동 새로고침이나 스케줄러 어느 쪽에서
    먼저 실행되든 동일하게 상태를 소비하므로, "방금 감지된 시그널"만 알림을 보내면
    다른 프로세스가 먼저 감지해버린 시그널은 영영 알림을 못 받는다. notified 플래그를
    별도로 두어, 언제 감지됐든 아직 발송 안 된 시그널을 스케줄러가 항상 찾아내게 한다.
    """
    signals_df = load_signals()
    if signals_df.empty:
        return signals_df
    return signals_df[~signals_df["notified"]]


def mark_notified(signal_ids: list[str]) -> None:
    """주어진 시그널들을 발송 완료(notified=True) 처리한다."""
    if not signal_ids:
        return
    signals_df = load_signals()
    signals_df.loc[signals_df["id"].isin(signal_ids), "notified"] = True
    save_signals(signals_df, f"Mark {len(signal_ids)} signal(s) as notified")


# ------------------------------------------------------------------
# 대시보드용 실시간 지표
# ------------------------------------------------------------------
def get_dashboard_metrics(trade: dict) -> dict:
    """현재가 대비 손절/목표가 거리(%), R-배수 등 대시보드 표시용 값을 계산한다."""
    df = fetch_ohlcv(trade["ticker"], period="5d")
    if df.empty:
        return {"error": f"{trade['ticker']} 현재가를 불러올 수 없습니다."}
    last_close = float(df["Close"].iloc[-1])

    entry_price = trade["entry_price"]
    initial_risk = entry_price - trade["initial_stop"]
    r_multiple = (last_close - entry_price) / initial_risk if initial_risk > 0 else float("nan")

    if trade["phase"] == "STAGE_0":
        target_price, target_label = entry_price * (1 + TP1_PCT), "TP1"
    elif trade["phase"] == "STAGE_1":
        target_price, target_label = entry_price * (1 + TP2_PCT), "TP2"
    else:
        target_price, target_label = None, "MA50"

    dist_to_stop_pct = (last_close / trade["current_stop"] - 1) * 100 if trade["current_stop"] else float("nan")
    dist_to_target_pct = (target_price / last_close - 1) * 100 if target_price else None

    return {
        "last_close": last_close,
        "r_multiple": r_multiple,
        "dist_to_stop_pct": dist_to_stop_pct,
        "target_label": target_label,
        "target_price": target_price,
        "dist_to_target_pct": dist_to_target_pct,
    }
