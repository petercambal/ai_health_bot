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
- `app/scheduler.py` — `AsyncIOScheduler`, dummy denná úloha `sync_garmin_data` o 03:00
- `db/init.sql` — DDL pre `auth_user` a `health_records`

## Lokálny beh

1. `cp .env.example .env` a doplň `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`,
   `TELEGRAM_WEBHOOK_BASE_URL` (verejná HTTPS URL, napr. z ngrok/cloudflared) a
   `TELEGRAM_WEBHOOK_SECRET`.
2. `docker compose up --build`
3. Dashboard: `http://localhost:8000/dashboard?user_id=<tvoje_telegram_id>`

## Bez Dockeru (uv)

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Vyžaduje bežiace PostgreSQL a `DATABASE_URL` smerujúce naň (napr. `docker compose up db`).

## Lokálny vývoj s reálnym Telegram webhookom (bez rebuildu image)

Na NASe pôjde appka za reverse proxy, ale kým vyvíjaš lokálne, nemusíš pri každej
zmene stavať Docker image — appka beží natívne s `--reload` a webhook ide cez tunel:

1. Nahoď iba databázu: `docker compose up -d db`
2. `.env` musí mať `DATABASE_URL=postgresql://health:health@localhost:5432/health_tracker`
   (appka teraz beží mimo Docker siete, takže `localhost`, nie `db`).
3. Spusti tunel, napr. `ngrok http 8000` (alebo `cloudflared tunnel --url http://localhost:8000`).
   Dostaneš HTTPS URL typu `https://abc123.ngrok-free.app`.
4. Daj túto URL do `.env` ako `TELEGRAM_WEBHOOK_BASE_URL`.
5. Spusti appku: `uv run uvicorn app.main:app --reload` — pri štarte sa webhook
   zaregistruje na Telegrame s touto URL.
6. Píš botovi v Telegrame — správy chodia cez tunel na tvoj lokálny bežiaci proces;
   `--reload` zachytí každú zmenu kódu bez reštartu appky (netreba ani reštart kvôli
   webhooku, ten sa registruje len raz pri štarte procesu).

Pozor: free ngrok URL sa mení pri každom reštarte tunela — ak tunel reštartuješ, uprav
`TELEGRAM_WEBHOOK_BASE_URL` a reštartni aj `uvicorn`, nech sa webhook prehlási na novú
URL.

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

## Persona / system instructions (`system_prompt.txt`)

`app/llm/service.py` skladá `system_instruction` pre Gemini z dvoch častí:

1. pevná routing inštrukcia v kóde (`_ROUTING_INSTRUCTION`) — zaručuje, že fungujú tri
   cesty: priama odpoveď / `insert_health_record` / `get_health_records`; needituj ju,
   inak sa môže pokaziť tool-calling.
2. `app/llm/system_prompt.txt` — voľný text s tvojou personou a profilom (napr. "Si môj
   kondičný tréner, mám 196 cm, plán je 2x posilka..."), ktorý sa pripojí za routing
   inštrukciu. Je v `.gitignore` (obsahuje osobné zdravotné údaje), takže priprav si
   vlastný podľa šablóny:

```bash
cp app/llm/system_prompt.example.txt app/llm/system_prompt.txt
```

Zmena súboru sa prejaví po reštarte procesu (`docker compose up --build` / reštart
uvicorn) — číta sa raz pri štarte, nie pri každej správe.

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
