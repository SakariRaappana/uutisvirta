from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from . import fetcher
from .fetcher import NewsItem
from .llm import LLMClient, LLMResponse, get_client

log = logging.getLogger(__name__)

_md = MarkdownIt()

CLASSIFY_MODEL = "gpt-4o-mini"
CLASSIFY_BATCH_SIZE = 10

CLASSIFY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index":                {"type": "integer"},
                    "category":             {"type": "string", "enum": ["tärkea", "lyhyt", "ohita"]},
                    "relevance":            {"type": "integer"},
                    "geo_match":            {"type": "boolean"},
                    "homepage_eligible":    {"type": "boolean"},
                    "homepage_score":       {"type": "integer"},
                    "keep_days":            {"type": "integer"},
                    "personal_relevance":   {"type": "integer"},
                    "strategic_importance": {"type": "integer"},
                    "actionability":        {"type": "integer"},
                    "novelty":              {"type": "integer"},
                    "why_relevant":         {"type": "string"},
                    "event_key":            {"type": "string"},
                },
                "required": [
                    "index", "category", "relevance", "geo_match",
                    "homepage_eligible", "homepage_score", "keep_days",
                    "personal_relevance", "strategic_importance", "actionability", "novelty",
                    "why_relevant", "event_key",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

TEMPLATES_DIR = Path(__file__).parent / "templates"


_MAX_TÄRKEAT = 3


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n")
        stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")].rstrip()
    return stripped


def _parse_classify_json(raw: str, n_items: int) -> list[dict] | None:
    """Parse and validate classification JSON. Returns list of entry dicts or None on any error."""
    try:
        outer = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        return None
    # Structured Outputs wraps the array in {"classifications": [...]}
    data = outer.get("classifications") if isinstance(outer, dict) else None
    if not isinstance(data, list):
        return None
    indices = {e["index"] for e in data if isinstance(e.get("index"), int)}
    if indices != set(range(n_items)):
        return None
    return data


def _apply_scoring(data: list[dict], items: list[NewsItem]) -> dict[str, str]:
    """Apply relevance/geo_match filtering and hard caps; return {url: category}."""
    entries = []
    for e in data:
        idx = e["index"]
        cat = e.get("category", "lyhyt")
        relevance = int(e.get("relevance", 3))
        geo_match = bool(e.get("geo_match", True))
        entries.append((idx, cat, relevance, geo_match))

    # Demote low-relevance lyhyt items to ohita
    entries = [
        (i, "ohita" if cat == "lyhyt" and rel < 2 else cat, rel, geo)
        for i, cat, rel, geo in entries
    ]

    # Keep only the top _MAX_TÄRKEAT by relevance (ties broken by lower index = more recent)
    tärkeat_entries = sorted(
        [(i, rel) for i, cat, rel, geo in entries if cat == "tärkea"],
        key=lambda x: (-x[1], x[0]),
    )
    kept_tärkea_indices = {i for i, _ in tärkeat_entries[:_MAX_TÄRKEAT]}

    result = {}
    for idx, cat, rel, geo in entries:
        if cat == "tärkea" and idx not in kept_tärkea_indices:
            cat = "lyhyt"
        result[items[idx].url] = cat
    return result


def _env(output_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def build_classify_system_prompt(stream_config: dict) -> str:
    profile = stream_config.get("profile", "").strip()
    return f"""Olet uutisluokittelija. Luokittele jokainen uutinen lukijaprofiilin perusteella.

Lukijaprofiili:
{profile}

Luokitukset:
- "tärkea": uutinen täyttää KAIKKI seuraavat ehdot:
  1. Koskee suoraan jotain profiilissa mainittua teknologiaa, regulaatiota tai aihetta (ei yleistä AI-uutisointia)
  2. Sisältää konkreettisen uuden tiedon: julkaisu, GA-siirtymä, arkkitehtuurimuutos, laki voimaan, merkittävä tutkimustulos
  3. Lukijan kannalta toiminnallinen tai strategisesti merkittävä — ei vain kiinnostava taustatieto
  Käytä "tärkea"-luokkaa säästeliäästi: enintään 2–3 uutista tästä erästä ansaitsee syvällisen analyysin.
- "lyhyt": aihe liittyy profiiliin ja on hyvä tietää, mutta ei täytä kaikkia "tärkea"-ehtoja
- "ohita": ei liity lukijaan lainkaan, tai on niin yleinen AI-hype-uutinen ettei se tuo lisäarvoa

Luokittele VAIN annettujen tietojen (otsikko + tiivistelmä) perusteella. \
Jos tiivistelmä on niin lyhyt, ettei sisällöstä voi sanoa mitään, luokittele "lyhyt" — älä arvaa sisältöä omasta tiedostasi.

Arvioi jokainen uutinen seuraavilla kentillä:

Peruskentät (kaikille uutisille):
- "relevance": 1–5 (5 = täsmää profiiliin maantieteellisesti ja aihepiiriltään suoraan, 1 = hyvin marginaalisesti liittyvä)
- "geo_match": true jos uutinen koskee profiilissa mainittua maantieteellistä aluetta

Henkilökohtaisen etusivun kentät (kaikille uutisille):
- "homepage_eligible": true VAIN jos KAIKKI toteutuvat:
    * category on "tärkea"
    * Uutinen voi konkreettisesti muuttaa sitä, mitä lukija oppii, tekee, ostaa tai ehdottaa asiakkaalle
    * Vaikutus on ajankohtainen, ei hypoteettinen
    * Tämä on poikkeuksellinen uutinen — suurimmalla osalla tärkea-uutisista tämä on false
  Muille: false
- "homepage_score": 0–100. Etusivun minimi on 75. Käytä ankkuripisteinä:
    90–100: Poikkeuksellinen, vaikuttaa suoraan lukijan ammatilliseen toimintaan jo tällä viikolla
    75–89: Selvästi merkittävä, muuttaa jonkin päätöksen tai toiminnan lähiviikkoina
    50–74: Kiinnostava mutta ei etusivukelpoinen (homepage_eligible pitäisi olla false)
    0–49: Marginaalinen tai epäolennainen
  0 kaikille joille homepage_eligible on false.
- "keep_days": 1–7, kuinka monta päivää uutinen ansaitsee pysyä etusivulla:
    1–2 = ajankohtainen mutta nopeasti vanheneva, 3–4 = selvästi relevantti,
    5–6 = strategisesti merkittävä, 7 = poikkeuksellinen pitkävaikutteinen
  0 kaikille joille homepage_eligible on false.
- "personal_relevance": 1–5 (miten suoraan koskee juuri tätä lukijaa)
- "strategic_importance": 1–5 (pitkäaikainen vaikutus työnkuvaan tai toimialaan)
- "actionability": 1–5 (vaatiiko tai mahdollistaa konkreettisen teon tällä viikolla)
- "novelty": 1–5 (onko sisältö aidosti uutta vai jo yleisesti tiedettyä)
  Lyhyt/ohita-uutisille kaikki 1.
- "why_relevant": Yksi konkreettinen suomenkielinen virke siitä, miksi uutinen on juuri tälle lukijalle tärkeä.
  Huono: "Tämä on tärkeä kehitys tekoälyalalla."
  Hyvä: "Vaikuttaa suoraan siihen, millä abstraktiotasolla sinun kannattaa rakentaa yritysasiakkaiden AI-agentteja."
  Lyhyt/ohita-uutisille: ""
- "event_key": Lyhyt slug-muotoinen tunniste samalle uutistapahtumalle (esim. "microsoft-agent-framework-ga"). \
  Käytä samaa tunnistetta saman tapahtuman eri uutisversioille. Lyhyt/ohita-uutisille: ""

Palauta JSON-objekti muodossa {{"classifications": [{{"index": 0, "category": "...", ...}}, ...]}}. \
Yksi objekti per uutinen. Ei muuta tekstiä."""


def build_classify_user_prompt(items: list[NewsItem]) -> str:
    lines = [f"Luokittele {len(items)} uutista:\n"]
    for i, item in enumerate(items):
        lines.append(f"[{i}] {item.title}")
        lines.append(f"Lähde: {item.source_name}")
        if item.summary:
            lines.append(item.summary[:200])
        lines.append("---")
    return "\n".join(lines)


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

TÄRKEÄÄ — älä koskaan keksi tietoja itse: kirjoita VAIN se, mitä alla annetussa \
uutismateriaalissa (otsikko + tiivistelmä) lukee. Jos tiivistelmä on lyhyt eikä \
kerro yksityiskohtia, kirjoita lyhyt maininta sen perusteella mitä tiedetään — \
älä täytä aukkoja omilla oletuksillasi tai yleistiedollasi aiheesta.

Uutiset on jaettu valmiiksi kahteen ryhmään:

TÄRKEÄT UUTISET — kirjoita 3–5 kappaletta suomeksi per uutinen. Tiivistä artikkelin \
konkreettinen sisältö: mitä tapahtui, mitä päätettiin, mitä julkaistiin, mitkä ovat \
todelliset seuraukset. Lopeta aina riviin: **[Lue alkuperäinen →](url)**

LYHYTMAININNAT — kirjoita 1–3 virkettä suomeksi artikkelin sisällöstä. \
Lopeta riviin: [Lue lisää](url)

Artikkelin rakenne:
1. ## Päivän yhteenveto
   Yksi kappale, max 60 sanaa. Mitä konkreettista tapahtui tänään?

2. ## Tärkeimmät uutiset
   Yksi `### Otsikko`-osio per tärkeä uutinen.

3. ## Lyhytmaininnat
   Luettelo lyhyistä maininnoista.

Palauta pelkkä Markdown. Ei johdantoa, ei loppusanoja."""


def build_user_prompt(
    tärkeat: list[NewsItem],
    lyhyet: list[NewsItem],
    run_date: date,
    full_texts: dict[str, str] | None = None,
) -> str:
    lines = [
        f"Tänään on {run_date.isoformat()}. "
        f"Alla on {len(tärkeat)} tärkeää uutista (syvä analyysi) ja "
        f"{len(lyhyet)} lyhyttä mainintaa.\n"
    ]
    lines.append("Kirjoita tämän päivän uutisdigestiartikkeli.\n")

    if tärkeat:
        lines.append("=== TÄRKEÄT UUTISET — kirjoita 3–5 kappaletta per uutinen ===\n")
        for item in tärkeat:
            lines.append(f"### {item.title}")
            lines.append(f"Lähde: {item.source_name} | URL: {item.url}")
            text = (full_texts or {}).get(item.url) or item.summary
            if text:
                lines.append(text)
            lines.append("---")

    if lyhyet:
        lines.append("\n=== LYHYTMAININNAT — kirjoita 1–3 virkettä + linkki per uutinen ===\n")
        for item in lyhyet:
            lines.append(f"### {item.title}")
            lines.append(f"Lähde: {item.source_name} | URL: {item.url}")
            if item.summary:
                lines.append(item.summary[:200])
            lines.append("---")

    return "\n".join(lines)


def _classify_batch(
    batch: list[NewsItem],
    offset: int,
    system: str,
    client: LLMClient,
) -> list[dict] | None:
    """Classify one batch. Returns entries with global indices (offset applied), or None on failure."""
    base_user = build_classify_user_prompt(batch)
    for attempt in range(2):
        user = base_user if attempt == 0 else (
            base_user + f"\n\nHUOM: Edellinen vastauksesi puuttui joitain indeksejä. "
            f"Palauta kaikki {len(batch)} uutista."
        )
        resp = client.complete(system, user, max_tokens=1200,
                               model_override=CLASSIFY_MODEL,
                               response_schema=CLASSIFY_SCHEMA)
        log.info("Batch offset=%d attempt %d: %d in / %d out tokens",
                 offset, attempt + 1, resp.input_tokens, resp.output_tokens)
        data = _parse_classify_json(resp.content, len(batch))
        if data is not None:
            for entry in data:
                entry["index"] += offset
            return data
        log.warning("Batch offset=%d: missing indices on attempt %d", offset, attempt + 1)
    return None


def _classify_items(
    items: list[NewsItem],
    stream_config: dict,
    client: LLMClient,
) -> tuple[dict[str, str], list[dict]] | None:
    """Returns (categories, raw_data) or None on failure.

    categories: {url: category} where category is 'tärkea'|'lyhyt'|'ohita'
    raw_data: full LLM classification entries with global indices, used for homepage metadata
    """
    system = build_classify_system_prompt(stream_config)
    batches = [items[i:i + CLASSIFY_BATCH_SIZE] for i in range(0, len(items), CLASSIFY_BATCH_SIZE)]
    log.info("Classifying %d items in %d batches with %s...", len(items), len(batches), CLASSIFY_MODEL)

    all_data: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = {
            executor.submit(_classify_batch, batch, i * CLASSIFY_BATCH_SIZE, system, client): i
            for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                log.error("Classification failed for batch %d — aborting digest", futures[future])
                return None
            all_data.extend(result)

    categories = _apply_scoring(all_data, items)
    log.info(
        "Classification ok: %d tärkea, %d lyhyt, %d ohita",
        sum(1 for v in categories.values() if v == "tärkea"),
        sum(1 for v in categories.values() if v == "lyhyt"),
        sum(1 for v in categories.values() if v == "ohita"),
    )
    return categories, all_data


def _extract_homepage_candidates(
    raw_data: list[dict],
    items: list[NewsItem],
    final_categories: dict[str, str],
) -> list[dict]:
    """Return homepage candidate dicts for items that are tärkea after scoring AND homepage_eligible."""
    candidates = []
    for entry in raw_data:
        idx = entry.get("index")
        if not isinstance(idx, int) or idx >= len(items):
            continue
        item = items[idx]
        if final_categories.get(item.url) != "tärkea":
            continue
        if not entry.get("homepage_eligible", False):
            continue
        score = int(entry.get("homepage_score", 0))
        if score < 1:
            continue
        candidates.append({
            "title": item.title,
            "source_name": item.source_name,
            "source_url": item.url,
            "published_at": item.published_dt.isoformat() if item.published_dt else None,
            "summary": item.summary[:300],
            "why_relevant": entry.get("why_relevant", ""),
            "homepage_eligible": True,
            "homepage_score": score,
            "keep_days": max(1, min(7, int(entry.get("keep_days", 3)))),
            "personal_relevance": int(entry.get("personal_relevance", 3)),
            "strategic_importance": int(entry.get("strategic_importance", 3)),
            "actionability": int(entry.get("actionability", 3)),
            "novelty": int(entry.get("novelty", 3)),
            "event_key": entry.get("event_key", ""),
            "category": "tärkea",
        })
    return candidates


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

    if dry_run:
        system_prompt = build_system_prompt(stream_config)
        classify_system = build_classify_system_prompt(stream_config)
        writing_preview = build_user_prompt(items, [], run_date)
        print("=" * 60)
        print(f"STREAM: {stream_config['name']}  DATE: {date_str}")
        print("--- VAIHE 1: LUOKITTELU (system) ---")
        print(classify_system)
        print("--- VAIHE 1: LUOKITTELU (user, ensin 3 uutista) ---")
        print(build_classify_user_prompt(items[:3]))
        print("--- VAIHE 2: KIRJOITUS (system) ---")
        print(system_prompt)
        print("--- VAIHE 2: KIRJOITUS (user, esikatselu kaikki tärkeinä) ---")
        print(writing_preview)
        print("=" * 60)
        return None

    if out_file.exists() and not force:
        log.info("Digest %s/%s already exists, skipping (use --force to overwrite)", slug, date_str)
        return out_file

    if not items:
        log.warning("No items for stream %s on %s — skipping LLM call", slug, date_str)
        if force:
            all_streams = _load_stream_nav(output_dir, stream_config)
            _rebuild_stream_index(stream_config, stream_dir, output_dir, all_streams)
        return None

    client = get_client()

    classify_result = _classify_items(items, stream_config, client)
    if classify_result is None:
        raise RuntimeError(f"Classification failed for stream {slug} after retries")
    categories, raw_classifications = classify_result

    tärkeat = [i for i in items if categories.get(i.url) == "tärkea"]
    lyhyet = [i for i in items if categories.get(i.url) == "lyhyt"]

    if not tärkeat and not lyhyet:
        log.info("All items classified as 'ohita' for stream %s — skipping", slug)
        if force:
            all_streams = _load_stream_nav(output_dir, stream_config)
            _rebuild_stream_index(stream_config, stream_dir, output_dir, all_streams)
        return None

    full_texts: dict[str, str] = {}
    for item in tärkeat[:5]:
        text = fetcher.fetch_article_text(item.url)
        if text:
            full_texts[item.url] = text
            log.info("Full text fetched for '%s' (%d chars)", item.title[:60], len(text))
        else:
            log.debug("Full text unavailable for '%s'", item.title[:60])

    system_prompt = build_system_prompt(stream_config)
    user_prompt = build_user_prompt(tärkeat, lyhyet, run_date, full_texts=full_texts)

    log.info(
        "Writing digest for %s: %d tärkea, %d lyhyt items...",
        slug, len(tärkeat), len(lyhyet),
    )
    response: LLMResponse = client.complete(system_prompt, user_prompt, max_tokens=8192)
    log.info("Writing done: %d in / %d out tokens", response.input_tokens, response.output_tokens)

    body_html = _md.render(_strip_code_fence(response.content))

    env = _env(output_dir)
    all_streams = _load_stream_nav(output_dir, stream_config)
    prev_date, next_date = _adjacent_dates(stream_dir, date_str)

    html = env.get_template("digest.html.j2").render(
        date=date_str,
        stream=stream_config,
        body_html=body_html,
        item_count=len(tärkeat) + len(lyhyet),
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

    homepage_candidates = _extract_homepage_candidates(raw_classifications, items, categories)
    if homepage_candidates:
        log.info(
            "Homepage candidates for %s/%s: %d items (scores: %s)",
            slug, date_str,
            len(homepage_candidates),
            ", ".join(str(c["homepage_score"]) for c in homepage_candidates),
        )

    json_path = stream_dir / f"{date_str}.json"
    json_path.write_text(json.dumps({
        "date": date_str,
        "stream_slug": slug,
        "stream_name": stream_config["name"],
        "body_html": body_html,
        "item_count": len(tärkeat) + len(lyhyet),
        "model": response.model,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "sources": [{"title": i.title, "url": i.url, "source_name": i.source_name} for i in items],
        "homepage_candidates": homepage_candidates,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

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
    stream_dir.mkdir(parents=True, exist_ok=True)
    RECENT_COUNT = 3
    dates = _list_digest_dates(stream_dir)

    recent_digests: list[dict] = []
    archive_entries: list[dict] = []

    for i, date_str in enumerate(dates):
        if i < RECENT_COUNT:
            json_path = stream_dir / f"{date_str}.json"
            if json_path.exists():
                recent_digests.append(json.loads(json_path.read_text(encoding="utf-8")))
            else:
                html_path = stream_dir / f"{date_str}.html"
                body_html = ""
                if html_path.exists():
                    body_html = _extract_body_html(html_path.read_text(encoding="utf-8")) or ""
                recent_digests.append({
                    "date": date_str,
                    "body_html": body_html,
                    "item_count": None,
                    "model": None,
                    "sources": [],
                })
        else:
            archive_entries.append({"date": date_str})

    env = _env(output_dir)
    html = env.get_template("stream_index.html.j2").render(
        stream=stream_config,
        recent_digests=recent_digests,
        archive_entries=archive_entries,
        all_streams=all_streams,
        current_slug=stream_config["slug"],
        root_path="../",
    )
    (stream_dir / "index.html").write_text(html, encoding="utf-8")


def _extract_body_html(html_content: str) -> str | None:
    m = re.search(r'<div class="digest-body">(.*?)</div>', html_content, re.DOTALL)
    return m.group(1).strip() if m else None


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


def build_homepage(stream_configs: list[dict], output_dir: Path, today: date) -> None:
    from . import homepage as _hp
    _hp.build_homepage(stream_configs, output_dir, today)


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
