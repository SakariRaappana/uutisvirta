# Uutisvirta

Henkilökohtainen tekoälyavusteinen uutisdigestityökalu. Hakee RSS-syötteitä ja uutisia NewsAPI:sta, suodattaa ne käyttäjän profiilia vasten LLM:llä, ja tuottaa päivittäisen HTML-digestiartikkelin.

## Tarkoitus ja tavoitteet

Uutisvirta ratkaisee tiedonhallintaongelman asiantuntijalle, jolla on kapea mutta syvä kiinnostusalue. Sen sijaan että selataan kymmeniä lähteitä manuaalisesti, työkalu:

- kokoaa uutiset useista RSS-lähteistä ja NewsAPI-hakusanoista
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
  fetcher.py      # Uutisten haku (RSS + NewsAPI) ja deduplikointi
  generator.py    # Promptien rakennus, LLM-kutsu, HTML-generointi
  llm.py          # LLM-abstraktio (Claude / OpenAI)
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
2. Per stream: `fetcher.fetch()` hakee RSS + NewsAPI, deduplikoi URL:n ja otsikkosamankaltaisuuden perusteella (Jaccard ≥ 0.8), järjestää uuimmat ensin, katkaisee `max_final_items`-rajaan
3. `generator.generate_digest()` rakentaa system- ja user-promptin, kutsuu LLM:ää (max 4096 output-tokenia), renderöi Markdown → HTML Jinja2-templatella
4. Kirjoittaa `output/<slug>/YYYY-MM-DD.html` ja päivittää stream-indeksin
5. Rakentaa lopuksi master-indeksin `output/index.html`

### Stream-konfiguraatio (YAML)

Jokainen `streams/*.yaml` määrittelee yhden aihevirran:

- `name`, `slug` — näyttönimi ja URL-turvallinen tunniste
- `profile` — vapaamuotoinen lukijaprofiili, välitetään suoraan LLM:n system-promptiin
- `rss_sources` — lista `{name, url}`-objekteja
- `newsapi_keywords` — lista hakusanoja NewsAPI:lle
- `digest_config.max_rss_items_per_source` — RSS-artikkeleita per lähde (oletus 20)
- `digest_config.max_final_items` — lopullinen katkaisuarvo (oletus 30)
- `digest_config.language` — digestin kieli, `fi` tai muu koodi
- `digest_config.lookback_hours` — kuinka vanha uutinen hyväksytään (oletus 26)

### LLM-integraatio

`llm.py` tarjoaa abstraktin `LLMClient`-rajapinnan kahdelle toteutukselle:

- `ClaudeClient` — Anthropic SDK, oletuksena `claude-opus-4-5`
- `OpenAIClient` — OpenAI SDK, oletuksena `gpt-4o`

Provider valitaan `LLM_PROVIDER`-ympäristömuuttujalla (`claude` tai `openai`).

LLM-prompti ohjaa mallin luokittelemaan jokaisen uutisen kolmeen kategoriaan:
- `[SYVÄLLINEN]` — 350–500 sanan analyysi arkkitehtuurillisesta tai strategisesta merkityksestä
- `[LYHYT]` — max 2 virkettä + URL
- `[OHITA]` — jätetään kokonaan pois

### Riippuvuudet

Pakettinhallinta: **uv** (`pyproject.toml` + `uv.lock`). Python ≥ 3.11.

Keskeiset kirjastot: `feedparser`, `httpx`, `pyyaml`, `jinja2`, `anthropic`, `openai`, `markdown-it-py`, `click`, `python-dotenv`.

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

# Avaa tulos suoraan selaimessa
uv run uutisvirta --open-browser
```

## Uuden streamin lisääminen

Luo `streams/<slug>.yaml` yllä olevan rakenteen mukaisesti. Työkalu löytää sen automaattisesti seuraavalla ajokerralla — ei koodiin tarvitse koskea.
