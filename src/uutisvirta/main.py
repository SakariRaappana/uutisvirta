from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from . import fetcher, generator

log = logging.getLogger("uutisvirta")

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _setup_logging(output_dir: Path) -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / "uutisvirta.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def _load_stream_configs() -> list[dict]:
    streams_dir = PROJECT_ROOT / "streams"
    configs = []
    for path in sorted(streams_dir.glob("*.yaml")):
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            configs.append(cfg)
        except Exception as exc:
            print(f"[WARNING] Could not load {path.name}: {exc}", file=sys.stderr)
    return configs


def _get_output_dir() -> Path:
    env_val = os.environ.get("OUTPUT_DIR", "").strip()
    if env_val:
        return Path(env_val)
    return PROJECT_ROOT / "output"


@click.command()
@click.option("--dry-run", is_flag=True, help="Tulosta promptit ilman LLM-kutsuja")
@click.option("--force", is_flag=True, help="Ylikirjoita olemassa oleva päivän digesti")
@click.option("--stream", default=None, help="Aja vain tämä stream (slug)")
@click.option("--open-browser", is_flag=True, help="Avaa tulos selaimessa ajon jälkeen")
@click.option("--fetch-only", is_flag=True, help="Hae artikkelit ja tulosta yhteenveto, älä kutsu LLM:ää")
def main(dry_run: bool, force: bool, stream: str | None, open_browser: bool, fetch_only: bool) -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    output_dir = _get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(output_dir)
    log.info("Uutisvirta käynnistyy (dry_run=%s, force=%s, fetch_only=%s)", dry_run, force, fetch_only)

    run_date = date.today()

    all_configs = _load_stream_configs()
    if not all_configs:
        log.error("Ei stream-konfiguraatioita hakemistossa streams/")
        sys.exit(1)

    if stream:
        configs = [c for c in all_configs if c.get("slug") == stream]
        if not configs:
            log.error("Streami '%s' ei löydy", stream)
            sys.exit(1)
    else:
        configs = all_configs

    if fetch_only:
        _print_fetch_summary(configs)
        return

    errored_streams: list[str] = []
    for cfg in configs:
        slug = cfg.get("slug", "unknown")
        log.info("--- Stream: %s ---", slug)
        try:
            items = fetcher.fetch(cfg)
            log.info("Haettiin %d uutista streamille %s", len(items), slug)
            generator.generate_digest(
                cfg, items, run_date, output_dir,
                dry_run=dry_run, force=force,
            )
        except Exception as exc:
            log.error("Stream %s epäonnistui: %s", slug, exc, exc_info=True)
            errored_streams.append(slug)

    if not dry_run:
        try:
            generator.build_homepage(all_configs, output_dir, run_date)
        except Exception as exc:
            log.error("Etusivun rakennus epäonnistui: %s", exc)

    index_path = output_dir / "index.html"
    if open_browser and index_path.exists() and not dry_run:
        import subprocess
        subprocess.run(["open", str(index_path)], check=False)

    if errored_streams and not dry_run:
        log.error("Seuraavat streamit kaatautuivat poikkeukseen: %s", ", ".join(errored_streams))
        sys.exit(1)

    log.info("Uutisvirta valmis.")


def _print_fetch_summary(configs: list[dict]) -> None:
    for cfg in configs:
        slug = cfg.get("slug", "unknown")
        name = cfg.get("name", slug)
        print(f"\n=== {name} ({slug}) ===")

        raw_items: list[fetcher.NewsItem] = []
        cfg_digest = cfg.get("digest_config", {})
        max_per_source = cfg_digest.get("max_rss_items_per_source", 20)
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=cfg_digest.get("lookback_hours", 26))

        for source in cfg.get("rss_sources", []):
            try:
                items = fetcher._fetch_rss(source, cutoff, max_per_source)
                print(f"  RSS: {source['name']} ({len(items)} kpl)")
                for item in items[:3]:
                    print(f"    • {item.title[:80]}")
                raw_items.extend(items)
            except Exception as exc:
                print(f"  RSS: {source['name']} — VIRHE: {exc}")

        for keyword in cfg.get("keyword_searches", []):
            try:
                source = {"name": f"Google News: {keyword}", "url": fetcher._google_news_url(keyword), "source_type": "google_news"}
                items = fetcher._fetch_rss(source, cutoff, max_per_source)
                print(f"  Google News: {keyword} ({len(items)} kpl)")
                for item in items[:3]:
                    print(f"    • {item.title[:80]}")
                raw_items.extend(items)
            except Exception as exc:
                print(f"  Google News: {keyword} — VIRHE: {exc}")

        deduped = fetcher._deduplicate(raw_items)
        max_final = cfg_digest.get("max_final_items", 30)
        print(f"\n  Yhteensä ennen deduplikointia: {len(raw_items)} | deduplikoinnin jälkeen: {len(deduped)} | rajattu: {min(len(deduped), max_final)}")
