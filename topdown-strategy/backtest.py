"""
탑다운(Top-Down) 4단계 전략 - Backtrader 기반 3년 백테스트
====================================================
실제 브로커(Alpaca 등) 연동 대신, 지난 3년간의 일봉 데이터를 사용하여
Backtrader로 전략을 시뮬레이션하고 성과 리포트를 터미널에 출력한다.

전략 로직 매핑
--------------
- 1단계(시장필터): SPY 종가/MA20 우상향 여부로 신규 진입 게이트를 켜고 끈다.
- 2단계(섹터모멘텀): 매 거래일 섹터 ETF들의 1주/1개월 수익률을 계산해 주도 섹터를 갱신한다.
- 3단계(종목선정): 주도 섹터 소속 종목 중 정배열/52주 신고가 근접/상대강도 조건을 만족하는 종목만 매수 후보로 남긴다.
- 4단계(진입/리스크): 눌림목 또는 박스권 돌파 시 진입, 손절(-5%/MA20이탈) 및 익절(+10%)로 청산하며
                     1회 진입 리스크를 총 자본의 2% 이내로 제한하는 포지션 사이징을 적용한다.

실행 방법
---------
    python backtest.py
    python backtest.py --years 3 --capital 100000
"""

import argparse
import warnings
from datetime import datetime, timedelta

import backtrader as bt
import matplotlib
matplotlib.use("Agg")  # 헤드리스 환경에서도 차트를 파일로 저장할 수 있도록 설정
import matplotlib.pyplot as plt

from config import (
    MARKET_INDEX, MARKET_MA_PERIOD, MARKET_MA_LOOKBACK,
    SECTOR_ETFS, SECTOR_RET_SHORT, SECTOR_RET_LONG, TOP_SECTOR_COUNT,
    SECTOR_STOCKS, STOCK_MA_SHORT, STOCK_MA_MID, STOCK_MA_LONG,
    NEAR_52W_HIGH_RATIO, RS_LOOKBACK, BOX_BREAKOUT_LOOKBACK,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, RISK_PER_TRADE_PCT,
    BACKTEST_YEARS, INITIAL_CASH, COMMISSION,
)
from market import fetch_price_history

warnings.filterwarnings("ignore")


# ------------------------------------------------------------------
# 데이터 로딩 유틸
# ------------------------------------------------------------------
def load_feed(ticker: str, years: int) -> bt.feeds.PandasData | None:
    """yfinance에서 데이터를 받아 backtrader PandasData 피드로 변환한다.
    지표 워밍업(200일선, 52주 신고가 등)을 위해 요청 기간보다 1년 더 길게 받는다."""
    df = fetch_price_history(ticker, period=f"{years + 1}y")
    if df is None or len(df) < STOCK_MA_LONG:
        print(f"  ⚠ {ticker}: 데이터 부족으로 백테스트 유니버스에서 제외")
        return None

    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    data = bt.feeds.PandasData(dataname=df)
    return data


def build_stock_sector_map() -> dict:
    """종목 티커 -> 대표 섹터 ETF 매핑 (여러 섹터에 중복 등장 시 첫 번째 섹터를 사용)."""
    mapping = {}
    for etf, tickers in SECTOR_STOCKS.items():
        for t in tickers:
            mapping.setdefault(t, etf)
    return mapping


