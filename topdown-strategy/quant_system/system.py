"""
TopDownQuantSystem - 통합 엔트리 포인트 (v2: 모멘텀 랭킹 + Runner 전략)
====================================================
MarketRegimeDetector -> SectorRotationEngine -> MomentumRanker -> ExecutionEngine
-> RiskManager 를 하나로 묶어 (1) 일일 매수 후보 스캔, (2) 3년 백테스트를 모두
제공하는 통합 시스템 클래스.

v1(AND 하드필터 + 눌림목/돌파 타이밍 대기)에서 v2(Cross-Sectional 모멘텀
랭킹 + 즉시매수 + ATR 손절/분할익절/Runner 추세추종)로 전면 교체됐다.
백테스트 결과 v1 최적조합(CAGR +6.83%) 대비 v2가 CAGR +16.65%로 크게
우수해 프로덕션(일일 스캔/웹 대시보드)에 반영한다.

실행 방법
---------
    python system.py scan        # 오늘자 매수 후보 스캔
    python system.py backtest    # 3년 백테스트 실행 및 성과 리포트 출력
"""

from __future__ import annotations

import sys

from qs_config import (
    SECTOR_STOCKS, INITIAL_EQUITY, BENCHMARK, SECTOR_ETFS,
    TOP_STOCK_COUNT, IDLE_CASH_FALLBACK_ENABLED, FALLBACK_INDEX_TICKER,
)
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from qs_sector import SectorRotationEngine
from screener import MomentumRanker
from execution import ExecutionEngine
from risk import RiskManager
from backtest_v2 import TopDownBacktesterV2

# v2 백테스트에서 검증된 최적 사이징 (STOP_ATR_MULT_V2=2.5 config와 함께 사용)
V2_RISK_PCT = 0.03
V2_POSITION_CAP_PCT = 0.40


class TopDownQuantSystem:
    """탑다운 전략(v2: 모멘텀 랭킹 + Runner)의 일일 스캔/백테스트를 총괄하는 파사드 클래스."""

    def __init__(self, capital: float = INITIAL_EQUITY) -> None:
        self.capital = capital
        self.regime_detector = MarketRegimeDetector()
        self.sector_engine = SectorRotationEngine()
        self.ranker = MomentumRanker(top_n=TOP_STOCK_COUNT)
        self.execution = ExecutionEngine()
        self.risk_manager = RiskManager(risk_pct=V2_RISK_PCT, cap_pct=V2_POSITION_CAP_PCT)

    def scan_today(self) -> None:
        """1단계~4단계를 순서대로 실행해 오늘자 매수 후보를 출력한다."""
        benchmark_df = fetch_ohlcv(BENCHMARK, period="3y")
        snapshot = self.regime_detector.current_regime(benchmark_df)
        regime = snapshot["regime"]
        print(f"[Module1-국면판정] {BENCHMARK} {snapshot['date'].date()} "
              f"Close={snapshot['close']:.2f} MA20={snapshot['ma20']:.2f} MA200={snapshot['ma200']:.2f} "
              f"Slope5d={snapshot['slope5d']:.3f}% => {regime} (Exposure={snapshot['exposure']*100:.0f}%)")

        if regime == "BEAR":
            print("\nBEAR 국면: 신규 매수를 전면 차단하고 현금 100%를 유지합니다.")
            return

        sector_data = {etf: fetch_ohlcv(etf, period="6mo") for etf in SECTOR_ETFS}
        leaders = self.sector_engine.select_leading_sectors(sector_data, regime)
        print(f"\n[Module2-섹터로테이션] 주도 섹터: {leaders}")
        if not leaders:
            print("주도 섹터가 없어 관망합니다.")
            return

        candidates_data = {}
        ticker_sector_map = {}
        for sector in leaders:
            for ticker in SECTOR_STOCKS.get(sector, []):
                df = fetch_ohlcv(ticker, period="3y")
                if not df.empty:
                    candidates_data[ticker] = df
                    ticker_sector_map.setdefault(ticker, sector)

        ranked = self.ranker.rank_candidates(candidates_data, benchmark_df)
        print(f"\n[Module3-모멘텀랭킹] CompositeScore 상위 {TOP_STOCK_COUNT}종목 (전체 {len(ranked)}개 중)")
        if ranked.empty:
            print("랭킹 가능한 종목이 없습니다.")
            return
        print(ranked.head(TOP_STOCK_COUNT)[["mom_121", "roc_63", "roc_21", "near_high_ratio",
                                             "mrs", "composite_score"]].round(3).to_string())

        top_stocks = ranked.head(TOP_STOCK_COUNT)
        print(f"\n[Module4-매수후보 & 리스크관리] (즉시매수, Runner 전략: 손절=진입가-2.5×ATR, "
              f"+10%/+20% 각 30%씩 분할익절 후 잔여 40%는 MA50 이탈까지 추세추종)")
        regime_mult = self.risk_manager.regime_multiplier(regime)
        stock_used = 0.0
        for ticker, row in top_stocks.iterrows():
            df = candidates_data[ticker]
            ind = self.execution.compute_indicators(df)
            atr = ind.iloc[-1]["ATR14"]
            if atr != atr or atr <= 0:  # NaN 체크
                continue
            entry_price = float(df["Close"].iloc[-1])
            stop = self.execution.initial_stop_v2(entry_price, float(atr))
            sizing = self.risk_manager.position_size(self.capital, entry_price, stop, regime_mult)
            if sizing["shares"] <= 0:
                continue
            stock_used += sizing["position_value"]
            sector = ticker_sector_map.get(ticker, "-")
            print(f"  ✅ {ticker:6s} 섹터={sector:5s} 진입가={entry_price:.2f} 손절={stop:.2f} "
                  f"CompositeScore={row['composite_score']:.1f} MRS={row['mrs']:+.1f} "
                  f"수량={sizing['shares']}주 (투입금액={sizing['position_value']:,.0f})")

        if IDLE_CASH_FALLBACK_ENABLED and regime == "BULL":
            idle_estimate = self.capital - stock_used
            if idle_estimate > self.capital * 0.05:
                print(f"\n[유휴자금 폭포수] 상위종목 매수 후 약 {idle_estimate:,.0f} 현금이 남습니다. "
                      f"BULL 국면 동안 현금 비중 0%를 유지하려면 {FALLBACK_INDEX_TICKER}에 배분하고, "
                      f"BEAR로 국면이 바뀌면 전량 청산하세요.")

    def run_backtest(self, years: int = 3) -> dict:
        """3년(기본) 백테스트를 실행하고 성과 dict를 반환한다."""
        bt = TopDownBacktesterV2(years=years, initial_equity=self.capital,
                                  risk_pct=V2_RISK_PCT, position_cap_pct=V2_POSITION_CAP_PCT)
        stats = bt.run()
        bt.print_report()
        return stats


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    system = TopDownQuantSystem()

    if mode == "backtest":
        system.run_backtest(years=3)
    else:
        system.scan_today()
