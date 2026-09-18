"""
GitHub Contents API 기반 공유 저장소
====================================================
Streamlit Cloud(라이브 대시보드)와 GitHub Actions(장중/마감 후 시그널 스캔
스케줄러)는 서로 다른 실행 환경이라 로컬 파일시스템을 공유하지 못한다.
두 곳 모두 같은 매매일지 상태를 읽고 쓸 수 있도록, 저장소의 CSV 파일을
GitHub Contents API를 통해 "공유 DB"처럼 사용한다.

GITHUB_TOKEN(레포 contents 쓰기 권한)이 설정되지 않은 환경(로컬 테스트 등)
에서는 자동으로 로컬 CSV 파일로 폴백한다.
"""

from __future__ import annotations

import base64
import os

import requests

DEFAULT_REPO = "notepro8318-oss/-"
DEFAULT_BRANCH = "main"
API_BASE = "https://api.github.com"


def _get_secret(name: str) -> str | None:
    """환경변수 또는 Streamlit secrets에서 값을 읽는다 (있는 쪽 우선순위: env > st.secrets)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st
        return st.secrets.get(name)
    except Exception:
        return None


def is_configured() -> bool:
    return bool(_get_secret("GITHUB_TOKEN"))


def _repo() -> str:
    return _get_secret("GITHUB_REPO") or DEFAULT_REPO


def _branch() -> str:
    return _get_secret("GITHUB_BRANCH") or DEFAULT_BRANCH


def _headers() -> dict:
    token = _get_secret("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_file(path: str) -> tuple[str | None, str | None]:
    """Contents API로 파일을 읽는다. Returns (content_str, sha). 없으면 (None, None)."""
    url = f"{API_BASE}/repos/{_repo()}/contents/{path}"
    resp = requests.get(url, headers=_headers(), params={"ref": _branch()}, timeout=15)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def put_file(path: str, content: str, message: str, sha: str | None = None) -> str:
    """Contents API로 파일을 생성/갱신한다. 최신 sha 충돌 시 자동으로 한 번 재시도한다."""
    url = f"{API_BASE}/repos/{_repo()}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": _branch(),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=_headers(), json=payload, timeout=15)
    if resp.status_code == 409:
        # 다른 프로세스가 먼저 갱신함 -> 최신 sha로 한 번 재시도
        _, latest_sha = get_file(path)
        payload["sha"] = latest_sha
        resp = requests.put(url, headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()["content"]["sha"]
