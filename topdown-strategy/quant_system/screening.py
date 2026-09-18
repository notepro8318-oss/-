"""
[Module 6] Screening Snapshot — 스크리닝 결과 스냅샷/비교
====================================================
Module1~4(국면판정 -> 섹터로테이션 -> 모멘텀랭킹 -> 매수후보)를 실행해 오늘의
매수 후보 스냅샷(종목/진입가/수량/비중)을 만들고, 직전 실행 때 저장해 둔
스냅샷과 비교해 변경 사항을 판정한다. 미국 장마감 직후 스케줄러
(screening_notify.py)가 이 모듈로 스크리닝을 재현하고 Telegram 알림을 보낸다.

스냅샷은 매매일지와 동일하게 GitHub Contents API(github_store.py)에 JSON으로
저장되어, 로컬 실행(Streamlit)과 스케줄러(GitHub Actions) 어느 쪽에서 실행되든
"직전 스크리닝 결과"를 공유한다.
"""

from __future__ import annotations

import json
import os

from qs_config import (
    BENCHMARK, SECTOR_ETFS, SECTOR_STOCKS, TOP_STOCK_COUNT, INITIAL_EQUITY,
)
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from qs_sector import SectorRotationEngine
from screener import MomentumRanker
from execution import ExecutionEngine
from risk import RiskManager
import github_store

SNAPSHOT_REPO_PATH = "topdown-strategy/quant_system/screening_snapshot.json"
SNAPSHOT_LOCAL_PATH = os.path.join(os.path.dirname(__file__), "screening_snapshot.json")

# v2 백테스트에서 검증된 최적 사이징 (streamlit_app.py/system.py와 동일)
V2_RISK_PCT = 0.03
V2_POSITION_CAP_PCT = 0.40


def compute_screening_snapshot(capital: float = INITIAL_EQUITY) -> dict:
    """Module1~4를 실행해 오늘의 국면/주도섹터/매수후보 스냅샷 dict를 만든다."""
    regime_detector = MarketRegimeDetector()
    sector_engine = SectorRotationEngine()
    ranker = MomentumRanker(top_n=TOP_STOCK_COUNT)
    execution = ExecutionEngine()
    risk_manager = RiskManager(risk_pct=V2_RISK_PCT, cap_pct=V2_POSITION_CAP_PCT)

    bench_df = fetch_ohlcv(BENCHMARK, period="3y")
    snap_regime = regime_detector.current_regime(bench_df)
    regime = snap_regime["regime"]

    result = {
        "date": str(snap_regime["date"].date()),
        "regime": regime,
        "leaders": [],
        "candidates": [],
    }
    if regime == "BEAR":
        return result

    sector_data = {etf: fetch_ohlcv(etf, period="6mo") for etf in SECTOR_ETFS}
    leaders = sector_engine.select_leading_sectors(sector_data, regime)
    result["leaders"] = leaders
    if not leaders:
        return result

    candidates_data: dict = {}
    ticker_sector_map: dict = {}
    for sector in leaders:
        for ticker in SECTOR_STOCKS.get(sector, []):
            df = fetch_ohlcv(ticker, period="3y")
            if not df.empty:
                candidates_data[ticker] = df
                ticker_sector_map.setdefault(ticker, sector)

    ranked = ranker.rank_candidates(candidates_data, bench_df)
    if ranked.empty:
        return result
    top_stocks = ranked.head(TOP_STOCK_COUNT)

    regime_mult = risk_manager.regime_multiplier(regime)
    inv_scores = {t: 1.0 / s for t, s in top_stocks["composite_score"].items() if s and s > 0}
    total_inv = sum(inv_scores.values())

    candidates = []
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
        candidates.append({
            "ticker": ticker,
            "sector": ticker_sector_map.get(ticker, "-"),
            "entry_price": round(entry_price, 2),
            "stop": round(stop, 2),
            "weight_pct": round(weight * 100, 1),
            "shares": shares,
        })
    result["candidates"] = candidates
    return result


def load_snapshot() -> dict | None:
    """직전 저장된 스크리닝 스냅샷을 불러온다. 없으면 None."""
    if github_store.is_configured():
        content, _ = github_store.get_file(SNAPSHOT_REPO_PATH)
        if content is None:
            return None
        return json.loads(content)
    if os.path.exists(SNAPSHOT_LOCAL_PATH):
        with open(SNAPSHOT_LOCAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_snapshot(snapshot: dict, message: str = "Update screening snapshot") -> None:
    content = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if github_store.is_configured():
        _, sha = github_store.get_file(SNAPSHOT_REPO_PATH)
        github_store.put_file(SNAPSHOT_REPO_PATH, content, message, sha=sha)
    else:
        with open(SNAPSHOT_LOCAL_PATH, "w", encoding="utf-8") as f:
            f.write(content)


def diff_snapshots(old: dict | None, new: dict) -> list[str]:
    """직전 스냅샷과 비교해 사람이 읽을 수 있는 변경 사항 목록을 만든다."""
    changes: list[str] = []

    old_regime = old.get("regime") if old else None
    if old_regime != new["regime"]:
        changes.append(f"국면 변경: {old_regime or '(없음)'} → {new['regime']}")

    old_candidates = {c["ticker"]: c for c in (old.get("candidates", []) if old else [])}
    new_candidates = {c["ticker"]: c for c in new["candidates"]}

    for ticker, c in new_candidates.items():
        if ticker not in old_candidates:
            changes.append(f"➕ 신규 편입: {ticker} (진입가 {c['entry_price']:.2f}, "
                            f"{c['shares']}주, 비중 {c['weight_pct']}%)")
        else:
            o = old_candidates[ticker]
            diffs = []
            if abs(o["entry_price"] - c["entry_price"]) > 0.01:
                diffs.append(f"진입가 {o['entry_price']:.2f}→{c['entry_price']:.2f}")
            if o["shares"] != c["shares"]:
                diffs.append(f"수량 {o['shares']}→{c['shares']}주")
            if abs(o["weight_pct"] - c["weight_pct"]) > 0.05:
                diffs.append(f"비중 {o['weight_pct']}%→{c['weight_pct']}%")
            if diffs:
                changes.append(f"🔄 {ticker}: " + ", ".join(diffs))

    for ticker in old_candidates:
        if ticker not in new_candidates:
            changes.append(f"➖ 편출: {ticker}")

    return changes