# ------------------------------------------------------------------
# 전략 클래스
# ------------------------------------------------------------------
class TopDownStrategy(bt.Strategy):
    params = dict(
        market_ticker=MARKET_INDEX,
        sector_tickers=tuple(SECTOR_ETFS),
        stock_sector_map=None,   # {stock_ticker: sector_etf}
        risk_pct=RISK_PER_TRADE_PCT,
        printlog=True,
    )

    def log(self, txt):
        if self.p.printlog:
            dt = self.datas[0].datetime.date(0)
            print(f"{dt.isoformat()} {txt}")

    def __init__(self):
        # ---- 데이터 피드를 이름으로 분류 ----
        self.market_data = self.getdatabyname(self.p.market_ticker)
        self.sector_data = {t: self.getdatabyname(t) for t in self.p.sector_tickers}
        self.stock_sector_map = self.p.stock_sector_map
        self.stock_data = {
            d._name: d for d in self.datas
            if d._name not in self.sector_data and d._name != self.p.market_ticker
        }

        # ---- 1단계: 시장 MA20 ----
        self.market_ma = bt.indicators.SMA(self.market_data.close, period=MARKET_MA_PERIOD)

        # ---- 3단계: 종목별 이동평균 / 52주 신고가 ----
        self.stock_ma20, self.stock_ma50, self.stock_ma200 = {}, {}, {}
        self.stock_high52w = {}
        for name, d in self.stock_data.items():
            self.stock_ma20[name] = bt.indicators.SMA(d.close, period=STOCK_MA_SHORT)
            self.stock_ma50[name] = bt.indicators.SMA(d.close, period=STOCK_MA_MID)
            self.stock_ma200[name] = bt.indicators.SMA(d.close, period=STOCK_MA_LONG)
            self.stock_high52w[name] = bt.indicators.Highest(d.close, period=252)

        # ---- 포지션별 손절/익절 가격 기록 ----
        self.entry_info = {}  # {ticker: {"stop": float, "target": float}}
        self.order_pending = set()

        # 거래 로그(리포트용)
        self.trade_log = []

    # ---------------- 헬퍼: 지표 계산 ----------------
    def _is_market_bull(self) -> bool:
        if len(self.market_ma) <= MARKET_MA_LOOKBACK:
            return False
        ma_now = self.market_ma[0]
        ma_prev = self.market_ma[-MARKET_MA_LOOKBACK]
        return (ma_now > ma_prev) and (self.market_data.close[0] > ma_now)

    def _sector_returns(self) -> dict:
        rets = {}
        for etf, d in self.sector_data.items():
            if len(d) > SECTOR_RET_LONG:
                rets[etf] = {
                    "ret_1w": d.close[0] / d.close[-SECTOR_RET_SHORT] - 1.0,
                    "ret_1m": d.close[0] / d.close[-SECTOR_RET_LONG] - 1.0,
                }
        return rets

    def _leading_sectors(self, sector_rets: dict) -> list:
        if not sector_rets:
            return []
        by_1w = sorted(sector_rets, key=lambda k: sector_rets[k]["ret_1w"], reverse=True)
        by_1m = sorted(sector_rets, key=lambda k: sector_rets[k]["ret_1m"], reverse=True)
        top_1w = set(by_1w[:TOP_SECTOR_COUNT])
        top_1m = set(by_1m[:TOP_SECTOR_COUNT])
        return list(top_1w & top_1m)

    def _passes_stock_filter(self, name: str, d, sector_ret_1m: float) -> bool:
        if len(d) <= STOCK_MA_LONG:
            return False
        close = d.close[0]
        ma20, ma50, ma200 = self.stock_ma20[name][0], self.stock_ma50[name][0], self.stock_ma200[name][0]
        high52w = self.stock_high52w[name][0]

        is_aligned = close > ma20 > ma50 > ma200
        is_near_high = close >= high52w * NEAR_52W_HIGH_RATIO

        if len(d) <= RS_LOOKBACK:
            return False
        stock_ret_1m = d.close[0] / d.close[-RS_LOOKBACK] - 1.0
        outperforms = stock_ret_1m > sector_ret_1m

        return bool(is_aligned and is_near_high and outperforms)

    def _entry_signal(self, name: str, d) -> str | None:
        """(A) 눌림목 또는 (B) 박스권 돌파 여부를 판별한다."""
        ma20 = self.stock_ma20[name][0]
        close, low = d.close[0], d.low[0]

        is_pullback = (low <= ma20) and (close > ma20)

        if len(d) > BOX_BREAKOUT_LOOKBACK:
            box_high = max(d.high.get(ago=-1, size=BOX_BREAKOUT_LOOKBACK))
            is_breakout = close > box_high
        else:
            is_breakout = False

        if is_pullback:
            return "PULLBACK"
        if is_breakout:
            return "BREAKOUT"
        return None

    # ---------------- 메인 루프 ----------------
    def next(self):
        market_bull = self._is_market_bull()

        # ---- 보유 포지션 리스크 관리(손절/익절) ----
        for name, d in list(self.stock_data.items()):
            pos = self.getposition(d)
            if pos.size <= 0:
                continue
            info = self.entry_info.get(name)
            if not info:
                continue
            ma20 = self.stock_ma20[name][0]
            close = d.close[0]

            hit_stop = (close <= info["stop"]) or (close < ma20)
            hit_target = close >= info["target"]

            if hit_stop or hit_target:
                self.close(data=d)
                reason = "손절(-5%/MA20이탈)" if hit_stop else "익절(+10%)"
                self.log(f"[청산] {name} {reason} 종가={close:.2f}")
                self.entry_info.pop(name, None)

        # ---- 시장이 약세면 신규 진입 중단 ----
        if not market_bull:
            return

        # ---- 2단계: 주도 섹터 ----
        sector_rets = self._sector_returns()
        leaders = self._leading_sectors(sector_rets)
        if not leaders:
            return

        # ---- 3단계 + 4단계: 종목 필터 -> 진입 시그널 ----
        for name, d in self.stock_data.items():
            pos = self.getposition(d)
            if pos.size > 0:
                continue  # 이미 보유 중이면 스킵

            sector = self.stock_sector_map.get(name)
            if sector not in leaders:
                continue

            sector_ret_1m = sector_rets.get(sector, {}).get("ret_1m", float("-inf"))
            if not self._passes_stock_filter(name, d, sector_ret_1m):
                continue

            entry_type = self._entry_signal(name, d)
            if entry_type is None:
                continue

            entry_price = d.close[0]
            ma20 = self.stock_ma20[name][0]
            stop = max(entry_price * (1 - STOP_LOSS_PCT), ma20) if ma20 < entry_price else entry_price * (1 - STOP_LOSS_PCT)
            target = entry_price * (1 + TAKE_PROFIT_PCT)

            # ---- 자금 관리: 총 자본의 2% 이내 리스크로 수량 산정 ----
            equity = self.broker.getvalue()
            cash = self.broker.getcash()
            risk_amount = equity * self.p.risk_pct
            per_share_risk = entry_price - stop
            if per_share_risk <= 0:
                continue
            shares_by_risk = int(risk_amount // per_share_risk)
            # 손절폭이 타이트해 리스크 기준 수량이 과도하게 커지는 경우를 대비해
            # 레버리지 없이 매수 가능한 현금 한도로도 제한한다.
            shares_by_cash = int(cash // entry_price)
            shares = max(0, min(shares_by_risk, shares_by_cash))
            if shares <= 0:
                continue

            self.buy(data=d, size=shares)
            self.entry_info[name] = {"stop": stop, "target": target}
            self.log(f"[진입] {name} [{entry_type}] 진입가={entry_price:.2f} "
                     f"손절={stop:.2f} 익절={target:.2f} 수량={shares}")

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trade_log.append({
                "ticker": trade.data._name,
                "pnl": trade.pnl,
                "pnlcomm": trade.pnlcomm,
            })


# ------------------------------------------------------------------
# 백테스트 실행 / 리포트
# ------------------------------------------------------------------
def run_backtest(years: int = BACKTEST_YEARS, capital: float = INITIAL_CASH,
                  printlog: bool = True, save_chart: bool = True):
    """
    백테스트를 실행하고 (strat, start_value, end_value)를 반환한다.

    printlog=False, save_chart=False로 호출하면 콘솔/파일 I/O 없이 결과 객체만
    돌려받을 수 있다 (예: streamlit_app.py에서 웹 UI에 직접 렌더링할 때 사용).
    """
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(capital)
    cerebro.broker.setcommission(commission=COMMISSION)

    if printlog:
        print(f"[데이터 로딩] 시장/섹터/종목 {1 + len(SECTOR_ETFS) + len(build_stock_sector_map())}개 티커, "
              f"최근 {years}년(+워밍업 1년)")

    # ---- 시장 데이터 ----
    market_feed = load_feed(MARKET_INDEX, years)
    if market_feed is None:
        raise RuntimeError(f"{MARKET_INDEX} 데이터를 불러올 수 없습니다.")
    cerebro.adddata(market_feed, name=MARKET_INDEX)

    # ---- 섹터 ETF 데이터 ----
    for etf in SECTOR_ETFS:
        feed = load_feed(etf, years)
        if feed is not None:
            cerebro.adddata(feed, name=etf)

    # ---- 종목 데이터 ----
    stock_sector_map = build_stock_sector_map()
    loaded_stocks = {}
    for ticker in stock_sector_map:
        feed = load_feed(ticker, years)
        if feed is not None:
            cerebro.adddata(feed, name=ticker)
            loaded_stocks[ticker] = stock_sector_map[ticker]

    cerebro.addstrategy(TopDownStrategy, stock_sector_map=loaded_stocks, printlog=printlog)

    # ---- 분석기 ----
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name="time_return", timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Years, riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    start_value = cerebro.broker.getvalue()
    if printlog:
        print(f"\n[백테스트 시작] 초기 자본 = {start_value:,.0f}\n{'-'*70}")

    results = cerebro.run()
    strat = results[0]

    end_value = cerebro.broker.getvalue()

    if printlog:
        print(f"{'-'*70}\n[백테스트 종료] 최종 자산 = {end_value:,.0f}\n")
        print_report(strat, start_value, end_value, years)
    if save_chart:
        save_equity_curve(strat, filename="backtest_equity_curve.png")

    return strat, start_value, end_value


def get_backtest_stats(strat, start_value: float, end_value: float, years: int) -> dict:
    """분석기 결과를 UI/콘솔 어디서나 쓸 수 있는 dict 형태로 정리한다."""
    dd = strat.analyzers.drawdown.get_analysis()
    sharpe = strat.analyzers.sharpe.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    total_return_pct = (end_value / start_value - 1.0) * 100.0
    cagr_pct = ((end_value / start_value) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else float("nan")

    total_trades = trades.get("total", {}).get("total", 0)
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    win_rate = (won / total_trades * 100.0) if total_trades else 0.0

    return {
        "start_value": start_value,
        "end_value": end_value,
        "total_return_pct": total_return_pct,
        "cagr_pct": cagr_pct,
        "mdd_pct": dd.get("max", {}).get("drawdown", float("nan")),
        "sharpe": sharpe.get("sharperatio", None),
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
        "avg_win": trades.get("won", {}).get("pnl", {}).get("average", 0.0),
        "avg_loss": trades.get("lost", {}).get("pnl", {}).get("average", 0.0),
    }


def get_equity_series(strat):
    """TimeReturn 분석기의 일별 수익률을 누적 자산곡선(dates, equity)으로 변환한다."""
    tr = strat.analyzers.time_return.get_analysis()
    if not tr:
        return [], []

    dates = sorted(tr.keys())
    equity = [1.0]
    for dt in dates:
        equity.append(equity[-1] * (1.0 + tr[dt]))
    return dates, equity[1:]


def print_report(strat, start_value: float, end_value: float, years: int):
    stats = get_backtest_stats(strat, start_value, end_value, years)
    print("=" * 70)
    print(" 탑다운(Top-Down) 4단계 전략 - 3년 백테스트 리포트 (Backtrader)")
    print("=" * 70)
    print(f" 초기 자본        : {stats['start_value']:,.0f}")
    print(f" 최종 자산        : {stats['end_value']:,.0f}")
    print(f" 총 수익률        : {stats['total_return_pct']:.2f}%")
    print(f" 연복리수익률(CAGR): {stats['cagr_pct']:.2f}%")
    print(f" 최대 낙폭(MDD)    : {stats['mdd_pct']:.2f}%")
    print(f" 샤프 비율         : {stats['sharpe']}")
    print(f" 총 거래 횟수      : {stats['total_trades']} (승 {stats['won']} / 패 {stats['lost']}, 승률 {stats['win_rate']:.1f}%)")
    print(f" 평균 수익 트레이드: {stats['avg_win']:,.2f}")
    print(f" 평균 손실 트레이드: {stats['avg_loss']:,.2f}")
    print("=" * 70)


def save_equity_curve(strat, filename: str = "backtest_equity_curve.png"):
    """TimeReturn 분석기의 일별 수익률을 누적하여 자산 곡선을 이미지로 저장한다."""
    dates, equity = get_equity_series(strat)
    if not dates:
        return

    plt.figure(figsize=(11, 5))
    plt.plot(dates, equity, linewidth=1.5)
    plt.title("Top-Down Strategy - Cumulative Equity Curve")
    plt.xlabel("Date")
    plt.ylabel("Equity (normalized, start=1.0)")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    plt.close()
    print(f"\n[차트 저장 완료] {filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="탑다운 4단계 전략 Backtrader 백테스트")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS, help="백테스트 기간(년)")
    parser.add_argument("--capital", type=float, default=INITIAL_CASH, help="초기 자본")
    args = parser.parse_args()

    run_backtest(years=args.years, capital=args.capital)
