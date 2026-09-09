"""
탑다운(Top-Down) 4단계 전략 - Streamlit 웹 대시보드
====================================================
Streamlit Community Cloud(share.streamlit.io) 배포용 진입점.

1~4단계(시장필터 -> 섹터모멘텀 -> 종목필터 -> 진입/리스크관리)를 yfinance로
매번 실시간 조회해 화면에 렌더링하고, 하단에서는 Backtrader 3년 백테스트를
버튼 클릭으로 직접 실행해볼 수 있다.

배포 방법
---------
1) 이 저장소를 GitHub에 push
2) https://share.streamlit.io 에서 "New app" -> 이 저장소 선택
   -> Main file path: topdown-strategy/streamlit_app.py 로 지정 후 Deploy
"""

from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import streamlit as st

# Streamlit Cloud는 앱 실행 시 작업 디렉터리가 이 스크립트의 위치와 다를 수 있으므로
# (예: 저장소 루트) 상대경로 대신 이 파일 기준 절대경로로 리소스를 찾는다.
APP_DIR = Path(__file__).resolve().parent
EQUITY_CURVE_PATH = APP_DIR / "backtest_equity_curve.png"

from config import (
    MARKET_INDEX, MARKET_MA_PERIOD, MARKET_MA_LOOKBACK,
    SECTOR_ETFS, SECTOR_RET_SHORT, SECTOR_RET_LONG, TOP_SECTOR_COUNT,
    STOCK_MA_SHORT, STOCK_MA_MID, STOCK_MA_LONG, NEAR_52W_HIGH_RATIO, RS_LOOKBACK,
    BOX_BREAKOUT_LOOKBACK, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    INITIAL_CASH, RISK_PER_TRADE_PCT, BACKTEST_YEARS,
)
from market import check_market_trend
from sector import rank_sectors, select_leading_sectors
from stock import scan_leading_stocks
from entry import check_entry_signal, calc_position_size

st.set_page_config(page_title="Top-Down Strategy Desk", page_icon="📈", layout="wide")

CACHE_TTL = 900  # 15분: yfinance 호출 빈도를 줄이기 위한 캐시 유효시간(초)


# ------------------------------------------------------------------
# 데이터 로딩 (캐시)
# ------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_market():
    return check_market_trend()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_sectors():
    ranked = rank_sectors()
    leaders = select_leading_sectors()
    return ranked, leaders


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_candidates(leaders: list, sector_returns: dict):
    candidates = scan_leading_stocks(leaders, sector_returns)
    # DataFrame(df)은 그대로 캐시에 저장되며, 4단계 진입시그널 계산에 재사용된다.
    return candidates


def pct_delta(v: float) -> str:
    return f"{v:+.2f}%"


