"""매매일지 복기/성과 분석 (순수 함수 — Streamlit 비의존).

- adherence(): 시그널 대비 실제 매도 이행 여부(규칙 준수) 자동 판정
- trade_results(): 포지션별 평균 매도가·손익률·R배수·실현손익
- performance(): 승률/평균 손익/손익비/기대값(R)/프로핏 팩터
- monthly_realized(): 월별 실현손익(매도일 기준, 분할 매도 포함)
- tag_stats() / adherence_stats(): 실수 태그·규칙 준수별 성과
- build_excel(): 거래내역/분할 매도/월별 손익/복기 요약 엑셀
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from journal import DISMISSED_MARK, SIGNAL_LABELS, summarize_closed_trade

MISTAKE_TAGS = ["시그널 무시", "손절 지연", "일찍 익절", "추격 매수", "비중 과다", "계획 없는 매수", "기타"]
GRACE_BDAYS = 1      # 시그널 발생일로부터 이 영업일 이내에 매도하면 "준수"
ADH_OK, ADH_LATE, ADH_MISSED, ADH_SKIP, ADH_NONE, ADH_NA = "준수", "지연", "미이행", "TP 건너뜀", "시그널 없음", "-"


def _txt(v) -> str:
    return "" if v is None or pd.isna(v) else str(v).strip()


def split_tags(v) -> list:
    return [t.strip() for t in _txt(v).split(",") if t.strip()]


def adherence(trade, exits_df: pd.DataFrame, signals_df: pd.DataFrame) -> dict:
    """시그널이 뜬 뒤 실제로 매도했는지를 판정한다.

    각 시그널마다 '시그널일 이후 첫 매도'까지의 영업일 수(lag)를 잰다.
    준수(모든 시그널 lag <= GRACE_BDAYS) / 지연 / 미이행(이후 매도 없음) / TP 건너뜀('체결 안 함' 처리).
    우선순위: 미이행 > 지연 > TP 건너뜀 > 준수.
    """
    tid = trade["id"]
    sales = exits_df[exits_df["trade_id"] == tid] if exits_df is not None and not exits_df.empty else None
    sigs = signals_df[signals_df["trade_id"] == tid] if signals_df is not None and not signals_df.empty else None
    if sales is None or sales.empty:
        return {"verdict": ADH_NA, "detail": "매도 기록 없음", "max_lag": None}
    if sigs is None or sigs.empty:
        return {"verdict": ADH_NONE, "detail": "시그널 없이 매도", "max_lag": None}

    sale_dates = sorted(sales["date"])
    lags, parts, skipped, missed = [], [], 0, 0
    for _, sg in sigs.sort_values("date").iterrows():
        label = SIGNAL_LABELS.get(sg["type"], sg["type"]).split(" ")[0]
        if DISMISSED_MARK in _txt(sg.get("note")):
            skipped += 1
            parts.append(f"{label} 건너뜀")
            continue
        later = [d for d in sale_dates if d >= sg["date"]]
        if not later:
            missed += 1
            parts.append(f"{label} 미이행")
            continue
        lag = int(np.busday_count(sg["date"], later[0]))
        lags.append(lag)
        parts.append(f"{label} " + ("당일" if lag == 0 else f"+{lag}영업일"))
    if missed:
        verdict = ADH_MISSED
    elif lags and max(lags) > GRACE_BDAYS:
        verdict = ADH_LATE
    elif skipped:
        verdict = ADH_SKIP
    else:
        verdict = ADH_OK
    return {"verdict": verdict, "detail": " · ".join(parts), "max_lag": max(lags) if lags else None}


def _sold_shares(trade_id, exits_df, signals_df) -> int:
    if exits_df is not None and not exits_df.empty:
        sub = exits_df[exits_df["trade_id"] == trade_id]
        if not sub.empty:
            return int(sub["shares"].sum())
    if signals_df is not None and not signals_df.empty:
        sub = signals_df[(signals_df["trade_id"] == trade_id) & (signals_df["confirmed"])]
        if not sub.empty:
            return int(sub["suggested_shares"].sum())
    return 0


def trade_results(trades_df: pd.DataFrame, exits_df: pd.DataFrame, signals_df: pd.DataFrame) -> pd.DataFrame:
    """포지션별 결과. 매도 내역이 없는 포지션은 손익 NaN."""
    rows = []
    for _, t in trades_df.iterrows():
        sm = summarize_closed_trade(t, exits_df, signals_df)
        entry, stop = float(t["entry_price"]), float(t["initial_stop"])
        risk = entry - stop
        have = sm["exit_price"] is not None
        sold = _sold_shares(t["id"], exits_df, signals_df) if have else 0
        adh = adherence(t, exits_df, signals_df)
        rows.append({
            "id": t["id"], "ticker": t["ticker"], "entry_date": t["entry_date"], "entry_price": entry,
            "shares": int(t["initial_shares"]), "exit_date": sm["exit_date"] if have else None,
            "exit_price": sm["exit_price"] if have else np.nan,
            "hold_days": sm["hold_days"] if have else np.nan,
            "return_pct": sm["return_pct"] if have else np.nan,
            "r_multiple": (sm["exit_price"] - entry) / risk if have and risk > 0 else np.nan,
            "pnl": (sm["exit_price"] - entry) * sold if have else np.nan,
            "adherence": adh["verdict"], "adherence_detail": adh["detail"],
            "tags": ", ".join(split_tags(t.get("mistake_tags"))), "lesson": _txt(t.get("lesson")),
            "memo": _txt(t.get("memo")),
        })
    return pd.DataFrame(rows)


def performance(results: pd.DataFrame) -> dict:
    """종료 포지션 성과 요약. 결과가 있는 포지션만 대상으로 한다."""
    r = results.dropna(subset=["return_pct"]) if not results.empty else results
    if r.empty:
        return {"n": 0}
    wins, losses = r[r["return_pct"] > 0], r[r["return_pct"] <= 0]
    avg_win = wins["return_pct"].mean() if len(wins) else np.nan
    avg_loss = losses["return_pct"].mean() if len(losses) else np.nan
    gross_win, gross_loss = wins["pnl"].sum(), -losses["pnl"].sum()
    rr = r["r_multiple"].dropna()
    return {
        "n": len(r), "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / len(r),
        "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
        "payoff": (avg_win / abs(avg_loss)) if len(wins) and len(losses) and avg_loss != 0 else np.nan,
        "avg_r": rr.mean() if len(rr) else np.nan,
        "expectancy_pct": r["return_pct"].mean(),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else np.nan,
        "realized_pnl": r["pnl"].sum(), "avg_hold_days": r["hold_days"].mean(),
        "best": r.loc[r["return_pct"].idxmax(), "ticker"], "worst": r.loc[r["return_pct"].idxmin(), "ticker"],
    }


def monthly_realized(trades_df: pd.DataFrame, exits_df: pd.DataFrame) -> pd.DataFrame:
    """월별 실현손익(매도일 기준, 매수가 대비 USD). 진행중 포지션의 부분 매도도 포함한다."""
    cols = ["월", "매도 건수", "실현손익", "승", "패"]
    if exits_df is None or exits_df.empty or trades_df.empty:
        return pd.DataFrame(columns=cols)
    entry = trades_df.set_index("id")["entry_price"].astype(float)
    x = exits_df[exits_df["trade_id"].isin(entry.index)].copy()
    if x.empty:
        return pd.DataFrame(columns=cols)
    x["pnl"] = (x["price"].astype(float) - x["trade_id"].map(entry)) * x["shares"]
    x["월"] = pd.to_datetime(x["date"]).dt.strftime("%Y-%m")
    g = x.groupby("월").agg(**{"매도 건수": ("pnl", "size"), "실현손익": ("pnl", "sum"),
                              "승": ("pnl", lambda s: int((s > 0).sum())),
                              "패": ("pnl", lambda s: int((s <= 0).sum()))})
    return g.reset_index().sort_values("월", ascending=False).reset_index(drop=True)


def tag_stats(results: pd.DataFrame) -> pd.DataFrame:
    """실수 태그별 발생 건수와 평균 R/손익률."""
    rows = []
    for _, r in results.iterrows():
        for tag in split_tags(r["tags"]):
            rows.append({"태그": tag, "r": r["r_multiple"], "ret": r["return_pct"]})
    if not rows:
        return pd.DataFrame(columns=["태그", "건수", "평균 R", "평균 손익률(%)"])
    d = pd.DataFrame(rows)
    out = d.groupby("태그").agg(건수=("r", "size"), **{"평균 R": ("r", "mean"), "평균 손익률(%)": ("ret", "mean")})
    return out.reset_index().sort_values("건수", ascending=False).reset_index(drop=True)


def adherence_stats(results: pd.DataFrame) -> pd.DataFrame:
    """규칙 준수 여부별 건수와 평균 R — 규칙을 지킨 거래와 어긴 거래의 성과 차이를 본다."""
    cols = ["규칙 준수", "건수", "평균 R", "평균 손익률(%)"]
    r = results[results["adherence"] != ADH_NA] if not results.empty else results
    if r.empty:
        return pd.DataFrame(columns=cols)
    out = r.groupby("adherence").agg(건수=("id", "size"), **{"평균 R": ("r_multiple", "mean"),
                                                         "평균 손익률(%)": ("return_pct", "mean")})
    return out.reset_index().rename(columns={"adherence": "규칙 준수"}).sort_values("건수", ascending=False) \
              .reset_index(drop=True)


def build_excel(trades_df: pd.DataFrame, exits_df: pd.DataFrame, signals_df: pd.DataFrame) -> bytes:
    """거래내역 / 분할 매도 / 월별 손익 / 복기 요약 엑셀 (한국식 색: 수익 빨강, 손실 파랑)."""
    res = trade_results(trades_df, exits_df, signals_df) if not trades_df.empty else pd.DataFrame()
    status = trades_df.set_index("id")["status"] if not trades_df.empty else pd.Series(dtype=str)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter", datetime_format="yyyy-mm-dd", date_format="yyyy-mm-dd") as xw:
        wb = xw.book
        hdr = wb.add_format({"bold": True, "bg_color": "#EDEFF2", "border": 1, "align": "center"})
        pct = wb.add_format({"num_format": '[Red]+0.00"%";[Blue]-0.00"%";0.00"%"'})
        rfmt = wb.add_format({"num_format": '[Red]+0.00"R";[Blue]-0.00"R";0.00"R"'})
        money = wb.add_format({"num_format": "[Red]+#,##0.00;[Blue]-#,##0.00;0.00"})
        px = wb.add_format({"num_format": "#,##0.00"})

        def put(df, name, widths, fmts):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c, col in enumerate(df.columns):
                ws.write(0, c, col, hdr)
                ws.set_column(c, c, widths.get(col, 12), fmts.get(col))
            ws.freeze_panes(1, 0)
            if len(df):
                ws.autofilter(0, 0, len(df), len(df.columns) - 1)

        if not res.empty:
            t = res.copy()
            t["상태"] = t["id"].map(status).map({"OPEN": "진행중", "CLOSED": "종료"}).fillna("")
            t = t.rename(columns={"ticker": "종목", "entry_date": "매수일", "entry_price": "매수가", "shares": "수량",
                                  "exit_date": "마지막 매도일", "exit_price": "평균 매도가", "hold_days": "보유일",
                                  "return_pct": "손익률(%)", "r_multiple": "R배수", "pnl": "실현손익(USD)",
                                  "adherence": "규칙 준수", "adherence_detail": "시그널 이행 상세",
                                  "tags": "실수 태그", "lesson": "교훈", "memo": "매수 이유"})
            t = t[["종목", "상태", "매수일", "매수가", "수량", "마지막 매도일", "평균 매도가", "보유일", "손익률(%)",
                   "R배수", "실현손익(USD)", "규칙 준수", "시그널 이행 상세", "실수 태그", "교훈", "매수 이유"]]
            put(t, "거래내역", {"시그널 이행 상세": 34, "교훈": 36, "매수 이유": 30, "실수 태그": 20},
                {"매수가": px, "평균 매도가": px, "손익률(%)": pct, "R배수": rfmt, "실현손익(USD)": money})
        else:
            pd.DataFrame({"안내": ["등록된 포지션이 없습니다."]}).to_excel(xw, sheet_name="거래내역", index=False)

        if exits_df is not None and not exits_df.empty and not trades_df.empty:
            tr = trades_df.set_index("id")
            x = exits_df.copy()
            x["종목"] = x["trade_id"].map(tr["ticker"])
            ep = x["trade_id"].map(tr["entry_price"]).astype(float)
            x["손익률(%)"] = (x["price"].astype(float) / ep - 1) * 100
            x["실현손익(USD)"] = (x["price"].astype(float) - ep) * x["shares"]
            x = x.rename(columns={"date": "매도일", "price": "매도가", "shares": "수량", "memo": "메모"})
            put(x[["종목", "매도일", "매도가", "수량", "손익률(%)", "실현손익(USD)", "메모"]].sort_values("매도일"),
                "분할 매도", {"메모": 30}, {"매도가": px, "손익률(%)": pct, "실현손익(USD)": money})
        put(monthly_realized(trades_df, exits_df), "월별 손익", {}, {"실현손익": money})

        perf = performance(res) if not res.empty else {"n": 0}
        lines = [("종료 거래 수", perf.get("n", 0))]
        if perf.get("n"):
            def rnd(v, d=2):
                return None if pd.isna(v) else round(float(v), d)
            lines += [("승률", f"{perf['win_rate']:.0%}"), ("평균 수익(%)", rnd(perf["avg_win_pct"])),
                      ("평균 손실(%)", rnd(perf["avg_loss_pct"])), ("손익비", rnd(perf["payoff"])),
                      ("평균 R", rnd(perf["avg_r"])), ("프로핏 팩터", rnd(perf["profit_factor"])),
                      ("실현손익 합계(USD)", rnd(perf["realized_pnl"])), ("평균 보유일", rnd(perf["avg_hold_days"], 1))]
        summ = pd.DataFrame(lines, columns=["지표", "값"])
        put(summ, "복기 요약", {"지표": 22, "값": 16}, {})
        ws = xw.sheets["복기 요약"]
        row = len(summ) + 2
        for title, df in (("규칙 준수별 성과", adherence_stats(res) if not res.empty else pd.DataFrame()),
                          ("실수 태그별 성과", tag_stats(res) if not res.empty else pd.DataFrame())):
            if df.empty:
                continue
            ws.write(row, 0, title, hdr)
            for c, col in enumerate(df.columns):
                ws.write(row + 1, c, col, hdr)
            for i, rec in enumerate(df.itertuples(index=False), start=row + 2):
                for c, v in enumerate(rec):
                    ws.write(i, c, "" if pd.isna(v) else (round(float(v), 2) if isinstance(v, float) else v))
            row += len(df) + 3
        ws.set_column(2, 3, 16)
    return buf.getvalue()
