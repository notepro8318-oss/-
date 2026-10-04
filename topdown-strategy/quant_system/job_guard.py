"""
정기 알림 작업의 "하루(또는 거래일) 1회 발송" 보장 장치
====================================================
GitHub Actions의 schedule은 정시에 실행되지 않고 수 시간 늦어지거나 건너뛰어지는 일이 잦다.
그래서 같은 작업을 서로 다른 시각의 슬롯 여러 개로 등록하고, 먼저 도착한 슬롯이 발송한 뒤
"이 키(날짜)는 처리 완료"라고 기록하면 나머지 슬롯은 건너뛰도록 한다.

- 키는 작업이 정한 문자열(뉴스 브리핑=한국 날짜, 보유현황/스크리닝=마지막 미국 거래일).
- 기록은 GitHub Contents API(github_store)에 작업별 JSON 파일로 저장한다. 토큰이 없는
  로컬 환경에서는 로컬 파일(JOB_STATE_LOCAL_DIR 또는 job_state/)로 폴백한다.
- 상태를 읽지 못하면(API 오류 등) "처리 안 됨"으로 간주한다. 중복 발송이 누락보다 낫다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import github_store

REPO_STATE_DIR = "topdown-strategy/quant_system/job_state"
LOCAL_STATE_DIR = os.path.join(os.path.dirname(__file__), "job_state")


def _repo_path(job: str) -> str:
    return f"{REPO_STATE_DIR}/{job}.json"


def _local_path(job: str) -> str:
    return os.path.join(os.environ.get("JOB_STATE_LOCAL_DIR") or LOCAL_STATE_DIR, f"{job}.json")


def last_done_key(job: str) -> str | None:
    try:
        if github_store.is_configured():
            content, _ = github_store.get_file(_repo_path(job))
        else:
            path = _local_path(job)
            content = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        return json.loads(content).get("last_key") if content else None
    except Exception as e:
        print(f"[job_guard] {job} 상태 조회 실패 - 처리 안 된 것으로 간주합니다: {e}")
        return None


def already_done(job: str, key: str) -> bool:
    return last_done_key(job) == key


def mark_done(job: str, key: str) -> None:
    """key를 처리 완료로 기록한다. 기록에 실패해도 예외를 던지지 않는다(이미 발송은 끝났으므로)."""
    body = json.dumps({"last_key": key, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                      ensure_ascii=False, indent=2)
    try:
        if github_store.is_configured():
            _, sha = github_store.get_file(_repo_path(job))
            github_store.put_file(_repo_path(job), body, f"Job state: {job} done for {key}", sha=sha)
        else:
            path = _local_path(job)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
    except Exception as e:
        print(f"[job_guard] {job} 완료 기록 실패(중복 발송 가능성): {e}")


# ---------------------------------------------------------------------------
# 미국 정규장 마감 판정 (보유현황 리포트 / 스크리닝 공용)
# ---------------------------------------------------------------------------
CLOSE_BUFFER_MIN = 20  # 마감(16:00 ET) 후 일봉이 확정되도록 두는 여유


def latest_completed_us_session(now_utc: datetime | None = None, bars=None):
    """가장 최근에 '마감이 확정된' 미국 거래일(SPY 마지막 일봉 날짜)을 반환한다.

    - 마지막 일봉이 오늘(미국 동부시간)이고 아직 16:20 ET 전이면 그 봉은 장중 값이므로 None.
    - 휴장일이면 마지막 일봉이 이전 거래일이라 그 날짜가 그대로 반환된다(이미 처리했다면 키 중복으로 건너뜀).
    - 시세 조회 실패 시 None.
    bars는 테스트용으로 일봉 DataFrame을 직접 넘길 수 있다.
    """
    from zoneinfo import ZoneInfo
    from data import fetch_ohlcv
    from qs_config import BENCHMARK

    df = bars if bars is not None else fetch_ohlcv(BENCHMARK, period="5d")
    if df is None or df.empty:
        return None
    last_bar = df.index[-1].date()
    ny_now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if last_bar == ny_now.date() and (ny_now.hour, ny_now.minute) < (16, CLOSE_BUFFER_MIN):
        return None
    return last_bar
