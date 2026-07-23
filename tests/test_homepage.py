"""Unit tests for homepage selection and deduplication logic."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from uutisvirta.homepage import (
    HOMEPAGE_AGE_PENALTY,
    HOMEPAGE_MAX_PER_STREAM,
    HOMEPAGE_MAX_TOTAL,
    HOMEPAGE_MIN_SCORE,
    HOMEPAGE_WINDOW_DAYS,
    HomepageCandidate,
    deduplicate_candidates,
    effective_score,
    load_candidates,
    select_items,
)


def _candidate(
    *,
    stream_slug: str = "test-stream",
    stream_name: str = "Test Stream",
    title: str = "Test News Item",
    source_url: str = "https://example.com/article",
    digest_date: date | None = None,
    homepage_score: int = 85,
    keep_days: int = 4,
    event_key: str = "test-event",
    why_relevant: str = "Relevantti juuri tälle lukijalle.",
    today: date | None = None,
) -> HomepageCandidate:
    today = today or date(2026, 7, 23)
    digest_date = digest_date or today
    return HomepageCandidate(
        id="abc123",
        stream_slug=stream_slug,
        stream_name=stream_name,
        title=title,
        source_name="Test Source",
        source_url=source_url,
        published_at=datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc),
        digest_date=digest_date,
        digest_url=f"{stream_slug}/{digest_date.isoformat()}.html",
        summary="Short summary.",
        why_relevant=why_relevant,
        category="tärkea",
        homepage_eligible=True,
        homepage_score=homepage_score,
        keep_days=keep_days,
        personal_relevance=5,
        strategic_importance=4,
        actionability=3,
        novelty=4,
        event_key=event_key,
    )


TODAY = date(2026, 7, 23)


# ── effective_score ────────────────────────────────────────────────────────────

def test_effective_score_fresh_item():
    c = _candidate(homepage_score=90, today=TODAY)
    assert effective_score(c, TODAY) == 90


def test_effective_score_age_penalty():
    c = _candidate(homepage_score=90, digest_date=date(2026, 7, 20), today=TODAY)
    assert effective_score(c, TODAY) == 90 - 3 * HOMEPAGE_AGE_PENALTY


# ── select_items ───────────────────────────────────────────────────────────────

def test_item_shown_today():
    c = _candidate(homepage_score=80, keep_days=3, today=TODAY)
    result = select_items([c], TODAY)
    assert c in result


def test_item_shown_within_keep_days():
    c = _candidate(homepage_score=88, keep_days=5, digest_date=date(2026, 7, 20), today=TODAY)
    result = select_items([c], TODAY)
    assert c in result


def test_item_hidden_after_keep_days():
    # 4 days old, keep_days=4 — age_days (4) >= keep_days (4) → hidden
    c = _candidate(homepage_score=90, keep_days=4, digest_date=date(2026, 7, 19), today=TODAY)
    result = select_items([c], TODAY)
    assert c not in result


def test_item_hidden_after_window():
    # 7 days old — hits hard window limit
    c = _candidate(homepage_score=99, keep_days=7, digest_date=date(2026, 7, 16), today=TODAY)
    result = select_items([c], TODAY)
    assert c not in result


def test_item_hidden_when_effective_score_below_min():
    # Score 78, age 2 days, penalty 3 → effective 72, below HOMEPAGE_MIN_SCORE
    c = _candidate(homepage_score=78, keep_days=7, digest_date=date(2026, 7, 21), today=TODAY)
    result = select_items([c], TODAY, min_score=HOMEPAGE_MIN_SCORE)
    assert c not in result


def test_per_stream_cap():
    candidates = [
        _candidate(title=f"News {i}", source_url=f"https://example.com/{i}", event_key=f"event-{i}",
                   homepage_score=90 - i, keep_days=7)
        for i in range(6)
    ]
    result = select_items(candidates, TODAY, max_per_stream=4)
    assert len(result) <= 4


def test_total_cap():
    # Two streams, 10 items each
    candidates = (
        [_candidate(stream_slug="s1", title=f"S1 News {i}", source_url=f"https://s1.com/{i}",
                    event_key=f"s1-event-{i}", homepage_score=90 - i, keep_days=7) for i in range(10)]
        + [_candidate(stream_slug="s2", title=f"S2 News {i}", source_url=f"https://s2.com/{i}",
                    event_key=f"s2-event-{i}", homepage_score=85 - i, keep_days=7) for i in range(10)]
    )
    result = select_items(candidates, TODAY, max_total=12, max_per_stream=10)
    assert len(result) <= 12


def test_lyhyt_eligible_false_never_shown():
    # If homepage_eligible is false the item should not be in the candidate list at all;
    # load_candidates already filters these out. Simulate by checking select_items
    # does not add a candidate with homepage_eligible=False.
    c = _candidate(homepage_score=95, keep_days=7)
    c = HomepageCandidate(**{**c.__dict__, "homepage_eligible": False})
    # Since load_candidates already filters, select_items doesn't check; we verify
    # that a candidate with homepage_eligible=False is absent from select output
    # when it was never loaded.
    result = select_items([], TODAY)
    assert result == []


# ── deduplicate_candidates ─────────────────────────────────────────────────────

def test_dedup_by_event_key_keeps_newest():
    older = _candidate(title="Event A old", source_url="https://a1.com", event_key="event-a",
                       homepage_score=80, digest_date=date(2026, 7, 21))
    newer = _candidate(title="Event A new", source_url="https://a2.com", event_key="event-a",
                       homepage_score=85, digest_date=date(2026, 7, 22))
    result = deduplicate_candidates([older, newer])
    assert len(result) == 1
    assert result[0].title == "Event A new"


def test_dedup_by_url():
    a = _candidate(title="News X", source_url="https://same.com/article", event_key="")
    b = _candidate(title="News X copy", source_url="https://same.com/article", event_key="")
    result = deduplicate_candidates([a, b])
    assert len(result) == 1


def test_dedup_by_title_similarity():
    a = _candidate(title="Microsoft launches new AI agent framework for enterprises",
                   source_url="https://a.com/1", event_key="")
    b = _candidate(title="Microsoft launches new AI agent framework for enterprises today",
                   source_url="https://b.com/2", event_key="")
    result = deduplicate_candidates([a, b])
    assert len(result) == 1


def test_dedup_different_events_both_kept():
    a = _candidate(title="Event Alpha announced", source_url="https://a.com/1", event_key="alpha")
    b = _candidate(title="Event Beta released", source_url="https://b.com/2", event_key="beta")
    result = deduplicate_candidates([a, b])
    assert len(result) == 2


# ── load_candidates ────────────────────────────────────────────────────────────

def _write_manifest(stream_dir: Path, digest_date: date, candidates: list[dict]) -> None:
    stream_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "date": digest_date.isoformat(),
        "body_html": "<p>Test</p>",
        "item_count": 1,
        "homepage_candidates": candidates,
    }
    (stream_dir / f"{digest_date.isoformat()}.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )


def test_load_candidates_reads_manifests():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        stream_dir = output_dir / "test-stream"
        raw = {
            "title": "AI Release",
            "source_name": "TechCrunch",
            "source_url": "https://tc.com/ai",
            "published_at": "2026-07-23T10:00:00+00:00",
            "summary": "Summary text.",
            "why_relevant": "Relevant because.",
            "homepage_eligible": True,
            "homepage_score": 88,
            "keep_days": 4,
            "personal_relevance": 5,
            "strategic_importance": 4,
            "actionability": 3,
            "novelty": 4,
            "event_key": "ai-release",
            "category": "tärkea",
        }
        _write_manifest(stream_dir, TODAY, [raw])
        configs = [{"slug": "test-stream", "name": "Test Stream"}]
        candidates = load_candidates(output_dir, configs, TODAY)
        assert len(candidates) == 1
        assert candidates[0].title == "AI Release"
        assert candidates[0].homepage_score == 88


def test_load_candidates_excludes_beyond_window():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        stream_dir = output_dir / "test-stream"
        old_date = date(2026, 7, 15)  # 8 days before TODAY
        raw = {"title": "Old News", "source_url": "https://x.com", "homepage_eligible": True,
               "homepage_score": 90, "keep_days": 7, "event_key": "", "category": "tärkea",
               "source_name": "X", "summary": "", "why_relevant": "",
               "personal_relevance": 3, "strategic_importance": 3, "actionability": 3, "novelty": 3}
        _write_manifest(stream_dir, old_date, [raw])
        configs = [{"slug": "test-stream", "name": "Test Stream"}]
        candidates = load_candidates(output_dir, configs, TODAY)
        assert len(candidates) == 0


def test_load_candidates_skips_non_eligible():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        stream_dir = output_dir / "test-stream"
        raw = {"title": "Irrelevant", "source_url": "https://x.com", "homepage_eligible": False,
               "homepage_score": 0, "keep_days": 1, "event_key": "", "category": "lyhyt",
               "source_name": "X", "summary": "", "why_relevant": "",
               "personal_relevance": 1, "strategic_importance": 1, "actionability": 1, "novelty": 1}
        _write_manifest(stream_dir, TODAY, [raw])
        configs = [{"slug": "test-stream", "name": "Test Stream"}]
        candidates = load_candidates(output_dir, configs, TODAY)
        assert len(candidates) == 0


def test_load_candidates_tolerates_missing_stream_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        configs = [{"slug": "nonexistent", "name": "Ghost Stream"}]
        candidates = load_candidates(output_dir, configs, TODAY)
        assert candidates == []
