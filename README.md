# Telegram-LLM Health Tracker & Dashboard

A single-process async Python application: Telegram bot (webhook) → Gemini (function
calling) → PostgreSQL, plus a simple FastAPI/Jinja2 dashboard with Chart.js.

## Layers

- `app/main.py` — FastAPI app, `lifespan` (DB pool, webhook, scheduler)
- `app/config.py` — settings from `.env` (pydantic-settings)
- `app/database.py` — asyncpg pool + query functions over `health_records` and `auth_user`
- `app/llm/` — Gemini client, tool schemas (`tools.py`), routing logic (`service.py`)
- `app/telegram/` — aiogram `Bot`/`Dispatcher` in webhook mode, `auth_user` check
- `app/routers/` — `telegram.py` (webhook endpoint), `dashboard.py` (`/dashboard`, `/api/stats`)
- `app/garmin/` — `sync.py` (fetching and storing the daily Garmin digest, catch-up),
  `bootstrap.py` (one-time interactive login)
- `app/scheduler.py` — `AsyncIOScheduler`, daily jobs at 09:00 (Garmin sync, nutrition
  sync, daily report push)
- `db/init.sql` — DDL for `auth_user`, `health_records`, `integrations`, `token_usage`

## Database

All tables live in their own Postgres schema, **`health_tracker`** (not `public`) —
the DB on the NAS is shared with other projects, so this avoids table-name
collisions. The schema is set via `server_settings={"search_path": ...}` when the
`asyncpg` pool is created (`app/database.py`, the `SCHEMA` constant) — **not** via a
plain `SET search_path`, since asyncpg always resets that with `RESET ALL` when a
connection is returned to the pool. The schema name must match in both
`db/init.sql` and `app/database.py`.

By default the app expects an **existing** PostgreSQL on the NAS (not a local
container) — `docker-compose.yml` no longer starts its own Postgres, just the app.
`DATABASE_URL` (in `.env` when running via `uv`, or in `docker-compose.override.yml`
when running via Docker — see below) points directly at it:

```
DATABASE_URL=postgresql://<user>:<password>@192.168.1.229:15432/<dbname>
```

Before the first run, the tables need to be created once on this DB from
[db/init.sql](db/init.sql) (it uses `CREATE TABLE IF NOT EXISTS`, so it's safe to
run repeatedly):

```bash
psql "postgresql://<user>:<password>@192.168.1.229:15432/<dbname>" -f db/init.sql
```

If you ever need a local Postgres (e.g. developing away from the NAS network), it's
available as an optional profile and doesn't start by default:

```bash
docker compose --profile local-db up -d db
```

## Running locally

`docker-compose.yml` is checked into git, so the `app` service's `environment:`
only has placeholder values (real secrets never belong there). Real values go into
`docker-compose.override.yml` (gitignored), which Compose automatically merges on
top:

1. `cp docker-compose.override.yml.example docker-compose.override.yml` and fill in
   `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_WEBHOOK_BASE_URL` (a public
   HTTPS URL, e.g. from ngrok/cloudflared), `TELEGRAM_WEBHOOK_SECRET`, and
   `DATABASE_URL` (the NAS DB, see above).
2. `docker compose up --build`
3. Dashboard: `http://localhost:8000/dashboard?user_id=<your_telegram_id>`

On the NAS (Portainer/Container Manager), instead of `docker-compose.override.yml`
you can just put the same variables directly into the project's `environment:` in
the UI, or replace the placeholder values directly in the `docker-compose.yml` you
paste there (that copy never goes back into git).

## Without Docker (uv)

```bash
uv sync
uv run uvicorn app.main:app --reload
```

## Local development with a real Telegram webhook (without rebuilding the image)

On the NAS the app runs behind a reverse proxy, but while developing locally you
don't need to build a Docker image for every change — the app runs natively with
`--reload` and the webhook goes through a tunnel:

1. `.env` has `DATABASE_URL` pointing directly at the NAS DB (`192.168.1.229:15432`)
   — this is the same whether you run the app via Docker or natively, no change
   needed.
2. Start a tunnel, e.g. `ngrok http 8000` (or
   `cloudflared tunnel --url http://localhost:8000`). You'll get an HTTPS URL like
   `https://abc123.ngrok-free.app`.
