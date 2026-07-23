# Uutisvirta

Henkilökohtainen tekoälyavusteinen uutisdigestityökalu. Hakee RSS-syötteitä ja Google News -hakuja, suodattaa ne käyttäjän profiilia vasten LLM:llä, ja tuottaa päivittäisen HTML-digestiartikkelin.

## Tarkoitus ja tavoitteet

Uutisvirta ratkaisee tiedonhallintaongelman asiantuntijalle, jolla on kapea mutta syvä kiinnostusalue. Sen sijaan että selataan kymmeniä lähteitä manuaalisesti, työkalu:

- kokoaa uutiset useista RSS-lähteistä ja Google News -hakusanoista
- kuratoi ne käyttäjän luonnollisella kielellä kirjoitetun lukijaprofiilin perusteella
- kirjoittaa asiantuntijatasoisen analyysin relevanteista uutisista (ei aloittelijan how-to-ohjeita)
- tallentaa tuloksen staattisiksi HTML-sivuiksi paikallisesti selattavaksi

Ohjelmaa ajetaan päivittäin (esim. cron). Jokainen ajo on idempotent: jos päivän digesti on jo olemassa, se ohitetaan (`--force` ylikirjoittaa).

## Tekninen arkkitehtuuri

### Hakemistorakenne

```
streams/          # Stream-konfiguraatiot (yksi .yaml per aihevirta)
src/uutisvirta/   # Paketin lähdekoodi
  main.py         # CLI-sisääntulopiste (Click)
  fetcher.py      # Uutisten haku (RSS + Google News RSS), deduplikointi, artikkelitekstien haku
  generator.py    # Promptien rakennus, LLM-kutsu, HTML-generointi
  homepage.py     # Etusivun logiikka: lataus, deduplikointi, valinta, renderöinti
  llm.py          # LLM-client (OpenAI)
  templates/      # Jinja2-HTML-mallit
tests/            # Yksikkötestit (pytest)
output/           # Generoitu staattinen sivusto (git-ignorattu)
  index.html      # Rullaava etusivu (7 vrk aikaikkunalla)
  <slug>/
    index.html    # Streamin arkisto-indeksi
    YYYY-MM-DD.html  # Päivittäinen digesti
logs/             # uutisvirta.log
```

### Suorituspolku

1. `main.py` lataa kaikki `streams/*.yaml`-konfiguraatiot
2. Per stream: `fetcher.fetch()` hakee RSS + Google News RSS -hakusanat, deduplikoi URL:n ja otsikkosamankaltaisuuden perusteella (Jaccard ≥ 0.8), järjestää uusimmat ensin, katkaisee `max_final_items`-rajaan
3. `generator.generate_digest()` tekee kaksi LLM-kutsua:
   - **Vaihe 1 (luokittelu):** `gpt-4o-mini` luokittelee uutiset OpenAI Structured Outputs -skeemalla 10 uutisen rinnakkaisissa erissä → `tärkea`/`lyhyt`/`ohita` + relevanssipisteytys ja **etusivumetatiedot** (homepage_score, keep_days, why_relevant, event_key jne.)
   - **Vaihe 2 (kirjoitus):** `gpt-4o` kirjoittaa analyysin tärkeistä uutisista (max 8192 output-tokenia)
4. Tärkeille uutisille haetaan artikkelien kokonaisteksti (`fetcher.fetch_article_text()`) ennen kirjoitusvaihetta
5. Renderöi Markdown → HTML Jinja2-templatella
6. Kirjoittaa `output/<slug>/YYYY-MM-DD.html` ja päivittää stream-indeksin
7. Kirjoittaa JSON-manifestin `output/<slug>/YYYY-MM-DD.json` etusivua varten (`homepage_candidates`-kenttä sisältää etusivukelpoiset uutiset)
8. Kaikkien streamien jälkeen rakentaa rullaavan etusivun `output/index.html` (`homepage.build_homepage()`)

### Etusivu (homepage.py)

Etusivu on rullaava 7 vuorokauden aikaikkunan kooste kaikista streameista.

