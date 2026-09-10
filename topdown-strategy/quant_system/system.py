"""
TopDownQuantSystem - 통합 엔트리 포인트
====================================================
MarketRegimeDetector -> SectorRotationEngine -> StockScreener -> ExecutionEngine
-> RiskManager 를 하나로 묶어 (1) 일일 시그널 스캔, (2) 3년 백테스트를 모두
제공하는 통합 시스템 클래스.

실행 방법
---------
    python system.py scan        # 오늘자 매매 시그널 스캔
    python system.py backtest    # 3년 백테스트 실행 및 성과 리포트 출력
"""

from __future__ import annotations

import sys

from config import SECTOR_STOCKS, INITIAL_EQUITY
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from sector import SectorRotationEngine
from screener import StockScreener
from execution import ExecutionEngine
from risk import RiskManager
from backtest import TopDownBacktester
from config import BENCHMARK, SECTOR_ETFS


class TopDownQuantSystem:
    """탑다운 4단계 전략의 일일 스캔/백테스트를 총괄하는 파사드 클래스."""

    def __init__(self, capital: float = INITIAL_EQUITY) -> None:
        self.capital = capital
        self.regime_detector = MarketRegimeDetector()
        self.sector_engine = SectorRotationEngine()
        self.screener = StockScreener()
        self.execution = ExecutionEngine()
        self.risk_manager = RiskManager()

    def scan_today(self) -> None:
        """1단계~4단계를 순서대로 실행해 오늘자 매매 시그널을 출력한다."""
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

        print("\n[Module3~4-종목스크리닝 & 진입시그널]")
        found_any = False
        for sector in leaders:
            for ticker in SECTOR_STOCKS.get(sector, []):
                stock_df = fetch_ohlcv(ticker, period="3y")
                if stock_df.empty:
                    continue
                passed = self.screener.screen(stock_df, benchmark_df)
                if not passed:
                    continue

                signal = self.execution.entry_signal(stock_df, regime)
                if not signal:
                    continue

                stop = self.execution.initial_stop(signal["entry_price"], signal["ma20"], signal.get("atr"))
                regime_mult = self.risk_manager.regime_multiplier(regime)
                sizing = self.risk_manager.position_size(self.capital, signal["entry_price"], stop, regime_mult)
                if sizing["shares"] <= 0:
                    continue

                found_any = True
                print(f"  ✅ {ticker:6s} 섹터={sector:5s} [{signal['type']:9s}] "
                      f"진입가={signal['entry_price']:.2f} 손절={stop:.2f} "
                      f"MRS={passed['mrs']:.2f} 수량={sizing['shares']}주 "
                      f"(투입금액={sizing['position_value']:,.0f})")

        if not found_any:
            print("  오늘은 조건을 만족하는 매수 시그널이 없습니다.")

    def run_backtest(self, years: int = 3) -> dict:
        """3년(기본) 백테스트를 실행하고 성과 dict를 반환한다."""
        bt = TopDownBacktester(years=years, initial_equity=self.capital)
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
