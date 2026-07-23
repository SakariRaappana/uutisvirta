from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# All tunable constants in one place — easy to move to yaml config later.
HOMEPAGE_WINDOW_DAYS = 7      # Rolling window for candidate loading
HOMEPAGE_MIN_SCORE = 75       # Effective score threshold for display
HOMEPAGE_MAX_TOTAL = 15       # Hard cap on total homepage items
HOMEPAGE_MAX_PER_STREAM = 4   # Cap per stream section
HOMEPAGE_AGE_PENALTY = 3      # Points deducted per day of age


@dataclass
class HomepageCandidate:
    id: str
    stream_slug: str
    stream_name: str
    title: str
    source_name: str
    source_url: str
    published_at: datetime | None
    digest_date: date
    digest_url: str
    summary: str
    why_relevant: str
    category: str
    homepage_eligible: bool
    homepage_score: int
    keep_days: int
    personal_relevance: int
    strategic_importance: int
    actionability: int
    novelty: int
    event_key: str


def load_candidates(
    output_dir: Path,
    stream_configs: list[dict],
    today: date,
    window_days: int = HOMEPAGE_WINDOW_DAYS,
) -> list[HomepageCandidate]:
    """Read JSON manifests from last window_days days across all streams."""
    cutoff = today - timedelta(days=window_days)
    candidates: list[HomepageCandidate] = []

    for cfg in stream_configs:
        slug = cfg["slug"]
        name = cfg["name"]
        stream_dir = output_dir / slug
        if not stream_dir.exists():
            continue

        for json_path in sorted(stream_dir.glob("????-??-??.json"), reverse=True):
            try:
                digest_date = date.fromisoformat(json_path.stem)
            except ValueError:
                continue
            if digest_date <= cutoff:
                continue

            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Skipping unreadable manifest %s: %s", json_path, exc)
                continue

            digest_url = f"{slug}/{json_path.stem}.html"

            for raw in data.get("homepage_candidates", []):
                if not isinstance(raw, dict):
                    continue
                if not raw.get("homepage_eligible", False):
                    continue

                published_at = None
                if raw.get("published_at"):
                    try:
                        published_at = datetime.fromisoformat(raw["published_at"])
                    except ValueError:
                        pass

                candidate_id = hashlib.md5(
                    f"{slug}:{json_path.stem}:{raw.get('source_url', raw.get('title', ''))}".encode()
                ).hexdigest()[:12]

                candidates.append(HomepageCandidate(
                    id=candidate_id,
                    stream_slug=slug,
                    stream_name=name,
                    title=raw.get("title", ""),
                    source_name=raw.get("source_name", ""),
                    source_url=raw.get("source_url", ""),
                    published_at=published_at,
                    digest_date=digest_date,
                    digest_url=digest_url,
                    summary=raw.get("summary", ""),
                    why_relevant=raw.get("why_relevant", ""),
                    category=raw.get("category", "tärkea"),
                    homepage_eligible=True,
                    homepage_score=int(raw.get("homepage_score", 0)),
                    keep_days=max(1, min(7, int(raw.get("keep_days", 3)))),
                    personal_relevance=int(raw.get("personal_relevance", 3)),
                    strategic_importance=int(raw.get("strategic_importance", 3)),
                    actionability=int(raw.get("actionability", 3)),
                    novelty=int(raw.get("novelty", 3)),
                    event_key=raw.get("event_key", ""),
                ))

    log.info("Loaded %d homepage candidates from %d streams", len(candidates), len(stream_configs))
    return candidates


def _title_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if len(tokens_a) < 4 or len(tokens_b) < 4:
        return False
    overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
    return overlap >= threshold


def deduplicate_candidates(candidates: list[HomepageCandidate]) -> list[HomepageCandidate]:
    """Deduplicate by event_key, then URL, then title similarity. Keeps newest per event."""
    # Newest first — first occurrence wins when deduplicating
    ordered = sorted(candidates, key=lambda c: c.digest_date, reverse=True)

    seen_urls: set[str] = set()
    seen_event_keys: set[str] = set()
    kept: list[HomepageCandidate] = []

    for c in ordered:
        if c.source_url and c.source_url in seen_urls:
            continue
        if c.event_key and c.event_key in seen_event_keys:
            continue
        if any(_title_similar(c.title, k.title) for k in kept):
            continue

        if c.source_url:
            seen_urls.add(c.source_url)
        if c.event_key:
            seen_event_keys.add(c.event_key)
        kept.append(c)

    log.info("After deduplication: %d candidates (was %d)", len(kept), len(candidates))
    return kept


