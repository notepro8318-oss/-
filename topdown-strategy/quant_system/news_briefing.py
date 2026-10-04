"""
GitHub Actions 스케줄러 진입점 — 매일 06:00(KST) 보유 종목 뉴스 브리핑을 Telegram으로 전송
====================================================
매매일지의 진행중(OPEN) 종목별로 **발행 시각 기준 최근 24시간 이내** 기사만 수집하고,
Gemini로 관련 기사 선별 + 호재/악재 판단 + 한국어 요약을 만들어 보낸다.
종목과 관련된 기사가 없으면 '새로운 뉴스 없음'으로 표시하고, 매일 발송한다.
영어 기사는 Gemini가 제목·요약을 한국어로 옮기며, 시장 전반 시황은 제외하고 종목 관련 기사만 쓴다.

Gemini 키가 없거나 호출이 실패하면 요약 없이 기사 제목(링크)만 보낸다.

필요 환경변수 (GitHub Actions repo secrets):
- GITHUB_TOKEN: 매매일지 CSV 읽기용
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: 발송용
- GEMINI_API_KEY: 요약용 (선택 — 없으면 제목만 발송)
- GEMINI_MODEL (선택, repo Variable): 기본 gemini-3.8-flash
- NEWS_WINDOW_HOURS (선택): 기사 수집 시간 범위, 기본 24
- DRY_RUN=1: 텔레그램으로 보내지 않고 내용만 출력
"""

from __future__ import annotations

import html
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from journal import load_trades
from news_sources import Article, collect_news, company_name
from news_summarize import summarize, DEFAULT_MODEL
from notify import send_telegram_message

KST = ZoneInfo("Asia/Seoul")
TELEGRAM_LIMIT = 3800
SENTIMENT_EMOJI = {"호재": "🟢", "악재": "🔴", "중립": "⚪"}
MAX_LINKS = 3
FOOTER = "※ 발행 24시간 이내 기사만 집계 · Gemini AI 요약은 참고용이며 오류가 있을 수 있으니 투자 판단 전 원문을 확인하세요."


NO_NEWS = "새로운 뉴스 없음"


def _link(a: Article, title_ko: str | None = None) -> str:
    when = a.published.astimezone(KST).strftime("%m/%d %H:%M")
    src = f"{html.escape(a.publisher)}, " if a.publisher else ""
    return (f'🔗 <a href="{html.escape(a.url, quote=True)}">{html.escape(title_ko or a.title)}</a> '
            f"<i>({src}{when} KST)</i>")


def format_section(ticker: str, articles: list[Article], summary: dict | None, error: bool = False) -> str:
    """종목 하나의 브리핑 블록(HTML). summary가 None이면 제목만 보여주는 폴백."""
    if error:
        return f"⚠️ <b>{html.escape(ticker)}</b> · 기사 조회 실패 (일시 오류)"
    if not articles:
        return f"⚪ <b>{html.escape(ticker)}</b> · {NO_NEWS}"

    if summary is None:  # 요약 불가 -> 제목만
        lines = [f"⚪ <b>{html.escape(ticker)}</b> · 최근 기사 (AI 요약 없음)"]
        lines += [_link(a) for a in articles[:MAX_LINKS]]
        return "\n".join(lines)

    picked = summary["relevant_ids"]
    if not picked:  # 수집은 됐지만 모델이 해당 종목 기사가 아니라고 판단 (시황/타사 기사 등)
        return f"⚪ <b>{html.escape(ticker)}</b> · {NO_NEWS}"
    lines = [f"{SENTIMENT_EMOJI.get(summary['sentiment'], '⚪')} <b>{html.escape(ticker)}</b> · {summary['sentiment']}"]
    lines += [f"• {html.escape(p)}" for p in summary["points"]]
    lines += [_link(articles[i - 1], summary["titles_ko"].get(i)) for i in picked[:MAX_LINKS]]
    return "\n".join(lines)


def chunk_messages(header: str, sections: list[str], footer: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    messages, current = [], header
    for section in sections + [footer]:
        if len(current) + len(section) + 2 > limit:
            messages.append(current)
            current = section
        else:
            current += "\n\n" + section
    messages.append(current)
    return messages


def build_briefing(tickers: list[str], news: dict[str, list[Article]], errors: set[str],
                   summaries: dict[str, dict] | None, now: datetime) -> list[str]:
    header = f"📰 <b>보유 종목 뉴스 브리핑</b> — {now.astimezone(KST):%Y-%m-%d %H:%M} KST"
    sections = []
    for t in tickers:
        summary = summaries.get(t) if summaries is not None else None
        sections.append(format_section(t, news.get(t, []), summary, error=t in errors))
    return chunk_messages(header, sections, html.escape(FOOTER))


def main() -> None:
    now = datetime.now(timezone.utc)
    hours = float(os.environ.get("NEWS_WINDOW_HOURS") or 24)
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    trades = load_trades()
    open_trades = trades[(trades["status"] == "OPEN") & (~trades["archived"])] if not trades.empty else trades
    if open_trades.empty:
        print("진행중인 보유 종목이 없습니다.")
        msg = (f"📰 <b>보유 종목 뉴스 브리핑</b> — {now.astimezone(KST):%Y-%m-%d %H:%M} KST\n\n"
               "현재 진행중인 보유 종목이 없습니다.")
        if not dry_run and not send_telegram_message(msg, parse_mode="HTML", disable_web_page_preview=True):
            sys.exit(1)
        return
    names_hint = {}
    for _, t in open_trades.iterrows():
        names_hint.setdefault(t["ticker"], str(t["asset_name"]) if isinstance(t["asset_name"], str) else "")
    tickers = list(names_hint)

    names, news, errors = {}, {}, set()
    for t in tickers:
        names[t] = company_name(t, names_hint[t])
        try:
            news[t] = collect_news(t, names[t], now, hours)
        except Exception as e:
            print(f"[{t}] 기사 수집 실패: {e}")
            errors.add(t)
        time.sleep(0.5)
        print(f"[{t}] {names[t]} — 최근 {hours:g}시간 기사 {len(news.get(t, []))}건")

    with_news = {t: a for t, a in news.items() if a}
    summaries = None
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if with_news and api_key:
        try:
            model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
            summaries = summarize(with_news, names, api_key, model, debug=dry_run)
            print(f"Gemini 요약 완료: {len(summaries)}종목")
        except Exception as e:
            print(f"Gemini 요약 실패 - 제목만 발송합니다: {e}")
    elif with_news:
        print("GEMINI_API_KEY 미설정 - 제목만 발송합니다.")
    else:
        summaries = {}  # 요약할 기사가 없으면 모든 종목이 '새로운 뉴스 없음'

    messages = build_briefing(tickers, news, errors, summaries, now)
    ok = True
    for text in messages:
        print(text, "\n")
        if dry_run:
            continue
        if not send_telegram_message(text, parse_mode="HTML", disable_web_page_preview=True):
            ok = False
            break
    if not ok:
        print("텔레그램 발송 실패")
        sys.exit(1)  # Actions 실행을 실패로 표시해 알림 장애를 바로 알아볼 수 있게 한다


if __name__ == "__main__":
    main()
