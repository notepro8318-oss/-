"""
GitHub Actions 스케줄러 진입점 — 미국 장마감 직후 스크리닝 실행 + 변경사항 Telegram 알림
====================================================
screening.compute_screening_snapshot()로 오늘의 매수 후보(종목/진입가/수량/비중)를
계산해 직전 스냅샷과 비교하고, 변경 사항이 있을 때만 Telegram으로 발송한다.

필요 환경변수 (GitHub Actions repo secrets/variables):
- GITHUB_TOKEN: contents 쓰기 권한 있는 PAT (screening.py의 github_store.py가 사용)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 알림 발송용
- SCREENING_CAPITAL_USD (선택, repo Variable): 수량 계산에 쓸 자본금. 미설정 시 기본 자본금(qs_config.INITIAL_EQUITY) 사용.
"""

from __future__ import annotations

import os

import requests

from qs_config import INITIAL_EQUITY
from screening import compute_screening_snapshot, load_snapshot, save_snapshot, diff_snapshots


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


def format_message(snapshot: dict, changes: list[str]) -> str:
    lines = [f"📊 *스크리닝 결과* ({snapshot['date']}) — 국면: {snapshot['regime']}"]
    if snapshot["leaders"]:
        lines.append(f"주도 섹터: {', '.join(snapshot['leaders'])}")
    lines.append("")
    lines.append("*변경 사항*")
    lines.extend(f"- {c}" for c in changes)
    lines.append("")
    lines.append("*현재 매수 후보*")
    if snapshot["candidates"]:
        for c in snapshot["candidates"]:
            lines.append(f"- {c['ticker']}({c['sector']}) 진입가 {c['entry_price']:.2f} · "
                          f"{c['shares']}주 · 비중 {c['weight_pct']}%")
    else:
        lines.append("- 없음")
    return "\n".join(lines)


def main() -> None:
    capital = float(os.environ.get("SCREENING_CAPITAL_USD") or INITIAL_EQUITY)
    new_snapshot = compute_screening_snapshot(capital)
    old_snapshot = load_snapshot()
    changes = diff_snapshots(old_snapshot, new_snapshot)

    print(f"국면={new_snapshot['regime']} 후보={len(new_snapshot['candidates'])}건 변경={len(changes)}건")
    for c in changes:
        print(c)

    if changes:
        send_telegram_message(format_message(new_snapshot, changes))

    save_snapshot(new_snapshot)


if __name__ == "__main__":
    main()