def effective_score(candidate: HomepageCandidate, today: date, age_penalty: int = HOMEPAGE_AGE_PENALTY) -> float:
    age_days = (today - candidate.digest_date).days
    return candidate.homepage_score - age_days * age_penalty


def select_items(
    candidates: list[HomepageCandidate],
    today: date,
    min_score: int = HOMEPAGE_MIN_SCORE,
    max_total: int = HOMEPAGE_MAX_TOTAL,
    max_per_stream: int = HOMEPAGE_MAX_PER_STREAM,
    age_penalty: int = HOMEPAGE_AGE_PENALTY,
    window_days: int = HOMEPAGE_WINDOW_DAYS,
) -> list[HomepageCandidate]:
    """Filter by eligibility and age, sort by effective score, apply caps."""
    eligible: list[tuple[float, HomepageCandidate]] = []

    for c in candidates:
        age_days = (today - c.digest_date).days
        # Hard age limits
        if age_days >= window_days:
            continue
        if age_days >= c.keep_days:
            continue
        score = c.homepage_score - age_days * age_penalty
        if score < min_score:
            continue
        eligible.append((score, c))

    eligible.sort(key=lambda x: -x[0])

    stream_counts: dict[str, int] = {}
    selected: list[HomepageCandidate] = []

    for score, c in eligible:
        if len(selected) >= max_total:
            break
        count = stream_counts.get(c.stream_slug, 0)
        if count >= max_per_stream:
            continue
        stream_counts[c.stream_slug] = count + 1
        selected.append(c)

    log.info("Selected %d homepage items from %d eligible", len(selected), len(eligible))
    return selected


def _age_label(age_days: int) -> str:
    if age_days == 0:
        return "tänään"
    if age_days == 1:
        return "eilen"
    return f"{age_days} pv sitten"


def _as_template_dict(c: HomepageCandidate, today: date) -> dict:
    age_days = (today - c.digest_date).days
    return {
        "title": c.title,
        "source_name": c.source_name,
        "source_url": c.source_url,
        "digest_url": c.digest_url,
        "why_relevant": c.why_relevant,
        "stream_slug": c.stream_slug,
        "stream_name": c.stream_name,
        "age_label": _age_label(age_days),
        "is_new": age_days == 0,
    }


def build_homepage(
    stream_configs: list[dict],
    output_dir: Path,
    today: date,
) -> None:
    candidates = load_candidates(output_dir, stream_configs, today)
    candidates = deduplicate_candidates(candidates)
    selected = select_items(candidates, today)

    # Build stream nav (same shape as other templates)
    all_streams = [{"slug": c["slug"], "name": c["name"]} for c in stream_configs]

    # Featured = highest effective score
    featured = _as_template_dict(selected[0], today) if selected else None

    # Group into sections ordered by highest per-stream score, excluding featured from its section
    stream_order: list[str] = []
    stream_items: dict[str, list[dict]] = {}

    for idx, c in enumerate(selected):
        slug = c.stream_slug
        template_dict = _as_template_dict(c, today)
        # Skip the featured item from its own section if it would appear alone
        if idx == 0:
            stream_items.setdefault(slug, [])
            if slug not in stream_order:
                stream_order.append(slug)
            # Still add featured to section so section header is rendered; template decides display
            stream_items[slug].append(template_dict)
        else:
            if slug not in stream_order:
                stream_order.append(slug)
            stream_items.setdefault(slug, []).append(template_dict)

    # Add streams that have no items so their empty-state is shown
    for cfg in stream_configs:
        if cfg["slug"] not in stream_order:
            stream_order.append(cfg["slug"])

    slug_to_name = {c["slug"]: c["name"] for c in stream_configs}
    sections = [
        {
            "stream_slug": slug,
            "stream_name": slug_to_name.get(slug, slug),
            # Items excluding the featured one so it isn't double-shown
            "items": [
                item for item in stream_items.get(slug, [])
                if not (featured and item["title"] == featured["title"])
            ],
        }
        for slug in stream_order
    ]

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("homepage.html.j2").render(
        today=today.isoformat(),
        featured=featured,
        sections=sections,
        all_streams=all_streams,
        current_slug=None,
        root_path="",
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("Wrote homepage with %d items across %d streams", len(selected), len(stream_order))
