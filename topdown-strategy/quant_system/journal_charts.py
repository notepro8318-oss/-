"""매매일지 Plotly 차트 (한국식 색: 상승·이익 빨강, 하락·손실 파랑). Streamlit 비의존 — figure만 만든다."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

UP, DOWN = "#E03131", "#1C7ED6"
ACCOUNT, BENCH, DIV, FEE, GRID = "#7048E8", "#868E96", "#F59F00", "#ADB5BD", "rgba(128,128,128,0.18)"


def _layout(fig: go.Figure, height: int = 330, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=28, b=8), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified", showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, font=dict(size=12)),
        font=dict(size=12))
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _sign_colors(values) -> list:
    return [UP if v > 0 else DOWN for v in values]


def fig_equity(eq: pd.DataFrame, bm: pd.DataFrame, twr: pd.Series, mode: str = "수익률") -> go.Figure:
    """계좌 vs 'SPY에 같은 자금'. mode: '수익률'(입출금 보정 TWR %) / '금액'(USD)."""
    fig = go.Figure()
    if mode == "금액":
        fig.add_trace(go.Scatter(x=eq.index, y=eq["equity"], name="내 계좌", line=dict(color=ACCOUNT, width=2.5),
                                 hovertemplate="%{y:$,.0f}"))
        if len(bm):
            fig.add_trace(go.Scatter(x=bm.index, y=bm["value"], name="SPY에 같은 자금", line=dict(color=BENCH, width=2, dash="dot"),
                                     hovertemplate="%{y:$,.0f}"))
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    else:
        fig.add_trace(go.Scatter(x=twr.index, y=twr * 100, name="내 계좌(입출금 보정)", line=dict(color=ACCOUNT, width=2.5),
                                 hovertemplate="%{y:+.2f}%"))
        if len(bm):
            fig.add_trace(go.Scatter(x=bm.index, y=bm["ret"] * 100, name="SPY", line=dict(color=BENCH, width=2, dash="dot"),
                                     hovertemplate="%{y:+.2f}%"))
        fig.add_hline(y=0, line_color=GRID)
        fig.update_yaxes(ticksuffix="%")
    for d, f in eq["flow"][eq["flow"].abs() > 50].items():
        fig.add_vline(x=d, line_dash="dot", line_color=BENCH, opacity=0.6)
        fig.add_annotation(x=d, y=1, yref="paper", text=f"{'입금' if f > 0 else '출금'} ${abs(f):,.0f}", showarrow=False,
                           font=dict(size=11, color=BENCH), yanchor="bottom", xanchor="left")
    return _layout(fig, 340)


def fig_monthly_equity(me: pd.DataFrame) -> go.Figure:
    """월말 계좌 평가금액(USD). 전월 대비 증감은 호버에, 입출금이 있던 달은 따로 표시한다."""
    fig = go.Figure()
    chg = me["증감"].fillna(0)
    fig.add_trace(go.Scatter(
        x=me["월"], y=me["평가금액"], mode="lines+markers+text", line=dict(color=ACCOUNT, width=2.5),
        marker=dict(size=9, color=[ACCOUNT if i == 0 else (UP if c >= 0 else DOWN) for i, c in enumerate(chg)],
                    line=dict(width=1, color="white")),
        text=[f"${v / 1000:,.1f}K" if (len(me) <= 12 or i == len(me) - 1) else "" for i, v in enumerate(me["평가금액"])],
        textposition="top center",
        customdata=np.stack([me["증감"].fillna(0), me["순입금"]], axis=1),
        hovertemplate="%{x}<br>평가금액 %{y:$,.0f}<br>전월 대비 %{customdata[0]:+$,.0f}<br>입출금 %{customdata[1]:+$,.0f}<extra></extra>"))
    lo, hi = float(me["평가금액"].min()), float(me["평가금액"].max())
    pad = (hi - lo) * 0.18 or hi * 0.05 or 1.0
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", range=[lo - pad, hi + pad * 1.4])
    fig.update_xaxes(type="category")
    return _layout(fig, 320, legend=False).update_layout(hovermode="closest")


def fig_monthly_stock(mc: pd.DataFrame) -> go.Figure:
    """월별 주식 매매손익(수수료 반영). 이익 빨강, 손실 파랑."""
    stock = mc["매매손익"] + mc["수수료"]
    fig = go.Figure(go.Bar(x=mc["월"], y=stock, marker_color=_sign_colors(stock),
                           text=[f"${v:+,.0f}" if (abs(v) >= 1 and len(mc) <= 12) else "" for v in stock],
                           textposition="outside",
                           cliponaxis=False, hovertemplate="%{x} 주식 %{y:$,.0f}<extra></extra>"))
    fig.update_xaxes(type="category")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return _layout(fig, 300, legend=False).update_layout(hovermode="closest")


def fig_monthly_dividend(mc: pd.DataFrame) -> go.Figure:
    """월별 배당 수령액(세후)."""
    d = mc["배당(세후)"]
    fig = go.Figure(go.Bar(x=mc["월"], y=d, marker_color=DIV,
                           text=[f"${v:,.0f}" if (v >= 1 and len(mc) <= 12) else "" for v in d],
                           textposition="outside",
                           cliponaxis=False, hovertemplate="%{x} 배당 %{y:$,.2f}<extra></extra>"))
    fig.update_xaxes(type="category")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return _layout(fig, 300, legend=False).update_layout(hovermode="closest")


def fig_scatter(res: pd.DataFrame) -> go.Figure:
    d = res.dropna(subset=["hold_days", "return_pct"])
    fig = go.Figure()
    if len(d):
        size = np.sqrt(d["pnl"].abs().fillna(0)) * 1.2 + 8
        fig.add_trace(go.Scatter(
            x=d["hold_days"], y=d["return_pct"], mode="markers+text", text=d["ticker"], textposition="top center",
            marker=dict(size=size, color=_sign_colors(d["return_pct"]), opacity=0.75, line=dict(width=1, color="white")),
            customdata=np.stack([d["pnl"].fillna(0), d["r_multiple"].fillna(0)], axis=1),
            hovertemplate="%{text}<br>보유 %{x:.0f}일 · %{y:+.2f}%<br>손익 $%{customdata[0]:,.0f} · %{customdata[1]:+.2f}R<extra></extra>"))
        fig.add_hline(y=0, line_color=GRID)
    fig.update_xaxes(title_text="보유일", ticksuffix="일")
    fig.update_yaxes(title_text="손익률", ticksuffix="%")
    return _layout(fig, 320, legend=False).update_layout(hovermode="closest")


def fig_donut(labels: list, values: list, title: str, colors: list | None = None) -> go.Figure:
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.55, sort=False, marker=dict(colors=colors),
                           textinfo="label+percent", textfont=dict(size=12),
                           hovertemplate="%{label} $%{value:,.0f} (%{percent})<extra></extra>"))
    fig.update_layout(title=dict(text=title, font=dict(size=14)))
    return _layout(fig, 280, legend=False).update_layout(hovermode="closest", margin=dict(l=8, r=8, t=40, b=8))


def fig_gauge(rows: list) -> go.Figure:
    """진행중 포지션의 위치: 매수가 대비 %. 손절(◀) ─ 매수가(0) ─ TP1 ─ TP2(▶), ★ = 현재가."""
    fig = go.Figure()
    labels = [r["label"] for r in rows]
    for i, r in enumerate(rows):
        fig.add_trace(go.Scatter(x=[r["stop"], r["tp2"]], y=[i, i], mode="lines", line=dict(color=GRID, width=10),
                                 hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(x=[r["stop"]], y=[i], mode="markers", marker=dict(symbol="line-ns-open", size=18,
                                 color=DOWN, line=dict(width=3, color=DOWN)), hovertemplate="손절 %{x:+.1f}%<extra></extra>",
                                 showlegend=False))
        fig.add_trace(go.Scatter(x=[0, r["tp1"], r["tp2"]], y=[i] * 3, mode="markers",
                                 marker=dict(symbol=["line-ns-open", "diamond", "diamond"], size=[18, 11, 11],
                                             color=[BENCH, UP, UP], line=dict(width=2, color=BENCH)),
                                 hovertemplate="%{x:+.0f}%<extra></extra>", showlegend=False))
        c = UP if r["now"] > 0 else DOWN
        fig.add_trace(go.Scatter(x=[r["now"]], y=[i], mode="markers+text", text=[f"{r['now']:+.1f}%"], textposition="top center",
                                 marker=dict(symbol="star", size=16, color=c, line=dict(width=1, color="white")),
                                 hovertemplate=f"{r['label']} 현재 %{{x:+.2f}}%<extra></extra>", showlegend=False))
    fig.update_yaxes(tickvals=list(range(len(labels))), ticktext=labels, autorange="reversed", showgrid=False)
    fig.update_xaxes(ticksuffix="%", zeroline=True, zerolinecolor=GRID)
    return _layout(fig, 70 + 52 * len(rows), legend=False).update_layout(hovermode="closest")
