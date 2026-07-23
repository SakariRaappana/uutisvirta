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
  llm.py          # LLM-client (OpenAI)
  templates/      # Jinja2-HTML-mallit
output/           # Generoitu staattinen sivusto (git-ignorattu)
  index.html      # Master-indeksi kaikista streameista
  <slug>/
    index.html    # Streamin arkisto-indeksi
    YYYY-MM-DD.html  # Päivittäinen digesti
logs/             # uutisvirta.log
```

### Suorituspolku

1. `main.py` lataa kaikki `streams/*.yaml`-konfiguraatiot
2. Per stream: `fetcher.fetch()` hakee RSS + Google News RSS -hakusanat, deduplikoi URL:n ja otsikkosamankaltaisuuden perusteella (Jaccard ≥ 0.8), järjestää uusimmat ensin, katkaisee `max_final_items`-rajaan
3. `generator.generate_digest()` tekee kaksi LLM-kutsua:
   - **Vaihe 1 (luokittelu):** `gpt-4o-mini` luokittelee uutiset OpenAI Structured Outputs -skeemalla → `tärkea`/`lyhyt`/`ohita` + relevanssipisteytys (1–5) ja maantieteellinen osuvuus
   - **Vaihe 2 (kirjoitus):** `gpt-4o` kirjoittaa analyysin tärkeistä uutisista (max 8192 output-tokenia)
4. Tärkeille uutisille haetaan artikkelien kokonaisteksti (`fetcher.fetch_article_text()`) ennen kirjoitusvaihetta
5. Renderöi Markdown → HTML Jinja2-templatella
6. Kirjoittaa `output/<slug>/YYYY-MM-DD.html` ja päivittää stream-indeksin
7. Rakentaa lopuksi master-indeksin `output/index.html`

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
