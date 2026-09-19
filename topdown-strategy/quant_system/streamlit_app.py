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
import importlib
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Streamlit Cloud는 새 커밋을 pull해도 프로세스를 재시작하지 않고 스크립트만 재실행한다.
# 이때 sys.modules에 남아 있는 옛 버전의 로컬 모듈(qs_config 등)이 그대로 쓰이면
# 새로 추가된 이름을 import하다 ImportError가 난다. 소스 파일이 바뀌었으면
# 의존 순서대로 로컬 모듈을 다시 불러온다.
_LOCAL_MODULES = ["qs_config", "data", "regime", "qs_sector", "screener", "execution",
                  "risk", "github_store", "journal", "backtest_v2", "screening"]
_src_sig = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in Path(__file__).parent.glob("*.py")))
if getattr(sys, "_qs_src_sig", None) != _src_sig:
    for _name in _LOCAL_MODULES:
        if _name in sys.modules:
            importlib.reload(sys.modules[_name])
    sys._qs_src_sig = _src_sig

from qs_config import (
    BENCHMARK, SECTOR_ETFS, DEFENSIVE_ETFS, OFFENSIVE_ETFS, SECTOR_STOCKS,
    SIDEWAYS_MODE_DEFENSIVE, SIDEWAYS_MODE_HYBRID, DEFAULT_SIDEWAYS_MODE,
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
from journal import (
    load_trades, load_signals, preview_entry, add_trade, delete_trade,
    archive_trade, restore_trade,
    load_exits, add_exit, delete_exit,
    scan_open_trades, confirm_signal, get_dashboard_metrics,
    PHASE_LABELS, SIGNAL_LABELS,
)
import github_store

st.set_page_config(page_title="TopDown Quant System", page_icon="📊", layout="wide")

# 실측 결과 기본 테마 글자 크기가 14~44px까지 7단계로 제각각이라(특히 st.metric 숫자가
# 36px로 라벨 14px 옆에 툭 튀어나와 보임) 5단계의 일관된 스케일로 재조정한다.
#   26 타이틀 > 22 섹션헤더/지표값 > 18 카드제목 > 15 본문/라벨/데이터표 > 13 캡션
st.markdown(
    """
    <style>
    h1 { font-size: 26px !important; }
    [data-testid="stHeadingWithActionElements"] h2 { font-size: 22px !important; }
    [data-testid="stSidebarContent"] h2 { font-size: 16px !important; }
    h3 { font-size: 18px !important; }
    [data-testid="stMetricValue"] { font-size: 22px !important; font-weight: 600 !important; }
    [data-testid="stMetricLabel"] { font-size: 13px !important; }
    [data-testid="stCaptionContainer"] p, .stCaption p { font-size: 13px !important; }
    [data-testid="stMarkdownContainer"] p { font-size: 15px !important; }
    [data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span { font-size: 15px !important; }
    [data-testid="stDataFrame"] [role="gridcell"],
    [data-testid="stDataFrame"] [role="columnheader"] { font-size: 14px !important; }
    [data-testid="stAlertContentInfo"] p,
    [data-testid="stAlertContentWarning"] p,
    [data-testid="stAlertContentSuccess"] p,
    [data-testid="stAlertContentError"] p { font-size: 15px !important; }
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


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_preview(ticker: str, entry_date, entry_price: float):
    return preview_entry(ticker, entry_date, entry_price)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_dashboard_metrics(ticker: str, entry_price: float, initial_stop: float,
                            current_stop: float, phase: str):
    return get_dashboard_metrics({
        "ticker": ticker, "entry_price": entry_price,
        "initial_stop": initial_stop, "current_stop": current_stop, "phase": phase,
    })


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%" if v is not None else "N/A"


def _has_text(v) -> bool:
    """빈 메모(NaN, 빈 문자열)를 걸러낸다. pandas가 빈 문자열을 NaN(float)으로 읽어
    '· nan'처럼 잘못 표시되는 것을 방지한다."""
    return pd.notna(v) and str(v).strip() != ""


def _stockanalysis_url(ticker: str) -> str:
    return f"https://stockanalysis.com/stocks/{ticker}/"


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


def _render_history_card(hrow, exits_df, key_prefix: str, compact: bool = False) -> None:
    """History 카드 하나를 렌더링한다 (사이드바=compact, 메인화면=상세).

    진입 정보(종목/진입일/진입가/진입수량/투자금액)는 항상 표시하고, 우측 "➕ 매도"
    버튼을 누르면 일시/매도가/매도수량/메모 입력폼이 나타나 분할 매도 기록을
    여러 건 남길 수 있다.
    """
    trade_id = hrow["id"]
    ticker = hrow["ticker"]
    invested = hrow["entry_price"] * hrow["initial_shares"]
    trade_exits = exits_df[exits_df["trade_id"] == trade_id] if not exits_df.empty else exits_df
    sold_shares = int(trade_exits["shares"].sum()) if not trade_exits.empty else 0
    remaining = int(hrow["initial_shares"]) - sold_shares
    show_key = f"{key_prefix}_show_exit_form_{trade_id}"
    if show_key not in st.session_state:
        st.session_state[show_key] = False

    with st.container(border=True):
        hcol1, hcol2 = st.columns([5, 1])
        hcol1.markdown(f"**[{ticker}]({_stockanalysis_url(ticker)})** · {hrow['entry_date']} 진입" +
                        (f" · _{hrow['memo']}_" if _has_text(hrow.get("memo")) else ""))
        if hcol2.button("➕ 매도", key=f"{key_prefix}_toggle_exit_{trade_id}", use_container_width=True):
            st.session_state[show_key] = not st.session_state[show_key]
            st.rerun()

        if compact:
            st.caption(f"진입가 {hrow['entry_price']:.2f} · 진입수량 {int(hrow['initial_shares']):,}주 · "
                       f"투자금액 ${invested:,.0f} · 잔여 {remaining:,}주")
        else:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("진입가", f"{hrow['entry_price']:.2f}")
            m2.metric("진입 수량", f"{int(hrow['initial_shares']):,}주")
            m3.metric("투자 금액", f"${invested:,.0f}")
            m4.metric("잔여 수량", f"{remaining:,}주")

        if st.session_state[show_key]:
            with st.container(border=True):
                ex_date = st.date_input("일시", value=datetime.now().date(), key=f"{key_prefix}_exit_date_{trade_id}")
                ex_price = st.number_input("매도가", min_value=0.0, step=0.01, format="%.2f",
                                            key=f"{key_prefix}_exit_price_{trade_id}")
                ex_shares = st.number_input("매도수량", min_value=0, step=1,
                                             key=f"{key_prefix}_exit_shares_{trade_id}")
                ex_memo = st.text_input("메모", placeholder="예: 1차 분할매도",
                                         key=f"{key_prefix}_exit_memo_{trade_id}")
                if st.button("💾 매도 기록 저장", key=f"{key_prefix}_exit_save_{trade_id}", use_container_width=True):
                    if ex_price > 0 and ex_shares > 0:
                        add_exit(trade_id, ex_date, ex_price, int(ex_shares), ex_memo)
                        st.session_state[show_key] = False
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.warning("매도가와 매도수량을 올바르게 입력해 주세요.")

        if not trade_exits.empty:
            st.caption("매도 기록")
            for _, ex in trade_exits.sort_values("date").iterrows():
                pct = (ex["price"] / hrow["entry_price"] - 1) * 100 if hrow["entry_price"] else None
                xcol1, xcol2 = st.columns([5, 1])
                xcol1.write(f"{ex['date']} · {ex['price']:.2f} · {int(ex['shares']):,}주 · {fmt_pct(pct)}" +
                            (f" · {ex['memo']}" if _has_text(ex.get("memo")) else ""))
                if xcol2.button("🗑️", key=f"{key_prefix}_exit_del_{ex['id']}", use_container_width=True):
                    delete_exit(ex["id"])
                    st.cache_data.clear()
                    st.rerun()

        rc1, rc2 = st.columns(2)
        if rc1.button("♻️ 복원", key=f"{key_prefix}_restore_{trade_id}", use_container_width=True):
            restore_trade(trade_id)
            st.cache_data.clear()
            st.rerun()
        if rc2.button("🗑️ 완전 삭제", key=f"{key_prefix}_harddel_{trade_id}", use_container_width=True):
            delete_trade(trade_id)
            st.cache_data.clear()
            st.rerun()


# ------------------------------------------------------------------
# 사이드바
# ------------------------------------------------------------------
usd_krw_rate = load_usd_krw_rate()

try:
    current_regime = load_regime(load_benchmark())["regime"]
except Exception:
    current_regime = None  # 조회 실패 시 옵션을 잠그지 않는다


def _on_krw_change():
    raw = _parse_comma(st.session_state["capital_krw_text"])
    st.session_state["capital_krw_text"] = _fmt_comma(raw)
    if usd_krw_rate:
        st.session_state["capital_usd_text"] = _fmt_comma(round(raw / usd_krw_rate, 2))


def _on_usd_change():
    raw = _parse_comma(st.session_state["capital_usd_text"])
    st.session_state["capital_usd_text"] = _fmt_comma(raw)


with st.sidebar:
    tab_journal, tab_screening = st.tabs(["📓 매매일지", "🔍 스크리닝"])

    # ================================================================
    # 탭 1: 매매일지 (Trade Journal) — 룰 기반 매도 시그널 추적
    # ================================================================
    with tab_journal:
        if github_store.is_configured():
            st.caption("✅ GitHub 저장소와 동기화 중 (스케줄러와 상태 공유)")
        else:
            st.caption("⚠️ GitHub 미연동 — 로컬에만 저장되며 정기 스캔 스케줄러와 공유되지 않습니다.")

        with st.expander("➕ 신규 포지션 등록"):
            nj_ticker = st.text_input("종목 티커", placeholder="예: AAPL", key="nj_ticker")
            nj_date = st.date_input("매매일", value=datetime.now().date(), key="nj_date")
            nj_price = st.number_input("매수가", min_value=0.0, step=0.01, format="%.2f", key="nj_price")
            nj_shares = st.number_input("수량", min_value=0, step=1, key="nj_shares")
            nj_memo = st.text_input("메모 (선택)", placeholder="예: 눌림목 진입", key="nj_memo")

            preview = None
            if nj_ticker.strip() and nj_price > 0:
                preview = load_preview(nj_ticker.strip().upper(), nj_date, nj_price)
                if "error" in preview:
                    st.warning(preview["error"])
                else:
                    st.caption(f"ATR14: {preview['atr14']:.2f} · 손절가: {preview['initial_stop']:.2f} · "
                               f"TP1: {preview['tp1_price']:.2f} · TP2: {preview['tp2_price']:.2f}")

            if st.button("➕ 매매일지에 추가", type="primary", use_container_width=True, key="nj_submit"):
                if not nj_ticker.strip() or nj_price <= 0 or nj_shares <= 0:
                    st.warning("종목 티커, 매수가, 수량을 올바르게 입력해 주세요.")
                elif preview is not None and "error" in preview:
                    pass  # 위 미리보기에 이미 동일한 오류가 표시되어 있으므로 중복 표시하지 않음
                else:
                    result = add_trade(nj_ticker.strip().upper(), nj_date, nj_price, int(nj_shares), nj_memo)
                    if "error" in result:
                        st.error(result["error"])
                    else:
                        st.cache_data.clear()
                        st.rerun()

        trades_df = load_trades()
        signals_df = load_signals()
        exits_df = load_exits()
        active_trades = trades_df[~trades_df["archived"]] if not trades_df.empty else trades_df
        open_trades = active_trades[active_trades["status"] == "OPEN"] if not active_trades.empty else active_trades
        closed_trades = active_trades[active_trades["status"] == "CLOSED"] if not active_trades.empty else active_trades
        archived_trades = trades_df[trades_df["archived"]] if not trades_df.empty else trades_df

        with st.expander(f"📜 History ({len(archived_trades)}건)"):
            if archived_trades.empty:
                st.caption("기록/삭제로 옮긴 포지션이 여기에 표시됩니다.")
            else:
                for _, hrow in archived_trades.iterrows():
                    _render_history_card(hrow, exits_df, key_prefix="side", compact=True)

        if st.button("🔄 시그널 확인", use_container_width=True):
            with st.spinner("전체 OPEN 포지션 스캔 중..."):
                new_signals = scan_open_trades()
            st.cache_data.clear()
            if new_signals:
                st.success(f"신규 시그널 {len(new_signals)}건 발생")
            else:
                st.info("신규 시그널 없음")
            st.rerun()

        if trades_df.empty:
            st.caption("아직 등록된 포지션이 없습니다.")

        for _, trow in open_trades.iterrows():
            trade_id = trow["id"]
            ticker = trow["ticker"]
            pending = signals_df[(signals_df["trade_id"] == trade_id) & (~signals_df["confirmed"])] \
                if not signals_df.empty else signals_df
            bell = "🔔" if len(pending) > 0 else ""
            phase_emoji = {"STAGE_0": "🟡", "STAGE_1": "🔵", "STAGE_2": "🟢"}.get(trow["phase"], "")
            label = f"{phase_emoji}{bell} {ticker} · {trow['entry_date']}"

            with st.expander(label):
                st.caption(f"매수가 {trow['entry_price']:.2f} · 최초 {int(trow['initial_shares']):,}주 · "
                           f"잔여 {int(trow['remaining_shares']):,}주" +
                           (f" · {trow['memo']}" if _has_text(trow.get("memo")) else ""))

                if st.button("📁 기록/삭제", key=f"del_{trade_id}", use_container_width=True):
                    archive_trade(trade_id)
                    st.cache_data.clear()
                    st.rerun()

                if len(pending) > 0:
                    for _, sig in pending.iterrows():
                        st.warning(f"🔔 **{SIGNAL_LABELS.get(sig['type'], sig['type'])}** "
                                   f"({sig['date']}, {sig['price']:.2f}, {int(sig['suggested_shares']):,}주)")
                        if st.button("✅ 체결 확인", key=f"confirm_{sig['id']}", use_container_width=True):
                            confirm_signal(sig["id"])
                            st.cache_data.clear()
                            st.rerun()

                metrics = load_dashboard_metrics(ticker, float(trow["entry_price"]), float(trow["initial_stop"]),
                                                  float(trow["current_stop"]), trow["phase"])
                if "error" in metrics:
                    st.error(metrics["error"])
                else:
                    st.info(f"**{PHASE_LABELS.get(trow['phase'], trow['phase'])}**")
                    st.write(f"현재가 **{metrics['last_close']:.2f}** · R배수 {metrics['r_multiple']:+.2f}R")
                    st.write(f"손절가 {trow['current_stop']:.2f} (거리 {fmt_pct(metrics['dist_to_stop_pct'])})")
                    if metrics["target_price"] is not None:
                        st.write(f"{metrics['target_label']} 목표가 {metrics['target_price']:.2f} "
                                 f"(거리 {fmt_pct(metrics['dist_to_target_pct'])})")
                    else:
                        st.write("Runner 모드 — MA50 이탈 시 잔여 물량 청산")

        if not closed_trades.empty:
            with st.expander(f"⚫ 종료된 포지션 ({len(closed_trades)}건)"):
                for _, trow in closed_trades.iterrows():
                    st.caption(f"{trow['ticker']} · {trow['entry_date']} 진입 {trow['entry_price']:.2f} · "
                               f"{PHASE_LABELS.get(trow['phase'], trow['phase'])}")
                    if st.button("📁 기록/삭제", key=f"del_closed_{trow['id']}"):
                        archive_trade(trow["id"])
                        st.cache_data.clear()
                        st.rerun()

    # ================================================================
    # 탭 2: 스크리닝 (자본금 설정)
    # ================================================================
    with tab_screening:
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

        _mode_labels = {
            SIDEWAYS_MODE_HYBRID: "혼합형 (기본)",
            SIDEWAYS_MODE_DEFENSIVE: "방어형",
        }
        sideways_mode = st.radio(
            "SIDEWAYS 국면 섹터 선정 방식",
            options=list(_mode_labels.keys()),
            index=list(_mode_labels.keys()).index(DEFAULT_SIDEWAYS_MODE),
            format_func=lambda m: _mode_labels[m],
            key="sideways_mode",
            disabled=current_regime in ("BULL", "BEAR"),
            help="SIDEWAYS(횡보) 국면에서 주도 섹터를 고르는 방식입니다. "
                 "혼합형은 방어 섹터 1개 + 공격 섹터 1개, 방어형은 방어 섹터(XLU/XLP/XLV/XLF)에서만 고릅니다. "
                 "10년 백테스트: 혼합형 CAGR +12.9%·MDD -21.3%·Sharpe 1.15 / 방어형 CAGR +10.2%·MDD -19.3%·Sharpe 1.01.",
        )
        if current_regime == "BULL":
            st.info("🔒 현재 시장 국면이 **BULL**이라 이 옵션은 적용되지 않습니다. "
                    "BULL에서는 11개 전체 섹터 중 순위 상위 2개를 선정합니다. "
                    "SIDEWAYS 국면으로 전환되면 선택할 수 있습니다.")
        elif current_regime == "BEAR":
            st.info("🔒 현재 시장 국면이 **BEAR**라 이 옵션은 적용되지 않습니다 (신규 매수 차단). "
                    "SIDEWAYS 국면으로 전환되면 선택할 수 있습니다.")
        elif current_regime == "SIDEWAYS":
            st.caption("〰️ 현재 SIDEWAYS 국면 — 선택한 방식이 주도 섹터 선정에 적용됩니다.")

        if st.button("🔄 시그널 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.divider()
        st.caption(f"조회 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        st.caption("데이터: yfinance (지연 시세) · 캐시 15분")

st.title("📊 TopDownQuantSystem — 모멘텀 랭킹 + Runner 대시보드")
st.caption("MarketRegimeDetector → SectorRotationEngine → MomentumRanker → ExecutionEngine/RiskManager "
           "순서로 실시간 매수 후보를 계산합니다. (12-1M 모멘텀 · 유휴자금 폭포수배분 · ATR손절/분할익절/Runner추세추종)")

# ================================================================
# 매매일지 현황 (사이드바에서 등록한 포지션의 매수/매도 정보를 메인 화면에 표시)
# ================================================================
st.header("📓 매매일지 현황", divider="gray")
st.caption("사이드바 '매매일지' 탭에서 등록/조회한 포지션의 매수·매도 정보입니다. "
           "🔔 표시는 확인 대기 중인 신규 시그널입니다.")

if trades_df.empty:
    st.info("아직 등록된 포지션이 없습니다. 좌측 사이드바 '📓 매매일지' 탭에서 종목을 등록해 보세요.")
else:
    tab_open, tab_closed, tab_history = st.tabs([
        f"🟢 진행중 ({len(open_trades)})",
        f"⚫ 종료 ({len(closed_trades)})",
        f"📜 History ({len(archived_trades)})",
    ])

    with tab_open:
        if open_trades.empty:
            st.info("현재 보유(OPEN) 포지션이 없습니다.")
        for _, trow in open_trades.iterrows():
            trade_id = trow["id"]
            ticker = trow["ticker"]
            pending = signals_df[(signals_df["trade_id"] == trade_id) & (~signals_df["confirmed"])] \
                if not signals_df.empty else signals_df

            with st.container(border=True):
                phase_emoji = {"STAGE_0": "🟡", "STAGE_1": "🔵", "STAGE_2": "🟢"}.get(trow["phase"], "")
                bell = " 🔔" if len(pending) > 0 else ""
                hcol1, hcol2 = st.columns([6, 1])
                hcol1.markdown(f"### {phase_emoji} [{ticker}]({_stockanalysis_url(ticker)}) · "
                                f"{trow['entry_date']} 진입{bell}" +
                                (f" · _{trow['memo']}_" if _has_text(trow.get("memo")) else ""))
                if hcol2.button("📁 기록/삭제", key=f"main_del_{trade_id}"):
                    archive_trade(trade_id)
                    st.cache_data.clear()
                    st.rerun()

                st.caption(PHASE_LABELS.get(trow["phase"], trow["phase"]))

                metrics = load_dashboard_metrics(ticker, float(trow["entry_price"]), float(trow["initial_stop"]),
                                                  float(trow["current_stop"]), trow["phase"])
                m1, m2, m3, m4, m5, m6 = st.columns(6)
                m1.metric("매수가", f"{trow['entry_price']:.2f}")
                m2.metric("현재가", f"{metrics['last_close']:.2f}" if "error" not in metrics else "N/A")
                m3.metric("손절가", f"{trow['current_stop']:.2f}",
                          fmt_pct(metrics.get("dist_to_stop_pct")) if "error" not in metrics else None)
                if "error" not in metrics and metrics["target_price"] is not None:
                    m4.metric(f"{metrics['target_label']} 목표가", f"{metrics['target_price']:.2f}",
                              fmt_pct(metrics["dist_to_target_pct"]))
                else:
                    m4.metric("목표가", "Runner(MA50)")
                m5.metric("잔여 수량", f"{int(trow['remaining_shares']):,} / {int(trow['initial_shares']):,}주")
                m6.metric("R배수", f"{metrics['r_multiple']:+.2f}R" if "error" not in metrics else "N/A")

                if "error" in metrics:
                    st.error(metrics["error"])

                if len(pending) > 0:
                    for _, sig in pending.iterrows():
                        scol1, scol2 = st.columns([4, 1])
                        scol1.warning(f"🔔 **{SIGNAL_LABELS.get(sig['type'], sig['type'])}** — "
                                      f"{sig['date']} 종가 {sig['price']:.2f}, 매도 제안 수량 {int(sig['suggested_shares']):,}주")
                        if scol2.button("✅ 체결 확인", key=f"main_confirm_{sig['id']}", use_container_width=True):
                            confirm_signal(sig["id"])
                            st.cache_data.clear()
                            st.rerun()

    with tab_closed:
        if closed_trades.empty:
            st.info("종료된 포지션이 없습니다.")
        for _, trow in closed_trades.iterrows():
            ccol1, ccol2 = st.columns([5, 1])
            ccol1.write(f"{trow['ticker']} · {trow['entry_date']} 진입 {trow['entry_price']:.2f} · "
                        f"{PHASE_LABELS.get(trow['phase'], trow['phase'])}" +
                        (f" · {trow['memo']}" if _has_text(trow.get("memo")) else ""))
            if ccol2.button("📁 기록/삭제", key=f"main_del_closed_{trow['id']}", use_container_width=True):
                archive_trade(trow["id"])
                st.cache_data.clear()
                st.rerun()

    with tab_history:
        st.caption("'기록/삭제'로 옮긴 포지션입니다. 시그널 추적 대상에서 제외됩니다. "
                   "➕ 매도 버튼으로 분할 매도를 포함한 매도 기록을 여러 건 남길 수 있습니다.")
        if archived_trades.empty:
            st.info("History에 저장된 포지션이 없습니다.")
        for _, trow in archived_trades.iterrows():
            _render_history_card(trow, exits_df, key_prefix="main", compact=False)

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
    if sideways_mode == SIDEWAYS_MODE_HYBRID:
        sideways_label = "혼합형"
        sideways_desc = (f"방어적 섹터({', '.join(DEFENSIVE_ETFS)}) 중 Score_Rank 1위 + "
                         f"공격적 섹터({', '.join(OFFENSIVE_ETFS)}) 중 Score_Rank 1위")
    else:
        sideways_label = "방어형"
        sideways_desc = f"방어적 섹터({', '.join(DEFENSIVE_ETFS)}) 4개 중 Score_Rank 상위 {TOP_SECTOR_COUNT}개"
    with st.expander("📋 조건 상세보기"):
        st.markdown(f"""
- **BULL**: GICS 11개 섹터 ETF 전체 중 Score_Rank 상위 {TOP_SECTOR_COUNT}개
- **SIDEWAYS** ({sideways_label}): {sideways_desc}
- **BEAR**: 섹터 미선정 (Cash 100%)
- Score_Rank = 0.3 × Rank(1주 수익률) + 0.7 × Rank(1개월 수익률) — 낮을수록 우수
""")

    with st.spinner("섹터 ETF 스코어 계산 중..."):
        scored, sector_price_data = load_sector_scores()
        leaders = SectorRotationEngine(sideways_mode=sideways_mode).select_leading_sectors(sector_price_data, regime)

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

    if regime == "SIDEWAYS" and sideways_mode == SIDEWAYS_MODE_HYBRID:
        # 혼합형: 방어 그룹과 공격 그룹 각각에서 1위를 뽑으므로, 선정에 쓰인 그룹별 순위표를 나눠 보여준다.
        defensive_data = {t: sector_price_data[t] for t in DEFENSIVE_ETFS if t in sector_price_data}
        offensive_data = {t: sector_price_data[t] for t in OFFENSIVE_ETFS if t in sector_price_data}
        st.caption("〰️ 현재 SIDEWAYS 국면 · 혼합형: 방어 그룹 1위 + 공격 그룹 1위를 주도 섹터로 선정합니다. "
                   "아래는 그룹별 실제 선정용 순위표입니다.")
        st.markdown("**방어 그룹** (" + ", ".join(DEFENSIVE_ETFS) + ")")
        st.dataframe(_format_score_table(sector_engine.score_sectors(defensive_data)), use_container_width=True)
        st.markdown("**공격 그룹** (" + ", ".join(OFFENSIVE_ETFS) + ")")
        st.dataframe(_format_score_table(sector_engine.score_sectors(offensive_data)), use_container_width=True)
        with st.expander("전체 11개 섹터 기준 순위표 보기 (참고용 — SIDEWAYS 선정에는 미사용)"):
            st.dataframe(_format_score_table(scored), use_container_width=True)
    elif regime == "SIDEWAYS":
        # 방어형: SIDEWAYS는 방어적 섹터 4개 안에서만 다시 순위를 매겨 선정하므로,
        # 전체 11개 기준 표를 그대로 보여주면 "1등인데 왜 안 뽑혔지?"로 오해하기 쉽다.
        # 실제 선정에 쓰인 것과 동일한(방어적 섹터로 제한된) 순위표를 보여준다.
        defensive_data = {t: sector_price_data[t] for t in DEFENSIVE_ETFS if t in sector_price_data}
        scored_defensive = sector_engine.score_sectors(defensive_data)
        st.caption(f"⚠️ 현재 SIDEWAYS 국면 · 방어형이라 방어적 섹터({', '.join(DEFENSIVE_ETFS)}) 4개 안에서만 "
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
                    st.markdown(f"**[{ticker}]({_stockanalysis_url(ticker)})** · "
                                f"`{ticker_sector_map.get(ticker, '-')}` · CompositeScore {row['composite_score']:.1f}")
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
                st.markdown(f"### ✅ [{ticker}]({_stockanalysis_url(ticker)}) · "
                            f"`{ticker_sector_map.get(ticker, '-')}` · CompositeScore {row['composite_score']:.1f}")
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
           "SIDEWAYS 섹터 선정 방식은 사이드바 '스크리닝' 탭에서 선택합니다. "
           "10년 검증(2016년 9월부터 2026년 9월까지, 종목풀 생존편향 있음): "
           "혼합형 CAGR +12.9% · MDD -21.3% · Sharpe 1.15 / 방어형 CAGR +10.2% · MDD -19.3% · Sharpe 1.01 "
           "(참고: SPY 보유 CAGR +15.4% · MDD -33.7% · Sharpe 0.89). "
           "구간에 따라 성과 편차가 크니(3년 롤링 평균 CAGR 혼합형 +8.2%, 방어형 +5.8%) 상대 비교용으로 참고하세요.")

with st.expander("▶ 백테스트 실행하기"):
    bt_years = st.slider("백테스트 기간(년)", min_value=1, max_value=5, value=BACKTEST_YEARS)
    run_clicked = st.button("백테스트 실행", type="primary")

    if run_clicked:
        from backtest_v2 import TopDownBacktesterV2

        with st.spinner(f"최근 {bt_years}년(+워밍업 2년) 데이터 다운로드 및 백테스트 실행 중... 잠시만 기다려주세요."):
            try:
                bt = TopDownBacktesterV2(years=bt_years, initial_equity=capital,
                                          risk_pct=V2_RISK_PCT, position_cap_pct=V2_POSITION_CAP_PCT,
                                          sizing_mode="composite_score",
                                          sideways_mode=sideways_mode)
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
