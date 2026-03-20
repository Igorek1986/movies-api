# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Run the server
```bash
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8888 --reload
```

### Install dependencies
```bash
poetry install
```

### Start PostgreSQL for development (Docker)
```bash
docker compose -f docker-compose.dev.yml up -d
```

### Deploy production (Docker)
```bash
docker compose up -d
```

### Run migrations (when adding new DB columns to existing installs)
```bash
poetry run python migrations/migrate_<name>.py
```

> Migration scripts live in `migrations/` and are committed to the repository.

## Environment Setup

Copy `.env.template` to `.env` and fill in the values. Required fields:
- `DB_USER`, `DB_PASSWORD`, `DB_NAME` — PostgreSQL credentials
- `TMDB_TOKEN` — TMDB API Bearer token (just the token, `Bearer` prefix added automatically)
- `MYSHOWS_AUTH_URL`, `MYSHOWS_API` — MyShows API endpoints
- `RELEASES_DIR` — path relative to `$HOME` containing gzipped JSON release files (see NUMParser below)
- `ADMIN_PASSWORD` — password for `/admin` and `/stats` panels
- `BASE_URL` — public URL of the server (used for Telegram webhook and links)

Optional:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_NAME`, `TELEGRAM_ADMIN_IDS` — Telegram bot
- `DEBUG` — set `True` to enable Swagger UI at `/docs` (disabled in production by default)

Database tables are created automatically on startup via SQLAlchemy `create_all`.

## RELEASES_DIR — NUMParser

`RELEASES_DIR` must point to a directory containing gzipped JSON category files (e.g. `movies_2025.json.gz`). The recommended way to populate it is **NUMParser**:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Igorek1986/NUMParser/refs/heads/main/install-numparser.sh)
```

NUMParser automatically fetches and updates category data, placing files into the configured releases directory that movies-api reads.

## Architecture

**FastAPI** application serving as a backend for the NUMParser / Lampa media client. Single data store:

- **PostgreSQL** (async via SQLAlchemy + asyncpg) — all data: users, devices, timecodes, stats, settings, Telegram, sessions, TMDB cache

### Routers and their roles

| Module | Prefix | Role |
|--------|--------|------|
| `app/main.py` | `/` | Core API: serves category JSON from gzipped files in `RELEASES_DIR`, fetches/caches TMDB metadata, M3U proxy, history endpoint |
| `app/api/auth.py` | — | Web UI: login, register, password change, account deletion, notifications settings, 2FA |
| `app/api/devices.py` | `/profiles` | Device CRUD, device activation flow (`/device/code` → `/device/link` → `/device/status`) |
| `app/api/sessions.py` | `/sessions` | Web session list and revocation |
| `app/api/timecodes.py` | `/timecode` | Save/export/import timecodes, history endpoint |
| `app/api/telegram.py` | `/telegram` | Generate link codes, Telegram link status, bot webhook receiver |
| `app/api/tg_miniapp.py` | `/tg-app` | Telegram Mini App (admin panel via Telegram WebApp) |
| `app/api/myshows_sync.py` | `/myshows/sync` | Full MyShows→PostgreSQL sync via SSE streaming |
| `app/myshows.py` | `/myshows` | Proxy for MyShows auth + filesystem cache (gzipped JSON in `myshows_cache/`) |
| `app/admin.py` | `/admin` | Admin panel: user management, roles, premium, settings |
| `app/stats.py` | `/stats` | Stats dashboard (PostgreSQL-backed) |
| `app/bot.py` | — | Telegram bot (aiogram v3): /start /link /status /broadcast, support chat |

**Dead code**: `app/api/profiles.py` — not imported in `main.py`, replaced by `devices.py`.

### Models (`app/db/models.py`)

| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | Accounts: role, premium_until, notifications, 2FA, inactive tracking |
| `Device` | `devices` | Lampa devices, each with unique `token` (plaintext) |
| `DeviceCode` | `device_codes` | Short-lived activation codes (ABC-123, 10 min TTL) |
| `Session` | `sessions` | Web sessions with sliding window TTL |
| `Timecode` | `timecodes` | Watch progress keyed by `(device_id, lampa_profile_id, card_id, item)` |
| `MediaCard` | `media_cards` | TMDB metadata cache (replaces `tmdb_cache.json`) |
| `LampaProfile` | `lampa_profiles` | Lampa internal profiles per device |
| `AppSetting` | `app_settings` | Key-value settings editable via `/admin` without restart |
| `TelegramUser` | `telegram_users` | User ↔ Telegram account link |
| `TelegramLinkCode` | `telegram_link_codes` | 6-digit link codes (10 min TTL) |
| `SupportMessage` | `support_messages` | Support chat messages (user ↔ admin via bot) |
| `MyShowsUser` | `stats_myshows_users` | Stats: MyShows auth events |
| `ApiUser` | `stats_api_users` | Stats: API users by IP with GeoIP |
| `CategoryRequest` | `stats_category_requests` | Stats: category request counts |
| `PasswordResetToken` | `password_reset_tokens` | Password recovery tokens |
| `Totp2faPending` | `totp_2fa_pending` | Pending 2FA setup sessions |

### Key patterns