# ------------------------------------------------------------------
# 사이드바
# ------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 설정")
    capital = st.number_input("계좌 자본금 ($)", min_value=1_000, value=INITIAL_CASH, step=1_000)
    st.caption(f"1회 진입 리스크 한도: 총 자본의 {RISK_PER_TRADE_PCT * 100:.0f}%")

    if st.button("🔄 시그널 새로고침", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(f"조회 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("데이터: yfinance (지연 시세) · 캐시 15분")

st.title("📈 탑다운(Top-Down) 4단계 전략 대시보드")
st.caption("시장추세 → 섹터모멘텀 → 종목필터 → 진입/리스크관리 순서로 매매 시그널을 스캔합니다.")

try:
    # ================================================================
    # 1단계: 시장 추세 확인
    # ================================================================
    st.header("1️⃣ 시장 추세 확인", divider="gray")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **대상**: `{MARKET_INDEX}` (S&P500 ETF)
- **조건 A — 추세 우상향**: 현재 {MARKET_MA_PERIOD}일 이동평균(MA{MARKET_MA_PERIOD})이 {MARKET_MA_LOOKBACK}거래일 전 MA{MARKET_MA_PERIOD}보다 높다
  (MA{MARKET_MA_PERIOD} 오늘 값 > MA{MARKET_MA_PERIOD} {MARKET_MA_LOOKBACK}일 전 값)
- **조건 B — 가격 위치**: 현재 종가가 MA{MARKET_MA_PERIOD} 위에 있다 (종가 > MA{MARKET_MA_PERIOD})
- **판정**: A·B를 **모두** 만족하면 `BULL`(매매 가능), 하나라도 어긋나면 `BEAR/중립`(역배열 포함) →
  신규 매수를 전면 중단하고 **현금(Cash) 100%**를 유지하는 것이 전략 원칙
- 이 단계가 `BEAR`이면 2~4단계는 실제 매매용이 아닌 **참고용 시그널**로만 계산·표시됩니다
""")
    with st.spinner(f"{MARKET_INDEX} 데이터 조회 중..."):
        market = load_market()

    m1, m2, m3, m4 = st.columns([1.2, 1, 1, 1])
    if market["is_bull"]:
        m1.success("● BULL · 매매 가능", icon="✅")
    else:
        m1.error("● BEAR/중립 · 신규 매수 중단", icon="🛑")
    m2.metric(f"{market['ticker']} 종가", f"{market['close']:.2f}")
    m3.metric("MA20(현재)", f"{market['ma20']:.2f}")
    m4.metric("MA20(5일전)", f"{market['ma20_prev']:.2f}",
              delta=f"{market['ma20'] - market['ma20_prev']:+.2f}")

    market_bull = market["is_bull"]
    if not market_bull:
        st.warning("시장이 하락/역배열 추세입니다. 전략 원칙상 신규 매수를 중단하고 현금(Cash) 100%를 유지합니다. "
                   "아래 2~4단계는 **참고용**으로 계속 계산해 보여줍니다.")

    # ================================================================
    # 2단계: 주도 섹터 선정
    # ================================================================
    st.header("2️⃣ 주도 섹터 모멘텀", divider="gray")
    st.caption(f"1주일({SECTOR_RET_SHORT}거래일)·1개월({SECTOR_RET_LONG}거래일) 수익률이 모두 상위 {TOP_SECTOR_COUNT}위 이내인 섹터를 주도 섹터로 선정합니다.")
    with st.expander("📋 조건 상세보기"):
        etf_list = ", ".join(f"`{t}`" for t in SECTOR_ETFS)
        st.markdown(f"""
- **대상 ETF ({len(SECTOR_ETFS)}개)**: {etf_list}
- **1단계 계산**: 각 ETF의 {SECTOR_RET_SHORT}거래일(1주일) 수익률과 {SECTOR_RET_LONG}거래일(1개월) 수익률을 각각 구한다
- **2단계 순위화**: 전체 ETF를 두 기간 각각의 수익률 기준 내림차순으로 순위를 매긴다 (1주 순위, 1개월 순위)
- **주도 섹터 판정**: 1주 순위와 1개월 순위가 **모두** 상위 {TOP_SECTOR_COUNT}위 이내인 ETF만 선정 (두 조건의 교집합)
  — 최근 1주일만 반짝 오른 섹터, 또는 1개월간 완만히 올랐지만 최근 1주일은 꺾인 섹터는 제외되어
  **단기·중기 모멘텀이 동시에 살아있는 섹터**만 남는다
- 주도 섹터가 하나도 없으면 3~4단계는 스캔할 대상이 없어 매수 후보가 나오지 않는다
""")

    with st.spinner("섹터 ETF 수익률 계산 중..."):
        ranked, leaders = load_sectors()

    if leaders:
        st.write(" ".join(f"🏆 **{t}**" for t in leaders))
    else:
        st.info("조건을 동시에 만족하는 주도 섹터가 없습니다.")

    disp = ranked.copy()
    disp["rank_1w"] = disp["rank_1w"].astype(int)
    disp["rank_1m"] = disp["rank_1m"].astype(int)
    disp.index.name = "ETF"
    disp = disp.rename(columns={
        "ret_1w": "1주 수익률(%)", "ret_1m": "1개월 수익률(%)",
        "rank_1w": "1주 순위", "rank_1m": "1개월 순위",
    })
    st.dataframe(disp.style.format({"1주 수익률(%)": "{:+.2f}", "1개월 수익률(%)": "{:+.2f}"}),
                 use_container_width=True)

    # ================================================================
    # 3단계 + 4단계: 종목 필터 -> 진입 시그널
    # ================================================================
    st.header("3️⃣ 강세 종목 발굴", divider="gray")
    st.caption(f"정배열(현재가>MA{STOCK_MA_SHORT}>MA{STOCK_MA_MID}>MA{STOCK_MA_LONG}) · 52주 신고가 {(1-NEAR_52W_HIGH_RATIO)*100:.0f}% 이내 · "
               f"{RS_LOOKBACK}거래일 상대강도(RS) 3가지 조건을 모두 만족한 종목입니다.")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **대상**: 2단계 주도 섹터 ETF의 대표 상위 구성 종목 (섹터별 약 10개 종목으로 큐레이션된 리스트)
- **조건 1 — 정배열**: 현재가 > MA{STOCK_MA_SHORT} > MA{STOCK_MA_MID} > MA{STOCK_MA_LONG}
  (단기·중기·장기 이동평균이 순서대로 위에서부터 정렬되어야 함 — 뚜렷한 상승 추세 확인)
- **조건 2 — 52주 신고가 근접**: 현재가 ≥ 52주 신고가 × {NEAR_52W_HIGH_RATIO:.2f}
  (52주 최고가 대비 {(1-NEAR_52W_HIGH_RATIO)*100:.0f}% 이내 — 신고가를 뚫을 힘이 있는 강세 종목만 선별)
- **조건 3 — 상대강도(RS)**: 최근 {RS_LOOKBACK}거래일(1개월) 종목 수익률 > 소속 섹터 ETF의 {RS_LOOKBACK}거래일 수익률
  (같은 섹터 안에서도 시장 대비, 섹터 대비 더 강하게 오르는 종목인지 확인)
- **판정**: 세 조건을 **모두** 동시에 만족해야 통과 (하나라도 어긋나면 후보에서 제외)
""")

    sector_returns = ranked["ret_1m"].to_dict()
    with st.spinner("주도 섹터 대표 종목 스캔 중... (수십 개 티커 조회로 다소 시간이 걸릴 수 있습니다)"):
        candidates = load_candidates(leaders, sector_returns) if leaders else []

    if not candidates:
        st.info("3단계 조건을 통과한 종목이 없습니다.")
    else:
        cols = st.columns(3)
        for i, c in enumerate(candidates):
            with cols[i % 3]:
                with st.container(border=True):
                    st.markdown(f"**{c['ticker']}** · `{c['sector']}`")
                    st.metric("현재가", f"${c['close']:.2f}")
                    st.caption(f"MA20 {c['ma20']:.2f} · MA50 {c['ma50']:.2f} · MA200 {c['ma200']:.2f}")
                    st.caption(f"52주 고점 {c['high_52w']:.2f} ({(c['close']/c['high_52w']-1)*100:+.1f}%)")
                    st.caption(f"1개월 수익률 {pct_delta(c['stock_ret_1m'])} vs 섹터 {pct_delta(c['sector_ret_1m'])} "
                               f"(RS {c['stock_ret_1m']-c['sector_ret_1m']:+.1f}%p)")

    st.header("4️⃣ 매수 타점 & 리스크 관리", divider="gray")
    st.caption(f"눌림목(저가가 MA{STOCK_MA_SHORT} 터치 후 종가 사수) 또는 {BOX_BREAKOUT_LOOKBACK}일 박스권 고점 돌파 시 매수. "
               f"손절 -{STOP_LOSS_PCT*100:.0f}%/MA{STOCK_MA_SHORT}이탈, 익절 +{TAKE_PROFIT_PCT*100:.0f}%, "
               f"1회 리스크는 계좌의 {RISK_PER_TRADE_PCT*100:.0f}%로 제한합니다.")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
**매수 타점 — 아래 (A) 또는 (B) 중 하나만 만족하면 진입**
- **(A) 눌림목**: 당일 **저가**가 MA{STOCK_MA_SHORT}을 터치(저가 ≤ MA{STOCK_MA_SHORT})했지만
  **종가**는 MA{STOCK_MA_SHORT}을 지켜냄(종가 > MA{STOCK_MA_SHORT}) — 단기 조정 후 지지선을 사수한 패턴
- **(B) 박스권 돌파**: 종가가 최근 {BOX_BREAKOUT_LOOKBACK}거래일(당일 제외) 고가의 최고치를 상향 돌파
  — 횡보 박스권을 강하게 뚫고 올라가는 패턴

**리스크 관리 (손익비 2:1)**
- **손절가**: 진입가 대비 −{STOP_LOSS_PCT*100:.0f}% 또는 MA{STOCK_MA_SHORT} 중 **더 타이트한(높은) 가격**
  — MA{STOCK_MA_SHORT}이 진입가에 가까우면 −{STOP_LOSS_PCT*100:.0f}%보다 먼저 MA{STOCK_MA_SHORT} 이탈로 손절
- **익절가**: 진입가 대비 +{TAKE_PROFIT_PCT*100:.0f}% 도달 시 전량 청산
- **포지션 사이징 (자금 관리)**:
  1. 리스크 허용액 = 계좌 자본 × {RISK_PER_TRADE_PCT*100:.0f}%
  2. 1주당 리스크 = 진입가 − 손절가
  3. 수량 = 리스크 허용액 ÷ 1주당 리스크 (단, 레버리지 없이 **보유 현금 한도**를 초과하지 않도록 한 번 더 제한)
  → 손절폭이 좁을수록(눌림목이 타이트할수록) 더 많은 수량을, 손절폭이 넓을수록 더 적은 수량을 매수해
    **거래마다 손실액을 계좌의 {RISK_PER_TRADE_PCT*100:.0f}%로 균일하게 맞춘다**
""")

    signal_count = 0
    for c in candidates:
        entry = check_entry_signal(c["df"])
        if not entry["signal"]:
            continue
        signal_count += 1
        sizing = calc_position_size(capital, entry["entry_price"], entry["stop_loss"])
        risk_pct_actual = sizing["risk_amount"] / capital * 100 if capital else 0

        with st.container(border=True):
            st.markdown(f"### ✅ {c['ticker']} · `{entry['type']}` · {c['sector']}")
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            s1.metric("진입가", f"{entry['entry_price']:.2f}")
            s2.metric("손절가", f"{entry['stop_loss']:.2f}")
            s3.metric("익절가", f"{entry['take_profit']:.2f}")
            s4.metric("수량", f"{sizing['shares']:,}주")
            s5.metric("투입금액", f"${sizing['position_value']:,.0f}")
            s6.metric("계좌 리스크", f"{risk_pct_actual:.1f}%")

    if signal_count == 0:
        st.info("오늘은 눌림목/돌파 조건을 만족하는 매수 시그널이 없습니다.")

except Exception as e:
    st.error(f"데이터 조회 중 오류가 발생했습니다: {e}\n\n"
             "yfinance가 일시적으로 요청을 제한했을 수 있습니다. 잠시 후 새로고침 해보세요.")

# ================================================================
# 3년 백테스트 (버튼 클릭 시 실행 - 무거운 작업이라 기본 비활성)
# ================================================================
st.header("📊 3년 백테스트 리포트 (Backtrader)", divider="gray")
st.caption("아래 정적 리포트는 로컬에서 미리 실행해 둔 참고용 결과입니다. "
           "직접 다시 실행하려면 버튼을 눌러주세요 — 전체 유니버스(~70개 티커) 다운로드로 1~3분 정도 걸릴 수 있습니다.")

b1, b2, b3, b4 = st.columns(4)
b1.metric("총 수익률 (최근 3년)", "+55.46%")
b2.metric("연복리수익률(CAGR)", "+15.84%")
b3.metric("최대낙폭(MDD)", "-19.27%")
b4.metric("샤프 비율 / 승률", "1.11", "51.1% (92건)")
if EQUITY_CURVE_PATH.exists():
    st.image(str(EQUITY_CURVE_PATH), caption="탑다운 전략 3년 백테스트 자산 곡선 (참고용, 로컬 실행 결과)")
else:
    st.info("자산곡선 이미지(backtest_equity_curve.png)를 찾을 수 없습니다.")

with st.expander("▶ 지금 다시 백테스트 실행하기"):
    bt_years = st.slider("백테스트 기간(년)", min_value=1, max_value=5, value=BACKTEST_YEARS)
    run_clicked = st.button("백테스트 실행", type="primary")

    if run_clicked:
        from backtest import run_backtest, get_backtest_stats, get_equity_series

        with st.spinner(f"최근 {bt_years}년(+워밍업 1년) 데이터 다운로드 및 백테스트 실행 중... 잠시만 기다려주세요."):
            try:
                strat, start_value, end_value = run_backtest(
                    years=bt_years, capital=capital, printlog=False, save_chart=False,
                )
                stats = get_backtest_stats(strat, start_value, end_value, bt_years)
                dates, equity = get_equity_series(strat)
            except Exception as e:
                st.error(f"백테스트 실행 중 오류: {e}")
            else:
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("총 수익률", f"{stats['total_return_pct']:+.2f}%")
                r2.metric("CAGR", f"{stats['cagr_pct']:+.2f}%")
                r3.metric("MDD", f"-{stats['mdd_pct']:.2f}%")
                sharpe_txt = f"{stats['sharpe']:.2f}" if stats["sharpe"] is not None else "N/A"
                r4.metric("샤프 비율", sharpe_txt)
                st.write(f"총 거래 {stats['total_trades']}건 (승 {stats['won']} / 패 {stats['lost']}, "
                         f"승률 {stats['win_rate']:.1f}%)")

                if dates:
                    fig, ax = plt.subplots(figsize=(10, 4))
                    ax.plot(dates, equity, linewidth=1.5)
                    ax.set_title("Cumulative Equity Curve")
                    ax.set_ylabel("Equity (normalized, start=1.0)")
                    ax.grid(alpha=0.3)
                    st.pyplot(fig)

st.caption("⚠️ 본 대시보드는 투자 자문이 아니며 참고용입니다. 백테스트는 과거 데이터 기반이며 향후 수익을 보장하지 않습니다.")
