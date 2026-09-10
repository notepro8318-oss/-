"""
[Module 5] TopDownBacktester
====================================================
MarketRegimeDetector -> SectorRotationEngine -> StockScreener -> ExecutionEngine
-> RiskManager 를 일 단위로 엮어 슬리피지/수수료(기본 10bp)를 반영한
종합 백테스트 시뮬레이터.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import (
    SECTOR_ETFS, SECTOR_STOCKS, BENCHMARK, COST_BPS, INITIAL_EQUITY,
    MAX_POSITIONS_BULL, STOCK_MA_LONG, LOOKBACK_52W, MRS_LOOKBACK,
)
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from sector import SectorRotationEngine
from screener import StockScreener
from execution import ExecutionEngine
from risk import RiskManager


@dataclass
class Position:
    ticker: str
    sector: str
    shares: int
    entry_price: float
    entry_date: pd.Timestamp
    stop: float
    peak: float
    entry_type: str
    partial_taken: bool = False


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    reason: str


class TopDownBacktester:
    """탑다운 4단계 전략 전체를 일 단위로 시뮬레이션하는 백테스터."""

    def __init__(self, years: int = 3, initial_equity: float = INITIAL_EQUITY,
                 cost_bps: float = COST_BPS, max_positions: int = MAX_POSITIONS_BULL) -> None:
        self.years = years
        self.initial_equity = initial_equity
        self.cost_rate = cost_bps / 10_000.0
        self.max_positions = max_positions

        self.regime_detector = MarketRegimeDetector()
        self.sector_engine = SectorRotationEngine()
        self.screener = StockScreener()
        self.execution = ExecutionEngine()
        self.risk_manager = RiskManager()

        self.equity_curve: pd.Series = pd.Series(dtype=float)
        self.trades: list[Trade] = []

    # ---------------- 데이터 준비 ----------------
    def _load_universe(self) -> dict[str, pd.DataFrame]:
        warmup_years = self.years + 2  # MA200/52주/MRS 워밍업
        period = f"{warmup_years}y"

        universe = {BENCHMARK: fetch_ohlcv(BENCHMARK, period=period)}
        for etf in SECTOR_ETFS:
            universe[etf] = fetch_ohlcv(etf, period=period)

        stock_sector_map: dict[str, str] = {}
        for etf, tickers in SECTOR_STOCKS.items():
            for t in tickers:
                stock_sector_map.setdefault(t, etf)

        for ticker in stock_sector_map:
            df = fetch_ohlcv(ticker, period=period)
            if len(df) >= STOCK_MA_LONG:
                universe[ticker] = df

        self.stock_sector_map = {t: s for t, s in stock_sector_map.items() if t in universe}
        return universe

    def _weekly_rebalance_dates(self, index: pd.DatetimeIndex) -> list[pd.Timestamp]:
        """각 ISO 주(week)의 마지막 거래일 목록 (주 1회 리밸런싱 시점)."""
        s = pd.Series(index, index=index)
        return s.groupby([index.isocalendar().year, index.isocalendar().week]).max().tolist()

    # ---------------- 실행 ----------------
    def run(self) -> dict:
        """백테스트를 실행하고 성과 dict를 반환한다."""
        universe = self._load_universe()
        benchmark_df = universe[BENCHMARK]

        regime_df = self.regime_detector.classify(benchmark_df)

        warmup = max(STOCK_MA_LONG, LOOKBACK_52W, MRS_LOOKBACK) + 5
        usable_index = benchmark_df.index[warmup:]

        target_days = self.years * 252
        common_index = usable_index[-target_days:] if len(usable_index) > target_days else usable_index

        rebalance_dates = set(self._weekly_rebalance_dates(benchmark_df.index))

        sector_data = {etf: universe[etf] for etf in SECTOR_ETFS if etf in universe}
        active_leaders: list[str] = []

        cash = self.initial_equity
        positions: dict[str, Position] = {}
        equity_history = []
        dates_history = []

        for current_date in common_index:
            regime_row = regime_df.loc[current_date] if current_date in regime_df.index else None
            if regime_row is None:
                continue
            regime = regime_row["Regime"]
            regime_mult = self.risk_manager.regime_multiplier(regime)

            # ---- 주간 섹터 리밸런싱 ----
            if current_date in rebalance_dates:
                sliced_sectors = {t: df.loc[:current_date] for t, df in sector_data.items()}
                active_leaders = self.sector_engine.select_leading_sectors(sliced_sectors, regime)

            # ---- 보유 포지션 관리(트레일링/분할익절/청산) ----
            for ticker in list(positions.keys()):
                pos = positions[ticker]
                df = universe[ticker]
                if current_date not in df.index:
                    continue
                row = df.loc[current_date]
                close = float(row["Close"])

                atr = self.execution.compute_indicators(df.loc[:current_date]).iloc[-1]["ATR14"]
                new_stop, new_peak = self.execution.update_trailing_stop(
                    pos.entry_price, pos.stop, pos.peak, close, atr)
                pos.stop, pos.peak = new_stop, new_peak

                # SIDEWAYS 분할 익절 (1회)
                if regime == "SIDEWAYS" and not pos.partial_taken and \
                        self.execution.sideways_partial_target_hit(pos.entry_price, close, atr):
                    exit_shares = int(pos.shares * self.execution.partial_exit_fraction)
                    if exit_shares > 0:
                        exit_price = close * (1 - self.cost_rate)
                        cash += exit_shares * exit_price
                        pnl = exit_shares * (exit_price - pos.entry_price)
                        self.trades.append(Trade(ticker, pos.entry_date, current_date,
                                                  pos.entry_price, exit_price, exit_shares, pnl, "PARTIAL_TP"))
                        pos.shares -= exit_shares
                        pos.partial_taken = True

                # 손절/트레일링 스탑 히트 -> 전량 청산
                if close <= pos.stop:
                    exit_price = close * (1 - self.cost_rate)
                    cash += pos.shares * exit_price
                    pnl = pos.shares * (exit_price - pos.entry_price)
                    self.trades.append(Trade(ticker, pos.entry_date, current_date,
                                              pos.entry_price, exit_price, pos.shares, pnl, "STOP"))
                    del positions[ticker]

            # ---- 신규 진입 (BEAR 및 포지션 만석 시 스킵) ----
            if regime != "BEAR" and len(positions) < self.max_positions and active_leaders:
                for sector in active_leaders:
                    for ticker in SECTOR_STOCKS.get(sector, []):
                        if len(positions) >= self.max_positions:
                            break
                        if ticker in positions or ticker not in universe:
                            continue
                        stock_df = universe[ticker]
                        if current_date not in stock_df.index:
                            continue

                        hist = stock_df.loc[:current_date]
                        passed = self.screener.screen(hist, benchmark_df.loc[:current_date])
                        if not passed:
                            continue

                        signal = self.execution.entry_signal(hist, regime)
                        if not signal:
                            continue

                        entry_price_raw = signal["entry_price"]
                        entry_price = entry_price_raw * (1 + self.cost_rate)
                        stop = self.execution.initial_stop(entry_price, signal["ma20"], signal.get("atr"))

                        equity_now = cash + sum(
                            p.shares * float(universe[t].loc[current_date, "Close"])
                            for t, p in positions.items() if current_date in universe[t].index
                        )
                        sizing = self.risk_manager.position_size(equity_now, entry_price, stop, regime_mult)
                        shares = sizing["shares"]
                        cost = shares * entry_price
                        if shares <= 0 or cost > cash:
                            continue

                        cash -= cost
                        positions[ticker] = Position(
                            ticker=ticker, sector=sector, shares=shares,
                            entry_price=entry_price, entry_date=current_date,
                            stop=stop, peak=entry_price, entry_type=signal["type"],
                        )

            # ---- 일별 자산 평가 ----
            mtm = sum(
                p.shares * float(universe[t].loc[current_date, "Close"])
                for t, p in positions.items() if current_date in universe[t].index
            )
            equity_history.append(cash + mtm)
            dates_history.append(current_date)

        self.equity_curve = pd.Series(equity_history, index=pd.DatetimeIndex(dates_history))
        return self.performance_report()

    # ---------------- 성과 분석 ----------------
    def performance_report(self) -> dict:
        """CAGR/연변동성/MDD/샤프/손익비를 계산한다."""
        eq = self.equity_curve
        if eq.empty:
            return {}

        daily_ret = eq.pct_change().dropna()
        n_years = len(eq) / 252.0

        total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1.0 / n_years) - 1.0 if n_years > 0 else float("nan")
        ann_vol = daily_ret.std() * np.sqrt(252)
        sharpe = (daily_ret.mean() * 252) / ann_vol if ann_vol > 0 else float("nan")

        cum_max = eq.cummax()
        drawdown = eq / cum_max - 1.0
        mdd = drawdown.min()

        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("nan")

        return {
            "start_equity": float(eq.iloc[0]),
            "end_equity": float(eq.iloc[-1]),
            "total_return_pct": total_return * 100,
            "cagr_pct": cagr * 100,
            "ann_vol_pct": ann_vol * 100,
            "mdd_pct": mdd * 100,
            "sharpe": sharpe,
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (len(wins) / len(self.trades) * 100) if self.trades else 0.0,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_loss_ratio": pl_ratio,
        }

    def print_report(self) -> None:
        stats = self.performance_report()
        if not stats:
            print("백테스트 결과가 없습니다.")
            return
        print("=" * 70)
        print(" TopDownQuantSystem - 백테스트 성과 리포트")
        print("=" * 70)
        print(f" 초기 자본        : {stats['start_equity']:,.0f}")
        print(f" 최종 자산        : {stats['end_equity']:,.0f}")
        print(f" 총 수익률        : {stats['total_return_pct']:.2f}%")
        print(f" CAGR             : {stats['cagr_pct']:.2f}%")
        print(f" 연환산 변동성    : {stats['ann_vol_pct']:.2f}%")
        print(f" 최대 낙폭(MDD)   : {stats['mdd_pct']:.2f}%")
        print(f" 샤프 지수        : {stats['sharpe']:.2f}")
        print(f" 총 거래 횟수     : {stats['total_trades']} (승 {stats['wins']} / 패 {stats['losses']}, "
              f"승률 {stats['win_rate_pct']:.1f}%)")
        print(f" 손익비(P/L Ratio): {stats['profit_loss_ratio']:.2f}")
        print("=" * 70)


if __name__ == "__main__":
    bt = TopDownBacktester(years=3)
    bt.run()
    bt.print_report()
