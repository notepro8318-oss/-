"""
GitHub Actions 스케줄러 진입점 — 미국 장마감 직후 스크리닝 실행 + 변경사항 Telegram 알림
====================================================
screening.compute_screening_snapshot()로 오늘의 매수 후보(종목/진입가/수량/비중)를
계산해 직전 스냅샷과 비교하고, 변경 사항이 있을 때만 Telegram으로 발송한다.

필요 환경변수 (GitHub Actions repo secrets/variables):
- GITHUB_TOKEN: contents 쓰기 권한 있는 PAT (screening.py의 github_store.py가 사용)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 알림 발송용
- SCREENING_CAPITAL_USD (선택, repo Variable): 수량 계산에 쓸 자본금. 미설정 시 기본 자본금(qs_config.INITIAL_EQUITY) 사용.
- SIDEWAYS_SECTOR_MODE (선택, repo Variable): "HYBRID"(C안, 기본) 또는 "DEFENSIVE"(A안, 방어섹터 전용).
"""

from __future__ import annotations

import os
import sys

import requests

from qs_config import INITIAL_EQUITY, DEFAULT_SIDEWAYS_MODE, SIDEWAYS_MODES
from job_guard import already_done, mark_done, latest_completed_us_session
from screening import compute_screening_snapshot, load_snapshot, save_snapshot, diff_snapshots

JOB = "screening"


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
    lines = [f"📊 *스크리닝 결과* ({snapshot['date']}) — 국면: {snapshot['regime']} "
             f"(SIDEWAYS 섹터: {snapshot.get('sideways_mode', '-')})"]
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
    # schedule은 정시에 돌지 않고 늦거나 건너뛰어지므로 서로 다른 시각의 슬롯을 여러 개 두고,
    # '마감이 확정된 마지막 미국 거래일' 하나당 한 번만 실행한다. 수동 실행은 검사/기록을 하지 않는다.
    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    session = latest_completed_us_session()
    if not manual:
        if session is None:
            print("미국 정규장이 아직 마감되지 않았거나(또는 일봉 미집계) 시세를 못 불러와 스크리닝을 보류합니다.")
            return
        if already_done(JOB, str(session)):
            print(f"{session} 거래일 스크리닝은 이미 처리했습니다. 건너뜁니다.")
            return

    capital = float(os.environ.get("SCREENING_CAPITAL_USD") or INITIAL_EQUITY)
    mode = (os.environ.get("SIDEWAYS_SECTOR_MODE") or DEFAULT_SIDEWAYS_MODE).strip().upper()
    if mode not in SIDEWAYS_MODES:
        print(f"알 수 없는 SIDEWAYS_SECTOR_MODE={mode!r} - 기본값 {DEFAULT_SIDEWAYS_MODE} 사용")
        mode = DEFAULT_SIDEWAYS_MODE
    new_snapshot = compute_screening_snapshot(capital, sideways_mode=mode)
    old_snapshot = load_snapshot()
    changes = diff_snapshots(old_snapshot, new_snapshot)

    print(f"국면={new_snapshot['regime']} 후보={len(new_snapshot['candidates'])}건 변경={len(changes)}건")
    for c in changes:
        print(c)

    if changes and not send_telegram_message(format_message(new_snapshot, changes)):
        # 발송에 실패했는데 스냅샷을 저장하면 이 변경 사항이 영영 알림되지 않는다.
        # 저장/완료 기록 없이 실패로 끝내 다음 슬롯이 다시 시도하게 한다.
        print("텔레그램 발송 실패 - 스냅샷을 저장하지 않고 다음 실행에서 다시 시도합니다.")
        sys.exit(1)

    save_snapshot(new_snapshot)
    if not manual:
        mark_done(JOB, str(session))


if __name__ == "__main__":
    main()
