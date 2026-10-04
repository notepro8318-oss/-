"""
GitHub Actions 스케줄러 진입점 — 매일 06:00(KST) 보유 종목 뉴스 브리핑을 Telegram으로 전송
====================================================
메시지 구성
1. 🏭 주요 섹터 동향 — 보유 비중이 큰 상위 3개 섹터의 업종 전체 뉴스 요약 (맨 위)
2. 보유 종목별 뉴스 — 매매일지의 진행중(OPEN) 종목별 기사 요약

모든 기사는 **발행 시각 기준 최근 24시간 이내**만 사용한다. Gemini가 관련 기사 선별 + 호재/악재 판단 +
한국어 요약/제목 번역을 하고, 관련 기사가 없으면 '새로운 뉴스 없음'으로 표시한다(매일 발송).
종목 섹션은 그 회사 기사만, 섹터 섹션은 업종 전체 동향 기사만 쓴다.

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

from journal import load_trades, sector_of
from news_sources import (
    Article, collect_news, collect_sector_news, company_name, SECTOR_KO, YF_SECTOR_TO_ETF,
)
from news_summarize import summarize, DEFAULT_MODEL, SECTOR_SYSTEM_PROMPT
from notify import send_telegram_message

KST = ZoneInfo("Asia/Seoul")
TELEGRAM_LIMIT = 3800
SENTIMENT_EMOJI = {"호재": "🟢", "악재": "🔴", "중립": "⚪"}
MAX_LINKS = 3
MAX_SECTORS = 3
NO_NEWS = "새로운 뉴스 없음"
FOOTER = "※ 발행 24시간 이내 기사만 집계 · Gemini AI 요약은 참고용이며 오류가 있을 수 있으니 투자 판단 전 원문을 확인하세요."


def _link(a: Article, title_ko: str | None = None) -> str:
    when = a.published.astimezone(KST).strftime("%m/%d %H:%M")
    src = f"{html.escape(a.publisher)}, " if a.publisher else ""
    return (f'🔗 <a href="{html.escape(a.url, quote=True)}">{html.escape(title_ko or a.title)}</a> '
            f"<i>({src}{when} KST)</i>")


def format_section(label: str, articles: list[Article], summary: dict | None, error: bool = False,
                   suffix: str = "") -> str:
    """종목 또는 섹터 하나의 브리핑 블록(HTML). summary가 None이면 제목만 보여주는 폴백.

    label은 이미 HTML 이스케이프가 필요 없는 평문(티커/섹터명)이며, suffix는 '(비중 58%)' 같은 부가 표기다.
    """
    name = f"<b>{html.escape(label)}</b>{html.escape(suffix)}"
    if error:
        return f"⚠️ {name} · 기사 조회 실패 (일시 오류)"
    if not articles:
        return f"⚪ {name} · {NO_NEWS}"

    if summary is None:  # 요약 불가 -> 제목만
        lines = [f"⚪ {name} · 최근 기사 (AI 요약 없음)"]
        lines += [_link(a) for a in articles[:MAX_LINKS]]
        return "\n".join(lines)

    picked = summary["relevant_ids"]
    if not picked:  # 수집은 됐지만 모델이 관련 기사가 아니라고 판단 (시황/타사 기사 등)
        return f"⚪ {name} · {NO_NEWS}"
    lines = [f"{SENTIMENT_EMOJI.get(summary['sentiment'], '⚪')} {name} · {summary['sentiment']}"]
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


def resolve_sector(ticker: str) -> str:
    """전략 유니버스에 있으면 그 섹터ETF, 없으면 yfinance의 sector 분류로 섹터ETF를 추정한다. 못 찾으면 '기타'."""
    etf = sector_of(ticker)
    if etf != "기타":
        return etf
    try:
        import yfinance as yf
        return YF_SECTOR_TO_ETF.get(yf.Ticker(ticker).info.get("sector") or "", "기타")
    except Exception:
        return "기타"


def top_sectors(open_trades, limit: int = MAX_SECTORS) -> list[tuple[str, float]]:
    """보유 종목의 매입원가(매수가×잔여수량) 기준 섹터 비중 상위 limit개를 [(섹터ETF, 비중%)]로 반환한다."""
    weights: dict[str, float] = {}
    for _, t in open_trades.iterrows():
        etf = resolve_sector(t["ticker"])
        if etf == "기타":
            continue
        weights[etf] = weights.get(etf, 0.0) + float(t["entry_price"]) * int(t["remaining_shares"])
    total = sum(float(t["entry_price"]) * int(t["remaining_shares"]) for _, t in open_trades.iterrows())
    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(etf, w / total * 100 if total > 0 else 0.0) for etf, w in ranked]


def build_briefing(tickers: list[str], news: dict[str, list[Article]], errors: set[str],
                   summaries: dict[str, dict] | None, now: datetime,
                   sectors: list[tuple[str, float]] | None = None,
                   sector_news: dict[str, list[Article]] | None = None, sector_errors: set[str] | None = None,
                   sector_summaries: dict[str, dict] | None = None) -> list[str]:
    header = f"📰 <b>보유 종목 뉴스 브리핑</b> — {now.astimezone(KST):%Y-%m-%d %H:%M} KST"
    sections = []
    for i, (etf, weight) in enumerate(sectors or []):
        block = format_section(f"{etf} {SECTOR_KO.get(etf, '')}".strip(), (sector_news or {}).get(etf, []),
                               sector_summaries.get(etf) if sector_summaries is not None else None,
                               error=etf in (sector_errors or set()), suffix=f" (보유 비중 {weight:.0f}%)")
        sections.append(("🏭 <b>주요 섹터 동향</b>\n\n" + block) if i == 0 else block)
    for t in tickers:
        summary = summaries.get(t) if summaries is not None else None
        sections.append(format_section(t, news.get(t, []), summary, error=t in errors))
    return chunk_messages(header, sections, html.escape(FOOTER))


def _summarize_safely(label: str, news: dict[str, list[Article]], names: dict[str, str], api_key: str,
                      model: str, dry_run: bool, system_prompt: str | None = None) -> dict[str, dict] | None:
    """기사가 있는 항목만 Gemini로 요약한다. 키가 없거나 실패하면 None(제목만 보내는 폴백)."""
    if not news:
        return {}
    if not api_key:
        print(f"GEMINI_API_KEY 미설정 - {label}는 제목만 발송합니다.")
        return None
    try:
        kwargs = {"system_prompt": system_prompt} if system_prompt else {}
        result = summarize(news, names, api_key, model, debug=dry_run, **kwargs)
        print(f"Gemini {label} 요약 완료: {len(result)}건")
        return result
    except Exception as e:
        print(f"Gemini {label} 요약 실패 - 제목만 발송합니다: {e}")
        return None


def main() -> None:
    now = datetime.now(timezone.utc)
    hours = float(os.environ.get("NEWS_WINDOW_HOURS") or 24)
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL

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

    # ---- 보유 종목 뉴스 ----
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
    summaries = _summarize_safely("종목", {t: a for t, a in news.items() if a}, names, api_key, model, dry_run)

    # ---- 주요 섹터 뉴스 ----
    sectors = top_sectors(open_trades)
    print("주요 섹터(보유 비중):", [(e, f"{w:.0f}%") for e, w in sectors])
    sector_news, sector_errors = {}, set()
    for etf, _ in sectors:
        try:
            sector_news[etf] = collect_sector_news(etf, now, hours)
        except Exception as e:
            print(f"[{etf}] 섹터 기사 수집 실패: {e}")
            sector_errors.add(etf)
        time.sleep(0.5)
        print(f"[{etf}] 섹터 최근 {hours:g}시간 기사 {len(sector_news.get(etf, []))}건")
    sector_names = {etf: f"{SECTOR_KO.get(etf, etf)} 섹터({etf})" for etf, _ in sectors}
    sector_summaries = _summarize_safely("섹터", {e: a for e, a in sector_news.items() if a}, sector_names,
                                         api_key, model, dry_run, system_prompt=SECTOR_SYSTEM_PROMPT)

    messages = build_briefing(tickers, news, errors, summaries, now, sectors, sector_news, sector_errors,
                              sector_summaries)
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
