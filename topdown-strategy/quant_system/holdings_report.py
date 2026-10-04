"""
GitHub Actions 스케줄러 진입점 — 미국 장마감 후 매매일지 보유 종목 현황을 Telegram으로 전송
====================================================
매매일지에 등록된 진행중(OPEN) 포지션의 마감 종가 기준 현황(총 투자금액/평가금액/손익,
섹터 비중, 종목별 손절가·목표가, 확인 대기 중인 시그널)을 하루 한 번 요약해 보낸다.

미국 증시 휴장일(마지막 일봉이 오늘 날짜가 아님)에는 발송하지 않는다.
수동 실행(workflow_dispatch)이나 FORCE_REPORT=1이면 휴장 여부와 관계없이 가장 최근
종가 기준으로 발송한다.

필요 환경변수 (GitHub Actions repo secrets):
- GITHUB_TOKEN: 매매일지 CSV 읽기용 (journal.py의 github_store.py가 사용)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 알림 발송용
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from data import fetch_ohlcv
from journal import (
    load_trades, load_signals, get_dashboard_metrics, sector_of,
    SIGNAL_LABELS, EXIT_SIGNAL_TYPES,
)
from notify import send_telegram_message
from qs_config import BENCHMARK

TELEGRAM_LIMIT = 3800  # 텔레그램 메시지 상한(4096자)보다 여유를 둔다
PHASE_EMOJI = {"STAGE_0": "🟡", "STAGE_1": "🔵", "STAGE_2": "🟢"}


def _pct(v) -> str:
    return f"{v:+.2f}%" if v is not None and v == v else "N/A"


def build_report(open_trades: pd.DataFrame, signals_df: pd.DataFrame, report_date: str,
                 metrics_fn=get_dashboard_metrics) -> list[str]:
    """보유 현황 리포트를 텔레그램 상한 이내의 메시지 조각 리스트로 만든다."""
    if open_trades.empty:
        return [f"📓 매매일지 보유 현황 — {report_date} 마감 기준\n\n현재 보유 중인 종목이 없습니다."]

    rows, blocks = [], []
    for _, t in open_trades.iterrows():
        trade = t.to_dict()
        m = metrics_fn(trade)
        ok = "error" not in m
        last = m["last_close"] if ok else float(t["entry_price"])  # 시세 조회 실패 시 매수가로 대체
        remaining = int(t["remaining_shares"])
        entry = float(t["entry_price"])
        rows.append({"sector": sector_of(t["ticker"]), "cost": entry * remaining, "value": last * remaining})

        ret = (last / entry - 1) * 100 if entry else None
        lines = [f"{PHASE_EMOJI.get(t['phase'], '')} {t['ticker']} · {t['entry_date']} 진입 · "
                 f"{remaining:,}/{int(t['initial_shares']):,}주",
                 f"  매수 {entry:.2f} → {'종가' if ok else '시세 없음, 매수가 기준'} {last:.2f} ({_pct(ret)})"]
        if ok:
            stop = f"손절 {float(t['current_stop']):.2f}({_pct(m['dist_to_stop_pct'])})"
            if m["target_price"] is not None:
                target = f"{m['target_label']} {m['target_price']:.2f}({_pct(m['dist_to_target_pct'])})"
            else:
                target = "Runner(MA50 이탈 시 청산)"
            lines.append(f"  {stop} · {target} · R {m['r_multiple']:+.2f}")
        blocks.append("\n".join(lines))

    df = pd.DataFrame(rows)
    total_cost, total_value = df["cost"].sum(), df["value"].sum()
    pnl = total_value - total_cost
    pnl_pct = pnl / total_cost * 100 if total_cost > 0 else 0.0
    sector = df.groupby("sector")["value"].sum().sort_values(ascending=False)

    head = [f"📓 매매일지 보유 현황 — {report_date} 마감 기준", "",
            f"총 투자금액 ${total_cost:,.0f} · 평가금액 ${total_value:,.0f}",
            f"평가손익 {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f} ({pnl_pct:+.2f}%)", "", "섹터 비중"]
    head += [f"- {name} {v / total_value * 100:.1f}% (${v:,.0f})" for name, v in sector.items()] \
        if total_value > 0 else ["- 없음"]

    tail = []
    if not signals_df.empty:
        open_ids = set(open_trades["id"])
        pending = signals_df[(~signals_df["confirmed"]) & (signals_df["trade_id"].isin(open_ids))]
        if not pending.empty:
            tail.append(f"🔔 확인 대기 알림 {len(pending)}건")
            for _, s in pending.sort_values("date").iterrows():
                action = "매도 완료 확인" if s["type"] in EXIT_SIGNAL_TYPES else "체결 확인"
                tail.append(f"- {s['ticker']} {SIGNAL_LABELS.get(s['type'], s['type'])} "
                            f"({s['date']}, {s['price']:.2f}) → {action} 필요")

    header_text = "\n".join(head)
    sections = [f"보유 종목 ({len(blocks)})"] + blocks
    if tail:
        sections.append("\n".join(tail))

    messages, current = [], header_text
    for section in sections:
        if len(current) + len(section) + 2 > TELEGRAM_LIMIT:
            messages.append(current)
            current = section
        else:
            current += "\n\n" + section
    messages.append(current)
    return messages


def market_was_open_today() -> bool:
    """SPY의 마지막 일봉 날짜가 미국 동부시간 기준 오늘이면 정규장이 열렸던 날로 본다."""
    df = fetch_ohlcv(BENCHMARK, period="5d")
    if df.empty:
        return False
    return df.index[-1].date() == datetime.now(ZoneInfo("America/New_York")).date()


def main() -> None:
    force = os.environ.get("FORCE_REPORT", "").strip().lower() in ("1", "true", "yes")
    if not force and not market_was_open_today():
        print("오늘은 미국 증시 휴장일(또는 일봉 미집계)이라 보유 현황 리포트를 보내지 않습니다.")
        return

    trades = load_trades()
    open_trades = trades[(trades["status"] == "OPEN") & (~trades["archived"])] if not trades.empty else trades
    if not open_trades.empty:
        open_trades = open_trades.sort_values("entry_date", kind="stable")
    last_bar = fetch_ohlcv(BENCHMARK, period="5d").index[-1].date()

    for text in build_report(open_trades, load_signals(), str(last_bar)):
        print(text, "\n")
        # 종목/단계 이름에 '_' 등이 있어 Markdown 파싱 오류가 나지 않도록 일반 텍스트로 보낸다.
        if not send_telegram_message(text, parse_mode=None):
            print("발송 실패 - 이후 조각 발송을 중단합니다.")
            break


if __name__ == "__main__":
    main()
