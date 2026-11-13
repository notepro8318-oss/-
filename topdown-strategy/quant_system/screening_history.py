"""
[Module 7] Screening History — 스크리닝 종목 날짜별 기록 + 가격 추이 추적 + 엑셀 출력
====================================================
screening.py의 스냅샷은 매번 덮어써져 "직전 결과"만 남는다. 이 모듈은 거래일마다의
스크리닝 결과를 `screening_history.csv`(날짜 x 종목, long 포맷)에 누적 기록하고,
편입일 이후 가격 추이(수익률/최대상승/최대하락/매도 시그널 기준 청산/SPY 대비 초과수익)를 계산해
엑셀(.xlsx)로 내보낸다.

저장소는 매매일지/스냅샷과 같은 방식이다. GITHUB_TOKEN이 있으면 GitHub Contents API
(github_store.py)로 스케줄러(GitHub Actions)와 Streamlit이 같은 파일을 공유하고, 없으면
로컬 CSV로 폴백한다.

추적 정의
---------
- 에피소드: 한 종목이 기록된 거래일에 연속으로 후보에 오른 구간.
- 기준가: 편입일(스크리닝 당일) 종가에 매수했다고 본다. 참고용으로 다음 거래일 시가 기준 수익률도
  함께 계산한다. (수익률은 모두 지금 시점의 조정주가로 일관되게 계산하므로 스냅샷의 entry_price와
  소수점 단위로 다를 수 있다.)
- 순위 이탈일: 후보(상위 5종목)에서 처음 빠진 기록일. 정보용이며 추적을 끝내지 않는다.
- 추적 종료: 매매일지와 같은 규칙(journal._simulate_day)으로 편입일 다음 거래일부터 매도 시그널을 찾고,
  전량 청산 시그널(초기 손절 / TP1 후 본전 손절 / TP2 후 종가 < MA50 Runner 청산)이 나는 날 끝낸다.
  TP1(+10%, 30%)·TP2(+20%, 30%)는 분할 매도라 도달일만 기록하고 추적은 이어 간다.
  전량 청산이 없으면 '진행중'(최신 거래일까지). 추적 기간 상한은 없다.
- 재편입: 추적 중인 종목이 후보에서 빠졌다가 다시 오르면 새 추적을 시작하지 않고 '재편입 횟수'만 센다.
  청산 이후 다시 후보에 오르면 새 에피소드로 추적한다.
- 시그널 가격: 손절류는 min(손절가, 당일 시가), TP는 max(목표가, 당일 시가)로 갭을 반영한다.
- 수익률: 편입일 종가 -> 종료가(청산 시그널가 또는 진행중이면 최신 종가). 전략 수익률은 TP1·TP2 분할
  매도를 비중(30/30/40)대로 가중한 값이다.
- 순위 이탈일 시점 수익률 / 이탈 후 추가 수익: 순위 이탈 때 팔았다면 vs 시그널까지 보유했을 때의 차이.
- 편입 후 +N일 수익률(D+N)은 청산 여부와 무관하게 편입 후 N번째 거래일 종가 기준이다.
- 실제 매도 시그널: 매매일지에서 매수일이 [편입일, 시뮬레이션 전량 청산일] 사이인 같은 종목 포지션을 연결해
  실제 첫 매도 시그널일과 전량 청산 시그널일을 나란히 붙인다 (attach_signals).
- 편입일 CompositeScore: 스냅샷에 저장된 값(과거분은 recompute_scores로 당시 시점 데이터에서 복원).
- 최대상승(MFE)/최대하락(MAE)은 추적 구간의 고가/저가 기준이다.

CLI
---
    python screening_history.py backfill          # git 커밋 이력에서 과거 스냅샷 복원
    python screening_history.py backfill-scores   # 과거 이력의 CompositeScore 재계산·복원
    python screening_history.py export out.xlsx   # 현재 이력으로 엑셀 생성
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import warnings
from datetime import date

import numpy as np
import pandas as pd

import github_store
from qs_config import TP1_FRACTION, TP2_FRACTION

HISTORY_REPO_PATH = "topdown-strategy/quant_system/screening_history.csv"
HISTORY_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "screening_history.csv")
SNAPSHOT_REPO_PATH = "topdown-strategy/quant_system/screening_snapshot.json"

HISTORY_COLUMNS = ["date", "regime", "sideways_mode", "leaders", "rank", "ticker", "sector",
                   "composite_score", "entry_price", "stop", "weight_pct", "shares"]

MAX_TRACK_DAYS = 60          # 경과일별 경로(D+N) 길이. 추적 종료 기준이 아니다(추적은 전량 청산 시그널까지).
FORWARD_DAYS = (1, 5, 10, 20, 60)
BENCHMARK = "SPY"


# ------------------------------------------------------------------
# 저장소 IO
# ------------------------------------------------------------------
def load_history() -> pd.DataFrame:
    """누적 스크리닝 이력을 불러온다. 없으면 빈 DataFrame."""
    if github_store.is_configured():
        content, _ = github_store.get_file(HISTORY_REPO_PATH)
        if content is None:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        df = pd.read_csv(io.StringIO(content), dtype={"ticker": str, "sector": str, "leaders": str})
    elif os.path.exists(HISTORY_LOCAL_PATH):
        df = pd.read_csv(HISTORY_LOCAL_PATH, dtype={"ticker": str, "sector": str, "leaders": str})
    else:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    for col in HISTORY_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[HISTORY_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for col in ["rank", "composite_score", "entry_price", "stop", "weight_pct", "shares"]:
        df[col] = pd.to_numeric(df[col])
    return df.sort_values(["date", "rank"], kind="stable").reset_index(drop=True)


def save_history(df: pd.DataFrame, message: str = "Update screening history") -> None:
    csv_text = df[HISTORY_COLUMNS].to_csv(index=False)
    if github_store.is_configured():
        _, sha = github_store.get_file(HISTORY_REPO_PATH)
        github_store.put_file(HISTORY_REPO_PATH, csv_text, message, sha=sha)
    else:
        with open(HISTORY_LOCAL_PATH, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)


def snapshot_to_rows(snapshot: dict) -> list[dict]:
    """스냅샷 1건을 이력 행들로 변환한다. 후보가 없으면(BEAR 등) ticker가 빈 1행을 남긴다."""
    base = {
        "date": snapshot["date"],
        "regime": snapshot.get("regime"),
        "sideways_mode": snapshot.get("sideways_mode"),
        "leaders": ",".join(snapshot.get("leaders") or []),
    }
    rows = []
    for i, c in enumerate(snapshot.get("candidates") or [], start=1):
        rows.append({**base, "rank": i, "ticker": c["ticker"], "sector": c.get("sector"),
                     "composite_score": c.get("composite_score"), "entry_price": c.get("entry_price"), "stop": c.get("stop"),
                     "weight_pct": c.get("weight_pct"), "shares": c.get("shares")})
    if not rows:
        rows.append({**base, "rank": None, "ticker": None, "sector": None, "composite_score": None,
                     "entry_price": None, "stop": None, "weight_pct": None, "shares": None})
    return rows


def _concat(hist: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return new
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)   # 후보 없는 날(전부 NaN 행) 병합 경고
        return pd.concat([hist, new], ignore_index=True)


def append_snapshot(snapshot: dict, message: str | None = None) -> pd.DataFrame:
    """오늘 스냅샷을 이력에 반영한다. 같은 날짜가 이미 있으면 그 날짜 행을 교체한다(멱등)."""
    hist = load_history()
    d = pd.to_datetime(snapshot["date"]).date()
    hist = hist[hist["date"] != d]
    new = pd.DataFrame(snapshot_to_rows(snapshot), columns=HISTORY_COLUMNS)
    new["date"] = pd.to_datetime(new["date"]).dt.date
    hist = _concat(hist, new)
    hist = hist.sort_values(["date", "rank"], kind="stable").reset_index(drop=True)
    save_history(hist, message or f"Screening history: {d}")
    return hist


def backfill_from_git(repo_root: str | None = None) -> pd.DataFrame:
    """screening_snapshot.json의 git 커밋 이력에서 과거 날짜별 스냅샷을 복원해 이력에 병합한다.

    같은 날짜에 커밋이 여러 개면 가장 마지막 것을 쓰고, 이력에 이미 있는 날짜는 건드리지 않는다.
    """
    root = repo_root or subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(__file__), text=True).strip()
    hashes = subprocess.check_output(
        ["git", "log", "--reverse", "--format=%H", "--", SNAPSHOT_REPO_PATH],
        cwd=root, text=True).split()
    by_date: dict[str, dict] = {}
    for h in hashes:
        try:
            raw = subprocess.check_output(["git", "show", f"{h}:{SNAPSHOT_REPO_PATH}"],
                                          cwd=root, stderr=subprocess.DEVNULL)
            snap = json.loads(raw.decode("utf-8"))
            by_date[snap["date"]] = snap        # 오래된 -> 최신 순이라 같은 날짜는 마지막이 남는다
        except Exception:
            continue

    hist = load_history()
    have = set(hist["date"])
    add_rows = []
    for d, snap in sorted(by_date.items()):
        if pd.to_datetime(d).date() in have:
            continue
        add_rows.extend(snapshot_to_rows(snap))
    if add_rows:
        new = pd.DataFrame(add_rows, columns=HISTORY_COLUMNS)
        new["date"] = pd.to_datetime(new["date"]).dt.date
        hist = _concat(hist, new)
        hist = hist.sort_values(["date", "rank"], kind="stable").reset_index(drop=True)
        save_history(hist, "Backfill screening history from git")
    print(f"git 커밋 {len(hashes)}건 -> 고유 날짜 {len(by_date)}일, 신규 복원 {len({r['date'] for r in add_rows})}일")
    return hist


def recompute_scores(hist: pd.DataFrame, only_missing: bool = True) -> tuple[pd.DataFrame, dict]:
    """과거 이력의 CompositeScore를 당시 시점 데이터로 다시 계산해 채운다.

    스냅샷에 점수가 저장되기 전(과거) 기록용이다. 날짜 d마다 그날 주도 섹터(이력의 leaders)의 대표 종목을
    d 이전 데이터만 잘라 MomentumRanker로 다시 랭킹한다. 지금의 조정주가·종목 풀로 계산하므로
    당시 값과 소수점 단위로 다를 수 있다. 반환 stats의 mismatch는 이력의 후보가 재계산 상위 5종목에
    없었던 (날짜, 종목) 수로, 복원 신뢰도 점검용이다.
    """
    import yfinance as yf
    from qs_config import SECTOR_STOCKS, TOP_STOCK_COUNT
    from screener import MomentumRanker

    hist = hist.copy()
    todo = hist[hist["ticker"].notna()]
    if only_missing:
        todo = todo[todo["composite_score"].isna()]
    dates = sorted(todo["date"].unique())
    stats = {"dates": len(dates), "filled": 0, "mismatch": 0, "total": 0}
    if not dates:
        return hist, stats

    universe = sorted({t for ts in SECTOR_STOCKS.values() for t in ts} | {BENCHMARK})
    raw = yf.download(universe, period="3y", progress=False, auto_adjust=True, group_by="ticker", threads=True)
    data: dict[str, pd.DataFrame] = {}
    for t in universe:
        try:
            sub = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
        except KeyError:
            continue
        if not sub.empty:
            sub.index = pd.to_datetime(sub.index).tz_localize(None).normalize()
            data[t] = sub
    bench = data[BENCHMARK]
    ranker = MomentumRanker(top_n=TOP_STOCK_COUNT)

    for d in dates:
        ts = pd.Timestamp(d)
        leaders = [x for x in str(hist.loc[hist["date"] == d, "leaders"].iloc[0]).split(",") if x and x != "nan"]
        cands = {t: data[t][data[t].index <= ts] for sec in leaders for t in SECTOR_STOCKS.get(sec, []) if t in data}
        ranked = ranker.rank_candidates(cands, bench[bench.index <= ts])
        if ranked.empty:
            continue
        top = set(ranked.head(TOP_STOCK_COUNT).index)
        mask = (hist["date"] == d) & hist["ticker"].notna()
        for i in hist[mask].index:
            t = hist.at[i, "ticker"]
            stats["total"] += 1
            if t not in top:
                stats["mismatch"] += 1
            if t in ranked.index and (not only_missing or pd.isna(hist.at[i, "composite_score"])):
                hist.at[i, "composite_score"] = round(float(ranked.at[t, "composite_score"]), 2)
                stats["filled"] += 1
    return hist, stats


# ------------------------------------------------------------------
# 에피소드 구성
# ------------------------------------------------------------------
def build_episodes(hist: pd.DataFrame) -> pd.DataFrame:
    """이력에서 종목별 '연속 편입 구간'(에피소드)을 만든다. 가격 정보는 아직 없다."""
    cols = ["ticker", "sector", "regime", "entry_date", "exit_date", "entry_price_snap",
            "stop_snap", "weight_pct", "rank", "composite_score"]
    if hist.empty:
        return pd.DataFrame(columns=cols)

    dates = sorted(hist["date"].unique())
    cand = hist.dropna(subset=["ticker"])
    present = {d: {r.ticker: r for r in g.itertuples()} for d, g in cand.groupby("date")}
    regime_of = hist.drop_duplicates("date").set_index("date")["regime"].to_dict()

    active: dict[str, dict] = {}
    episodes: list[dict] = []
    for d in dates:
        today = present.get(d, {})
        for t in list(active):
            if t not in today:                       # 순위 이탈 — 후보에서 빠진 첫 기록일이 이탈일
                active[t]["exit_date"] = d
                episodes.append(active.pop(t))
        for t, r in today.items():
            if t not in active:
                active[t] = {"ticker": t, "sector": r.sector, "regime": regime_of.get(d),
                             "entry_date": d, "exit_date": None,
                             "entry_price_snap": r.entry_price, "stop_snap": r.stop,
                             "weight_pct": r.weight_pct, "rank": r.rank,
                             "composite_score": r.composite_score}
    episodes.extend(active.values())
    return (pd.DataFrame(episodes, columns=cols)
            .sort_values(["entry_date", "rank"], kind="stable").reset_index(drop=True))


# ------------------------------------------------------------------
# 매매일지 매도 시그널 연결
# ------------------------------------------------------------------
EXIT_SHORT = {"STOP": "초기 손절", "BREAKEVEN_STOP": "본전 손절", "RUNNER_EXIT": "Runner 청산"}
SIGNAL_COLS = ["trade_entry_date", "first_signal_date", "first_signal_type",
               "exit_signal_date", "exit_signal_type"]


def attach_signals(episodes: pd.DataFrame, trades: pd.DataFrame | None,
                   signals: pd.DataFrame | None) -> pd.DataFrame:
    """에피소드마다 매매일지의 실제 매도 시그널(발송 기준)을 붙인다.

    연결 규칙: 같은 종목의 매매일지 포지션 중 매수일이 [편입일, 추적 종료일(시뮬레이션 전량 청산일)] 사이
    (아직 진행중이면 상한 없음)인 가장 이른 것 1건. 그 포지션의 시그널 중 가장 이른 것을 '첫 매도 시그널', 전량 청산 시그널
    (초기 손절/본전 손절/Runner 청산) 중 가장 이른 것을 '전량 청산 시그널'로 한다.
    (확인 여부와 무관하게 시그널이 발생한 날짜 기준)
    """
    ep = episodes.copy()
    for c in SIGNAL_COLS:
        ep[c] = None
    if ep.empty or trades is None or trades.empty:
        return ep
    from journal import SIGNAL_LABELS, EXIT_SIGNAL_TYPES

    sig = signals if signals is not None and not signals.empty else pd.DataFrame(columns=["trade_id", "date", "type"])
    for i, e in ep.iterrows():
        cand = trades[(trades["ticker"] == e["ticker"]) & (trades["entry_date"] >= e["entry_date"])]
        end = e["closed_date"] if "closed_date" in ep.columns else e["exit_date"]
        if not pd.isna(end):
            cand = cand[cand["entry_date"] <= end]
        if cand.empty:
            continue
        trade = cand.sort_values("entry_date", kind="stable").iloc[0]
        ep.at[i, "trade_entry_date"] = trade["entry_date"]
        ts = sig[sig["trade_id"] == trade["id"]].sort_values("date", kind="stable")
        if ts.empty:
            continue
        first = ts.iloc[0]
        ep.at[i, "first_signal_date"] = first["date"]
        ep.at[i, "first_signal_type"] = SIGNAL_LABELS.get(first["type"], first["type"])
        ex = ts[ts["type"].isin(EXIT_SIGNAL_TYPES)]
        if not ex.empty:
            ep.at[i, "exit_signal_date"] = ex.iloc[0]["date"]
            ep.at[i, "exit_signal_type"] = SIGNAL_LABELS.get(ex.iloc[0]["type"], ex.iloc[0]["type"])
    return ep


# ------------------------------------------------------------------
# 가격 조회 + 추적 계산
# ------------------------------------------------------------------
def fetch_prices(tickers: list[str], start: date) -> dict[str, pd.DataFrame]:
    """yfinance에서 여러 티커의 일봉(OHLCV, 조정주가)을 한 번에 받는다.

    Runner 청산(MA50)·ATR14 계산에 편입일 이전 데이터가 필요해 start보다 150일 앞부터 받는다."""
    import yfinance as yf

    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    raw = yf.download(tickers, start=str(start - pd.Timedelta(days=150)), progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna()
        except KeyError:
            continue
        if not sub.empty:
            sub.index = pd.to_datetime(sub.index).tz_localize(None).normalize()
            out[t] = sub
    return out


def _idx_on_or_after(index: pd.DatetimeIndex, d: date) -> int | None:
    pos = index.searchsorted(pd.Timestamp(d))
    return int(pos) if pos < len(index) else None


def _simulate_exit_rules(ind: pd.DataFrame, i0: int, c0: float, stop: float) -> dict:
    """편입일 종가(i0)에 매수했다고 보고 매매일지와 같은 상태 머신(journal._simulate_day)으로 매도 시그널을 찾는다.

    STAGE_0: 초기 손절 / +10% TP1(30%) -> STAGE_1: 본전 손절 / +20% TP2(30%) -> STAGE_2: 종가 < MA50 Runner 청산.
    전량 청산 시그널(초기 손절·본전 손절·Runner 청산)이 나면 거기서 추적을 끝낸다. 편입일 당일 고저가는
    매수 전 움직임이라 제외하고 다음 거래일부터 검사한다. 시그널 가격은 갭을 반영한다
    (손절류는 min(손절가, 시가), TP는 max(목표가, 시가)).
    """
    from journal import _simulate_day, EXIT_SIGNAL_TYPES

    trade = {"id": "sim", "ticker": "", "status": "OPEN", "phase": "STAGE_0", "entry_price": c0,
             "initial_shares": 100, "remaining_shares": 100, "current_stop": stop}
    sigs: list[dict] = []
    exit_idx = None
    for j in range(i0 + 1, len(ind)):
        row = ind.iloc[j]
        trade, day = _simulate_day(trade, ind.index[j], row)
        for sg in day:
            op = float(row["Open"])
            if sg["type"] in ("STOP", "BREAKEVEN_STOP"):
                sg["price"] = min(sg["price"], op)
            elif sg["type"] in ("TP1", "TP2"):
                sg["price"] = max(sg["price"], op)
            sg["idx"] = j
            sigs.append(sg)
        if any(sg["type"] in EXIT_SIGNAL_TYPES for sg in day):
            exit_idx = j
            break
    return {"signals": sigs, "exit_idx": exit_idx}


def _strategy_return(c0: float, sigs: list[dict], last_close: float) -> float:
    """분할 매도(TP1 30% · TP2 30% · 잔량 청산/현재가)를 비중대로 가중한 전략 수익률."""
    realized, remaining = 0.0, 1.0
    for sg in sigs:
        r = sg["price"] / c0 - 1
        if sg["type"] == "TP1":
            realized += TP1_FRACTION * r
            remaining -= TP1_FRACTION
        elif sg["type"] == "TP2":
            realized += TP2_FRACTION * r
            remaining -= TP2_FRACTION
        else:                                   # 전량 청산류
            realized += remaining * r
            remaining = 0.0
    return realized + remaining * (last_close / c0 - 1)


def compute_tracking(episodes: pd.DataFrame, prices: dict[str, pd.DataFrame],
                     max_days: int = MAX_TRACK_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """에피소드별 가격 추이 지표와 경과일별 수익률 경로(paths)를 계산한다.

    추적은 순위 이탈과 무관하게 전량 청산 시그널(초기 손절/본전 손절/Runner 청산)이 날 때까지 이어진다.
    같은 종목이 아직 추적 중일 때 다시 후보에 오르면 새 추적을 시작하지 않고 '재편입 횟수'로만 센다.
    max_days는 경과일별 경로(D+N)의 길이일 뿐 추적 기간 상한이 아니다.
    """
    from execution import ExecutionEngine

    engine = ExecutionEngine()
    bench = prices.get(BENCHMARK)
    ind_cache: dict[str, pd.DataFrame] = {}
    rows, paths = [], {}
    last_row_of: dict[str, int] = {}       # 종목 -> rows 내 마지막 추적 행 위치
    busy_until: dict[str, object] = {}     # 종목 -> 추적 종료일(진행중이면 None)

    for ep in episodes.sort_values(["ticker", "entry_date"], kind="stable").itertuples():
        row = ep._asdict()
        row.pop("Index", None)

        # 앞선 추적이 아직 안 끝났으면 재편입으로만 센다
        if ep.ticker in busy_until and (busy_until[ep.ticker] is None or ep.entry_date < busy_until[ep.ticker]):
            rows[last_row_of[ep.ticker]]["reentries"] = rows[last_row_of[ep.ticker]].get("reentries", 0) + 1
            continue

        px = prices.get(ep.ticker)
        i0 = _idx_on_or_after(px.index, ep.entry_date) if px is not None else None
        if px is None or i0 is None or px.index[i0] != pd.Timestamp(ep.entry_date):
            rows.append({**row, "status": "가격없음", "reentries": 0})
            continue

        if ep.ticker not in ind_cache:
            ind_cache[ep.ticker] = engine.compute_indicators(px)
        ind = ind_cache[ep.ticker]
        close = px["Close"].to_numpy()
        c0, last = float(close[i0]), len(px) - 1

        if ep.entry_price_snap and ep.stop_snap and ep.entry_price_snap == ep.entry_price_snap:
            stop = c0 * float(ep.stop_snap) / float(ep.entry_price_snap)
        else:
            atr = ind["ATR14"].iloc[i0]
            stop = engine.initial_stop_v2(c0, float(atr)) if atr == atr else np.nan
        if stop != stop:
            rows.append({**row, "status": "가격없음", "reentries": 0})
            continue

        sim = _simulate_exit_rules(ind, i0, c0, stop)
        sigs, exit_idx = sim["signals"], sim["exit_idx"]
        end = exit_idx if exit_idx is not None else last
        exit_sig = next((sg for sg in sigs if sg["idx"] == exit_idx and sg["type"] in
                         ("STOP", "BREAKEVEN_STOP", "RUNNER_EXIT")), None) if exit_idx is not None else None
        exit_price = exit_sig["price"] if exit_sig else float(close[end])
        status = "진행중" if exit_sig is None else f"종료({EXIT_SHORT[exit_sig['type']]})"

        win = px.iloc[i0 + 1:end + 1]
        next_open = float(px["Open"].iloc[i0 + 1]) if i0 + 1 <= last else np.nan
        ret_close = exit_price / c0 - 1
        ret_open = exit_price / next_open - 1 if next_open == next_open and end >= i0 + 1 else np.nan
        mfe = float(win["High"].max() / c0 - 1) if not win.empty else np.nan
        mae = float(win["Low"].min() / c0 - 1) if not win.empty else np.nan
        strat = _strategy_return(c0, sigs, float(close[end]))
        tp = {sg["type"]: px.index[sg["idx"]].date() for sg in sigs if sg["type"] in ("TP1", "TP2")}

        spy_ret = np.nan
        if bench is not None:
            b0 = _idx_on_or_after(bench.index, ep.entry_date)
            b1 = bench.index.searchsorted(px.index[end], side="right") - 1
            if b0 is not None and b1 >= b0:
                spy_ret = float(bench["Close"].iloc[b1] / bench["Close"].iloc[b0] - 1)

        # 순위 이탈일 시점 수익률(= 이탈 때 팔았다면) vs 시그널까지 보유
        ret_at_rank_exit = np.nan
        if not pd.isna(ep.exit_date):
            ix = _idx_on_or_after(px.index, ep.exit_date)
            if ix is not None and i0 < ix <= end:
                ret_at_rank_exit = float(close[ix] / c0 - 1)

        fwd = {f"d{n}": (close[i0 + n] / c0 - 1 if i0 + n <= last else np.nan) for n in FORWARD_DAYS}
        paths[f"{ep.ticker} {ep.entry_date:%Y-%m-%d}"] = [
            close[i0 + k] / c0 - 1 if i0 + k <= last else np.nan for k in range(max_days + 1)]

        closed = px.index[end].date() if exit_sig is not None else None
        busy_until[ep.ticker] = closed
        last_row_of[ep.ticker] = len(rows)
        rows.append({**row, "status": status, "tracked_days": end - i0, "closed_date": closed,
                     "exit_type": exit_sig["type"] if exit_sig else None, "last_date": px.index[end].date(),
                     "entry_close": c0, "next_open": next_open, "exit_price": exit_price,
                     "ret_close": ret_close, "ret_open": ret_open, "strat_ret": strat,
                     "spy_ret": spy_ret, "excess": ret_close - spy_ret if spy_ret == spy_ret else np.nan,
                     "mfe": mfe, "mae": mae, "stop_hit": exit_sig is not None and exit_sig["type"] == "STOP",
                     "tp1_date": tp.get("TP1"), "tp2_date": tp.get("TP2"), "reentries": 0,
                     "ret_at_rank_exit": ret_at_rank_exit,
                     "extra_after_exit": ret_close - ret_at_rank_exit if ret_at_rank_exit == ret_at_rank_exit else np.nan,
                     **fwd})

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["entry_date", "rank"], kind="stable").reset_index(drop=True)
    path_df = pd.DataFrame.from_dict(paths, orient="index", columns=[f"D+{k}" for k in range(max_days + 1)])
    path_df.index.name = "에피소드"
    return out, path_df


def track(hist: pd.DataFrame, max_days: int = MAX_TRACK_DAYS, trades: pd.DataFrame | None = None,
          signals: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """이력 -> (추적 지표가 붙은 에피소드, 경과일별 경로). 가격은 yfinance에서 새로 받는다.
    trades/signals는 journal.load_trades()/load_signals() 결과(없으면 실제 매매 시그널 컬럼은 빈칸)."""
    episodes = build_episodes(hist)
    if episodes.empty:
        return episodes, pd.DataFrame()
    prices = fetch_prices(list(episodes["ticker"].unique()) + [BENCHMARK], min(episodes["entry_date"]))
    ep, paths = compute_tracking(episodes, prices, max_days)
    return attach_signals(ep, trades, signals), paths


# ------------------------------------------------------------------
# 요약 통계
# ------------------------------------------------------------------
def _group_stats(df: pd.DataFrame, by: str, label: str) -> pd.DataFrame:
    g = df.groupby(by, dropna=False)
    out = pd.DataFrame({
        "에피소드 수": g.size(),
        "평균 보유일": g["tracked_days"].mean(),
        "평균 수익률": g["ret_close"].mean(),
        "평균 전략 수익률": g["strat_ret"].mean(),
        "승률": g["strat_ret"].apply(lambda s: (s > 0).mean()),
        "평균 SPY 대비 초과": g["excess"].mean(),
        "평균 최대상승": g["mfe"].mean(),
        "평균 최대하락": g["mae"].mean(),
    })
    return out.reset_index().rename(columns={by: label})


def summarize(ep: pd.DataFrame) -> dict:
    """요약 시트용 통계. 가격이 계산된 에피소드만 대상으로 한다.

    평균 수익률은 편입일 종가 -> 종료가(청산 시그널가 또는 현재가), 전략 수익률은 TP1/TP2 분할 매도를
    비중대로 가중한 값이다. 승률은 전략 수익률 기준.
    """
    empty = {"overall": pd.DataFrame(), "by_exit": pd.DataFrame(), "by_regime": pd.DataFrame(),
             "by_sector": pd.DataFrame(), "fwd": pd.DataFrame()}
    ok = ep[ep["status"] != "가격없음"] if "status" in ep.columns else ep
    if ok.empty or "ret_close" not in ok.columns:
        return empty
    closed = ok[ok["status"] != "진행중"]
    overall = pd.DataFrame({
        "지표": ["에피소드 수", "청산 완료", "진행중", "평균 보유일", "평균 수익률(종료가 기준)",
                "평균 전략 수익률(분할매도 가중)", "승률(전략 수익률 > 0)", "평균 SPY 대비 초과",
                "평균 최대상승(MFE)", "평균 최대하락(MAE)", "순위 이탈 후 평균 추가 수익(이탈일→종료)"],
        "값": [len(ok), len(closed), int((ok["status"] == "진행중").sum()), ok["tracked_days"].mean(),
              ok["ret_close"].mean(), ok["strat_ret"].mean(), (ok["strat_ret"] > 0).mean(),
              ok["excess"].mean(), ok["mfe"].mean(), ok["mae"].mean(), ok["extra_after_exit"].mean()],
    })
    fwd = pd.DataFrame({
        "경과일": [f"D+{n}" for n in FORWARD_DAYS],
        "표본 수": [int(ok[f"d{n}"].notna().sum()) for n in FORWARD_DAYS],
        "평균 수익률": [ok[f"d{n}"].mean() for n in FORWARD_DAYS],
        "승률": [(ok[f"d{n}"].dropna() > 0).mean() if ok[f"d{n}"].notna().any() else np.nan
                for n in FORWARD_DAYS],
    })
    return {"overall": overall, "by_exit": _group_stats(ok, "status", "추적 상태"),
            "by_regime": _group_stats(ok, "regime", "편입 시 국면"),
            "by_sector": _group_stats(ok, "sector", "섹터"), "fwd": fwd}


def daily_summary(hist: pd.DataFrame) -> pd.DataFrame:
    """날짜별 1행: 국면, 주도섹터, 후보 종목 목록, 신규편입/순위 이탈."""
    rows, prev = [], set()
    for d, g in hist.groupby("date"):
        tickers = list(g.dropna(subset=["ticker"]).sort_values("rank")["ticker"])
        cur = set(tickers)
        rows.append({"날짜": d, "국면": g["regime"].iloc[0], "주도 섹터": g["leaders"].iloc[0] or "",
                     "후보 수": len(tickers), "후보 종목": ", ".join(tickers),
                     "신규 편입": ", ".join(t for t in tickers if t not in prev),
                     "순위 이탈": ", ".join(sorted(prev - cur))})
        prev = cur
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 엑셀 출력
# ------------------------------------------------------------------
EP_EXPORT = [
    ("composite_score", "편입일 CompositeScore"), ("ticker", "종목"), ("sector", "섹터"),
    ("regime", "편입 시 국면"), ("entry_date", "편입일"),
    ("status", "추적 상태"), ("tracked_days", "추적 거래일"), ("last_date", "추적 종료일(진행중=최신일)"),
    ("tp1_date", "TP1 도달일"), ("tp2_date", "TP2 도달일"),
    ("exit_date", "순위 이탈일"), ("reentries", "재편입 횟수"),
    ("entry_close", "편입일 종가"), ("next_open", "익일 시가"), ("exit_price", "종료가(청산가/현재가)"),
    ("ret_close", "수익률(종가 기준)"), ("ret_open", "수익률(익일시가 기준)"),
    ("strat_ret", "전략 수익률(분할매도 가중)"), ("spy_ret", "SPY 수익률"), ("excess", "SPY 대비 초과"),
    ("mfe", "최대상승(MFE)"), ("mae", "최대하락(MAE)"),
    ("ret_at_rank_exit", "순위 이탈일 시점 수익률"), ("extra_after_exit", "이탈 후 추가 수익"),
    ("trade_entry_date", "매매일지 매수일"), ("first_signal_date", "실제 첫 매도 시그널일"),
    ("first_signal_type", "실제 첫 매도 시그널"), ("exit_signal_date", "실제 전량 청산 시그널일"),
    ("exit_signal_type", "실제 전량 청산 시그널"),
    ("d1", "D+1"), ("d5", "D+5"), ("d10", "D+10"), ("d20", "D+20"), ("d60", "D+60"),
    ("weight_pct", "비중(%)"), ("rank", "편입 시 순위"),
]
_PCT_COLS = {"수익률(종가 기준)", "수익률(익일시가 기준)", "전략 수익률(분할매도 가중)", "SPY 수익률",
             "SPY 대비 초과", "최대상승(MFE)", "최대하락(MAE)", "순위 이탈일 시점 수익률", "이탈 후 추가 수익",
             "D+1", "D+5", "D+10", "D+20", "D+60"}
_DATE_COLS = {"편입일", "순위 이탈일", "추적 종료일(진행중=최신일)", "TP1 도달일", "TP2 도달일",
              "매매일지 매수일", "실제 첫 매도 시그널일", "실제 전량 청산 시그널일"}


def build_excel(hist: pd.DataFrame, episodes: pd.DataFrame, paths: pd.DataFrame) -> bytes:
    """이력/추적 결과를 5개 시트의 .xlsx 바이트로 만든다."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 아직 도달 못한 경과일(전부 NaN) 평균
        summ = summarize(episodes)
    with pd.ExcelWriter(buf, engine="xlsxwriter", datetime_format="yyyy-mm-dd", date_format="yyyy-mm-dd") as xw:
        wb = xw.book
        f_head = wb.add_format({"bold": True, "bg_color": "#1F3A5F", "font_color": "#FFFFFF",
                                "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
        f_pct = wb.add_format({"num_format": "+0.0%;[Blue]-0.0%;0.0%"})
        f_date = wb.add_format({"num_format": "yyyy-mm-dd"})
        f_num = wb.add_format({"num_format": "#,##0.00"})
        f_title = wb.add_format({"bold": True, "font_size": 14})
        f_note = wb.add_format({"italic": True, "font_color": "#666666"})
        f_red = wb.add_format({"font_color": "#C00000"})

        def write_table(df: pd.DataFrame, sheet: str, widths: dict | None = None, startrow: int = 0,
                        freeze_cols: int = 1):
            df.to_excel(xw, sheet_name=sheet, index=False, startrow=startrow)
            ws = xw.sheets[sheet]
            for c, name in enumerate(df.columns):
                ws.write(startrow, c, name, f_head)
                w = (widths or {}).get(name, max(10, min(34, len(str(name)) * 2 + 2)))
                fmt = f_pct if name in _PCT_COLS else (f_date if name in _DATE_COLS else None)
                ws.set_column(c, c, w, fmt)
            ws.freeze_panes(startrow + 1, freeze_cols)
            ws.autofilter(startrow, 0, startrow + len(df), len(df.columns) - 1)
            return ws

        # --- 시트1: 요약 --------------------------------------------------
        ws = wb.add_worksheet("요약")
        xw.sheets["요약"] = ws
        ws.write(0, 0, "스크리닝 종목 추적 요약", f_title)
        ws.write(1, 0, f"생성일 {date.today():%Y-%m-%d} · 기록 {hist['date'].nunique() if not hist.empty else 0}거래일 · "
                       f"에피소드 {len(episodes)}건 · 기준가=편입일 종가, 추적은 전량 청산 시그널(초기 손절·본전 손절·"
                       f"Runner 청산)까지(순위 이탈과 무관)", f_note)
        r = 3
        if not summ["overall"].empty:
            ov = summ["overall"]
            ws.write(r, 0, "전체", f_head); ws.write(r, 1, "값", f_head)
            for i, (k, v) in enumerate(zip(ov["지표"], ov["값"]), start=1):
                ws.write(r + i, 0, k)
                if k in ("에피소드 수", "청산 완료", "진행중"):
                    ws.write_number(r + i, 1, v)
                elif k == "평균 보유일":
                    ws.write_number(r + i, 1, v, f_num) if v == v else ws.write(r + i, 1, "-")
                else:
                    ws.write_number(r + i, 1, v, f_pct) if v == v else ws.write(r + i, 1, "-")
            r += len(ov) + 3
            for title, key in (("추적 상태별(청산 사유)", "by_exit"), ("편입 후 경과일별(D+N) 평균", "fwd"),
                               ("편입 시 국면별", "by_regime"), ("섹터별", "by_sector")):
                df = summ[key]
                ws.write(r, 0, title, f_title)
                r += 1
                for c, name in enumerate(df.columns):
                    ws.write(r, c, name, f_head)
                for i, rec in enumerate(df.itertuples(index=False), start=1):
                    for c, v in enumerate(rec):
                        name = df.columns[c]
                        if isinstance(v, (float, np.floating)) and name == "평균 보유일":
                            ws.write_number(r + i, c, v, f_num) if v == v else ws.write(r + i, c, "-")
                        elif isinstance(v, (float, np.floating)) and name != "표본 수":
                            ws.write_number(r + i, c, v, f_pct) if v == v else ws.write(r + i, c, "-")
                        elif isinstance(v, (int, np.integer)):
                            ws.write_number(r + i, c, int(v))
                        else:
                            ws.write(r + i, c, "-" if v is None else str(v))
                r += len(df) + 3
            ws.set_column(0, 0, 24); ws.set_column(1, 8, 16)

            # 평균 경로 차트 (경과일별 수익률 시트의 평균/중앙값 행 참조)
            if not paths.empty:
                n = paths.shape[1]
                ch = wb.add_chart({"type": "line"})
                for label, row_off, color in (("평균", 1, "#1F3A5F"), ("중앙값", 2, "#E07B00")):
                    ch.add_series({"name": label, "categories": ["경과일 수익률", 0, 1, 0, n],
                                   "values": ["경과일 수익률", len(paths) + row_off, 1, len(paths) + row_off, n],
                                   "line": {"color": color, "width": 2}})
                ch.set_title({"name": "편입 후 경과일별 평균/중앙값 수익률"})
                ch.set_y_axis({"num_format": "0%", "major_gridlines": {"visible": True, "line": {"color": "#DDDDDD"}}})
                ch.set_x_axis({"name": "편입 후 거래일", "interval_unit": 5})
                ch.set_size({"width": 760, "height": 340})
                ws.insert_chart(3, 7, ch)

        # --- 시트2: 일자별 요약 -------------------------------------------
        if not hist.empty:
            write_table(daily_summary(hist), "일자별 요약",
                        {"날짜": 12, "국면": 11, "주도 섹터": 16, "후보 수": 8, "후보 종목": 40,
                         "신규 편입": 22, "순위 이탈": 22})

        # --- 시트3: 스크리닝 이력(원본) -----------------------------------
        raw = hist.rename(columns={"date": "날짜", "regime": "국면", "sideways_mode": "SIDEWAYS 방식",
                                   "leaders": "주도 섹터", "rank": "순위", "ticker": "종목", "sector": "섹터",
                                   "composite_score": "CompositeScore",
                                   "entry_price": "진입가", "stop": "손절가", "weight_pct": "비중(%)",
                                   "shares": "수량"})
        write_table(raw, "스크리닝 이력", {"날짜": 12})

        # --- 시트4: 종목별 추적 -------------------------------------------
        ep = episodes.copy()
        for col, _ in EP_EXPORT:
            if col not in ep.columns:
                ep[col] = np.nan
        tr = ep[[c for c, _ in EP_EXPORT]].rename(columns=dict(EP_EXPORT))
        tr = tr.astype(object).where(tr.notna(), None)
        wst = write_table(tr, "종목별 추적", {"종목": 9, "편입일": 12, "순위 이탈일": 12, "추적 종료일(진행중=최신일)": 14,
                                              "TP1 도달일": 12, "TP2 도달일": 12, "추적 상태": 20,
                                              "편입 시 국면": 12, "편입일 CompositeScore": 14,
                                              "매매일지 매수일": 12, "실제 첫 매도 시그널일": 14,
                                              "실제 첫 매도 시그널": 22, "실제 전량 청산 시그널일": 15,
                                              "실제 전량 청산 시그널": 22, "전략 수익률(분할매도 가중)": 16},
                         freeze_cols=2)
        if len(tr):
            cols = list(tr.columns)
            for name in _PCT_COLS & set(cols):
                c = cols.index(name)
                wst.conditional_format(1, c, len(tr), c, {"type": "cell", "criteria": ">", "value": 0,
                                                          "format": f_red})
            for name in ("수익률(종가 기준)", "전략 수익률(분할매도 가중)"):
                c = cols.index(name)
                wst.conditional_format(1, c, len(tr), c, {"type": "data_bar", "bar_solid": True,
                                                          "bar_negative_color": "#4F81BD", "bar_color": "#E06666"})

        # --- 시트5: 경과일 수익률 (히트맵) --------------------------------
        if not paths.empty:
            pdf = paths.reset_index()
            wsp = write_table(pdf, "경과일 수익률", {"에피소드": 18})
            for c in range(1, pdf.shape[1]):
                wsp.set_column(c, c, 7.5, f_pct)
            n = pdf.shape[1] - 1
            base = len(pdf) + 1
            wsp.write(base, 0, "평균", f_head)
            wsp.write(base + 1, 0, "중앙값", f_head)
            for c in range(1, n + 1):
                col = pdf.columns[c]
                has = paths[col].notna().any()           # 아직 도달 못한 경과일(전부 NaN)은 평균 생략
                a = paths[col].mean() if has else np.nan
                m = paths[col].median() if has else np.nan
                if a == a:
                    wsp.write_number(base, c, a, f_pct)
                if m == m:
                    wsp.write_number(base + 1, c, m, f_pct)
            wsp.conditional_format(1, 1, len(pdf), n, {"type": "3_color_scale", "min_color": "#4F81BD",
                                                       "mid_color": "#FFFFFF", "max_color": "#E06666",
                                                       "mid_type": "num", "mid_value": 0})
            wsp.write(base + 3, 0, "※ 순위 이탈 여부와 무관하게 편입 후 N번째 거래일 종가 기준(이탈 후에도 보유했다면). "
                                   "아직 도달하지 않은 경과일은 빈칸.", f_note)
    return buf.getvalue()


# ------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backfill":
        h = backfill_from_git()
        print(f"이력 {h['date'].nunique()}일 / {len(h)}행 저장")
    elif cmd == "backfill-scores":
        h, st = recompute_scores(load_history())
        save_history(h, "Backfill CompositeScore into screening history")
        print(f"CompositeScore 복원: {st['dates']}일 / {st['filled']}건 채움 · "
              f"재계산 상위5 불일치 {st['mismatch']}/{st['total']}건")
    elif cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "screening_tracking.xlsx"
        h = load_history()
        import journal
        e, p = track(h, trades=journal.load_trades(), signals=journal.load_signals())
        with open(out, "wb") as f:
            f.write(build_excel(h, e, p))
        print(f"{out} 생성 (에피소드 {len(e)}건)")
    else:
        print(__doc__)
