# CLAUDE.md — Project Context & Strict Architectural Rules

## 1. Project Identity
- **Name:** Paper Finder Telegram Bot (ربات تلگرامی مقالهیاب)
- **Language & Stack:** Python 3.11+, `aiogram 3.x` (Async), `httpx` (Async HTTP), `curl_cffi` (Anti-TLS fingerprint bypass for PDF fallback), `aiosqlite` (Local Async DB), `pydantic-settings`.
- **Target Environments:** Windows Local (behind SOCKS5/HTTP proxy), Linux VPS (Docker), and Hugging Face Spaces (Port 7860 Healthcheck).

## 2. Strict Rules for AI Models (Flash 3.8 / Claude / Antigravity) — READ BEFORE EDITING
When generating, refactoring, or updating code for this repository, you MUST obey these 7 rules to prevent runtime bugs:

1. **Telegram Callback Data 64-Byte Limit:**
   - NEVER put raw DOIs, URLs, or paper titles inside `InlineKeyboardButton(callback_data=...)`.
   - Always save/cache the `PaperItem` in SQLite (`papers_cache` table) via `await db.save_paper(paper)` and use its 10-character `short_id` in callback data (e.g., `abs:a1b2c3d4e5` or `pdf:a1b2c3d4e5`).

2. **Aiogram 3.x Syntax Only:**
   - Do NOT use `aiogram 2.x` syntax (such as `@dp.message_handler` or `executor.start_polling`).
   - Always use `Router()`, `@router.message(...)`, `@router.callback_query(F.data.startswith(...))`, and `DefaultBotProperties(parse_mode=ParseMode.HTML)`.

3. **HTML Escaping in Telegram Messages:**
   - Academic paper titles and abstracts frequently contain `<`, `>`, and `&` symbols (e.g., math formulas or chemical names).
   - ALWAYS wrap dynamic text (`title`, `authors`, `abstract`, `venue`, `doi`) with `html.escape(text)` before formatting into HTML strings.

4. **Strict Async Flow (No Blocking I/O):**
   - Never use synchronous `requests` or synchronous `sqlite3` inside handlers.
   - Use `httpx.AsyncClient` for API calls, `aiosqlite` for database queries, and `curl_cffi.requests.AsyncSession(impersonate="chrome120")` for fallback PDF extraction and downloading.

5. **Proxy Compatibility:**
   - All Telegram connections in `main.py` must check `settings.clean_proxy`. If set (e.g., `socks5://127.0.0.1:10808`), pass `AiohttpSession(proxy=settings.clean_proxy)` to `Bot(...)`.
   - All `httpx` and `curl_cffi` requests in `app/services/aggregator.py` must also receive `proxy=settings.clean_proxy`.

6. **OpenAlex Inverted Index Reconstruction:**
   - OpenAlex returns abstracts as `abstract_inverted_index` (mapping word -> list of positions). Always convert it to plain text via `reconstruct_abstract()` before storing.

7. **Hugging Face Spaces Port 7860 Alive Check:**
   - Do NOT remove the `aiohttp.web` healthcheck server in `main.py`. Hugging Face Spaces requires an active listener on `0.0.0.0:7860`.

## 3. How to Add New Features
- **Adding a new academic source:** Create an async method `search_<source>(query: str)` in `app/services/aggregator.py` returning `list[PaperItem]`, then include it inside `asyncio.gather()` in `search_papers()`.
- **Adding new bot commands:** Register handlers in `app/bot/handlers.py` and keep business/network logic inside `app/services/`.

## 4. Git & GitHub Workflow for Continuous Updates
- Branch: `main`
- Standard commit messages: `feat: ...`, `fix: ...`, `refactor: ...`, `docs: ...`
- Push workflow:
  ```bash
  git add .
  git commit -m "feat: description of changes"
  git push origin main
  ```

## 5. Linux VPS Deployment & Hot Reload
- Production deployment uses Docker Compose:
  ```bash
  # First time:
  git clone <REPO_URL> paper-finder-bot
  cd paper-finder-bot
  cp .env.example .env
  nano .env  # set BOT_TOKEN, CONTACT_EMAIL
  docker compose up -d --build

  # On every update from GitHub:
  git pull origin main
  docker compose up -d --build
  ```

