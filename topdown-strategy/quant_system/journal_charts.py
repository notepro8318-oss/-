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


def fig_cum_realized(cum: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.06)
    last = float(cum["cum"].iloc[-1]) if len(cum) else 0.0
    col = UP if last >= 0 else DOWN
    fig.add_trace(go.Scatter(x=cum.index, y=cum["cum"], name="누적 실현손익", line=dict(color=col, width=2.5, shape="hv"),
                             fill="tozeroy", fillcolor="rgba(224,49,49,0.10)" if last >= 0 else "rgba(28,126,214,0.10)",
                             hovertemplate="%{y:$,.0f}"), row=1, col=1)
    fig.add_trace(go.Scatter(x=cum.index, y=cum["dd"], name="고점 대비 낙폭", line=dict(color=DOWN, width=1.5, shape="hv"),
                             fill="tozeroy", fillcolor="rgba(28,126,214,0.20)", hovertemplate="%{y:$,.0f}"), row=2, col=1)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return _layout(fig, 340)


def fig_monthly(mc: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    gain, loss = mc["매매손익"].clip(lower=0), mc["매매손익"].clip(upper=0)
    fig.add_trace(go.Bar(x=mc["월"], y=gain, name="매매이익", marker_color=UP, hovertemplate="%{y:$,.0f}"))
    if (loss != 0).any():
        fig.add_trace(go.Bar(x=mc["월"], y=loss, name="매매손실", marker_color=DOWN, hovertemplate="%{y:$,.0f}"))
    if (mc["배당(세후)"] != 0).any():
        fig.add_trace(go.Bar(x=mc["월"], y=mc["배당(세후)"], name="배당(세후)", marker_color=DIV, hovertemplate="%{y:$,.0f}"))
    if (mc["수수료"] != 0).any():
        fig.add_trace(go.Bar(x=mc["월"], y=mc["수수료"], name="수수료", marker_color=FEE, hovertemplate="%{y:$,.2f}"))
    fig.update_layout(barmode="relative")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return _layout(fig, 300)


def fig_r_hist(res: pd.DataFrame) -> go.Figure:
    """R배수 분포. 손실은 −1R 근처에 모이고 수익은 오른쪽으로 길게 뻗는 모양이 건강한 전략이다."""
    r = res["r_multiple"].dropna()
    fig = go.Figure()
    if len(r):
        lo, hi = np.floor(r.min() * 2) / 2, np.ceil(r.max() * 2) / 2 + 0.5
        edges = np.arange(lo, max(hi, lo + 1.0) + 0.001, 0.5)
        cnt, _ = np.histogram(r, bins=edges)
        centers = (edges[:-1] + edges[1:]) / 2
        fig.add_trace(go.Bar(x=centers, y=cnt, width=0.45, marker_color=_sign_colors(centers), name="거래 수",
                             hovertemplate="%{x:+.2f}R 부근 %{y}건"))
        fig.add_vline(x=float(r.mean()), line_dash="dash", line_color=ACCOUNT,
                      annotation_text=f"평균 {r.mean():+.2f}R", annotation_position="top")
        fig.add_vline(x=0, line_color=GRID)
    fig.update_xaxes(ticksuffix="R", title_text=None)
    fig.update_yaxes(dtick=1, title_text="거래 수")
    return _layout(fig, 300, legend=False)


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


def fig_contrib(ct: pd.DataFrame) -> go.Figure:
    labels = [f"{t}  ${v:+,.0f}" for t, v in zip(ct["ticker"], ct["net"])]
    fig = go.Figure(go.Bar(x=ct["net"], y=labels, orientation="h", marker_color=_sign_colors(ct["net"]),
                           hovertemplate="%{y}<extra></extra>"))
    fig.update_xaxes(tickprefix="$", tickformat=",.0f")
    fig.update_yaxes(automargin=True)
    return _layout(fig, max(220, 30 * len(ct) + 70), legend=False).update_layout(hovermode="closest")


def fig_donut(labels: list, values: list, title: str, colors: list | None = None) -> go.Figure:
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.55, sort=False, marker=dict(colors=colors),
                           textinfo="label+percent", textfont=dict(size=12),
                           hovertemplate="%{label} $%{value:,.0f} (%{percent})<extra></extra>"))
    fig.update_layout(title=dict(text=title, font=dict(size=14)))
    return _layout(fig, 280, legend=False).update_layout(hovermode="closest", margin=dict(l=8, r=8, t=40, b=8))


def fig_calendar(daily: pd.DataFrame) -> go.Figure:
    """일별 실현손익 달력(열=주, 행=요일). 빨강 이익, 파랑 손실."""
    fig = go.Figure()
    if len(daily):
        d = daily.copy()
        d["date"] = pd.to_datetime(d["date"])
        first = d["date"].min() - pd.Timedelta(days=int(d["date"].min().weekday()))
        weeks = int((d["date"].max() - first).days // 7) + 1
        z = np.full((7, weeks), np.nan)
        text = np.full((7, weeks), "", dtype=object)
        for _, r in d.iterrows():
            w, wd = int((r["date"] - first).days // 7), int(r["date"].weekday())
            z[wd, w] = r["net"]
            text[wd, w] = f"{r['date']:%Y-%m-%d} ${r['net']:+,.0f}"
        xs = [(first + pd.Timedelta(weeks=i)).strftime("%m/%d") for i in range(weeks)]
        m = float(np.nanmax(np.abs(z))) or 1.0
        fig.add_trace(go.Heatmap(z=z, x=xs, y=["월", "화", "수", "목", "금", "토", "일"], text=text, hoverinfo="text",
                                 colorscale=[[0, DOWN], [0.5, "rgba(240,240,240,0.9)"], [1, UP]], zmin=-m, zmax=m,
                                 showscale=False, xgap=3, ygap=3))
        fig.update_yaxes(autorange="reversed")
    fig.update_layout(hovermode="closest")
    return _layout(fig, 250, legend=False)


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