3. Put this URL into `.env` as `TELEGRAM_WEBHOOK_BASE_URL`.
4. Start the app: `uv run uvicorn app.main:app --reload` — on startup the webhook
   gets registered with Telegram using this URL.
5. Message the bot in Telegram — messages come through the tunnel to your locally
   running process; `--reload` picks up every code change without restarting the
   app (no restart is needed for the webhook either, it's only registered once at
   process startup).

Note: the free ngrok URL changes every time the tunnel restarts — if you restart the
tunnel, update `TELEGRAM_WEBHOOK_BASE_URL` and restart `uvicorn` too, so the webhook
re-registers with the new URL.

### Automated via `scripts/dev_with_tunnel.sh`

This script does steps 2-4 above for you — it restarts the cloudflared tunnel,
waits for the new `trycloudflare.com` URL to become DNS-resolvable (cloudflared
prints it before it's actually reachable — calling `set_webhook` right after
startup would otherwise fail with "Failed to resolve host"), writes it into `.env`,
and starts the app:

```bash
./scripts/dev_with_tunnel.sh
```

Deliberately runs **without** `--reload` — if the app crashes during startup (e.g.
exactly that DNS race), the reloader process can survive and keep port 8000 held
even after the script ends. In PyCharm you can wire this up as a separate
**Shell Script** run configuration (Script path: `scripts/dev_with_tunnel.sh`) —
Start runs the whole flow, Stop sends `SIGTERM` and the script cleanly shuts down
both the app and the tunnel (no orphaned process). Keep the original Start/Debug
configuration with `--reload` around for fast code iteration without a tunnel.

## User authorization (`auth_user`)

The bot only replies to a `telegram_id` that's in the `auth_user` table with
`is_active = TRUE`; everyone else gets "You don't have access" and the request is
logged (`app/telegram/bot.py`). This is independent of `TELEGRAM_WEBHOOK_SECRET`,
which only verifies that the webhook request itself came from Telegram — it says
nothing about which specific Telegram account is allowed to talk to the bot.

Adding an allowed user (find your own `telegram_id` e.g. via `@userinfobot`):

```sql
INSERT INTO auth_user (telegram_id, display_name) VALUES (123456789, 'Peter');
```

or programmatically via `app.database.add_auth_user(telegram_id, display_name)`.

## Garmin Connect sync (`app/garmin/`)

A third Gemini tool alongside `insert_health_record`/`get_health_records`:
`sync_garmin_day` — when you tell the bot e.g. "fetch yesterday's Garmin data",
Gemini calls this tool, the app fetches the data from Garmin Connect and stores it.

**How authentication works** — the password is never stored, not in `.env`, not in
the DB:

1. One-time, interactively (because of MFA), you run this for each user/friend:
   ```bash
   uv run python -m app.garmin.bootstrap <telegram_id>
   ```
   It asks for the Garmin email/password/MFA code right there in the terminal, logs
   in, and stores **only** the serialized session token (`garminconnect`'s
   `client.dumps()`/`loads()`) into the `integrations` table (`service='garmin'`,
   tied to `auth_user.telegram_id`) — the password only ever lives in the memory of
   this one script run and is never written anywhere.
2. From then on, `sync_day()`/`sync_catch_up()` (`app/garmin/sync.py`) load the
   session from the DB and log in with it (`Garmin().login(tokenstore=session_json)`)
   — no password, no MFA. After every run, the (possibly renewed/refreshed) token
   is saved back to the DB.
3. If the token stops working (e.g. a refresh token unused for too long), the app
   returns a clear error instructing you to run the bootstrap again — it never
   tries to log in with a username/password itself (it doesn't have a password
   available).

This design naturally supports multiple people: each friend has their own row in
`integrations`, their own Garmin account, their own history in `health_records`
(`typ='garmin_daily'`).

**Linking via the web instead of the terminal** — for anyone without terminal
access (e.g. friends), there's also a web flow: message the bot `/garmin_link` in
Telegram and you get back a link to `/garmin-login?token=...`
(`app/routers/garmin_login.py`). The token is HMAC-signed (`app/link_token.py`, key
= `TELEGRAM_WEBHOOK_SECRET`), scoped to your `telegram_id`, and valid for 30 minutes
— so the app never exposes a raw `user_id` in a URL that someone could guess or use
for someone else's account. The form walks you through email/password and any MFA
step exactly like the CLI bootstrap, and likewise never stores the password, only
the resulting session token.