**Tietomalli `HomepageCandidate`:** dataclass, joka sisältää mm. `homepage_score` (0–100), `keep_days` (1–7), `why_relevant` (yksi konkreettinen virke), `event_key` (slug-muotoinen tapahtumantunniste).

**Valintaperiaatteet:**
- Vain `tärkea`-luokan uutiset voivat olla `homepage_eligible=True`
- Etusivun näyttöraja: `effective_score = homepage_score - age_days × 3 ≥ 75`
- Uutinen poistetaan kun `age_days ≥ keep_days` TAI `age_days ≥ 7`
- Enintään 4 uutista per stream, enintään 15 yhteensä
- Deduplikointi: event_key → URL → otsikkosamankaltaisuus (Jaccard ≥ 0.75)

**Konfiguraatiovakiot** (`homepage.py`): `HOMEPAGE_WINDOW_DAYS=7`, `HOMEPAGE_MIN_SCORE=75`, `HOMEPAGE_MAX_TOTAL=15`, `HOMEPAGE_MAX_PER_STREAM=4`, `HOMEPAGE_AGE_PENALTY=3`.

### Stream-konfiguraatio (YAML)

Jokainen `streams/*.yaml` määrittelee yhden aihevirran:

- `name`, `slug` — näyttönimi ja URL-turvallinen tunniste
- `profile` — vapaamuotoinen lukijaprofiili, välitetään suoraan LLM:n system-promptiin
- `rss_sources` — lista `{name, url}`-objekteja
- `keyword_searches` — lista hakusanoja Google News RSS -hauille
- `digest_config.max_rss_items_per_source` — RSS-artikkeleita per lähde (oletus 20)
- `digest_config.max_final_items` — lopullinen katkaisuarvo (oletus 30)
- `digest_config.language` — digestin kieli, `fi` tai muu koodi
- `digest_config.lookback_hours` — kuinka vanha uutinen hyväksytään (oletus 26)

### LLM-integraatio

`llm.py` tarjoaa `LLMClient`-luokan OpenAI SDK:n päälle. Provider on aina OpenAI; malli konfiguroidaan `OPENAI_MODEL`-ympäristömuuttujalla (oletus `gpt-4o`).

Luokitteluun käytetään `gpt-4o-mini`-mallia OpenAI Structured Outputs -ominaisuudella (`response_format: json_schema`), joka pakottaa JSON-rakenteen API-tasolla. Luokittelu tuottaa jokaiselle uutiselle:

- `category`: `tärkea` / `lyhyt` / `ohita`
- `relevance`: 1–5 (maantieteellinen ja aihepiirillinen osuvuus profiiliin)
- `geo_match`: tosi/epätosi

Tärkeistä uutisista haetaan lisäksi artikkelien kokonaisteksti (max 3000 merkkiä) kirjoitusmallin pohjaksi. Jos tekstiä ei saada (paywall, timeout), käytetään RSS-tiivistelmää varavalintana.

### Riippuvuudet

Pakettinhallinta: **uv** (`pyproject.toml` + `uv.lock`). Python ≥ 3.11.

Keskeiset kirjastot: `feedparser`, `pyyaml`, `jinja2`, `openai`, `markdown-it-py`, `click`, `python-dotenv`.

## Kehitys ja ajaminen

```bash
# Asenna riippuvuudet
uv sync

# Kopioi ja täytä ympäristömuuttujat
cp .env.example .env

# Aja kaikki streamit
uv run uutisvirta

# Aja vain yksi stream
uv run uutisvirta --stream ammatillinen-osaaminen

# Tarkasta promptit ilman LLM-kutsuja
uv run uutisvirta --dry-run

# Testaa uutishaku (RSS + Google News) ilman LLM-kutsuja
uv run uutisvirta --stream ammatillinen-osaaminen --fetch-only

# Avaa tulos suoraan selaimessa
uv run uutisvirta --open-browser
```

## Uuden streamin lisääminen

Luo `streams/<slug>.yaml` yllä olevan rakenteen mukaisesti. Työkalu löytää sen automaattisesti seuraavalla ajokerralla — ei koodiin tarvitse koskea.
