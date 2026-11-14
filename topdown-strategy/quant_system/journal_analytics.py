"""매매일지 투자 분석 (순수 함수 — Streamlit 비의존).

- build_equity(): 거래내역 잔고(ledger)와 시세로 일별 계좌 평가금액(USD)을 복원
- twr_series() / same_cash_benchmark(): 입출금 보정 수익률(TWR)과 'SPY에 같은 자금을 넣었다면' 비교
- exit_pnl_frame() 이후: 실현손익 곡선·월별·달력·종목별 기여
- position_gauge_rows() / open_risk(): 진행중 포지션 손절·목표 위치, 총 오픈 리스크
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

PERIOD_ALL, PERIOD_YTD, PERIOD_3M, PERIOD_STRATEGY = "전체", "올해", "최근 3개월", "전략 시작 이후"


# ------------------------------------------------------------------
# 기간 필터
# ------------------------------------------------------------------
def strategy_start(trades_df: pd.DataFrame, signals_df: pd.DataFrame):
    """전략(시그널 추적)을 쓰기 시작한 날: 가장 이른 시그널 발생일."""
    if signals_df is None or signals_df.empty:
        return None
    return min(signals_df["date"])


def period_range(choice: str, today: date, strat_start=None, data_start=None) -> tuple:
    """(시작일, 종료일). 시작일 None = 처음부터."""
    if choice == PERIOD_YTD:
        return date(today.year, 1, 1), today
    if choice == PERIOD_3M:
        return today - timedelta(days=91), today
    if choice == PERIOD_STRATEGY and strat_start:
        return strat_start, today
    return data_start, today


def in_range(d, start, end) -> bool:
    return (start is None or d >= start) and (end is None or d <= end)


# ------------------------------------------------------------------
# 계좌 평가금액 복원
# ------------------------------------------------------------------
def _stepped(L: pd.DataFrame, col: str, idx: pd.DatetimeIndex) -> pd.Series:
    """그날 마지막으로 보고된 값을 다음 보고 전까지 이어 붙인다."""
    sub = L.dropna(subset=[col])
    if sub.empty:
        return pd.Series(0.0, index=idx)
    s = sub.groupby("date")[col].last()
    s.index = pd.to_datetime(s.index)
    return s.reindex(idx.union(s.index)).ffill().reindex(idx)


def build_equity(ledger: pd.DataFrame, get_close, get_fx, end=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """일별 계좌 평가금액(USD) = USD 현금 + KRW 현금/환율 + Σ 보유수량 × 종가.

    get_close(ticker) → 일별 종가 Series(배당 미조정). get_fx() → USD/KRW 일별 Series.
    반환: (equity_df[cash_usd, cash_krw_usd, holdings, equity, flow], 종목별 평가금액 DataFrame).
    flow = 그날 외부 입출금(USD, 입금 +). 파일 마지막 거래 이후에는 거래가 없다고 가정한다.
    """
    if ledger is None or ledger.empty:
        return pd.DataFrame(), pd.DataFrame()
    L = ledger.copy().sort_values(["date", "no"]).reset_index(drop=True)
    start = pd.Timestamp(L["date"].min())
    last = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    idx = pd.bdate_range(start, max(last, start))

    cash_usd = _stepped(L, "usd_cash", idx).fillna(0.0)
    cash_krw = _stepped(L, "krw_cash", idx).fillna(0.0)
    fx = get_fx()
    fx = fx.reindex(idx.union(fx.index)).ffill().bfill().reindex(idx) if fx is not None and len(fx) else pd.Series(1400.0, index=idx)

    values = {}
    trades = L[(L["ticker"] != "") & L["balance"].notna() & L["side"].isin(["BUY", "SELL"])]
    for ticker, g in trades.groupby("ticker"):
        g = g.sort_values(["date", "no"])
        opening = float(g["balance"].iloc[0] - g["qty"].iloc[0])      # 파일 이전부터 보유하던 수량
        q = g.groupby("date")["balance"].last()
        q.index = pd.to_datetime(q.index)
        qty = q.reindex(idx.union(q.index)).ffill().reindex(idx).fillna(opening)
        px = get_close(ticker)
        if px is None or len(px) == 0:
            continue
        px = px.reindex(idx.union(px.index)).ffill().bfill().reindex(idx)
        values[ticker] = qty * px
    pos = pd.DataFrame(values, index=idx).fillna(0.0)

    flow = pd.Series(0.0, index=idx)
    for _, r in L[L["flow_krw"].notna()].iterrows():
        d = pd.Timestamp(r["date"])
        pos_i = idx.searchsorted(d)
        if pos_i >= len(idx):
            continue
        rate = r["fx_hint"] if pd.notna(r["fx_hint"]) and r["fx_hint"] > 0 else fx.iloc[pos_i]
        flow.iloc[pos_i] += float(r["flow_krw"]) / float(rate)

    eq = pd.DataFrame({"cash_usd": cash_usd, "cash_krw_usd": cash_krw / fx, "holdings": pos.sum(axis=1), "flow": flow})
    eq["equity"] = eq["cash_usd"] + eq["cash_krw_usd"] + eq["holdings"]
    return eq, pos


def twr_series(eq: pd.DataFrame, start=None) -> pd.Series:
    """입출금 보정 시간가중수익률(누적, 0 기준). start 이후로 다시 0에서 시작한다."""
    e = eq if start is None else eq[eq.index >= pd.Timestamp(start)]
    if e.empty:
        return pd.Series(dtype=float)
    r = (e["equity"] - e["flow"]) / e["equity"].shift(1) - 1
    r.iloc[0] = 0.0
    return (1 + r.replace([np.inf, -np.inf], 0).fillna(0)).cumprod() - 1


def same_cash_benchmark(eq: pd.DataFrame, spy: pd.Series, start=None) -> pd.DataFrame:
    """같은 시점에 같은 금액(최초 평가금액 + 이후 입출금)을 SPY에 넣었다면의 평가금액과 수익률(SPY 자체)."""
    e = eq if start is None else eq[eq.index >= pd.Timestamp(start)]
    if e.empty or spy is None or len(spy) == 0:
        return pd.DataFrame(columns=["value", "ret"])
    s = spy.reindex(e.index.union(spy.index)).ffill().bfill().reindex(e.index)
    units = e["equity"].iloc[0] / s.iloc[0]
    vals = []
    for i in range(len(e)):
        if i > 0:
            units += e["flow"].iloc[i] / s.iloc[i]
        vals.append(units * s.iloc[i])
    return pd.DataFrame({"value": vals, "ret": s / s.iloc[0] - 1}, index=e.index)


def max_drawdown(growth: pd.Series) -> float:
    """성장지수(1+누적수익률 또는 금액 시계열)의 최대 낙폭(음수 비율)."""
    if growth is None or len(growth) == 0:
        return 0.0
    return float((growth / growth.cummax() - 1).min())


def fetch_close(ticker: str, start, adjusted: bool = False) -> pd.Series:
    """일별 종가. adjusted=False면 배당 미조정(보유 평가용), True면 배당 재투자 조정(수익률 비교용)."""
    import yfinance as yf
    try:
        df = yf.download(ticker, start=pd.Timestamp(start) - pd.Timedelta(days=7), progress=False,
                         auto_adjust=adjusted)
        if df is None or df.empty:
            return pd.Series(dtype=float)
        c = df["Close"]
        if isinstance(c, pd.DataFrame):
            c = c.iloc[:, 0]
        c.index = pd.to_datetime(c.index).tz_localize(None)
        return c.dropna().astype(float)
    except Exception:
        return pd.Series(dtype=float)


# ------------------------------------------------------------------
# 실현손익 (매도 건별)
# ------------------------------------------------------------------
def exit_pnl_frame(trades_df: pd.DataFrame, exits_df: pd.DataFrame) -> pd.DataFrame:
    """매도 기록 1건당 실현손익(수수료 전/후). 매수 수수료는 판 수량 비율만큼 매도 시점에 배분한다."""
    cols = ["date", "ticker", "pnl", "fee", "net", "trade_id"]
    if trades_df is None or trades_df.empty or exits_df is None or exits_df.empty:
        return pd.DataFrame(columns=cols)
    t = trades_df.set_index("id")
    x = exits_df[exits_df["trade_id"].isin(t.index)].copy()
    if x.empty:
        return pd.DataFrame(columns=cols)
    entry = x["trade_id"].map(t["entry_price"]).astype(float)
    init = x["trade_id"].map(t["initial_shares"]).astype(float).replace(0, np.nan)
    efee = x["trade_id"].map(t["entry_fee"]).astype(float) if "entry_fee" in t.columns else 0.0
    x["pnl"] = (x["price"].astype(float) - entry) * x["shares"]
    xfee = x["fee"].astype(float) if "fee" in x.columns else 0.0
    x["fee"] = xfee + efee * x["shares"] / init.fillna(1)
    x["net"] = x["pnl"] - x["fee"]
    x["ticker"] = x["trade_id"].map(t["ticker"])
    return x[cols].sort_values("date").reset_index(drop=True)


def cumulative_realized(ex: pd.DataFrame, start=None, end=None, col: str = "net") -> pd.DataFrame:
    """일별 누적 실현손익과 낙폭(누적 최고점 대비 금액)."""
    if ex.empty:
        return pd.DataFrame(columns=["cum", "dd"])
    e = ex[[in_range(d, start, end) for d in ex["date"]]]
    if e.empty:
        return pd.DataFrame(columns=["cum", "dd"])
    daily = e.groupby("date")[col].sum()
    daily.index = pd.to_datetime(daily.index)
    cum = daily.cumsum()
    peak = np.maximum.accumulate(np.maximum(cum.values, 0))
    return pd.DataFrame({"cum": cum, "dd": cum.values - peak}, index=cum.index)


def monthly_components(ex: pd.DataFrame, div_df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """월별 매매손익(수수료 전) / 배당(세후) / 수수료(음수)."""
    e = ex[[in_range(d, start, end) for d in ex["date"]]] if not ex.empty else ex
    parts = {}
    if not e.empty:
        m = pd.to_datetime(e["date"]).dt.strftime("%Y-%m")
        parts["매매손익"] = e.groupby(m)["pnl"].sum()
        parts["수수료"] = -e.groupby(m)["fee"].sum()
    if div_df is not None and not div_df.empty:
        d = div_df[[in_range(x, start, end) for x in div_df["date"]]]
        if not d.empty:
            parts["배당(세후)"] = d.groupby(pd.to_datetime(d["date"]).dt.strftime("%Y-%m"))["net"].sum()
    if not parts:
        return pd.DataFrame(columns=["월", "매매손익", "배당(세후)", "수수료"])
    out = pd.DataFrame(parts).fillna(0.0)
    for c in ("매매손익", "배당(세후)", "수수료"):
        if c not in out.columns:
            out[c] = 0.0
    out = out[["매매손익", "배당(세후)", "수수료"]].sort_index()
    out.index.name = "월"
    return out.reset_index()


def calendar_frame(ex: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    """일별 실현손익(수수료 후) → 주/요일 격자용."""
    if ex.empty:
        return pd.DataFrame(columns=["date", "net"])
    e = ex[[in_range(d, start, end) for d in ex["date"]]]
    if e.empty:
        return pd.DataFrame(columns=["date", "net"])
    return e.groupby("date")["net"].sum().reset_index()


def contribution_by_ticker(ex: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if ex.empty:
        return pd.DataFrame(columns=["ticker", "net"])
    e = ex[[in_range(d, start, end) for d in ex["date"]]]
    if e.empty:
        return pd.DataFrame(columns=["ticker", "net"])
    return e.groupby("ticker")["net"].sum().sort_values().reset_index()


# ------------------------------------------------------------------
# 진행중 포지션
# ------------------------------------------------------------------
def position_gauge_rows(open_df: pd.DataFrame, last_close: dict, tp1_pct: float, tp2_pct: float) -> list:
    """게이지용: 매수가 대비 %로 환산한 손절·현재·TP1·TP2 위치."""
    rows = []
    for _, t in open_df.iterrows():
        entry = float(t["entry_price"])
        px = last_close.get(t["id"])
        if not entry or px is None or pd.isna(px):
            continue
        rows.append({"label": f"{t['ticker']} @{entry:,.2f}", "stop": (float(t["current_stop"]) / entry - 1) * 100,
                     "now": (px / entry - 1) * 100, "tp1": tp1_pct * 100, "tp2": tp2_pct * 100,
                     "phase": t["phase"]})
    return rows


def open_risk(open_df: pd.DataFrame, last_close: dict) -> dict:
    """모든 진행중 포지션이 현재 손절가까지 갔을 때의 손실(USD): Σ max(0, 현재가 − 손절가) × 잔여수량."""
    risk, value = 0.0, 0.0
    for _, t in open_df.iterrows():
        px = last_close.get(t["id"])
        if px is None or pd.isna(px):
            continue
        sh = int(t["remaining_shares"])
        risk += max(0.0, px - float(t["current_stop"])) * sh
        value += px * sh
    return {"risk": risk, "value": value}
