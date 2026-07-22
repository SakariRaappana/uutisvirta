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
    ]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
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
def main(dry_run: bool, force: bool, stream: str | None, open_browser: bool) -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    output_dir = _get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(output_dir)
    log.info("Uutisvirta käynnistyy (dry_run=%s, force=%s)", dry_run, force)

    newsapi_key = os.environ.get("NEWSAPI_KEY") or None
    run_date = date.today()

    configs = _load_stream_configs()
    if not configs:
        log.error("Ei stream-konfiguraatioita hakemistossa streams/")
        sys.exit(1)

    if stream:
        configs = [c for c in configs if c.get("slug") == stream]
        if not configs:
            log.error("Streami '%s' ei löydy", stream)
            sys.exit(1)

    for cfg in configs:
        slug = cfg.get("slug", "unknown")
        log.info("--- Stream: %s ---", slug)
        try:
            items = fetcher.fetch(cfg, newsapi_key=newsapi_key)
            log.info("Haettiin %d uutista streamille %s", len(items), slug)
            generator.generate_digest(
                cfg, items, run_date, output_dir,
                dry_run=dry_run, force=force,
            )
        except Exception as exc:
            log.error("Stream %s epäonnistui: %s", slug, exc, exc_info=True)

    if not dry_run:
        try:
            generator.build_master_index(configs, output_dir)
        except Exception as exc:
            log.error("Master-indeksin rakennus epäonnistui: %s", exc)

    index_path = output_dir / "index.html"
    if open_browser and index_path.exists() and not dry_run:
        import subprocess
        subprocess.run(["open", str(index_path)], check=False)

    log.info("Uutisvirta valmis.")
