"""
보유 종목 뉴스 수집 — Google News RSS (키 불필요)
====================================================
종목별로 "회사명 + 티커" 검색어를 만들어 Google News RSS에서 기사를 가져오고,
**발행 시각 기준 최근 N시간(기본 24시간) 이내** 기사만 남긴다.

- 발행 시각(pubDate)이 없거나 파싱할 수 없는 기사는 24시간 이내임을 증명할 수 없으므로 버린다.
- Yahoo 시세/종목 소개 페이지 같은 '뉴스가 아닌' 검색 결과는 제목 패턴으로 걸러낸다.
- 기사 본문은 가져오지 않고 제목·출처·발행시각·링크만 사용한다(유료 구독 기사/저작권 이슈 회피).
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = "Mozilla/5.0 (compatible; TopDownQuantSystem-news/1.0)"
# 뉴스 기사가 아닌 시세/종목 소개/토큰화 상품 페이지
NON_NEWS_TITLE = re.compile(r"Stock Price|Quote\s*&\s*History|Stock Forecasts|Tokenized|Price Today", re.I)
_COMPANY_SUFFIX = re.compile(
    r"\b(Inc|Incorporated|Corporation|Corp|Company|Co|Holdings|Holding|Ltd|Limited|plc|Group|The|Class [A-C])\b", re.I)


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    publisher: str
    published: datetime  # timezone-aware (UTC)


def company_name(ticker: str, fallback: str = "") -> str:
    """회사명을 가져온다. 매매일지의 asset_name이 있으면 우선 사용, 없으면 yfinance, 실패 시 티커."""
    if fallback and fallback.strip():
        return fallback.strip()
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return (info.get("longName") or info.get("shortName") or ticker).strip()
    except Exception:
        return ticker


def clean_name(name: str) -> str:
    """'Target Corporation' -> 'Target' 처럼 검색에 불필요한 법인 접미어를 제거한다."""
    cleaned = _COMPANY_SUFFIX.sub(" ", re.sub(r"[,.]", " ", name))
    return re.sub(r"\s+", " ", cleaned).strip() or name


def build_query(ticker: str, name: str) -> str:
    core = clean_name(name)
    if core.upper() == ticker.upper():
        return f'"{ticker}" stock when:1d'
    # 'Target'처럼 일반 단어인 회사명은 티커를 같이 넣어 정확도를 높인다.
    # 1~2글자 티커(V 등)는 오히려 잡음이 많아 회사명 + stock으로 검색한다.
    if len(ticker) >= 3:
        return f'"{core}" {ticker} when:1d'
    return f'"{core}" stock when:1d'


def _parse_rss(content: bytes) -> list[Article]:
    articles = []
    for item in ET.fromstring(content).findall("./channel/item"):
        title, link, pub = item.findtext("title"), item.findtext("link"), item.findtext("pubDate")
        if not title or not link or not pub:
            continue
        try:
            published = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        publisher = (item.findtext("source") or "").strip()
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        articles.append(Article(title.strip(), link.strip(), publisher, published.astimezone(timezone.utc)))
    return articles


def fetch_google_news(query: str, retries: int = 2) -> list[Article]:
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(GOOGLE_NEWS_RSS, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            return _parse_rss(resp.content)
        except (requests.RequestException, ET.ParseError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Google News 조회 실패: {last_err}")


def filter_recent(articles: list[Article], now: datetime, hours: float = 24) -> list[Article]:
    """발행 시각 기준 (now - hours, now] 구간의 기사만 남기고, 같은 기사(제목 중복)는 하나만 둔다."""
    cutoff = now - timedelta(hours=hours)
    seen, kept = set(), []
    for a in sorted(articles, key=lambda x: x.published, reverse=True):
        if not (cutoff < a.published <= now + timedelta(minutes=5)):
            continue
        if NON_NEWS_TITLE.search(a.title):
            continue
        key = re.sub(r"\W+", "", a.title.lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append(a)
    return kept


def rank_articles(articles: list[Article], ticker: str, name: str, limit: int = 12) -> list[Article]:
    """제목에 회사명/티커가 직접 등장하는 기사를 우선하고, 같은 점수끼리는 최신순으로 limit개만 남긴다."""
    core = clean_name(name).lower()
    tick = re.compile(rf"\b{re.escape(ticker)}\b", re.I)

    def score(a: Article) -> int:
        return int(core in a.title.lower()) + int(bool(tick.search(a.title)))

    return sorted(articles, key=lambda a: (score(a), a.published), reverse=True)[:limit]


def collect_news(ticker: str, name: str, now: datetime, hours: float = 24, limit: int = 12) -> list[Article]:
    articles = fetch_google_news(build_query(ticker, name))
    return rank_articles(filter_recent(articles, now, hours), ticker, name, limit)
