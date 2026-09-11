"""
TopDownQuantSystem - Streamlit 웹 대시보드 (v2: 모멘텀 랭킹 + Runner 전략)
====================================================
Streamlit Community Cloud(share.streamlit.io) 배포용 진입점.

MarketRegimeDetector -> SectorRotationEngine -> MomentumRanker -> ExecutionEngine
-> RiskManager 를 yfinance 실시간 데이터로 조회해 렌더링하고,
하단에서 TopDownBacktesterV2를 버튼 클릭으로 직접 실행해 볼 수 있다.

v1(AND 하드필터 + 눌림목/돌파 타이밍 대기)에서 v2(Cross-Sectional 모멘텀
랭킹 + 즉시매수 + ATR 손절/분할익절/Runner 추세추종)로 전면 교체됐다.
백테스트 검증 결과 v1 최적조합(CAGR +6.83%) 대비 v2가 CAGR +16.65%로
우수해 프로덕션에 반영했다.

배포 방법
---------
1) 이 저장소를 GitHub에 push
2) https://share.streamlit.io 에서 "New app" -> 이 저장소 선택
   -> Main file path: topdown-strategy/quant_system/streamlit_app.py 로 지정 후 Deploy
"""

from __future__ import annotations

from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import streamlit as st

from qs_config import (
    BENCHMARK, SECTOR_ETFS, DEFENSIVE_ETFS, SECTOR_STOCKS,
    ROC_SHORT, ROC_LONG, TOP_SECTOR_COUNT, TOP_STOCK_COUNT,
    STOP_ATR_MULT_V2, TP1_PCT, TP1_FRACTION, TP2_PCT, TP2_FRACTION, RUNNER_EXIT_MA_PERIOD,
    IDLE_CASH_FALLBACK_ENABLED, FALLBACK_INDEX_TICKER,
    INITIAL_EQUITY, BACKTEST_YEARS,
)
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from qs_sector import SectorRotationEngine
from screener import MomentumRanker
from execution import ExecutionEngine
from risk import RiskManager

st.set_page_config(page_title="TopDown Quant System", page_icon="📊", layout="wide")