**Daily data** is stored as a single JSONB "digest" object per day (steps,
distance, calories, heart rate, sleep, HRV, SpO2, body battery, training readiness,
hydration, activity list) — not per-minute intraday series or laps, to keep the
record compact; easy to extend if you want. On every sync for a given day, the
**existing record is deleted and replaced** with a new one
(`database.replace_health_record_for_day`), so re-syncing the same day never
duplicates anything.

**A cron job** at 09:00 (`app/scheduler.py`) goes through every user with a linked
Garmin account and, for each, catches up every day from their last synced day
through **yesterday** (today at 9:00 would still be incomplete, e.g. overnight
sleep), up to `MAX_RANGE_DAYS` (31) days per run — after a longer outage it catches
up gradually over several days/cron runs rather than in one big burst.

**Multi-day ranges** — alongside `sync_garmin_day` (one day), there's also
`sync_garmin_range` (`start_date`, `end_date`, max **31 days** at once,
`app/garmin/sync.py`), for initializing history without calling day by day. In
Telegram it's enough to say e.g. "fetch Garmin data from Aug 1 to Aug 31" — Gemini
picks the right tool. Both tools and `sync_catch_up` now share one function
(`sync_range()`), which logs into Garmin **once** for the whole range (not once per
day as before) — faster and less prone to Garmin rate-limiting.

## Token usage audit (`token_usage`)

