"""스마트폰 화면 최적화 (감지·스타일·차트 조정). Streamlit 비의존 — 순수 함수와 상수만 둔다."""
from __future__ import annotations

import re

# 휴대폰만 모바일로 본다. iPad·안드로이드 태블릿(UA에 'Mobile' 없음)은 화면이 넓어 PC 구성을 쓴다.
_PHONE_UA = re.compile(r"iPhone|iPod|Android.*Mobile|Windows Phone|Mobile.*Firefox|Opera Mini", re.I)

OPEN_TABLE_COLUMNS = ["종목", "손익률(%)", "현재가", "손절까지(%)", "시그널"]
CLOSED_TABLE_COLUMNS = ["종목", "손익률(%)", "R배수", "매도일", "규칙 준수"]


def is_mobile(user_agent: str | None, view: str | None = None) -> bool:
    """주소 뒤 ?view=mobile / ?view=desktop 이 있으면 그것을 따르고, 없으면 User-Agent로 판단한다."""
    v = (view or "").strip().lower()
    if v in ("mobile", "m"):
        return True
    if v in ("desktop", "pc"):
        return False
    return bool(user_agent and _PHONE_UA.search(user_agent))


CSS = """
<style>
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"], .block-container { padding: 3.2rem 0.75rem 5rem !important; }
  h1 { font-size: 20px !important; }
  [data-testid="stHeadingWithActionElements"] h2 { font-size: 18px !important; }
  h3 { font-size: 16px !important; }
  [data-testid="stMetricValue"] { font-size: 19px !important; }
  [data-testid="stMetricLabel"] p { font-size: 12px !important; }
  /* 숫자 카드는 세로로 길게 쌓지 않고 한 줄에 2개씩 */
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) {
    flex-direction: row !important; flex-wrap: wrap !important; gap: 0.5rem !important; }
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [data-testid="stMetric"]) > [data-testid="stColumn"] {
    min-width: calc(50% - 0.5rem) !important; width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important; }
  /* 탭은 한 줄에서 옆으로 밀어서 보기 */
  [data-testid="stTabs"] [role="tablist"] { gap: 0.9rem !important; overflow-x: auto !important; }
  [data-testid="stTabs"] [role="tab"] p { font-size: 14px !important; white-space: nowrap; }
  /* 손가락으로 누르기 쉬운 버튼, iOS 입력창 자동 확대 방지(16px) */
  [data-testid="stMainBlockContainer"] button[kind] { min-height: 44px; }
  input, textarea, select { font-size: 16px !important; }
}
</style>
"""


def tune_figure(fig, drop_point_labels: bool = False):
    """휴대폰용 차트: 손가락 스크롤이 막히지 않게 확대·드래그를 끄고 여백과 높이를 줄인다."""
    height = fig.layout.height or 320
    fig.update_layout(dragmode=False, height=int(min(height, 300)) if height <= 400 else int(height),
                      margin=dict(l=2, r=2, t=30, b=4), font=dict(size=11),
                      legend=dict(font=dict(size=11)))
    fig.layout.annotations = ()      # 입출금 라벨은 좁은 화면에서 범례와 겹친다(점선은 남는다)
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True, title_text=None)
    if drop_point_labels:            # 점마다 붙는 글자는 좁은 화면에서 겹치므로 눌러서(호버) 본다
        fig.for_each_trace(lambda t: t.update(mode="markers") if getattr(t, "mode", None) == "markers+text" else None)
    return fig


def summary_html(cost: float, value: float, pnl: float, pnl_pct: float, up: str, down: str) -> str:
    """보유 현황 요약을 한 줄 3칸으로."""
    color = up if pnl > 0 else down if pnl < 0 else "inherit"
    cell = ("<div style='flex:1;min-width:0;border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:8px'>"
            "<div style='font-size:11px;opacity:.7'>{label}</div>"
            "<div style='font-size:17px;font-weight:700;color:{color};white-space:nowrap'>{value}</div>"
            "<div style='font-size:11px;color:{color}'>{sub}</div></div>")
    return ("<div style='display:flex;gap:6px;margin-bottom:8px'>"
            + cell.format(label="투자금액", color="inherit", value=f"&#36;{cost:,.0f}", sub="&nbsp;")
            + cell.format(label="평가금액", color="inherit", value=f"&#36;{value:,.0f}", sub="&nbsp;")
            + cell.format(label="평가손익", color=color, value=f"&#36;{pnl:+,.0f}", sub=f"{pnl_pct:+.2f}%")
            + "</div>")
