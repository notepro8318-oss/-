"""
탑다운(Top-Down) 4단계 전략 - 일일 시그널 스캐너
====================================================
1단계(시장) -> 2단계(섹터) -> 3단계(종목) -> 4단계(진입/리스크) 순서로 실행하여
매일 미국장 마감 직후 매매 시그널을 터미널에 출력한다.

실행 방법
---------
1) 즉시 1회 실행:      python main.py --once
2) 스케줄러로 상시 실행: python main.py
   (평일 미국 동부시간 16:05 = 한국시간 05:05 에 자동 실행되도록 스케줄 등록.
    schedule 라이브러리는 로컬 시스템 시간대를 기준으로 하므로, 서버의 실제
    시간대에 맞게 SCHEDULE_TIME 값을 조정해서 사용할 것.)
"""

import argparse
import time
from datetime import datetime

import schedule

from config import INITIAL_CASH, RISK_PER_TRADE_PCT
from market import check_market_trend
from sector import rank_sectors, select_leading_sectors
from stock import scan_leading_stocks
from entry import check_entry_signal, calc_position_size

# 스케줄 실행 시각 (실행 서버의 로컬 시간 기준, 24시간제 "HH:MM")
# 예: 서버가 한국시간(KST)일 때 미국 정규장 마감(16:00 ET) 직후는 대략 05:05 KST(서머타임 기준)
SCHEDULE_TIME = "05:05"


def run_topdown_scan(account_capital: float = INITIAL_CASH) -> None:
    """탑다운 4단계 전략을 1회 실행하고 결과를 터미널에 출력한다."""
    print("=" * 70)
    print(f"[탑다운 전략 스캔 시작] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ---------------- 1단계: 시장 추세 확인 ----------------
    market = check_market_trend()
    state = "BULL" if market["is_bull"] else "BEAR/중립"
    print(f"\n[1단계-시장필터] {market['ticker']} 종가={market['close']:.2f} "
          f"MA20={market['ma20']:.2f}(5일전 {market['ma20_prev']:.2f}) => {state}")

    if not market["is_bull"]:
        print("\n시장이 하락/역배열 추세입니다. 신규 매수를 중단하고 현금(Cash) 100%를 유지합니다.")
        return

    # ---------------- 2단계: 주도 섹터 선정 ----------------
    ranked = rank_sectors()
    leaders = select_leading_sectors()
    sector_returns = ranked["ret_1m"].to_dict()

    print("\n[2단계-섹터모멘텀] 전체 섹터 ETF 순위")
    print(ranked.round(2).to_string())
    print(f"\n[2단계-주도섹터] {leaders}")

    if not leaders:
        print("\n1주일/1개월 수익률 조건을 동시에 만족하는 주도 섹터가 없습니다. 관망합니다.")
        return

    # ---------------- 3단계: 강세 종목 발굴 ----------------
    candidates = scan_leading_stocks(leaders, sector_returns)
    print(f"\n[3단계-종목필터] 정배열/신고가근접/상대강도 통과 종목 {len(candidates)}개")
    for c in candidates:
        print(f"  {c['ticker']:6s} 섹터={c['sector']:5s} 현재가={c['close']:.2f} "
              f"종목1M={c['stock_ret_1m']:.1f}% 섹터1M={c['sector_ret_1m']:.1f}%")

    if not candidates:
        print("\n3단계 조건을 통과한 종목이 없습니다.")
        return

    # ---------------- 4단계: 매수 타점 및 리스크 관리 ----------------
    print("\n[4단계-매수시그널]")
    signal_count = 0
    for c in candidates:
        entry = check_entry_signal(c["df"])
        if not entry["signal"]:
            continue

        sizing = calc_position_size(account_capital, entry["entry_price"], entry["stop_loss"])
        signal_count += 1

        print(f"  ✅ {c['ticker']:6s} [{entry['type']:9s}] "
              f"진입가={entry['entry_price']:.2f} "
              f"손절가={entry['stop_loss']:.2f}(-{(1 - entry['stop_loss']/entry['entry_price'])*100:.1f}%) "
              f"익절가={entry['take_profit']:.2f}(+{(entry['take_profit']/entry['entry_price']-1)*100:.1f}%) "
              f"수량={sizing['shares']}주 (리스크={sizing['risk_amount']:,.0f}, "
              f"투입금액={sizing['position_value']:,.0f}, 계좌리스크={RISK_PER_TRADE_PCT*100:.0f}%)")

    if signal_count == 0:
        print("  오늘은 눌림목/돌파 조건을 만족하는 매수 시그널이 없습니다.")

    print("\n" + "=" * 70)
    print("[탑다운 전략 스캔 종료]")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="탑다운 4단계 전략 일일 시그널 스캐너")
    parser.add_argument("--once", action="store_true", help="스케줄러 없이 즉시 1회만 실행")
    parser.add_argument("--capital", type=float, default=INITIAL_CASH, help="계좌 자본금 (포지션 사이징에 사용)")
    args = parser.parse_args()

    if args.once:
        run_topdown_scan(args.capital)
        return

    # ---------------- 스케줄러 뼈대 ----------------
    # 매일 지정된 시각(SCHEDULE_TIME)에 1회 실행되도록 등록.
    # 실전 배포 시에는 cron / Windows 작업 스케줄러 / systemd timer 등으로
    # 이 스크립트 자체를 트리거하는 방식도 고려할 것.
    schedule.every().monday.at(SCHEDULE_TIME).do(run_topdown_scan, args.capital)
    schedule.every().tuesday.at(SCHEDULE_TIME).do(run_topdown_scan, args.capital)
    schedule.every().wednesday.at(SCHEDULE_TIME).do(run_topdown_scan, args.capital)
    schedule.every().thursday.at(SCHEDULE_TIME).do(run_topdown_scan, args.capital)
    schedule.every().friday.at(SCHEDULE_TIME).do(run_topdown_scan, args.capital)

    print(f"[스케줄러 등록 완료] 평일 {SCHEDULE_TIME}(서버 로컬 시간)에 자동 실행됩니다. "
          f"(Ctrl+C 로 종료)")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
