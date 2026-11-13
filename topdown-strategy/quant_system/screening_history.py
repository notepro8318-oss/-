"""
[Module 7] Screening History — 스크리닝 종목 날짜별 기록 + 가격 추이 추적 + 엑셀 출력
====================================================
screening.py의 스냅샷은 매번 덮어써져 "직전 결과"만 남는다. 이 모듈은 거래일마다의
스크리닝 결과를 `screening_history.csv`(날짜 x 종목, long 포맷)에 누적 기록하고,
편입일 이후 가격 추이(수익률/최대상승/최대하락/손절 터치/SPY 대비 초과수익)를 계산해
엑셀(.xlsx)로 내보낸다.

저장소는 매매일지/스냅샷과 같은 방식이다. GITHUB_TOKEN이 있으면 GitHub Contents API
(github_store.py)로 스케줄러(GitHub Actions)와 Streamlit이 같은 파일을 공유하고, 없으면
로컬 CSV로 폴백한다.

추적 정의
---------
- 에피소드: 한 종목이 기록된 거래일에 연속으로 후보에 오른 구간. 후보에서 빠진 뒤 다시
  들어오면 새 에피소드로 시작한다.
- 기준가: 편입일(스크리닝 당일) 종가. 참고용으로 다음 거래일 시가 기준 수익률도 함께 계산한다.
  (수익률은 모두 지금 시점의 조정주가로 일관되게 계산하므로 스냅샷의 entry_price와 소수점
  단위로 다를 수 있다.)
- 추적 구간: 편입일 ~ min(편출일, 편입 후 MAX_TRACK_DAYS 거래일, 최신 거래일).
  상태는 '진행중' / '편출' / '추적종료(60일)'.
- 편입 후 +N일 수익률(D+N)은 편출 여부와 무관하게 편입 후 N번째 거래일 종가 기준으로
  계산한다 (편출 후에도 이 종목을 들고 있었다면의 성과 비교용).
- 최대상승(MFE)/최대하락(MAE)은 추적 구간의 고가/저가 기준, 손절가는 스냅샷의
  (손절가/진입가) 비율을 기준가에 적용해 구한다.

CLI
---
    python screening_history.py backfill          # git 커밋 이력에서 과거 스냅샷 복원
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

HISTORY_REPO_PATH = "topdown-strategy/quant_system/screening_history.csv"
HISTORY_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "screening_history.csv")
SNAPSHOT_REPO_PATH = "topdown-strategy/quant_system/screening_snapshot.json"

HISTORY_COLUMNS = ["date", "regime", "sideways_mode", "leaders", "rank", "ticker", "sector",
                   "entry_price", "stop", "weight_pct", "shares"]

MAX_TRACK_DAYS = 60
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
    for col in ["rank", "entry_price", "stop", "weight_pct", "shares"]:
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
                     "entry_price": c.get("entry_price"), "stop": c.get("stop"),
                     "weight_pct": c.get("weight_pct"), "shares": c.get("shares")})
    if not rows:
        rows.append({**base, "rank": None, "ticker": None, "sector": None,
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


# ------------------------------------------------------------------
# 에피소드 구성
# ------------------------------------------------------------------
def build_episodes(hist: pd.DataFrame) -> pd.DataFrame:
    """이력에서 종목별 '연속 편입 구간'(에피소드)을 만든다. 가격 정보는 아직 없다."""
    cols = ["ticker", "sector", "regime", "entry_date", "exit_date", "entry_price_snap",
            "stop_snap", "weight_pct", "rank"]
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
            if t not in today:                       # 편출 — 후보에서 빠진 첫 기록일이 편출일
                active[t]["exit_date"] = d
                episodes.append(active.pop(t))
        for t, r in today.items():
            if t not in active:
                active[t] = {"ticker": t, "sector": r.sector, "regime": regime_of.get(d),
                             "entry_date": d, "exit_date": None,
                             "entry_price_snap": r.entry_price, "stop_snap": r.stop,
                             "weight_pct": r.weight_pct, "rank": r.rank}
    episodes.extend(active.values())
    return (pd.DataFrame(episodes, columns=cols)
            .sort_values(["entry_date", "rank"], kind="stable").reset_index(drop=True))


# ------------------------------------------------------------------
# 가격 조회 + 추적 계산
# ------------------------------------------------------------------
def fetch_prices(tickers: list[str], start: date) -> dict[str, pd.DataFrame]:
    """yfinance에서 여러 티커의 일봉(Open/High/Low/Close, 조정주가)을 한 번에 받는다."""
    import yfinance as yf

    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    raw = yf.download(tickers, start=str(start - pd.Timedelta(days=7)), progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            sub = sub[["Open", "High", "Low", "Close"]].dropna()
        except KeyError:
            continue
        if not sub.empty:
            sub.index = pd.to_datetime(sub.index).tz_localize(None).normalize()
            out[t] = sub
    return out


def _idx_on_or_after(index: pd.DatetimeIndex, d: date) -> int | None:
    pos = index.searchsorted(pd.Timestamp(d))
    return int(pos) if pos < len(index) else None


def compute_tracking(episodes: pd.DataFrame, prices: dict[str, pd.DataFrame],
                     max_days: int = MAX_TRACK_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """에피소드별 가격 추이 지표(episodes 확장본)와 경과일별 수익률 경로(paths)를 계산한다."""
    bench = prices.get(BENCHMARK)
    rows, paths = [], {}
    for ep in episodes.itertuples():
        row = ep._asdict()
        row.pop("Index", None)
        px = prices.get(ep.ticker)
        key = f"{ep.ticker} {ep.entry_date:%Y-%m-%d}"
        i0 = _idx_on_or_after(px.index, ep.entry_date) if px is not None else None
        if px is None or i0 is None or px.index[i0] != pd.Timestamp(ep.entry_date):
            rows.append({**row, "status": "가격없음"})
            continue

        close, c0 = px["Close"].to_numpy(), float(px["Close"].iloc[i0])
        last = len(px) - 1

        # 추적 구간 끝: 편출일 / 편입 후 max_days / 최신 거래일 중 가장 빠른 것
        has_exit = not pd.isna(ep.exit_date)
        i_exit = _idx_on_or_after(px.index, ep.exit_date) if has_exit else None
        i_cap = i0 + max_days
        if i_exit is not None and i_exit <= min(i_cap, last):
            end, status = i_exit, "편출"
        elif i_cap <= last:
            end, status = i_cap, "추적종료(60일)"
        else:
            end, status = last, "진행중"
        if has_exit and status == "진행중":
            status = "편출"   # 편출은 확정됐지만 편출일 일봉이 아직 없어 최신가까지만 추적

        win = px.iloc[i0 + 1:end + 1]
        ret_close = close[end] / c0 - 1
        next_open = float(px["Open"].iloc[i0 + 1]) if i0 + 1 <= last else np.nan
        ret_open = close[end] / next_open - 1 if next_open == next_open and end >= i0 + 1 else np.nan
        mfe = float(win["High"].max() / c0 - 1) if not win.empty else np.nan
        mae = float(win["Low"].min() / c0 - 1) if not win.empty else np.nan

        stop_hit, stop_hit_date = False, None
        if ep.entry_price_snap and ep.stop_snap and not win.empty:
            stop_level = c0 * float(ep.stop_snap) / float(ep.entry_price_snap)
            hit = win[win["Low"] <= stop_level]
            if not hit.empty:
                stop_hit, stop_hit_date = True, hit.index[0].date()

        spy_ret = np.nan
        if bench is not None:
            b0 = _idx_on_or_after(bench.index, ep.entry_date)
            b1 = bench.index.searchsorted(px.index[end], side="right") - 1
            if b0 is not None and b1 >= b0:
                spy_ret = float(bench["Close"].iloc[b1] / bench["Close"].iloc[b0] - 1)

        fwd = {f"d{n}": (close[i0 + n] / c0 - 1 if i0 + n <= last else np.nan) for n in FORWARD_DAYS}
        paths[key] = [close[i0 + k] / c0 - 1 if i0 + k <= last else np.nan for k in range(max_days + 1)]

        rows.append({**row, "status": status, "tracked_days": end - i0, "last_date": px.index[end].date(),
                     "entry_close": c0, "next_open": next_open, "last_close": float(close[end]),
                     "ret_close": ret_close, "ret_open": ret_open, "mfe": mfe, "mae": mae,
                     "stop_hit": stop_hit, "stop_hit_date": stop_hit_date,
                     "spy_ret": spy_ret, "excess": ret_close - spy_ret if spy_ret == spy_ret else np.nan, **fwd})

    out = pd.DataFrame(rows)
    path_df = pd.DataFrame.from_dict(paths, orient="index", columns=[f"D+{k}" for k in range(max_days + 1)])
    path_df.index.name = "에피소드"
    return out, path_df


def track(hist: pd.DataFrame, max_days: int = MAX_TRACK_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """이력 -> (추적 지표가 붙은 에피소드, 경과일별 경로). 가격은 yfinance에서 새로 받는다."""
    episodes = build_episodes(hist)
    if episodes.empty:
        return episodes, pd.DataFrame()
    prices = fetch_prices(list(episodes["ticker"].unique()) + [BENCHMARK], min(episodes["entry_date"]))
    return compute_tracking(episodes, prices, max_days)


# ------------------------------------------------------------------
# 요약 통계
# ------------------------------------------------------------------
def _group_stats(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by, dropna=False)
    out = pd.DataFrame({
        "에피소드 수": g.size(),
        "평균 수익률": g["ret_close"].mean(),
        "승률": g["ret_close"].apply(lambda s: (s > 0).mean()),
        "평균 SPY 대비 초과": g["excess"].mean(),
        "평균 최대상승": g["mfe"].mean(),
        "평균 최대하락": g["mae"].mean(),
        "손절 터치율": g["stop_hit"].mean(),
    })
    return out.reset_index().rename(columns={by: {"regime": "편입 시 국면", "sector": "섹터"}[by]})


def summarize(ep: pd.DataFrame) -> dict:
    """요약 시트용 통계. 가격이 계산된 에피소드만 대상으로 한다."""
    ok = ep[ep["status"] != "가격없음"] if "status" in ep.columns else ep
    if ok.empty or "ret_close" not in ok.columns:
        return {"overall": pd.DataFrame(), "by_regime": pd.DataFrame(), "by_sector": pd.DataFrame(),
                "fwd": pd.DataFrame()}
    overall = pd.DataFrame({
        "지표": ["에피소드 수", "진행중", "평균 수익률(추적구간)", "승률", "평균 SPY 대비 초과",
                "평균 최대상승(MFE)", "평균 최대하락(MAE)", "손절 터치율"],
        "값": [len(ok), int((ok["status"] == "진행중").sum()), ok["ret_close"].mean(),
              (ok["ret_close"] > 0).mean(), ok["excess"].mean(), ok["mfe"].mean(),
              ok["mae"].mean(), ok["stop_hit"].mean()],
    })
    fwd = pd.DataFrame({
        "경과일": [f"D+{n}" for n in FORWARD_DAYS],
        "표본 수": [int(ok[f"d{n}"].notna().sum()) for n in FORWARD_DAYS],
        "평균 수익률": [ok[f"d{n}"].mean() for n in FORWARD_DAYS],
        "승률": [(ok[f"d{n}"].dropna() > 0).mean() if ok[f"d{n}"].notna().any() else np.nan
                for n in FORWARD_DAYS],
    })
    return {"overall": overall, "by_regime": _group_stats(ok, "regime"),
            "by_sector": _group_stats(ok, "sector"), "fwd": fwd}


def daily_summary(hist: pd.DataFrame) -> pd.DataFrame:
    """날짜별 1행: 국면, 주도섹터, 후보 종목 목록, 신규편입/편출."""
    rows, prev = [], set()
    for d, g in hist.groupby("date"):
        tickers = list(g.dropna(subset=["ticker"]).sort_values("rank")["ticker"])
        cur = set(tickers)
        rows.append({"날짜": d, "국면": g["regime"].iloc[0], "주도 섹터": g["leaders"].iloc[0] or "",
                     "후보 수": len(tickers), "후보 종목": ", ".join(tickers),
                     "신규 편입": ", ".join(t for t in tickers if t not in prev),
                     "편출": ", ".join(sorted(prev - cur))})
        prev = cur
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# 엑셀 출력
# ------------------------------------------------------------------
EP_EXPORT = [
    ("ticker", "종목"), ("sector", "섹터"), ("regime", "편입 시 국면"), ("entry_date", "편입일"),
    ("exit_date", "편출일"), ("status", "상태"), ("tracked_days", "추적 거래일"),
    ("entry_close", "편입일 종가"), ("next_open", "익일 시가"), ("last_date", "추적 종료일"),
    ("last_close", "종료일 종가"), ("ret_close", "수익률(종가 기준)"), ("ret_open", "수익률(익일시가 기준)"),
    ("spy_ret", "SPY 수익률"), ("excess", "SPY 대비 초과"), ("mfe", "최대상승(MFE)"), ("mae", "최대하락(MAE)"),
    ("stop_hit", "손절 터치"), ("stop_hit_date", "손절 터치일"),
    ("d1", "D+1"), ("d5", "D+5"), ("d10", "D+10"), ("d20", "D+20"), ("d60", "D+60"),
    ("weight_pct", "비중(%)"), ("rank", "편입 시 순위"),
]
_PCT_COLS = {"수익률(종가 기준)", "수익률(익일시가 기준)", "SPY 수익률", "SPY 대비 초과", "최대상승(MFE)",
             "최대하락(MAE)", "D+1", "D+5", "D+10", "D+20", "D+60"}
_DATE_COLS = {"편입일", "편출일", "추적 종료일", "손절 터치일"}


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

        def write_table(df: pd.DataFrame, sheet: str, widths: dict | None = None, startrow: int = 0):
            df.to_excel(xw, sheet_name=sheet, index=False, startrow=startrow)
            ws = xw.sheets[sheet]
            for c, name in enumerate(df.columns):
                ws.write(startrow, c, name, f_head)
                w = (widths or {}).get(name, max(10, min(34, len(str(name)) * 2 + 2)))
                fmt = f_pct if name in _PCT_COLS else (f_date if name in _DATE_COLS else None)
                ws.set_column(c, c, w, fmt)
            ws.freeze_panes(startrow + 1, 1)
            ws.autofilter(startrow, 0, startrow + len(df), len(df.columns) - 1)
            return ws

        # --- 시트1: 요약 --------------------------------------------------
        ws = wb.add_worksheet("요약")
        xw.sheets["요약"] = ws
        ws.write(0, 0, "스크리닝 종목 추적 요약", f_title)
        ws.write(1, 0, f"생성일 {date.today():%Y-%m-%d} · 기록 {hist['date'].nunique() if not hist.empty else 0}거래일 · "
                       f"에피소드 {len(episodes)}건 · 기준가=편입일 종가, 수익률은 편입일 이후 변화", f_note)
        r = 3
        if not summ["overall"].empty:
            ov = summ["overall"]
            ws.write(r, 0, "전체", f_head); ws.write(r, 1, "값", f_head)
            for i, (k, v) in enumerate(zip(ov["지표"], ov["값"]), start=1):
                ws.write(r + i, 0, k)
                if k in ("에피소드 수", "진행중"):
                    ws.write_number(r + i, 1, v)
                else:
                    ws.write_number(r + i, 1, v, f_pct) if v == v else ws.write(r + i, 1, "-")
            r += len(ov) + 3
            for title, key in (("편입 후 경과일별(D+N) 평균", "fwd"), ("편입 시 국면별", "by_regime"),
                               ("섹터별", "by_sector")):
                df = summ[key]
                ws.write(r, 0, title, f_title)
                r += 1
                for c, name in enumerate(df.columns):
                    ws.write(r, c, name, f_head)
                for i, rec in enumerate(df.itertuples(index=False), start=1):
                    for c, v in enumerate(rec):
                        name = df.columns[c]
                        if isinstance(v, (float, np.floating)) and name != "표본 수":
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
                         "신규 편입": 22, "편출": 22})

        # --- 시트3: 스크리닝 이력(원본) -----------------------------------
        raw = hist.rename(columns={"date": "날짜", "regime": "국면", "sideways_mode": "SIDEWAYS 방식",
                                   "leaders": "주도 섹터", "rank": "순위", "ticker": "종목", "sector": "섹터",
                                   "entry_price": "진입가", "stop": "손절가", "weight_pct": "비중(%)",
                                   "shares": "수량"})
        write_table(raw, "스크리닝 이력", {"날짜": 12})

        # --- 시트4: 종목별 추적 -------------------------------------------
        ep = episodes.copy()
        for col, _ in EP_EXPORT:
            if col not in ep.columns:
                ep[col] = np.nan
        tr = ep[[c for c, _ in EP_EXPORT]].rename(columns=dict(EP_EXPORT))
        tr["손절 터치"] = tr["손절 터치"].map({True: "O", False: "", np.nan: ""}).fillna("")
        tr = tr.astype(object).where(tr.notna(), None)
        wst = write_table(tr, "종목별 추적", {"종목": 9, "편입일": 12, "편출일": 12, "추적 종료일": 12,
                                              "손절 터치일": 12, "상태": 14, "편입 시 국면": 12})
        if len(tr):
            wst.conditional_format(1, 11, len(tr), 16, {"type": "cell", "criteria": ">", "value": 0,
                                                       "format": f_red})
            # 수익률 컬럼 막대 시각화
            wst.conditional_format(1, 11, len(tr), 11, {"type": "data_bar", "bar_solid": True,
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
                a = paths[col].mean(); m = paths[col].median()
                if a == a:
                    wsp.write_number(base, c, a, f_pct)
                if m == m:
                    wsp.write_number(base + 1, c, m, f_pct)
            wsp.conditional_format(1, 1, len(pdf), n, {"type": "3_color_scale", "min_color": "#4F81BD",
                                                       "mid_color": "#FFFFFF", "max_color": "#E06666",
                                                       "mid_type": "num", "mid_value": 0})
            wsp.write(base + 3, 0, "※ 편출 여부와 무관하게 편입 후 N번째 거래일 종가 기준(편출 후에도 보유했다면). "
                                   "아직 도달하지 않은 경과일은 빈칸.", f_note)
    return buf.getvalue()


# ------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "backfill":
        h = backfill_from_git()
        print(f"이력 {h['date'].nunique()}일 / {len(h)}행 저장")
    elif cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "screening_tracking.xlsx"
        h = load_history()
        e, p = track(h)
        with open(out, "wb") as f:
            f.write(build_excel(h, e, p))
        print(f"{out} 생성 (에피소드 {len(e)}건)")
    else:
        print(__doc__)
