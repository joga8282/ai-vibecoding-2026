from __future__ import annotations

import asyncio
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

POSITIVE_WORDS = ("상승", "회복", "호조", "개선", "성장", "완화", "인하", "수출 증가", "실적 개선")
NEGATIVE_WORDS = ("하락", "침체", "우려", "악화", "긴축", "인상", "전쟁", "관세", "급락", "물가 상승")
KST = ZoneInfo("Asia/Seoul")


class WeeklyReportService:
    """Create daily and weekly reports on fixed Korea-time refresh cycles."""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._cache_cycle: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def cached_direction(self) -> str:
        score = sum(int(report.get("sentiment_score", 0)) for report in self._cache.values())
        return "risk_on" if score >= 2 else "defensive" if score <= -2 else "neutral"

    async def report(self, period: str = "daily", force: bool = False) -> dict:
        if period not in {"daily", "weekly"}:
            raise ValueError("period must be daily or weekly")
        now = datetime.now(timezone.utc)
        cycle, next_refresh = self._cycle(period, now)
        if not force and period in self._cache and self._cache_cycle.get(period) == cycle:
            return self._cache[period]
        async with self._lock:
            if not force and period in self._cache and self._cache_cycle.get(period) == cycle:
                return self._cache[period]
            days = 1 if period == "daily" else 7
            try:
                domestic, global_news = await asyncio.gather(
                    asyncio.to_thread(self._fetch, f"한국 증시 금리 환율 반도체 when:{days}d", days),
                    asyncio.to_thread(self._fetch, f"미국 증시 연준 금리 국제유가 when:{days}d", days),
                )
                report = self._analyze(domestic, global_news, now, period, next_refresh)
            except Exception as exc:
                report = self._fallback(now, period, next_refresh, type(exc).__name__)
            self._cache[period] = report
            self._cache_cycle[period] = cycle
            return report

    @staticmethod
    def _cycle(period: str, now: datetime) -> tuple[str, datetime]:
        local = now.astimezone(KST)
        if period == "daily":
            boundary = local.replace(hour=18, minute=0, second=0, microsecond=0)
            if local < boundary:
                boundary -= timedelta(days=1)
            next_refresh = boundary + timedelta(days=1)
        else:
            days_since_sunday = (local.weekday() + 1) % 7
            boundary = (local - timedelta(days=days_since_sunday)).replace(hour=18, minute=0, second=0, microsecond=0)
            if local < boundary:
                boundary -= timedelta(days=7)
            next_refresh = boundary + timedelta(days=7)
        return boundary.isoformat(), next_refresh.astimezone(timezone.utc)

    @staticmethod
    def _fetch(query: str, days: int) -> list[dict]:
        params = urllib.parse.urlencode({"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        request = urllib.request.Request(f"https://news.google.com/rss/search?{params}", headers={"User-Agent": "PaperTrader/0.2 market-report"})
        with urllib.request.urlopen(request, timeout=8) as response:
            root = ET.fromstring(response.read())
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items: list[dict] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
            except (TypeError, ValueError):
                published = datetime.now(timezone.utc)
            if title and published >= cutoff:
                items.append({"title": title, "url": (item.findtext("link") or "").strip(), "source": (item.findtext("source") or "").strip(), "published_at": published.isoformat()})
        return items

    @staticmethod
    def _unique(items: list[dict], limit: int, seen: set[str]) -> list[dict]:
        result: list[dict] = []
        for item in items:
            normalized = re.sub(r"[^0-9a-z가-힣]", "", item["title"].casefold().split(" - ")[0])
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(item)
            if len(result) == limit:
                break
        return result

    @classmethod
    def _analyze(cls, domestic: list[dict], global_news: list[dict], now: datetime, period: str = "weekly", next_refresh: datetime | None = None) -> dict:
        seen: set[str] = set()
        selected_global = cls._unique(global_news, 3, seen)
        selected_domestic = cls._unique(domestic, 3, seen)
        titles = " ".join(item["title"] for item in selected_domestic + selected_global)
        score = max(-10, min(10, sum(titles.count(w) for w in POSITIVE_WORDS) - sum(titles.count(w) for w in NEGATIVE_WORDS)))
        direction = "risk_on" if score >= 2 else "defensive" if score <= -2 else "neutral"
        guidance = {
            "risk_on": "위험선호 우세: 지지선 확인 종목을 분할 접근하되 추천금액 한도를 유지합니다.",
            "neutral": "중립: 현금 비중을 유지하고 일봉·주봉 지지가 동시에 확인된 종목만 선별합니다.",
            "defensive": "방어 우세: 신규 진입 규모를 줄이고 현금 비중과 손실 제한을 우선합니다.",
        }[direction]
        return {
            "period": period, "generated_at": now.isoformat(), "period_days": 1 if period == "daily" else 7,
            "next_refresh_at": (next_refresh or now).isoformat(), "direction": direction, "sentiment_score": score,
            "guidance": guidance, "domestic": selected_domestic, "global": selected_global,
            "source_note": "Google News RSS 제목을 중복 제거 후 키워드 방식으로 분석했습니다.", "available": True,
        }

    @staticmethod
    def _fallback(now: datetime, period: str, next_refresh: datetime, error: str) -> dict:
        return {
            "period": period, "generated_at": now.isoformat(), "period_days": 1 if period == "daily" else 7,
            "next_refresh_at": next_refresh.isoformat(), "direction": "neutral", "sentiment_score": 0,
            "guidance": "뉴스를 불러오지 못해 중립으로 처리했습니다. 기술적 지지선과 위험 한도만 적용합니다.",
            "domestic": [], "global": [], "source_note": f"뉴스 수집 실패: {error}", "available": False,
        }
