"""
섹터 로테이션 화면용 계산/차트 모듈
====================================================
섹터 ETF 11개의 가격 데이터로 (1) 기간별 선그래프(누적 수익률 / SPY 대비 상대강도 / 단일 섹터 가격)와
(2) 섹터 상태 표(낙폭 / 회복 / 보유·상대성과)를 만든다. Streamlit에는 의존하지 않아 단독으로 테스트할 수 있다.

역할 구분: 차트는 '흐름과 모양'(언제부터, 누가 앞서나)을, 표는 '지금 상태의 정확한 수치와 판단 정보'
(얼마나 빠졌나, 회복 중인가, 내 보유와의 관계)를 담당한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from execution import compute_atr
from qs_config import SECTOR_NAMES_KO

PERIODS = {"1개월": 21, "3개월": 63, "6개월": 126, "1년": 252}   # 라벨 -> 거래일 수
SPARK_POINTS = 60                                              # 표 안 미니 차트(흐름)의 최대 점 개수


def sector_label(etf: str, leaders=()) -> str:
    return ("★ " if etf in leaders else "") + f"{etf} {SECTOR_NAMES_KO.get(etf, '')}".strip()


# ---------------------------------------------------------------------------
# 상태 라벨
# ---------------------------------------------------------------------------
def drop_status(drop_pct: float) -> str:
    """고점 대비 낙폭으로 본 상태 (신호등). 정보용이며 매도 신호가 아니다."""
    if drop_pct >= -3:
        return "🟢 고점 부근"
    if drop_pct >= -8:
        return "🟡 조정"
    return "🔴 깊은 조정"


def recovery_status(drop_pct: float, recovery_pct: float, trough_age: int) -> str:
    """고점 갱신·부근 / 하락 진행 / 반등 / 회복 근접 상태. trough_age는 저점 이후 경과 거래일."""
    if drop_pct > -1e-9:
        return "🟢 고점 갱신"
    if drop_pct >= -1:   # 고점에서 1%도 안 빠졌으면 하락 진행으로 보지 않는다
        return "🟢 고점 부근"
    if trough_age <= 3 and recovery_pct < 25:
        return "🔻 하락 진행"
    if recovery_pct >= 75:
        return "↗️ 회복 근접"
    return "🔄 반등 중"


def _spark(close: pd.Series) -> list[float]:
    """기간 시작=100으로 맞춘 종가를 SPARK_POINTS개 이하로 줄여 표 안 미니 차트용 리스트로 만든다."""
    rebased = (close / close.iloc[0] * 100).tolist()
    if len(rebased) <= SPARK_POINTS:
        return rebased
    step = len(rebased) / SPARK_POINTS
    pts = [rebased[int(i * step)] for i in range(SPARK_POINTS)]
    pts[-1] = rebased[-1]
    return pts


# ---------------------------------------------------------------------------
# 표 데이터
# ---------------------------------------------------------------------------
def build_sector_table(price_data: dict, leaders, days: int, spy_close: pd.Series | None = None,
                       held: dict | None = None, scored_now: pd.DataFrame | None = None,
                       scored_prev: pd.DataFrame | None = None) -> pd.DataFrame:
    """섹터별로 낙폭/회복/보유·상대성과 열을 모두 계산한 표(인덱스=섹터 라벨)를 반환한다.

    held: {섹터ETF: {"weight": 보유 비중(%), "tickers": [티커,...]}}  (매매일지 진행중 포지션 기준)
    scored_now/scored_prev: SectorRotationEngine.score_sectors 결과(현재 / 5거래일 전) — 전략 순위 변화 계산용
    """
    held = held or {}
    spy_ret = None
    if spy_close is not None and len(spy_close.dropna()) > days:
        s = spy_close.dropna().tail(days + 1)
        spy_ret = (s.iloc[-1] / s.iloc[0] - 1) * 100

    rank_now = scored_now["score"].rank(method="min") if scored_now is not None and not scored_now.empty else None
    rank_prev = scored_prev["score"].rank(method="min") if scored_prev is not None and not scored_prev.empty else None

    rows = []
    for etf, df in price_data.items():
        if df is None or df.empty:
            continue
        close = df["Close"].dropna().tail(days + 1)
        if len(close) < 2:
            continue
        peak_date, peak = close.idxmax(), float(close.max())
        last_date, last = close.index[-1], float(close.iloc[-1])
        after = close.loc[peak_date:]                                  # 고점 이후 구간
        trough_date, trough = after.idxmin(), float(after.min())
        drop_pt, drop_pct = last - peak, (last / peak - 1) * 100
        td = len(after) - 1                                            # 고점 이후 경과 거래일
        recovery = (last - trough) / (peak - trough) * 100 if peak > trough else 100.0
        atr = compute_atr(df).iloc[-1]
        period_ret = (last / float(close.iloc[0]) - 1) * 100
        info = held.get(etf, {})

        rank_chg = None
        if rank_now is not None and rank_prev is not None and etf in rank_now.index and etf in rank_prev.index:
            rank_chg = int(rank_prev[etf] - rank_now[etf])             # +면 순위 상승

        rows.append({
            "섹터": sector_label(etf, leaders),
            # 낙폭
            "상태": drop_status(drop_pct),
            "고점일": peak_date.strftime("%Y-%m-%d"),
            "고점": peak,
            "현재가": last,
            "하락 포인트": drop_pt,
            "고점 대비(%)": drop_pct,
            "변동폭 대비(ATR배)": abs(drop_pt) / atr if atr and atr > 0 else np.nan,
            "하락 기간(일)": (last_date - peak_date).days,
            "하락 기간(거래일)": td,
            # 회복
            "회복 상태": recovery_status(drop_pct, recovery, len(close.loc[trough_date:]) - 1),
            "최대 낙폭(%)": (trough / peak - 1) * 100,
            "저점일": trough_date.strftime("%Y-%m-%d"),
            "저점 대비 반등(%)": (last / trough - 1) * 100,
            "낙폭 회복률(%)": recovery,
            "최근 5일(%)": (last / float(close.iloc[-6]) - 1) * 100 if len(close) > 5 else np.nan,
            "일평균 하락률(%/일)": drop_pct / td if td > 0 else 0.0,
            # 보유 · 상대성과
            "보유 비중(%)": info.get("weight", 0.0),
            "보유 종목": ", ".join(info.get("tickers", [])) or "-",
            "주도 섹터": "✅" if etf in leaders else "",
            "기간 수익률(%)": period_ret,
            "SPY 대비(%p)": period_ret - spy_ret if spy_ret is not None else np.nan,
            "전략 순위": int(rank_now[etf]) if rank_now is not None and etf in rank_now.index else None,
            "순위 변화(1주)": ("▲" + str(rank_chg) if rank_chg > 0 else "▼" + str(-rank_chg) if rank_chg < 0 else "-")
                              if rank_chg is not None else "-",
            # 공통
            "흐름": _spark(close),
        })
    return pd.DataFrame(rows).set_index("섹터") if rows else pd.DataFrame()


VIEW_COLUMNS = {
    "낙폭": ["상태", "고점일", "고점", "현재가", "하락 포인트", "고점 대비(%)", "변동폭 대비(ATR배)",
           "하락 기간(일)", "하락 기간(거래일)", "흐름"],
    "회복": ["회복 상태", "고점 대비(%)", "최대 낙폭(%)", "저점일", "저점 대비 반등(%)", "낙폭 회복률(%)",
           "최근 5일(%)", "일평균 하락률(%/일)", "흐름"],
    "보유·상대성과": ["보유 비중(%)", "보유 종목", "주도 섹터", "기간 수익률(%)", "SPY 대비(%p)", "전략 순위",
                 "순위 변화(1주)", "흐름"],
}
VIEW_SORT = {"낙폭": ("고점 대비(%)", True), "회복": ("낙폭 회복률(%)", True), "보유·상대성과": ("보유 비중(%)", False)}


def table_view(table: pd.DataFrame, view: str) -> pd.DataFrame:
    """전체 표에서 선택한 보기의 열만 골라 정렬한다."""
    if table.empty:
        return table
    col, ascending = VIEW_SORT[view]
    return table.sort_values(col, ascending=ascending, kind="stable")[VIEW_COLUMNS[view]]


# ---------------------------------------------------------------------------
# 차트
# ---------------------------------------------------------------------------
def _closes(price_data: dict, days: int) -> pd.DataFrame:
    closes = pd.DataFrame({e: d["Close"] for e, d in price_data.items() if d is not None and not d.empty})
    return closes.sort_index().ffill().tail(days + 1)


def line_chart(price_data: dict, leaders, days: int, period_label: str, mode: str = "return",
               spy_close: pd.Series | None = None):
    """섹터 11개 비교 선그래프. mode: 'return'=기간 시작 대비 누적 수익률(%), 'relative'=SPY 대비 상대강도(시작=100).
    주도 섹터는 굵은 선+★, 범례 클릭으로 섹터 강조."""
    import altair as alt

    closes = _closes(price_data, days)
    if closes.empty:
        return None
    if mode == "relative":
        if spy_close is None or spy_close.dropna().empty:
            return None
        rel = closes.div(spy_close.reindex(closes.index).ffill(), axis=0)
        series, base = rel / rel.iloc[0] * 100, 100.0
        y_title, tip_fmt = f"SPY 대비 상대강도 ({period_label} 전 = 100)", ".1f"
        zero_scale = alt.Scale(zero=False)
    else:
        series, base = (closes / closes.iloc[0] - 1) * 100, 0.0
        y_title, tip_fmt = f"{period_label} 전 대비 수익률 (%)", "+.1f"
        zero_scale = alt.Scale(zero=True)
    series.index.name = "date"
    long = series.reset_index().melt(id_vars="date", var_name="etf", value_name="v")
    long["sector"] = long["etf"].map(lambda e: sector_label(e, leaders))
    long["leader"] = long["etf"].isin(leaders)

    pick = alt.selection_point(fields=["sector"], bind="legend")
    lines = (
        alt.Chart(long).mark_line()
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("v:Q", title=y_title, scale=zero_scale),
            color=alt.Color("sector:N", title=None, sort=alt.SortField("etf")),
            strokeWidth=alt.condition(alt.datum.leader, alt.value(3.5), alt.value(1.4)),
            opacity=alt.condition(pick, alt.value(1.0), alt.value(0.15)),
            tooltip=[alt.Tooltip("date:T", title="날짜"), alt.Tooltip("sector:N", title="섹터"),
                     alt.Tooltip("v:Q", title=y_title, format=tip_fmt)],
        )
        .add_params(pick)
    )
    rule = alt.Chart(pd.DataFrame({"y": [base]})).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="y:Q")
    return (rule + lines).properties(height=420)


def price_chart(df: pd.DataFrame, etf: str, days: int, period_label: str):
    """한 섹터의 실제 가격 차트: 종가 + MA20/MA50/MA200 + 기간 고점(점선). 이동평균은 기간 이전 데이터까지 써서 계산한다."""
    import altair as alt

    d = pd.DataFrame({"종가": df["Close"]})
    for n in (20, 50, 200):
        d[f"MA{n}"] = d["종가"].rolling(n).mean()
    d = d.tail(days + 1)
    d.index.name = "date"
    long = d.reset_index().melt(id_vars="date", var_name="series", value_name="price").dropna()
    names = ["종가", "MA20", "MA50", "MA200"]
    color = alt.Color("series:N", title=None, sort=names,
                      scale=alt.Scale(domain=names, range=["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]))
    lines = (
        alt.Chart(long).mark_line()
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("price:Q", title=f"{sector_label(etf)} 가격 ($)", scale=alt.Scale(zero=False)),
            color=color,
            strokeWidth=alt.condition(alt.datum.series == "종가", alt.value(2.8), alt.value(1.2)),
            tooltip=[alt.Tooltip("date:T", title="날짜"), alt.Tooltip("series:N", title="구분"),
                     alt.Tooltip("price:Q", title="가격($)", format=",.2f")],
        )
    )
    peak = float(d["종가"].max())
    rule = (alt.Chart(pd.DataFrame({"y": [peak]})).mark_rule(color="gray", strokeDash=[4, 4]).encode(y="y:Q"))
    label = (alt.Chart(pd.DataFrame({"y": [peak], "t": [f"{period_label} 고점 ${peak:,.2f}"]}))
             .mark_text(align="left", dx=4, dy=-6, color="gray").encode(y="y:Q", text="t:N"))
    return (rule + label + lines).properties(height=420)
