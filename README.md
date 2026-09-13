# Telegram-LLM Health Tracker & Dashboard

Jedno-procesová async Python aplikácia: Telegram bot (webhook) → Gemini (function
calling) → PostgreSQL, plus jednoduchý FastAPI/Jinja2 dashboard s Chart.js.

## Vrstvy

- `app/main.py` — FastAPI app, `lifespan` (DB pool, webhook, scheduler)
- `app/config.py` — nastavenia z `.env` (pydantic-settings)
- `app/database.py` — asyncpg pool + query funkcie nad `health_records` a `auth_user`
- `app/llm/` — Gemini klient, tool schémy (`tools.py`), routovacia logika (`service.py`)
- `app/telegram/` — aiogram `Bot`/`Dispatcher` vo webhook móde, kontrola `auth_user`
- `app/routers/` — `telegram.py` (webhook endpoint), `dashboard.py` (`/dashboard`, `/api/stats`)
- `app/garmin/` — `sync.py` (sťahovanie a ukladanie denného Garmin digestu, catch-up),
  `bootstrap.py` (jednorazové interaktívne prihlásenie)
- `app/scheduler.py` — `AsyncIOScheduler`, denná úloha `sync_garmin_data` o 09:00
- `db/init.sql` — DDL pre `auth_user`, `health_records`, `garmin_account`

## Databáza

Všetky tabuľky žijú vo vlastnej Postgres schéme **`health_tracker`** (nie `public`) —
DB na NASe je zdieľaná s inými projektmi, takto sa vyhneme kolízii názvov tabuliek.
Schéma sa nastavuje ako `server_settings={"search_path": ...}` pri vytváraní
`asyncpg` poolu (`app/database.py`, `SCHEMA` konštanta) — **nie** cez obyčajný
`SET search_path`, lebo ten by asyncpg po vrátení spojenia do poolu vždy vynuloval
príkazom `RESET ALL`. Meno schémy musí byť zhodné v `db/init.sql` aj `app/database.py`.

Appka defaultne očakáva **existujúce** PostgreSQL na NASe (nie lokálny kontajner) —
`docker-compose.yml` už nespúšťa vlastné Postgres, len appku. `DATABASE_URL` v `.env`
smeruje priamo tam:

```
DATABASE_URL=postgresql://<user>:<password>@192.168.1.229:15432/<dbname>
```

Pred prvým spustením treba na tejto DB jednorazovo vytvoriť tabuľky z
[db/init.sql](db/init.sql) (obsahuje `CREATE TABLE IF NOT EXISTS`, takže sa dá pustiť
opakovane bez rizika):

```bash
psql "postgresql://<user>:<password>@192.168.1.229:15432/<dbname>" -f db/init.sql
```

Ak by si niekedy potreboval lokálny Postgres (napr. vývoj mimo NAS siete), je
pripravený ako voliteľný profil, nespúšťa sa defaultne:

```bash
docker compose --profile local-db up -d db
```

## Lokálny beh

1. `cp .env.example .env` a doplň `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`,
   `TELEGRAM_WEBHOOK_BASE_URL` (verejná HTTPS URL, napr. z ngrok/cloudflared),
   `TELEGRAM_WEBHOOK_SECRET` a `DATABASE_URL` (NAS DB, viď vyššie).
2. `docker compose up --build`
3. Dashboard: `http://localhost:8000/dashboard?user_id=<tvoje_telegram_id>`

## Bez Dockeru (uv)

```bash
uv sync
uv run uvicorn app.main:app --reload
```

## Lokálny vývoj s reálnym Telegram webhookom (bez rebuildu image)

Na NASe pôjde appka za reverse proxy, ale kým vyvíjaš lokálne, nemusíš pri každej
zmene stavať Docker image — appka beží natívne s `--reload` a webhook ide cez tunel:

1. `.env` má `DATABASE_URL` smerujúce priamo na NAS DB (`192.168.1.229:15432`) — platí
   to isté, či appku spúšťaš cez Docker alebo natívne, žiadna zmena netreba.
2. Spusti tunel, napr. `ngrok http 8000` (alebo `cloudflared tunnel --url http://localhost:8000`).
   Dostaneš HTTPS URL typu `https://abc123.ngrok-free.app`.
3. Daj túto URL do `.env` ako `TELEGRAM_WEBHOOK_BASE_URL`.
4. Spusti appku: `uv run uvicorn app.main:app --reload` — pri štarte sa webhook
   zaregistruje na Telegrame s touto URL.
