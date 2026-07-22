from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from .fetcher import NewsItem
from .llm import LLMResponse, get_client

log = logging.getLogger(__name__)

_md = MarkdownIt()


def _strip_code_fence(text: str) -> str:
    """Remove a wrapping ```markdown ... ``` fence that LLMs sometimes add."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n")
        stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")].rstrip()
    return stripped

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _env(output_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


@dataclass
class DigestEntry:
    date: str
    title: str


def build_system_prompt(stream_config: dict) -> str:
    name = stream_config["name"]
    profile = stream_config.get("profile", "").strip()
    lang = stream_config.get("digest_config", {}).get("language", "fi")
    lang_instruction = "suomeksi" if lang == "fi" else f"kielellä: {lang}"

    return f"""Olet {name}-uutisdigestin kirjoittaja.

Lukijaprofiili:
{profile}

Tehtäväsi on kirjoittaa päivittäinen uutisdigestiartikkeli {lang_instruction}. \
Käännät englanninkieliset uutiset suomeksi ja tiivistät niiden sisällön — \
et kirjoita meta-kommentteja siitä mitä artikkeli käsittelee, vaan kerrot itse asian. \
Käytä arkipäiväistä mutta täsmällistä kieltä. Vältä jargonia.

Merkitse jokainen käsittelemäsi uutinen yhdellä seuraavista luokituksista:

[TÄRKEÄ] — aihe on keskeinen lukijaprofiilin kannalta.
  Kirjoita 3–5 kappaletta suomeksi. Tiivistä artikkelin konkreettinen sisältö: \
mitä tapahtui, mitä päätettiin, mitä julkaistiin, mitkä ovat todelliset seuraukset. \
Lopeta aina riviin: **[Lue alkuperäinen →](url)**

[LYHYT] — aihe on hyvä tietää, mutta ei vaadi pitkää käsittelyä.
  Kirjoita 1–3 virkettä suomeksi artikkelin sisällöstä. \
Lopeta riviin: [Lue lisää](url)

[OHITA] — aihe ei liity lukijaan lainkaan. Jätä kokonaan pois.

Artikkelin rakenne:
1. ## Päivän yhteenveto
   Yksi kappale, max 60 sanaa. Mitä konkreettista tapahtui tänään?

2. ## Tärkeimmät uutiset
   Yksi `### Otsikko`-osio per [TÄRKEÄ]-uutinen.

3. ## Lyhytmaininnat
   Luettelo [LYHYT]-uutisista.

Palauta pelkkä Markdown. Ei johdantoa, ei loppusanoja."""


def build_user_prompt(items: list[NewsItem], run_date: date) -> str:
    lines = [f"Tänään on {run_date.isoformat()}. Alla on {len(items)} uutisotsikkoa ja tiivistelmää.\n"]
    lines.append("Kirjoita tämän päivän uutisdigestiartikkeli.\n")
    for item in items:
        lines.append(f"### {item.title}")
        lines.append(f"Lähde: {item.source_name} | URL: {item.url}")
        if item.summary:
            lines.append(item.summary)
        lines.append("---")
    return "\n".join(lines)


def generate_digest(
    stream_config: dict,
    items: list[NewsItem],
    run_date: date,
    output_dir: Path,
    dry_run: bool = False,
    force: bool = False,
) -> Path | None:
    slug = stream_config["slug"]
    stream_dir = output_dir / slug
    stream_dir.mkdir(parents=True, exist_ok=True)

    date_str = run_date.isoformat()
    out_file = stream_dir / f"{date_str}.html"

    if out_file.exists() and not force:
        log.info("Digest %s/%s already exists, skipping (use --force to overwrite)", slug, date_str)
        return out_file

    system_prompt = build_system_prompt(stream_config)
    user_prompt = build_user_prompt(items, run_date)

    if dry_run:
        print("=" * 60)
        print(f"STREAM: {stream_config['name']}  DATE: {date_str}")
        print("--- SYSTEM PROMPT ---")
        print(system_prompt)
        print("--- USER PROMPT ---")
        print(user_prompt)
        print("=" * 60)
        return None

    if not items:
        log.warning("No items for stream %s on %s — skipping LLM call", slug, date_str)
        return None

    log.info("Calling LLM for stream %s (%d items)...", slug, len(items))
    client = get_client()
    response: LLMResponse = client.complete(system_prompt, user_prompt, max_tokens=4096)
    log.info("LLM done: %d in / %d out tokens", response.input_tokens, response.output_tokens)

    body_html = _md.render(_strip_code_fence(response.content))

    env = _env(output_dir)
    all_streams = _load_stream_nav(output_dir, stream_config)
    prev_date, next_date = _adjacent_dates(stream_dir, date_str)

    html = env.get_template("digest.html.j2").render(
        date=date_str,
        stream=stream_config,
        body_html=body_html,
        item_count=len(items),
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        sources=[{"title": i.title, "url": i.url, "source_name": i.source_name} for i in items],
        prev_date=prev_date,
        next_date=next_date,
        all_streams=all_streams,
        current_slug=slug,
        root_path="../",
    )
    out_file.write_text(html, encoding="utf-8")
    log.info("Wrote %s", out_file)

    _rebuild_stream_index(stream_config, stream_dir, output_dir, all_streams)
    return out_file


def build_master_index(stream_configs: list[dict], output_dir: Path) -> None:
    all_streams = []
    for cfg in stream_configs:
        slug = cfg["slug"]
        stream_dir = output_dir / slug
        dates = _list_digest_dates(stream_dir)
        all_streams.append({
            "slug": slug,
            "name": cfg["name"],
            "latest_date": dates[0] if dates else None,
        })

    env = _env(output_dir)
    html = env.get_template("master_index.html.j2").render(
        all_streams=all_streams,
        current_slug=None,
        root_path="",
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("Wrote master index")


def _rebuild_stream_index(
    stream_config: dict,
    stream_dir: Path,
    output_dir: Path,
    all_streams: list[dict],
) -> None:
    dates = _list_digest_dates(stream_dir)
    digests = [DigestEntry(date=d, title=d) for d in dates]
    env = _env(output_dir)
    html = env.get_template("stream_index.html.j2").render(
        stream=stream_config,
        digests=digests,
        all_streams=all_streams,
        current_slug=stream_config["slug"],
        root_path="../",
    )
    (stream_dir / "index.html").write_text(html, encoding="utf-8")


def _list_digest_dates(stream_dir: Path) -> list[str]:
    if not stream_dir.exists():
        return []
    dates = sorted(
        [p.stem for p in stream_dir.glob("????-??-??.html")],
        reverse=True,
    )
    return dates


def _adjacent_dates(stream_dir: Path, current: str) -> tuple[str | None, str | None]:
    dates = _list_digest_dates(stream_dir)
    if current not in dates:
        dates = sorted(dates + [current], reverse=True)
    idx = dates.index(current)
    prev_date = dates[idx + 1] if idx + 1 < len(dates) else None
    next_date = dates[idx - 1] if idx > 0 else None
    return prev_date, next_date


def _load_stream_nav(output_dir: Path, current_stream: dict) -> list[dict]:
    import yaml
    streams_dir = output_dir.parent / "streams"
    result = []
    if streams_dir.exists():
        for f in sorted(streams_dir.glob("*.yaml")):
            cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
            result.append({"slug": cfg["slug"], "name": cfg["name"]})
    if not result:
        result = [{"slug": current_stream["slug"], "name": current_stream["name"]}]
    return result