**Authentication — two separate mechanisms:**
- *Web UI*: Cookie `session_key` → looked up in `Session` table → returns `User`. Sliding window TTL (configurable via `settings_cache`). `get_current_user()` in `dependencies.py`.
- *Lampa API*: `?token=KEY` query param → `Device.token` (stored plaintext). `get_device_by_token()` in `dependencies.py`.

**Device Activation Flow**: Lampa calls `/device/code` to get a short code (e.g. `ABC-123`). User enters the code on the web page, selects a device → `/device/link`. Lampa polls `/device/status` until `linked=true` and receives the plaintext token. Codes expire in 10 minutes.

**User roles**: `simple` (1 device, 5000 timecodes), `premium` (3 devices), `super` (unlimited). Managed via `/admin`. Premium expiry handled by background task in `app/tasks.py`.

**TMDB cache**: In-memory dict `{(media_type, tmdb_id): data}` loaded from `media_cards` table on startup. Written back via `upsert_tmdb_cache()` after each TMDB API fetch. No file-based cache.

**Category data**: Release files are gzipped JSON in `~/RELEASES_DIR/`. Two file formats supported: lampac (`results` key or plain list) and NUMParser (`items` key with `media_type`/`id` fields requiring TMDB enrichment). Filtering watched items runs **before** TMDB enrichment. `card_id` format: `"{tmdb_id}_{media_type}"`.

**App settings** (`app/settings_cache.py`): 33+ configurable keys (device limits, rate limits, TTLs, quiet hours, etc.) stored in `app_settings` table. Read at startup into memory, updated live via `/admin` without server restart. Use `settings_cache.get(key)`, `get_int(key)`, `get_role_limit(role, resource)`.

**Background tasks** (`app/tasks.py`): Daily job at 05:00 — demotes expired premium users, optionally sets timecode grace period, deletes inactive accounts, sends Telegram notifications with quiet hours support.

**Telegram bot** (`app/bot.py`): aiogram v3. Supports polling (`TELEGRAM_USE_POLLING=True`) or webhook (`{BASE_URL}/bot/webhook`). Webhook set on startup, session closed on shutdown. Commands: `/start`, `/status`. Admin commands: `/setpremium`, `/setsuper`, `/setsimple`, `/broadcast`. Account linking: site generates a code → user clicks deep link `t.me/bot?start=CODE` → bot receives `/start CODE` automatically. Support chat: user messages forwarded to admins, admin replies routed back.

**Telegram Mini App** (`/tg-app`): Mobile admin panel served inside Telegram. Auth via HMAC-SHA256 validation of `X-Telegram-Init-Data` header. Tabs: stats, user management, support chat.

**`/docs` (Swagger UI)**: Disabled in production. Enable with `DEBUG=True` in `.env`.

**Origin blocking**: Middleware in `main.py` checks request origins against `BANNED_PATTERNS` env var (JSON array of strings), returning a fake 200 response from `blocked.json` for banned origins.

**Stats**: PostgreSQL-backed (`MyShowsUser`, `ApiUser`, `CategoryRequest`). GeoIP via async `ipwho.is` lookup (skips localhost/private IPs). Fire-and-forget via `asyncio.create_task`.

**`app/db/__init__.py`** imports `models` so all SQLAlchemy model classes are registered in `Base.metadata` before `init_db()` runs — without this `create_all` creates no tables.

**Lampa hash** (`app/utils.py:lampa_hash`): Java-style hashCode with multiplier 31, used to generate episode/movie identifiers compatible with Lampa's internal format.

## File structure highlights

```
app/
  main.py             — FastAPI app, category serving, TMDB enrichment
  bot.py              — Telegram bot (aiogram v3)
  admin.py            — /admin panel
  stats.py            — /stats dashboard
  myshows.py          — MyShows proxy + filesystem cache
  settings_cache.py   — In-memory settings cache (from app_settings table)
  tasks.py            — Background tasks (premium expiry, inactive cleanup)
  config.py           — Pydantic Settings (reads .env)
  utils.py            — lampa_hash, helpers
  rate_limit.py       — In-memory sliding-window rate limiter
  templates.py        — Jinja2 template loader
  db/
    models.py         — All SQLAlchemy models
    database.py       — Engine, session maker, init_db()
  api/
    auth.py           — Login/register/password/2FA/notifications
    devices.py        — Device CRUD + activation (GET /profiles)
    sessions.py       — Session management
    timecodes.py      — Timecode CRUD + import/export + history
    myshows_sync.py   — MyShows sync (SSE streaming)
    telegram.py       — Telegram link/unlink/webhook
    tg_miniapp.py     — Telegram Mini App API
    dependencies.py   — get_current_user(), get_device_by_token()
    profiles.py       — DEAD (not imported)
dev/                  — Local-only (gitignored): one-time migration scripts for existing installs
scripts/
  install.sh          — Unified install/uninstall/switch script (systemd or Docker)
nginx/                — Example nginx config for HTTPS
templates/            — Jinja2 HTML templates (Pico CSS v2)
static/
  css/main.css        — Pico CSS overrides
  js/
    profiles.js       — Device activation, MyShows sync, import handlers
    password.js       — Password strength validation UI
    stats.js          — Stats dashboard
```
