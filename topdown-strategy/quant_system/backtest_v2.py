"""
[v2] TopDownBacktesterV2 - 구조 개편판
====================================================
사용자 제안에 따른 4가지 구조 변경을 반영한 백테스터.

1) 유휴자금 폭포수(Waterfall) 배분: BULL 국면에서 목표 종목 수를 못 채워 남는
   현금은 1순위로 주도 섹터 ETF 상위 2개에 50:50, 주도 섹터가 없으면
   FALLBACK_INDEX_TICKER(QQQ)에 배분해 현금 비중 0%를 유지한다.
2) 모멘텀 팩터를 12-1M 중심(Mom_12_1/ROC_63/ROC_21 가중)으로 교체.
3) StockScreener의 AND 하드필터 대신 MomentumRanker의 Cross-Sectional
   CompositeScore로 매주 상위 N종목을 선정(리밸런싱)한다.
4) 손절/트레일링을 Runner 전략으로 개편: EntryPrice-2.5*ATR 손절,
   +10%/+20%에서 각각 30%씩 분할익절, 잔여 40%는 Close<MA50까지 추세추종.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qs_config import (
    SECTOR_ETFS, SECTOR_STOCKS, BENCHMARK, COST_BPS, INITIAL_EQUITY,
    STOCK_MA_LONG, LOOKBACK_52W, MRS_LOOKBACK, MOM_12_1_START,
    TOP_STOCK_COUNT, TP1_PCT, TP1_FRACTION, TP2_PCT, TP2_FRACTION,
    IDLE_CASH_FALLBACK_ENABLED, IDLE_CASH_THRESHOLD_PCT, FALLBACK_INDEX_TICKER,
)
from data import fetch_ohlcv
from regime import MarketRegimeDetector
from qs_sector import SectorRotationEngine
from screener import MomentumRanker
from execution import ExecutionEngine
from risk import RiskManager


@dataclass
class StockPosition:
    ticker: str
    sector: str
    shares: int
    original_shares: int
    entry_price: float
    entry_date: pd.Timestamp
    stop: float
    tp1_taken: bool = False
    tp2_taken: bool = False


@dataclass
class FallbackPosition:
    ticker: str
    shares: float = 0.0
    cost_basis: float = 0.0  # 총 매입금액(평단가 * 수량)
    entry_date: pd.Timestamp | None = None


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    reason: str


class TopDownBacktesterV2:
    """구조 개편판 백테스터: 폭포수 배분 + 모멘텀 랭킹 + Runner 전략."""

    def __init__(self, years: int = 3, initial_equity: float = INITIAL_EQUITY,
                 cost_bps: float = COST_BPS, max_positions: int = TOP_STOCK_COUNT,
                 risk_pct: float = 0.03, position_cap_pct: float = 0.40,
                 sizing_mode: str = "risk_atr") -> None:
        """
        sizing_mode : {"risk_atr", "equal", "composite_score"}
            - "risk_atr": 기존 방식. RiskManager의 고정비율 리스크(ATR 손절폭 기반) 사이징.
            - "equal": 매수 후보 max_positions개에 자본을 동일 비중(1/N)으로 배분.
            - "composite_score": CompositeScore가 좋을수록(낮을수록) 더 큰 비중을 배분
              (가중치 ∝ 1/CompositeScore, 그 주 후보군 내에서 정규화).
        두 경우 모두 손절/분할익절/Runner 청산 로직은 동일하게 ATR 기반을 사용하고,
        "진입 수량(비중)"만 다르게 계산한다.
        """
        self.years = years
        self.initial_equity = initial_equity
        self.cost_rate = cost_bps / 10_000.0
        self.max_positions = max_positions
        self.sizing_mode = sizing_mode

        self.regime_detector = MarketRegimeDetector()
        self.sector_engine = SectorRotationEngine()
        self.ranker = MomentumRanker(top_n=max_positions)
        self.execution = ExecutionEngine()
        self.risk_manager = RiskManager(risk_pct=risk_pct, cap_pct=position_cap_pct)

        self.equity_curve: pd.Series = pd.Series(dtype=float)
        self.trades: list[Trade] = []

    # ---------------- 데이터 준비 ----------------
    def _load_universe(self) -> dict[str, pd.DataFrame]:
        warmup_years = self.years + 2
        period = f"{warmup_years}y"

        universe = {BENCHMARK: fetch_ohlcv(BENCHMARK, period=period)}
        for etf in SECTOR_ETFS:
            universe[etf] = fetch_ohlcv(etf, period=period)
        if FALLBACK_INDEX_TICKER not in universe:
            universe[FALLBACK_INDEX_TICKER] = fetch_ohlcv(FALLBACK_INDEX_TICKER, period=period)

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

    def _weekly_rebalance_dates(self, index: pd.DatetimeIndex) -> set:
        s = pd.Series(index, index=index)
        return set(s.groupby([index.isocalendar().year, index.isocalendar().week]).max().tolist())

    # ---------------- 실행 ----------------
    def run(self) -> dict:
        universe = self._load_universe()
        benchmark_df = universe[BENCHMARK]
        regime_df = self.regime_detector.classify(benchmark_df)

        warmup = max(STOCK_MA_LONG, LOOKBACK_52W, MRS_LOOKBACK, MOM_12_1_START) + 5
        usable_index = benchmark_df.index[warmup:]
        target_days = self.years * 252
        common_index = usable_index[-target_days:] if len(usable_index) > target_days else usable_index

        rebalance_dates = self._weekly_rebalance_dates(benchmark_df.index)
        sector_data = {etf: universe[etf] for etf in SECTOR_ETFS if etf in universe}

        active_leaders: list[str] = []
        approved_candidates: list[str] = []
        candidate_scores: dict[str, float] = {}

        cash = self.initial_equity
        stock_positions: dict[str, StockPosition] = {}
        fallback_positions: dict[str, FallbackPosition] = {}
        equity_history, dates_history = [], []

        def price_on(ticker: str, date) -> float | None:
            df = universe.get(ticker)
            if df is None or date not in df.index:
                return None
            return float(df.loc[date, "Close"])

        def mark_to_market(date) -> float:
            total = cash
            for t, p in stock_positions.items():
                px = price_on(t, date)
                if px is not None:
                    total += p.shares * px
            for t, fp in fallback_positions.items():
                px = price_on(t, date)
                if px is not None:
                    total += fp.shares * px
            return total

        for current_date in common_index:
            if current_date not in regime_df.index:
                continue
            regime = regime_df.loc[current_date, "Regime"]
            regime_mult = self.risk_manager.regime_multiplier(regime)

            # ---- 주간 리밸런싱: 주도 섹터 + 모멘텀 랭킹 상위 N종목 갱신 ----
            if current_date in rebalance_dates:
                sliced_sectors = {t: df.loc[:current_date] for t, df in sector_data.items()}
                active_leaders = self.sector_engine.select_leading_sectors(sliced_sectors, regime)

                candidates_data = {}
                for sector in active_leaders:
                    for ticker in SECTOR_STOCKS.get(sector, []):
                        if ticker in universe and current_date in universe[ticker].index:
                            candidates_data[ticker] = universe[ticker].loc[:current_date]

                if candidates_data:
                    ranked_df = self.ranker.rank_candidates(candidates_data, benchmark_df.loc[:current_date])
                    top_ranked = ranked_df.head(self.max_positions)
                    approved_candidates = top_ranked.index.tolist()
                    candidate_scores = top_ranked["composite_score"].to_dict()
                else:
                    approved_candidates = []
                    candidate_scores = {}

            # ---- 보유 주식 포지션 관리 (Runner 전략) ----
            for ticker in list(stock_positions.keys()):
                pos = stock_positions[ticker]
                df = universe[ticker]
                if current_date not in df.index:
                    continue
                close = float(df.loc[current_date, "Close"])
                ind = self.execution.compute_indicators(df.loc[:current_date]).iloc[-1]
                ma50 = ind["MA50Runner"]

                def _exit_all(reason: str):
                    nonlocal cash
                    exit_price = close * (1 - self.cost_rate)
                    cash += pos.shares * exit_price
                    pnl = pos.shares * (exit_price - pos.entry_price)
                    self.trades.append(Trade(ticker, pos.entry_date, current_date,
                                              pos.entry_price, exit_price, pos.shares, pnl, reason))
                    del stock_positions[ticker]

                def _take_partial(fraction: float, reason: str):
                    nonlocal cash
                    exit_shares = int(pos.original_shares * fraction)
                    exit_shares = min(exit_shares, pos.shares)
                    if exit_shares <= 0:
                        return
                    exit_price = close * (1 - self.cost_rate)
                    cash += exit_shares * exit_price
                    pnl = exit_shares * (exit_price - pos.entry_price)
                    self.trades.append(Trade(ticker, pos.entry_date, current_date,
                                              pos.entry_price, exit_price, exit_shares, pnl, reason))
                    pos.shares -= exit_shares

                if not pos.tp1_taken:
                    if close <= pos.stop:
                        _exit_all("STOP")
                        continue
                    if close >= pos.entry_price * (1 + TP1_PCT):
                        _take_partial(TP1_FRACTION, "TP1")
                        pos.stop = pos.entry_price  # 본전 상향
                        pos.tp1_taken = True
                elif not pos.tp2_taken:
                    if close <= pos.stop:
                        _exit_all("STOP_BE")
                        continue
                    if close >= pos.entry_price * (1 + TP2_PCT):
                        _take_partial(TP2_FRACTION, "TP2")
                        pos.tp2_taken = True
                else:
                    if not pd.isna(ma50) and close < ma50:
                        _exit_all("MA50_EXIT")

            # ---- 폭포수 배분 종목: BEAR 확정 시에만 전량 청산 ----
            # (BULL<->SIDEWAYS는 국면 판정이 자주 뒤바뀌므로, 그때마다 청산하면
            #  왕복 비용만 반복 발생하는 churn이 생긴다. 진짜 하락(BEAR) 확정 시에만 턴다.)
            if regime == "BEAR":
                for ticker in list(fallback_positions.keys()):
                    fp = fallback_positions[ticker]
                    close = price_on(ticker, current_date)
                    if close is None or fp.shares <= 0:
                        continue
                    exit_price = close * (1 - self.cost_rate)
                    proceeds = fp.shares * exit_price
                    pnl = proceeds - fp.cost_basis
                    cash += proceeds
                    self.trades.append(Trade(ticker, fp.entry_date or current_date, current_date,
                                              fp.cost_basis / fp.shares if fp.shares else 0.0,
                                              exit_price, fp.shares, pnl, "FALLBACK_EXIT"))
                    del fallback_positions[ticker]

            # ---- 신규 진입 (랭킹 승인 종목은 눌림목/돌파 타이밍 대기 없이 즉시 매수) ----
            if regime != "BEAR" and len(stock_positions) < self.max_positions:
                for ticker in approved_candidates:
                    if len(stock_positions) >= self.max_positions or ticker in stock_positions:
                        continue
                    df = universe.get(ticker)
                    if df is None or current_date not in df.index:
                        continue

                    hist = df.loc[:current_date]
                    ind = self.execution.compute_indicators(hist)
                    atr = ind.iloc[-1]["ATR14"]
                    if pd.isna(atr) or atr <= 0:
                        continue

                    entry_price = float(df.loc[current_date, "Close"]) * (1 + self.cost_rate)
                    stop = self.execution.initial_stop_v2(entry_price, float(atr))
                    if stop >= entry_price:
                        continue

                    equity_now = mark_to_market(current_date)

                    if self.sizing_mode == "equal":
                        target_value = equity_now * regime_mult / self.max_positions
                        shares = int(target_value // entry_price)
                    elif self.sizing_mode == "composite_score":
                        inv_scores = {t: 1.0 / s for t, s in candidate_scores.items() if s and s > 0}
                        total_inv = sum(inv_scores.values())
                        weight = (inv_scores.get(ticker, 0.0) / total_inv) if total_inv > 0 else 0.0
                        target_value = equity_now * regime_mult * weight
                        shares = int(target_value // entry_price)
                    else:  # "risk_atr" (기존 방식)
                        sizing = self.risk_manager.position_size(equity_now, entry_price, stop, regime_mult)
                        shares = sizing["shares"]

                    cost = shares * entry_price
                    if shares <= 0:
                        continue

                    # 현금이 부족하면 폭포수(fallback) 보유분을 매도해 자금을 확보한다.
                    if cost > cash and fallback_positions:
                        shortfall = cost - cash
                        for fb_ticker in list(fallback_positions.keys()):
                            if shortfall <= 0:
                                break
                            fp = fallback_positions[fb_ticker]
                            fb_close = price_on(fb_ticker, current_date)
                            if fb_close is None or fp.shares <= 0:
                                continue
                            fb_value = fp.shares * fb_close
                            sell_value = min(fb_value, shortfall)
                            sell_shares = sell_value / fb_close
                            exit_price = fb_close * (1 - self.cost_rate)
                            proceeds = sell_shares * exit_price
                            cash += proceeds
                            fp.cost_basis *= max(0.0, (fp.shares - sell_shares) / fp.shares) if fp.shares > 0 else 0.0
                            fp.shares -= sell_shares
                            if fp.shares <= 1e-6:
                                del fallback_positions[fb_ticker]
                            shortfall -= proceeds

                    if cost > cash:
                        continue

                    sector = self.stock_sector_map.get(ticker, "")
                    cash -= cost
                    stock_positions[ticker] = StockPosition(
                        ticker=ticker, sector=sector, shares=shares, original_shares=shares,
                        entry_price=entry_price, entry_date=current_date, stop=stop,
                    )

            # ---- 유휴자금 폭포수 배분 (BULL, 목표 종목수 미달 + 현금 여유) ----
            if IDLE_CASH_FALLBACK_ENABLED and regime == "BULL" and len(stock_positions) < self.max_positions:
                equity_now = mark_to_market(current_date)
                if cash > equity_now * IDLE_CASH_THRESHOLD_PCT:
                    # 주도 섹터 ETF는 국면 전환기에 낙폭이 커서 폭포수 대상에서 제외하고,
                    # 더 보수적인 지수 ETF(QQQ/SPY)로만 유휴자금을 채운다.
                    deploy_amount = cash
                    targets = [FALLBACK_INDEX_TICKER]
                    per_target = deploy_amount / len(targets)
                    for t in targets:
                        px = price_on(t, current_date)
                        if px is None or px <= 0:
                            continue
                        buy_price = px * (1 + self.cost_rate)
                        buy_shares = per_target / buy_price
                        if buy_shares <= 0:
                            continue
                        fp = fallback_positions.setdefault(t, FallbackPosition(ticker=t, entry_date=current_date))
                        fp.shares += buy_shares
                        fp.cost_basis += buy_shares * buy_price
                        cash -= buy_shares * buy_price

            equity_history.append(mark_to_market(current_date))
            dates_history.append(current_date)

        self.equity_curve = pd.Series(equity_history, index=pd.DatetimeIndex(dates_history))
        return self.performance_report()

    # ---------------- 성과 분석 (v1과 동일) ----------------
    def performance_report(self) -> dict:
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
        print(" TopDownBacktesterV2 - 구조개편판 백테스트 성과 리포트")
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
    bt = TopDownBacktesterV2(years=3)
    bt.run()
    bt.print_report()