Every Gemini API call (one Telegram message can trigger two — the original and a
follow-up, when `get_health_records` feeds data back to the model) is logged to
`health_tracker.token_usage`: `user_id`, timestamp, model, `prompt_tokens`,
`response_tokens`, `total_tokens` (`app/llm/service.py` → `_log_usage()`,
best-effort — a logging failure never breaks the bot's actual reply).

**`/tokens [YYYY-MM]`** in Telegram shows a monthly summary (current month if no
argument) — call count, tokens, and an estimated cost in USD from
`app/llm/pricing.py` (a hand-maintained USD/1M-token price table, verified
2026-09-11 against ai.google.dev/gemini-api/docs/pricing — Gemini's pricing changes
and isn't exposed via the API, so the table needs manual updates when
models/prices change). The summary is broken down per model, in case you changed
`GEMINI_MODEL` during the month; a model not in the table shows tokens without a
cost instead of silently showing a wrong total.

## Persona / system instructions (`/system_prompt`)

`app/llm/service.py` builds Gemini's `system_instruction` from **three** layers,
per request (`_build_config()`), not once at startup:

1. **`_routing_instruction()`** — fixed, in code. Guarantees tool routing works
   (`insert_health_record` / `get_health_records` / `sync_garmin_day` /
   `sync_garmin_range` / `search_scientific_studies` / and the rest) and sends the
   model today's real date plus the list of `typ` values that actually exist for
   that user (so it doesn't guess blindly). Don't edit this carelessly, or
   tool-calling can break.
2. **`_BASE_PERSONA`** — fixed, in code, shared across all users: "you are a
   longevity expert and coach", combines data from multiple sources with current
   scientific research via OpenAlex (`search_scientific_studies`), always cites the
   author and year. Edit this directly in `service.py` if you want to change the
   bot's base character/expertise for everyone at once.
3. **`auth_user.system_prompt`** — free-form text with your personal profile
   (height, goals, training plan...), per user, stored in the DB. Set directly in
   Telegram:

```
/system_prompt I'm 196cm, my plan is 2x strength training, 1x running, 1x swimming...
/system_prompt            (no text - shows the currently set prompt)
/system_prompt clear      (clears it)
```

Since `system_prompt` is read from the DB on every message, a change takes effect
immediately — no app restart needed (changing `_BASE_PERSONA`/`_routing_instruction()`
does require a restart, since those live in code).

### Scientific studies (`search_scientific_studies`)

When a topic is worth grounding in research (sleep, HRV, recovery, training load,
VO2 max, nutrition, longevity...), the model derives keywords itself and calls the
OpenAlex API (`app/llm/studies.py`) — works with no API key, no signup or approval
needed.

**Why OpenAlex and not Semantic Scholar**: the tool was originally built on
Semantic Scholar, but its unauthenticated rate limit (~100 requests/5 min shared
across **every** unauthenticated caller in the world) proved unusable in practice —
both during testing and real use, almost every request came back 429, and getting
your own API key requires manual approval (the community reports ~5 days of
waiting). OpenAlex is an open, keyless alternative built exactly for this use case
— verified live on 2026-09-13, works instantly. The optional `OPENALEX_EMAIL` in
`.env` (not an API key, just a contact email) puts requests into the "polite pool"
for more reliable service — no approval needed, but I didn't auto-fill it, since
it's your email and requests go out to an external service with it.

If OpenAlex does fail (an outage, timeout), the tool quietly gives up (no studies
for that message) and the model tells the user this transparently instead of
inventing a citation — that's intentional, not a bug.

Since some models (observed with `gemini-3.6-flash`) can issue several parallel
tool calls at once for a broad question ("run an analysis") (e.g. 5x
`get_health_records` with different types), `_dispatch_tool_calls` supports this
directly — it matches responses by the call's `id`, not just by name. If the model
still wants more tool calls after the first round instead of a text reply, the
loop continues up to `_MAX_TOOL_ROUNDS`, then forces one final text-only reply
instead of looping forever.

## Daily report (`/daily_report`)

A shortcut for exactly the question you could ask yourself — runs a fixed prompt
(`DAILY_REPORT_PROMPT` in `app/telegram/bot.py`) that summarizes yesterday across
**all** of your recorded data (not just Garmin — weight, food, glucose readings,
whatever you log), calls out any standout values with a scientific explanation
(`search_scientific_studies`), and tells you whether to train fully today or ease
off. Available on demand (the command, or asking the same thing in your own words),
and also sent automatically: `app/scheduler.py`'s `send_daily_reports` job runs for
every active user daily at 09:00, first making sure yesterday's data from every
linked integration (Garmin, kaloricketabulky.sk nutrition, ...) is fresh via
`app.integrations.ensure_day_synced(..., force=True)`, then generating the report
and pushing it proactively via `bot.send_message` (see `send_message()` in
`app/telegram/bot.py`).

## Message formatting (`app/telegram/formatting.py`)

Gemini routinely generates markdown (`### headings`, `**bold**`, `* bullets`,
`> quotes`), which Telegram would otherwise display literally, `#`/`*` characters
and all. The bot runs with `parse_mode=HTML`, and `to_telegram_html()` converts
markdown into Telegram's HTML subset (headings → bold, bullets → `•`, `> quote` →
`<blockquote>`, escapes `&`/`<`/`>`). `send_reply()` (in `app/telegram/bot.py`)
falls back to plain text if Telegram ever rejects the converted HTML as invalid for
any reason — a formatting bug must never block a reply from being delivered.

## CI/CD — GitHub Container Registry

`.github/workflows/docker-publish.yml` builds a Docker image on every push to
`main` and pushes it to GHCR as:

- `ghcr.io/<owner>/<repo>:latest`
- `ghcr.io/<owner>/<repo>:<short-sha>`

Needs no extra secret — it uses the built-in `GITHUB_TOKEN` (the workflow
explicitly declares `permissions: packages: write`).

The first run creates a GHCR package tied to the repo, which is **private by
default** (if the repo is private). On the NAS, then either:

- set the package to public (GitHub → repo → Packages → the package → Package
  settings → Change visibility), and `docker pull` will work without logging in, or
- leave it private and log in on the NAS before pulling: `docker login ghcr.io -u
  <github_username> -p <PAT with read:packages scope>`.

On the NAS it's then enough to replace `build: .` with
`image: ghcr.io/<owner>/<repo>:latest` in `docker-compose.yml` and run
`docker compose pull && docker compose up -d`.

## Design notes

- LLM routing has no hardcoded parser: every message goes to Gemini along with all
  of its declared tools; without a tool call, Gemini's direct reply is sent.
- Arguments returned from function calling are validated via Pydantic
  (`app/llm/tools.py`) before they're written to the DB — invalid/hallucinated JSON
  is caught and the user gets clear feedback instead of a crash.
- `health_records.data` is JSONB, so a new record `typ` never requires a migration.
