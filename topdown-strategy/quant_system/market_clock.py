"""미국 증시(NYSE/Nasdaq) 장 상태 — 순수 함수 (Streamlit 비의존).

정규장 09:30~16:00(미국 동부시간, 서머타임 자동 반영). 휴장일은 NYSE 규칙으로 계산한다:
신정, MLK(1월 3째 월), 대통령의 날(2월 3째 월), 성금요일, 메모리얼(5월 마지막 월), 준틴스(6/19),
독립기념일(7/4), 노동절(9월 첫 월), 추수감사절(11월 4째 목), 성탄절(12/25). 토요일이면 전날, 일요일이면
다음 월요일에 쉰다(단 1/1이 토요일이면 전 해 12/31은 쉬지 않음). 조기 마감(13:00): 추수감사절 다음 날,
12/24(평일), 7/3(7/4가 화~금일 때).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.easter import easter

NY, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
OPEN_T, CLOSE_T, EARLY_CLOSE_T = time(9, 30), time(16, 0), time(13, 0)
PRE_T, POST_END_T = time(4, 0), time(20, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict:
    """{날짜: 휴장 이름}. year 해의 휴장일."""
    h = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                      # 토요일이면 쉬지 않는다
        h[_observed(ny)] = "신정"
    h[_nth_weekday(year, 1, 0, 3)] = "마틴 루터 킹 데이"
    h[_nth_weekday(year, 2, 0, 3)] = "대통령의 날"
    h[easter(year) - timedelta(days=2)] = "성금요일"
    h[_last_weekday(year, 5, 0)] = "메모리얼 데이"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "준틴스"
    h[_observed(date(year, 7, 4))] = "독립기념일"
    h[_nth_weekday(year, 9, 0, 1)] = "노동절"
    h[_nth_weekday(year, 11, 3, 4)] = "추수감사절"
    h[_observed(date(year, 12, 25))] = "성탄절"
    return h


def holiday_name(d: date) -> str | None:
    # 다음 해 1/1이 금요일이면 그 휴장은 다음 해 목록에 있으므로 두 해를 모두 본다
    for y in (d.year, d.year + 1):
        n = nyse_holidays(y).get(d)
        if n:
            return n
    return None


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and holiday_name(d) is None


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    if d.month == 12 and d.day == 24:
        return True
    return d.month == 7 and d.day == 3 and date(d.year, 7, 4).weekday() in (1, 2, 3, 4)


def session(d: date) -> tuple[datetime, datetime] | None:
    """거래일이면 (정규장 시작, 마감) — 미국 동부 시각."""
    if not is_trading_day(d):
        return None
    close_t = EARLY_CLOSE_T if is_early_close(d) else CLOSE_T
    return datetime.combine(d, OPEN_T, NY), datetime.combine(d, close_t, NY)


def next_open(after: datetime) -> tuple[datetime, date]:
    """after 이후 첫 정규장 시작(미국 시각)과 그 날짜."""
    d = after.astimezone(NY).date()
    for _ in range(14):
        s = session(d)
        if s and s[0] > after:
            return s[0], d
        d += timedelta(days=1)
    raise RuntimeError("다음 개장일을 찾지 못했습니다.")


def market_status(now: datetime) -> dict:
    """현재 장 상태.

    state: 'open'(정규장) / 'pre'(프리마켓) / 'post'(애프터마켓) / 'closed'(그 외·휴장)
    target: 다음 이벤트 시각(open→마감, 그 외→다음 정규장 개장), seconds: 남은 초
    """
    now = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
    today = now.date()
    sess = session(today)
    out = {"now": now, "holiday": holiday_name(today) if today.weekday() < 5 else None, "early": False}
    if sess and sess[0] <= now < sess[1]:
        out.update(state="open", target=sess[1], target_name="마감", early=is_early_close(today))
    else:
        if sess and now < sess[0] and now.time() >= PRE_T:
            state = "pre"
        elif sess and now >= sess[1] and now.time() < POST_END_T:
            state = "post"
        else:
            state = "closed"
        nxt, nd = next_open(now)
        out.update(state=state, target=nxt, target_name="개장", next_open_date=nd)
    out["seconds"] = max(0, int((out["target"] - now).total_seconds()))
    cur = sess or session(out.get("next_open_date", today))
    if cur:
        out["open_kst"], out["close_kst"] = cur[0].astimezone(KST), cur[1].astimezone(KST)
    return out


def fmt_countdown(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return (f"{d}일 " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"