# 섹션 대제목(1️⃣~4️⃣, 백테스트 등 divider가 있는 st.header)만 3pt(=4px) 축소한다.
# stHeadingWithActionElements는 divider가 붙은 헤더에만 생기는 래퍼라 사이드바 제목과는 겹치지 않는다.
st.markdown(
    """
    <style>
    [data-testid="stHeadingWithActionElements"] h2 { font-size: 32px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

CACHE_TTL = 900  # 15분

# v2 백테스트에서 검증된 최적 사이징
V2_RISK_PCT = 0.03
V2_POSITION_CAP_PCT = 0.40

regime_detector = MarketRegimeDetector()
sector_engine = SectorRotationEngine()
ranker = MomentumRanker(top_n=TOP_STOCK_COUNT)
execution = ExecutionEngine()
risk_manager = RiskManager(risk_pct=V2_RISK_PCT, cap_pct=V2_POSITION_CAP_PCT)


# ------------------------------------------------------------------
# 데이터 로딩 (캐시)
# ------------------------------------------------------------------
@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_benchmark():
    return fetch_ohlcv(BENCHMARK, period="3y")


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_regime(_bench_df):
    return regime_detector.current_regime(_bench_df)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_sector_scores():
    data = {etf: fetch_ohlcv(etf, period="6mo") for etf in SECTOR_ETFS}
    return sector_engine.score_sectors(data), data


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_ranked_candidates(leaders: list, _bench_df):
    candidates_data = {}
    ticker_sector_map = {}
    for sector in leaders:
        for ticker in SECTOR_STOCKS.get(sector, []):
            df = fetch_ohlcv(ticker, period="3y")
            if not df.empty:
                candidates_data[ticker] = df
                ticker_sector_map.setdefault(ticker, sector)
    ranked = ranker.rank_candidates(candidates_data, _bench_df)
    return ranked, candidates_data, ticker_sector_map


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_usd_krw_rate():
    """실시간 USD/KRW 환율(1달러 = ?원)을 조회한다. 실패 시 None."""
    df = fetch_ohlcv("KRW=X", period="5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%" if v is not None else "N/A"


def _fmt_comma(v) -> str:
    """숫자를 3자리마다 콤마를 넣은 문자열로 변환한다 (예: 1000000 -> '1,000,000')."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0"
    return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"


def _parse_comma(s: str) -> float:
    """콤마/공백 등을 제거하고 숫자만 남겨 float으로 변환한다. 빈 값이면 0."""
    digits = "".join(ch for ch in str(s) if ch.isdigit() or ch == ".")
    return float(digits) if digits else 0.0


# ------------------------------------------------------------------
# 사이드바
# ------------------------------------------------------------------
usd_krw_rate = load_usd_krw_rate()


def _on_krw_change():
    raw = _parse_comma(st.session_state["capital_krw_text"])
    st.session_state["capital_krw_text"] = _fmt_comma(raw)
    if usd_krw_rate:
        st.session_state["capital_usd_text"] = _fmt_comma(round(raw / usd_krw_rate, 2))


def _on_usd_change():
    raw = _parse_comma(st.session_state["capital_usd_text"])
    st.session_state["capital_usd_text"] = _fmt_comma(raw)


with st.sidebar:
    st.header("⚙️ 설정")

    if usd_krw_rate:
        st.caption(f"실시간 환율(USD/KRW): 1달러 = {usd_krw_rate:,.2f}원")
        default_krw = int(INITIAL_EQUITY * usd_krw_rate)
    else:
        st.caption("⚠️ 환율 조회 실패 — 달러 금액을 직접 입력해 주세요.")
        default_krw = int(INITIAL_EQUITY * 1_300)

    st.text_input(
        "계좌 자본금 (₩)", value=_fmt_comma(default_krw),
        key="capital_krw_text", on_change=_on_krw_change, disabled=usd_krw_rate is None,
        help="원화로 입력하면 실시간 환율로 환산되어 아래 달러 금액에 자동 반영됩니다. "
             "3자리마다 콤마(,)가 자동으로 표시됩니다.",
    )
    st.text_input(
        "계좌 자본금 ($)", value=_fmt_comma(INITIAL_EQUITY),
        key="capital_usd_text", on_change=_on_usd_change,
        help="3자리마다 콤마(,)가 자동으로 표시됩니다.",
    )
    capital = _parse_comma(st.session_state["capital_usd_text"])
    st.caption(f"1회 진입 리스크: 자본의 {V2_RISK_PCT*100:.0f}% · 단일종목 상한: 자본의 {V2_POSITION_CAP_PCT*100:.0f}%")

    if st.button("🔄 시그널 새로고침", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.caption(f"조회 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("데이터: yfinance (지연 시세) · 캐시 15분")

st.title("📊 TopDownQuantSystem — 모멘텀 랭킹 + Runner 대시보드")
st.caption("MarketRegimeDetector → SectorRotationEngine → MomentumRanker → ExecutionEngine/RiskManager "
           "순서로 실시간 매수 후보를 계산합니다. (12-1M 모멘텀 · 유휴자금 폭포수배분 · ATR손절/분할익절/Runner추세추종)")

try:
    # ================================================================
    # Module 1: 시장 국면 판정
    # ================================================================
    st.header("1️⃣ 시장 국면 판정 (MarketRegimeDetector)", divider="gray")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **대상**: `{BENCHMARK}`
- **BULL**: Close ≥ MA20×1.005 AND Slope5d(MA20) ≥ +0.05% AND Close ≥ MA200 — **2거래일 연속** 확인 시 확정
- **BEAR**: Close ≤ MA20×0.995 AND Slope5d(MA20) ≤ −0.05% AND Close < MA200 — **2거래일 연속** 확인 시 확정
- **SIDEWAYS**: 위 두 조건에 해당하지 않는 모든 상태
- **노출 비중**: BULL 100% · SIDEWAYS 40% · BEAR 0%(현금 보존, 신규매수 차단)
""")

    with st.spinner(f"{BENCHMARK} 데이터 조회 중..."):
        bench_df = load_benchmark()
        snapshot = load_regime(bench_df)

    regime = snapshot["regime"]
    exposure = snapshot["exposure"]

    c1, c2, c3, c4, c5 = st.columns([1.3, 1, 1, 1, 1])
    badge = {"BULL": ("✅", "success"), "SIDEWAYS": ("〰️", "warning"), "BEAR": ("🛑", "error")}[regime]
    getattr(c1, badge[1])(f"{badge[0]} {regime} · 노출 {exposure*100:.0f}%")
    c2.metric(f"{BENCHMARK} 종가", f"{snapshot['close']:.2f}")
    c3.metric("MA20", f"{snapshot['ma20']:.2f}")
    c4.metric("MA200", f"{snapshot['ma200']:.2f}")
    c5.metric("Slope5d(MA20)", fmt_pct(snapshot["slope5d"]))

    if regime == "BEAR":
        st.error("BEAR 국면입니다. 신규 매수를 전면 차단하고 현금 100%를 유지합니다. "
                 "아래 2~4단계는 참고용으로만 계속 표시됩니다.")

    # ================================================================
    # Module 2: 섹터 로테이션
    # ================================================================
    st.header("2️⃣ 섹터 로테이션 (SectorRotationEngine)", divider="gray")
    st.caption(f"1주({ROC_SHORT}거래일)/1개월({ROC_LONG}거래일) 수익률 순위를 0.3:0.7로 가중해 "
               f"Score_Rank가 가장 낮은(우수) 상위 {TOP_SECTOR_COUNT}개 섹터를 주도 섹터로 선정합니다.")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **BULL**: GICS 11개 섹터 ETF 전체 중 Score_Rank 상위 {TOP_SECTOR_COUNT}개
- **SIDEWAYS**: 방어적 섹터({', '.join(DEFENSIVE_ETFS)}) 4개 중 Score_Rank 상위 {TOP_SECTOR_COUNT}개
- **BEAR**: 섹터 미선정 (Cash 100%)
- Score_Rank = 0.3 × Rank(1주 수익률) + 0.7 × Rank(1개월 수익률) — 낮을수록 우수
""")

    with st.spinner("섹터 ETF 스코어 계산 중..."):
        scored, sector_price_data = load_sector_scores()
        leaders = sector_engine.select_leading_sectors(sector_price_data, regime)

    if leaders:
        st.write(" ".join(f"🏆 **{t}**" for t in leaders))
    else:
        st.info("BEAR 국면이거나 조건을 만족하는 주도 섹터가 없습니다.")

    def _format_score_table(df):
        disp = df.copy()
        disp.index.name = "ETF"
        disp = disp.rename(columns={
            "roc_5d": "ROC 1주", "roc_21d": "ROC 1개월",
            "rank_1w": "순위(1주)", "rank_1m": "순위(1개월)", "score": "Score_Rank",
        })
        return disp.style.format({"ROC 1주": "{:+.2%}", "ROC 1개월": "{:+.2%}", "Score_Rank": "{:.2f}"})

    if regime == "SIDEWAYS":
        # SIDEWAYS는 방어적 섹터 4개 안에서만 다시 순위를 매겨 선정하므로,
        # 전체 11개 기준 표를 그대로 보여주면 "1등인데 왜 안 뽑혔지?"로 오해하기 쉽다.
        # 실제 선정에 쓰인 것과 동일한(방어적 섹터로 제한된) 순위표를 보여준다.
        defensive_data = {t: sector_price_data[t] for t in DEFENSIVE_ETFS if t in sector_price_data}
        scored_defensive = sector_engine.score_sectors(defensive_data)
        st.caption(f"⚠️ 현재 SIDEWAYS 국면이라 방어적 섹터({', '.join(DEFENSIVE_ETFS)}) 4개 안에서만 "
                   "다시 순위를 매겨 선정합니다. 아래는 그 4개 기준 실제 선정용 순위표입니다.")
        st.dataframe(_format_score_table(scored_defensive), use_container_width=True)
        with st.expander("전체 11개 섹터 기준 순위표 보기 (참고용 — SIDEWAYS 선정에는 미사용)"):
            st.dataframe(_format_score_table(scored), use_container_width=True)
    else:
        st.dataframe(_format_score_table(scored), use_container_width=True)

    # ================================================================
    # Module 3: 모멘텀 랭킹 (Cross-Sectional CompositeScore)
    # ================================================================
    st.header("3️⃣ 모멘텀 랭킹 (MomentumRanker)", divider="gray")
    st.caption(f"12-1M 모멘텀 + 63일/21일 수익률 + 52주 신고가 근접도 + 맨스필드 RS(MRS)를 가중 합산한 "
               f"CompositeScore로 주도 섹터 내 상위 {TOP_STOCK_COUNT}종목을 매주 선정합니다 (AND 하드필터 대신 랭킹).")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **MomentumScore** = 0.5×Rank(Mom_12_1) + 0.3×Rank(ROC_63) + 0.2×Rank(ROC_21)
  - Mom_12_1 = (Close[t-21] − Close[t-252]) / Close[t-252]
- **CompositeScore** = 0.4×Rank(MomentumScore) + 0.3×Rank(52주신고가근접도) + 0.3×Rank(MRS) — 낮을수록 우수
- 상위 {TOP_STOCK_COUNT}종목은 눌림목/돌파 타이밍을 기다리지 않고 즉시 매수 후보로 선정됩니다
""")

    with st.spinner("주도 섹터 대표 종목 모멘텀 랭킹 계산 중... (수십 개 티커 조회로 다소 시간이 걸릴 수 있습니다)"):
        ranked, candidates_data, ticker_sector_map = load_ranked_candidates(leaders, bench_df) if leaders else (None, {}, {})

    if ranked is None or ranked.empty:
        st.info("랭킹 가능한 종목이 없습니다.")
        top_stocks = None
    else:
        top_stocks = ranked.head(TOP_STOCK_COUNT)
        cols = st.columns(3)
        for i, (ticker, row) in enumerate(top_stocks.iterrows()):
            with cols[i % 3]:
                with st.container(border=True):
                    st.markdown(f"**{ticker}** · `{ticker_sector_map.get(ticker, '-')}` · CompositeScore {row['composite_score']:.1f}")
                    st.metric("현재가", f"${row['close']:.2f}")
                    st.caption(f"12-1M모멘텀 {row['mom_121']:+.1%} · 63일 {row['roc_63']:+.1%} · 21일 {row['roc_21']:+.1%}")
                    st.caption(f"52주고점근접도 {row['near_high_ratio']:.2f} · MRS {row['mrs']:+.2f}")

        disp_rank = ranked.rename(columns={
            "mom_121": "12-1M모멘텀", "roc_63": "63일수익률", "roc_21": "21일수익률",
            "near_high_ratio": "52주고점근접도", "mrs": "MRS", "composite_score": "CompositeScore",
        })
        with st.expander(f"전체 후보 {len(ranked)}종목 랭킹표 보기"):
            st.dataframe(
                disp_rank[["12-1M모멘텀", "63일수익률", "21일수익률", "52주고점근접도", "MRS", "CompositeScore"]]
                .style.format({"12-1M모멘텀": "{:+.1%}", "63일수익률": "{:+.1%}", "21일수익률": "{:+.1%}",
                               "52주고점근접도": "{:.2f}", "MRS": "{:+.2f}", "CompositeScore": "{:.1f}"}),
                use_container_width=True,
            )

    # ================================================================
    # Module 4: 매수 후보 & 리스크 관리 (Runner 전략)
    # ================================================================
    st.header("4️⃣ 매수 후보 & 리스크 관리 (Runner 전략)", divider="gray")
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- 3단계 랭킹 상위 {TOP_STOCK_COUNT}종목은 타이밍 대기 없이 즉시 매수 후보입니다
- **손절가** = 진입가 − {STOP_ATR_MULT_V2}×ATR14
- **+{TP1_PCT*100:.0f}%** 도달 시 물량의 {TP1_FRACTION*100:.0f}% 분할익절 + 본전 손절가 상향
- **+{TP2_PCT*100:.0f}%** 도달 시 추가 {TP2_FRACTION*100:.0f}% 분할익절
- 잔여 {(1-TP1_FRACTION-TP2_FRACTION)*100:.0f}%는 종가가 MA{RUNNER_EXIT_MA_PERIOD} 아래로 이탈할 때까지 추세추종(Runner)
- **포지션 비중** = CompositeScore 비례배분 — 점수가 좋을수록(낮을수록) 비중이 커지도록 1/Score로 가중해
  이번 상위 {TOP_STOCK_COUNT}종목 내에서 정규화(합 100%) × 국면 노출 승수
""")

    stock_value = 0.0
    if top_stocks is not None:
        regime_mult = risk_manager.regime_multiplier(regime)
        inv_scores = {t: 1.0 / s for t, s in top_stocks["composite_score"].items() if s and s > 0}
        total_inv = sum(inv_scores.values())
        signal_count = 0
        for ticker, row in top_stocks.iterrows():
            df = candidates_data[ticker]
            ind = execution.compute_indicators(df)
            atr = ind.iloc[-1]["ATR14"]
            if atr != atr or atr <= 0:
                continue
            entry_price = float(df["Close"].iloc[-1])
            stop = execution.initial_stop_v2(entry_price, float(atr))

            weight = (inv_scores.get(ticker, 0.0) / total_inv) if total_inv > 0 else 0.0
            position_value = capital * regime_mult * weight
            shares = int(position_value // entry_price)
            if shares <= 0:
                continue
            actual_value = shares * entry_price
            signal_count += 1
            stock_value += actual_value

            with st.container(border=True):
                st.markdown(f"### ✅ {ticker} · `{ticker_sector_map.get(ticker, '-')}` · CompositeScore {row['composite_score']:.1f}")
                r1, r2, r3, r4, r5, r6 = st.columns(6)
                r1.metric("진입가", f"{entry_price:.2f}")
                r2.metric("손절가", f"{stop:.2f}")
                r3.metric("비중", f"{weight*100:.1f}%")
                r4.metric("수량", f"{shares:,}주")
                r5.metric("투입금액", f"${actual_value:,.0f}")
                r6.metric("MRS", f"{row['mrs']:+.2f}")

        if signal_count == 0:
            st.info("오늘은 매수 가능한 후보가 없습니다.")

    if IDLE_CASH_FALLBACK_ENABLED and regime == "BULL":
        idle_estimate = capital - stock_value
        if idle_estimate > capital * 0.05:
            st.info(f"💧 **유휴자금 폭포수 배분**: 상위 종목 매수 후 약 ${idle_estimate:,.0f}의 현금이 남습니다. "
                    f"BULL 국면 동안 현금 비중 0%를 유지하려면 이 금액을 {FALLBACK_INDEX_TICKER}에 배분하고, "
                    f"국면이 BEAR로 바뀌면 전량 청산하세요.")

except Exception as e:
    st.error(f"데이터 조회 중 오류가 발생했습니다: {e}\n\n"
             "yfinance가 일시적으로 요청을 제한했을 수 있습니다. 잠시 후 새로고침 해보세요.")

# ================================================================
# 백테스트 (버튼 클릭 시 실행 - 무거운 작업이라 기본 비활성)
# ================================================================
st.header("📈 백테스트 (TopDownBacktesterV2)", divider="gray")
st.caption(f"전체 유니버스(벤치마크+섹터ETF 11개+대표종목 ~{sum(len(v) for v in SECTOR_STOCKS.values())}개) 다운로드로 "
           "수 분 정도 걸릴 수 있습니다. 포지션 비중은 CompositeScore 비례배분. "
           "검증 결과: 3년 기준 CAGR +16.39% · MDD -13.86% · Sharpe 1.13 · 승률 63.6%.")

with st.expander("▶ 백테스트 실행하기"):
    bt_years = st.slider("백테스트 기간(년)", min_value=1, max_value=5, value=BACKTEST_YEARS)
    run_clicked = st.button("백테스트 실행", type="primary")

    if run_clicked:
        from backtest_v2 import TopDownBacktesterV2

        with st.spinner(f"최근 {bt_years}년(+워밍업 2년) 데이터 다운로드 및 백테스트 실행 중... 잠시만 기다려주세요."):
            try:
                bt = TopDownBacktesterV2(years=bt_years, initial_equity=capital,
                                          risk_pct=V2_RISK_PCT, position_cap_pct=V2_POSITION_CAP_PCT,
                                          sizing_mode="composite_score")
                stats = bt.run()
            except Exception as e:
                st.error(f"백테스트 실행 중 오류: {e}")
                stats = None

        if stats:
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("총 수익률", f"{stats['total_return_pct']:+.2f}%")
            r2.metric("CAGR", f"{stats['cagr_pct']:+.2f}%")
            r3.metric("MDD", f"{stats['mdd_pct']:.2f}%")
            sharpe_txt = f"{stats['sharpe']:.2f}" if stats["sharpe"] == stats["sharpe"] else "N/A"
            r4.metric("샤프 비율", sharpe_txt)
            st.write(f"총 거래 {stats['total_trades']}건 (승 {stats['wins']} / 패 {stats['losses']}, "
                     f"승률 {stats['win_rate_pct']:.1f}%) · 손익비 {stats['profit_loss_ratio']:.2f} · "
                     f"연환산 변동성 {stats['ann_vol_pct']:.2f}%")

            if not bt.equity_curve.empty:
                fig, ax = plt.subplots(figsize=(10, 4))
                ax.plot(bt.equity_curve.index, bt.equity_curve.values, linewidth=1.5)
                ax.set_title("Cumulative Equity Curve")
                ax.set_ylabel("Equity ($)")
                ax.grid(alpha=0.3)
                st.pyplot(fig)

st.caption("⚠️ 본 대시보드는 투자 자문이 아니며 참고용입니다. 백테스트는 과거 데이터 기반이며 향후 수익을 보장하지 않습니다.")
