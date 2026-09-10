"""
TopDownQuantSystem - 공통 설정
====================================================
Module 1~4 전 구간에서 공유하는 유니버스/파라미터 정의.
"""

from __future__ import annotations

# ------------------------------------------------------------------
# Benchmark / Market Regime
# ------------------------------------------------------------------
BENCHMARK = "SPY"
MA_SHORT = 20
MA_LONG = 200
SLOPE_LOOKBACK = 5          # Slope_5d(MA20) 산출용
SLOPE_THRESHOLD = 0.05      # %  (+-0.05%)
BAND_PCT = 0.005            # Close 대비 MA20 이격 밴드 (0.5%)
CONFIRM_DAYS = 2            # 국면 확정에 필요한 연속 거래일 수

REGIME_EXPOSURE = {
    "BULL": 1.0,
    "SIDEWAYS": 0.4,
    "BEAR": 0.0,
}
MAX_POSITIONS_BULL = 5

# ------------------------------------------------------------------
# Sector Rotation (GICS 11 Sector ETFs)
# ------------------------------------------------------------------
SECTOR_ETFS = ["XLK", "XLC", "XLF", "XLI", "XLE", "XLY", "XLP", "XLV", "XLU", "XLRE", "XLB"]
DEFENSIVE_ETFS = ["XLU", "XLP", "XLV", "XLF"]

ROC_SHORT = 5     # 1주(5거래일)
ROC_LONG = 21     # 1개월(21거래일)
RANK_WEIGHT_SHORT = 0.3
RANK_WEIGHT_LONG = 0.7
TOP_SECTOR_COUNT = 2
REBALANCE_FREQ = "W"      # 주 1회(매주 마지막 거래일)

# ------------------------------------------------------------------
# Sector -> 대표 종목 유니버스 (holdings API 대체용 큐레이션)
# ------------------------------------------------------------------
SECTOR_STOCKS = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "CRM", "ORCL", "ADBE", "AMD", "CSCO", "ACN"],
    "XLC":  ["META", "GOOGL", "NFLX", "TMUS", "DIS", "CMCSA", "T", "VZ", "EA", "TTWO"],
    "XLF":  ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "SPGI", "AXP"],
    "XLI":  ["GE", "CAT", "RTX", "UBER", "HON", "UNP", "BA", "DE", "ADP", "ETN"],
    "XLE":  ["XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "WMB", "OXY", "VLO"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "SBUX", "NKE", "CMG"],
    "XLP":  ["PG", "COST", "KO", "PEP", "WMT", "PM", "MO", "MDLZ", "CL", "TGT"],
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "PFE", "DHR", "AMGN"],
    "XLU":  ["NEE", "SO", "DUK", "CEG", "AEP", "SRE", "D", "EXC", "PEG", "ED"],
    "XLRE": ["PLD", "AMT", "EQIX", "WELL", "SPG", "PSA", "O", "DLR", "CCI", "VICI"],
    "XLB":  ["LIN", "SHW", "FCX", "ECL", "APD", "NEM", "CTVA", "NUE", "DOW", "PPG"],
}

# ------------------------------------------------------------------
# Stock Screener
# ------------------------------------------------------------------
STOCK_MA_SHORT = 20
STOCK_MA_MID = 60
STOCK_MA_LONG = 120
MA_LONG_TREND_LOOKBACK = 20     # MA120 상승 지속성 확인 기간
MA_LONG_TREND_MIN_PCT = 0.10    # % 이상 상승해야 함

LOOKBACK_52W = 252
NEAR_52W_HIGH_RATIO = 0.85      # 신고가 대비 -15% 이내
ABOVE_52W_LOW_RATIO = 1.30      # 52주 저점 대비 +30% 이상

MRS_LOOKBACK = 252

# ------------------------------------------------------------------
# Execution / Entry Triggers
# ------------------------------------------------------------------
PULLBACK_BAND_UPPER = 1.015
PULLBACK_BAND_LOWER = 0.985
PULLBACK_VOLUME_RATIO = 0.75
PULLBACK_VOLUME_MA = 20

BREAKOUT_LOOKBACK = 20
BREAKOUT_VOLUME_MA = 50
BREAKOUT_VOLUME_RATIO = 1.50
BREAKOUT_ATR_PERIOD = 14
BREAKOUT_CANDLE_ATR_RATIO = 0.5

# ------------------------------------------------------------------
# Risk Management
# ------------------------------------------------------------------
ATR_PERIOD = 14
STOP_INITIAL_PCT = 0.05          # EntryPrice * (1 - 5%)
BREAKEVEN_ATR_TRIGGER = 1.0      # 1*ATR 상승 시 손절가 -> 본전
TRAIL_ATR_MULT = 3.0             # PeakPrice - 3*ATR 트레일링
SIDEWAYS_PARTIAL_ATR_TRIGGER = 1.5
SIDEWAYS_PARTIAL_FRACTION = 0.5  # 1.5*ATR 도달 시 절반 분할 익절

RISK_PCT = 0.01                  # 1회 거래 리스크 비율
POSITION_CAP_PCT = 0.20          # 단일 종목 최대 투자 한도
MIN_DPS_FLOOR_PCT = 0.01         # DPS<=0 일 때 최소 주당 위험액

# ------------------------------------------------------------------
# Backtest
# ------------------------------------------------------------------
INITIAL_EQUITY = 100_000.0
COST_BPS = 10                    # 왕복 아님, 편도 10bp (수수료+슬리피지)
BACKTEST_YEARS = 3
