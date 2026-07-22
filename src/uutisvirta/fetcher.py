from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import feedparser
import httpx

log = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    url: str
    summary: str
    published_dt: datetime
    source_name: str
    source_type: str  # "rss" | "newsapi"


def fetch(stream_config: dict, newsapi_key: str | None = None) -> list[NewsItem]:
    cfg = stream_config.get("digest_config", {})
    lookback_hours = cfg.get("lookback_hours", 26)
    max_per_source = cfg.get("max_rss_items_per_source", 20)
    max_final = cfg.get("max_final_items", 30)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)

    items: list[NewsItem] = []

    for source in stream_config.get("rss_sources", []):
        try:
            fetched = _fetch_rss(source, cutoff, max_per_source)
            items.extend(fetched)
            log.info("RSS %s: %d items", source["name"], len(fetched))
        except Exception as exc:
            log.warning("RSS fetch failed for %s: %s", source.get("name"), exc)

    if newsapi_key:
        for keyword in stream_config.get("newsapi_keywords", []):
            try:
                fetched = _fetch_newsapi(keyword, newsapi_key, cutoff)
                items.extend(fetched)
                log.info("NewsAPI '%s': %d items", keyword, len(fetched))
            except Exception as exc:
                log.warning("NewsAPI fetch failed for '%s': %s", keyword, exc)
    else:
        if stream_config.get("newsapi_keywords"):
            log.info("NEWSAPI_KEY not set — skipping keyword searches")

    items = _deduplicate(items)
    items.sort(key=lambda x: x.published_dt, reverse=True)
    return items[:max_final]


def _fetch_rss(source: dict, cutoff: datetime, max_items: int) -> list[NewsItem]:
    parsed = feedparser.parse(source["url"])
    items = []
    for entry in parsed.entries[:max_items]:
        published = _parse_feed_date(entry)
        if published and published < cutoff:
            continue
        summary = (
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or ""
        )
        summary = _strip_html(summary)[:500]
        items.append(NewsItem(
            title=entry.get("title", "").strip(),
            url=entry.get("link", ""),
            summary=summary,
            published_dt=published or datetime.now(tz=timezone.utc),
            source_name=source["name"],
            source_type="rss",
        ))
    return items


def _fetch_newsapi(keyword: str, api_key: str, cutoff: datetime) -> list[NewsItem]:
    from_date = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = httpx.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": keyword,
            "from": from_date,
            "sortBy": "publishedAt",
            "pageSize": 20,
            "language": "en",
        },
        headers={"X-Api-Key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    items = []
    for article in data.get("articles", []):
        if article.get("title") in ("[Removed]", None, ""):
            continue
        published = _parse_iso(article.get("publishedAt", ""))
        items.append(NewsItem(
            title=article["title"].strip(),
            url=article.get("url", ""),
            summary=(article.get("description") or "")[:500],
            published_dt=published or datetime.now(tz=timezone.utc),
            source_name=article.get("source", {}).get("name", "NewsAPI"),
            source_type="newsapi",
        ))
    return items


def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    seen_urls: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        # title similarity check against already-kept items
        if _is_duplicate_title(item.title, unique):
            continue
        unique.append(item)
    return unique


def _is_duplicate_title(title: str, existing: list[NewsItem]) -> bool:
    tokens_a = set(title.lower().split())
    if len(tokens_a) < 4:
        return False
    for item in existing:
        tokens_b = set(item.title.lower().split())
        if not tokens_b:
            continue
        overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        if overlap >= 0.8:
            return True
    return False


def _parse_feed_date(entry: Any) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                import time
                ts = time.mktime(val)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    return None


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text).strip()
