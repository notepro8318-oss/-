"""
GitHub Actions 스케줄러 진입점 — 장중/마감 후 매매일지 시그널 스캔 + Telegram 알림
====================================================
journal.scan_open_trades()로 모든 OPEN 포지션의 신규 시그널(TP1/TP2/STOP/
BREAKEVEN_STOP/RUNNER_EXIT)을 감지해 GitHub에 상태를 저장하고, 새로 발생한
시그널을 Telegram 봇으로 발송한다.

필요 환경변수 (GitHub Actions repo secrets):
- GITHUB_TOKEN: contents 쓰기 권한 있는 PAT (journal.py의 github_store.py가 사용)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 알림 발송용
"""

from __future__ import annotations

import os

import requests

from journal import scan_open_trades, SIGNAL_LABELS


def send_telegram_message(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 - 알림 발송을 건너뜁니다.")
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        print(f"Telegram 발송 실패: {resp.status_code} {resp.text}")


def format_signal(sig: dict) -> str:
    label = SIGNAL_LABELS.get(sig["type"], sig["type"])
    return (
        f"🔔 *매매일지 시그널*\n"
        f"*{sig['ticker']}* · {label}\n"
        f"일자: {sig['date']}\n"
        f"가격: {sig['price']:.2f}\n"
        f"수량: {sig['suggested_shares']:,}주"
    )


def main() -> None:
    new_signals = scan_open_trades()
    print(f"신규 시그널 {len(new_signals)}건 감지")
    for sig in new_signals:
        print(sig)
        send_telegram_message(format_signal(sig))


if __name__ == "__main__":
    main()