5. Píš botovi v Telegrame — správy chodia cez tunel na tvoj lokálny bežiaci proces;
   `--reload` zachytí každú zmenu kódu bez reštartu appky (netreba ani reštart kvôli
   webhooku, ten sa registruje len raz pri štarte procesu).

Pozor: free ngrok URL sa mení pri každom reštarte tunela — ak tunel reštartuješ, uprav
`TELEGRAM_WEBHOOK_BASE_URL` a reštartni aj `uvicorn`, nech sa webhook prehlási na novú
URL.

### Automatizovane cez `scripts/dev_with_tunnel.sh`

Kroky 2-4 vyššie robí za teba tento skript — reštartuje cloudflared tunel, počká na
DNS resolúciu novej `trycloudflare.com` URL (cloudflared ju vypíše skôr, než je
reálne dostupná — priamy `set_webhook` hneď po štarte by inak padal na "Failed to
resolve host"), zapíše ju do `.env` a spustí appku:

```bash
./scripts/dev_with_tunnel.sh
```

Zámerne beží **bez** `--reload` — pri páde appky počas štartu (napr. práve tá DNS
race) vie reloader proces prežiť a držať port 8000 obsadený aj po ukončení skriptu.
Na PyCharm to vieš napojiť ako samostatnú **Shell Script** run konfiguráciu
(Script path: `scripts/dev_with_tunnel.sh`) — Start spustí celý flow, Stop pošle
`SIGTERM` a skript korektne ukončí aj appku aj tunel (žiadny osamotený proces).
Pôvodnú Start/Debug konfiguráciu s `--reload` nechaj bokom pre rýchle iterovanie kódu
bez tunela.

## Autorizácia používateľov (`auth_user`)

Bot odpovie iba `telegram_id`, ktoré je v tabuľke `auth_user` a má `is_active = TRUE`;
ostatným pošle "Nemáš prístup" a request zaloguje (`app/telegram/bot.py`). Toto je
nezávislé od `TELEGRAM_WEBHOOK_SECRET`, ktorý len overuje, že samotný webhook request
prišiel z Telegramu — nič nehovorí o tom, ktorý konkrétny Telegram účet smie s botom
písať.

Pridanie povoleného používateľa (zisti si vlastné `telegram_id` napr. cez `@userinfobot`):

```sql
INSERT INTO auth_user (telegram_id, display_name) VALUES (123456789, 'Peter');
```

alebo programaticky cez `app.database.add_auth_user(telegram_id, display_name)`.

## Garmin Connect sync (`app/garmin/`)

Tretí Gemini nástroj popri `insert_health_record`/`get_health_records`:
`sync_garmin_day` — keď napíšeš botovi napr. "stiahni Garmin dáta za včera", Gemini
zavolá tento tool, appka stiahne dáta z Garmin Connect a uloží ich.

**Ako funguje autentifikácia** — heslo sa nikdy neukladá, ani do `.env`, ani do DB:

1. Jednorazovo, interaktívne (kvôli MFA) spustíš pre každého používateľa/kamaráta:
   ```bash
   uv run python -m app.garmin.bootstrap <telegram_id>
   ```
   Vypýta si Garmin email/heslo/MFA kód priamo v termináli, prihlási sa, a do
   tabuľky `garmin_account` (naviazanej na `auth_user.telegram_id`) uloží **iba**
   serializovaný session token (`garminconnect`'s `client.dumps()`/`loads()`) —
   heslo skončí len v pamäti tohto jedného behu skriptu a nikam sa nezapíše.
2. Odvtedy `sync_day()`/`sync_catch_up()` (`app/garmin/sync.py`) session z DB
   načítajú a prihlásia sa ňou (`Garmin().login(tokenstore=session_json)`) — bez
   hesla, bez MFA. Po každom behu sa (prípadne obnovený/refreshnutý) token uloží
   späť do DB.
3. Ak token prestane platiť (napr. dlhodobo nepoužívaný refresh token), appka
   vráti jasnú chybu s inštrukciou znova spustiť bootstrap — nikdy sa sama
   nepokúsi prihlásiť menom/heslom (žiadne heslo nemá k dispozícii).

Tento dizajn prirodzene podporuje viac ľudí: každý kamarát má vlastný riadok v
`garmin_account`, vlastný Garmin účet, vlastnú históriu v `health_records`
(`typ='garmin_daily'`).

**Prepojenie cez web namiesto terminálu** — pre kohokoľvek bez prístupu k
terminálu (napr. kamaráti) existuje aj webový flow: napíš botovi v Telegrame
`/garmin_link` a dostaneš späť odkaz na `/garmin-login?token=...`
(`app/routers/garmin_login.py`). Token je podpísaný HMAC-om (`app/garmin/link_token.py`,
kľúč = `TELEGRAM_WEBHOOK_SECRET`), viazaný na tvoje `telegram_id` a platí 30 minút —
appka teda nikde nevystavuje surové `user_id` v URL, ktoré by niekto mohol uhádnuť
alebo použiť pre cudzí účet. Formulár prevedie cez email/heslo a prípadný MFA krok
presne tak ako CLI bootstrap, a rovnako neukladá heslo, iba výsledný session token.

**Denné dáta** sa ukladajú ako jeden JSONB "digest" objekt na deň (kroky,
vzdialenosť, kalórie, tep, spánok, HRV, SpO2, body battery, tréningová
pripravenosť, hydratácia, zoznam aktivít) — nie per-minútové intraday série ani
lapy, aby záznam ostal kompaktný; dá sa ľahko rozšíriť, ak by si to chcel. Pri
každom sync-i pre daný deň sa **existujúci záznam zmaže a nahradí** novým
(`database.replace_health_record_for_day`), takže opakovaný sync toho istého dňa
nič neduplikuje.

**Cron** o 09:00 (`app/scheduler.py`) prebehne všetkých používateľov s riadkom v
`garmin_account` a pre každého dobehne (catch-up) všetky dni od posledného
synchronizovaného dňa po **včerajšok** (dnešok o 9:00 by bol ešte neúplný, napr.
spánok cez noc), max `MAX_RANGE_DAYS` (31) dní za jeden beh — pri dlhšom výpadku sa
dobehne postupne cez viacero dní/cronov, nie v jednom veľkom nápore.

**Rozsah dní naraz** — popri `sync_garmin_day` (jeden deň) existuje aj
`sync_garmin_range` (`start_date`, `end_date`, max **31 dní** naraz,
`app/garmin/sync.py`), na inicializáciu histórie bez ručného volania po dňoch. V
Telegrame stačí napr. "stiahni Garmin dáta od 1.8. do 31.8." — Gemini zvolí správny
nástroj. Oba nástroje aj `sync_catch_up` teraz zdieľajú jednu funkciu
(`sync_range()`), ktorá sa do Garminu prihlási **raz** pre celý rozsah (nie raz na
deň ako predtým) — rýchlejšie a menej náchylné na Garmin rate-limiting.

## Token usage audit (`token_usage`)

Každé volanie Gemini API (jedna Telegram správa môže spustiť dve — pôvodné a
follow-up, keď `get_health_records` vracia dáta späť modelu) sa zaloguje do
`health_tracker.token_usage`: `user_id`, čas, model, `prompt_tokens`,
`response_tokens`, `total_tokens` (`app/llm/service.py` → `_log_usage()`,
best-effort — zlyhanie logovania nikdy nezhodí samotnú odpoveď bota).

**`/tokens [RRRR-MM]`** v Telegrame ukáže mesačný súhrn (bez argumentu aktuálny
mesiac) — počet volaní, tokeny a odhad ceny v USD podľa `app/llm/pricing.py`
(hand-maintained cenník USD/1M tokenov, overený 2026-09-11 na
ai.google.dev/gemini-api/docs/pricing — Gemini cenník sa mení a API ho nevystavuje,
takže pri zmene modelu/cien treba tabuľku ručne doplniť). Súhrn je rozpísaný
per-model, keby si `GEMINI_MODEL` počas mesiaca zmenil; neznámy model v tabuľke
ukáže tokeny bez ceny namiesto tichého nesprávneho súčtu.

## Persona / system instructions (`/system_prompt`)

`app/llm/service.py` skladá `system_instruction` pre Gemini z **troch** vrstiev,
per-request (`_build_config()`), nie raz pri štarte:

1. **`_routing_instruction()`** — pevná, v kóde. Zaručuje, že funguje tool-routing
   (`insert_health_record` / `get_health_records` / `sync_garmin_day` /
   `sync_garmin_range` / `search_scientific_studies`) a posiela modelu skutočný
   dnešný dátum + zoznam reálne existujúcich `typ` hodnôt pre daného usera (aby
   nehádal naslepo). Needituj bez rozmyslu, inak sa môže pokaziť tool-calling.
2. **`_BASE_PERSONA`** — pevná, v kóde, spoločná pre všetkých používateľov: "si
   longevity expert a coach", kombinuje dáta z viacerých zdrojov s aktuálnym
   vedeckým výskumom cez Semantic Scholar (`search_scientific_studies`), pri citácii
   vždy uvedie autora a rok. Toto uprav priamo v `service.py`, ak chceš zmeniť
   základný charakter/expertízu bota pre všetkých naraz.
3. **`auth_user.system_prompt`** — voľný text s tvojím osobným profilom (výška, ciele,
   tréningový plán...), per-používateľ, uložený v DB. Nastavuje sa priamo v Telegrame:

```
/system_prompt Mám 196 cm, plán je 2x posilka, 1x beh, 1x plávanie...
/system_prompt            (bez textu - zobrazí aktuálne nastavený prompt)
/system_prompt clear      (zmaže ho)
```

Keďže sa `system_prompt` číta z DB pri každej správe, zmena sa prejaví okamžite —
žiadny reštart appky netreba (zmena `_BASE_PERSONA`/`_routing_instruction()` reštart
vyžaduje, keďže sú v kóde).

### Vedecké štúdie (`search_scientific_studies`)

Keď sa téma oplatí podložiť výskumom (spánok, HRV, regenerácia, tréningová záťaž,
VO2 max, výživa, longevity...), model si sám odvodí kľúčové slová a zavolá
Semantic Scholar API (`app/llm/studies.py`) — bez API kľúča, len s veľmi nízkym
zdieľaným rate limitom (v testovaní padol 429 hneď na prvý request). Free kľúč
(`SEMANTIC_SCHOLAR_API_KEY` v `.env`) dá vlastný, oveľa vyšší limit —
[https://www.semanticscholar.org/product/api#api-key-form](https://www.semanticscholar.org/product/api#api-key-form).
Bez neho sa nástroj len ticho vzdá (žiadne štúdie tú správu) a model to používateľovi
transparentne povie namiesto vymyslenej citácie — to je zámer, nie chyba.

Keďže niektoré modely (pozorované pri `gemini-3.6-flash`) vedia na širšiu otázku
("sprav analýzu") vydať viacero paralelných tool-callov naraz (napr. 5x
`get_health_records` s rôznymi typmi), `_dispatch_tool_calls` to podporuje priamo —
spáruje odpovede podľa `id` volania, nie len podľa mena. Ak model chce po prvom kole
ešte ďalšie tool cally namiesto textovej odpovede, cyklus pokračuje až do
`_MAX_TOOL_ROUNDS` (3), potom vráti čo má, namiesto nekonečného cyklu.

## CI/CD — GitHub Container Registry

`.github/workflows/docker-publish.yml` pri každom push do `main` postaví Docker image
a pushne ho do GHCR ako:

- `ghcr.io/<owner>/<repo>:latest`
- `ghcr.io/<owner>/<repo>:<short-sha>`

Nepotrebuje žiadny extra secret — používa vstavaný `GITHUB_TOKEN` (workflow má
explicitne `permissions: packages: write`).

Prvé spustenie vytvorí GHCR package naviazaný na repo, ktorý je **defaultne privátny**
(ak je repo privátne). Na NASe potom buď:

- nastav package na public (GitHub → repo → Packages → balík → Package settings →
  Change visibility), a `docker pull` pôjde bez prihlásenia, alebo
- nechaj ho privátny a na NASe sa pred pullom prihlás: `docker login ghcr.io -u
  <github_username> -p <PAT s právom read:packages>`.

Na NASe potom stačí v `docker-compose.yml` nahradiť `build: .` za
`image: ghcr.io/<owner>/<repo>:latest` a `docker compose pull && docker compose up -d`.

## Poznámky k dizajnu

- LLM routing nemá hardcoded parser: každý text ide do Gemini s dvoma nástrojmi
  (`insert_health_record`, `get_health_records`); bez tool-callu sa pošle priama
  odpoveď Gemini.
- Argumenty vrátené z function-callingu sa validujú cez Pydantic
  (`app/llm/tools.py`) skôr, než sa zapíšu do DB — chybný/halucinovaný JSON sa
  odchytí a používateľ dostane zrozumiteľnú spätnú väzbu namiesto pádu.
- `health_records.data` je JSONB, takže nový `typ` záznamu nevyžaduje migráciu.
