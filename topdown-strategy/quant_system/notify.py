"""
GitHub Actions 스케줄러 진입점 — 장중/마감 후 매매일지 시그널 스캔 + Telegram 알림
====================================================
journal.scan_open_trades()로 모든 OPEN 포지션의 신규 시그널(TP1/TP2/STOP/
BREAKEVEN_STOP/RUNNER_EXIT)을 감지해 GitHub에 상태를 저장한 뒤, journal.
get_unnotified_signals()로 "아직 Telegram으로 안 보낸" 시그널을 모두 찾아 발송한다.

시그널 감지는 대시보드의 수동 "시그널 확인" 버튼에서도 똑같이 일어날 수 있으므로,
"이번 스캔에서 방금 감지된 것"만 보내면 다른 경로에서 먼저 감지된 시그널은 영영
알림을 못 받는다. notified 플래그로 감지와 발송을 분리해, 언제 감지됐든 미발송
시그널은 이 스크립트가 실행될 때마다 다시 찾아서 보낸다.

필요 환경변수 (GitHub Actions repo secrets):
- GITHUB_TOKEN: contents 쓰기 권한 있는 PAT (journal.py의 github_store.py가 사용)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 알림 발송용
"""

from __future__ import annotations

import os

import requests

from journal import scan_open_trades, get_unnotified_signals, mark_notified, SIGNAL_LABELS


def send_telegram_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 - 알림 발송을 건너뜁니다.")
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        print(f"Telegram 발송 실패: {resp.status_code} {resp.text}")
        return False
    return True


def format_signal(sig) -> str:
    label = SIGNAL_LABELS.get(sig["type"], sig["type"])
    return (
        f"🔔 *매매일지 시그널*\n"
        f"*{sig['ticker']}* · {label}\n"
        f"일자: {sig['date']}\n"
        f"가격: {sig['price']:.2f}\n"
        f"수량: {int(sig['suggested_shares']):,}주"
    )


def main() -> None:
    new_signals = scan_open_trades()
    print(f"이번 스캔에서 신규 감지 {len(new_signals)}건")

    pending = get_unnotified_signals()
    print(f"미발송(notified=False) 시그널 {len(pending)}건")

    sent_ids = []
    for _, sig in pending.iterrows():
        print(dict(sig))
        if send_telegram_message(format_signal(sig)):
            sent_ids.append(sig["id"])

    if sent_ids:
        mark_notified(sent_ids)
        print(f"Telegram 발송 및 notified 처리 완료: {len(sent_ids)}건")


if __name__ == "__main__":
    main()
