import asyncio
import logging
import sys
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import settings
from app.database import db
from app.bot.handlers import router

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("PaperFinderBot")


async def healthcheck_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "Paper Finder Telegram Bot"})


async def start_healthcheck_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", healthcheck_handler)
    app.router.add_get("/health", healthcheck_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.PORT)
    await site.start()
    logger.info(f"Healthcheck server running on http://0.0.0.0:{settings.PORT}")
    return runner


async def main():
    if not settings.is_bot_token_valid:
        logger.critical(
            "❌ [FATAL] BOT_TOKEN is invalid or unset! "
            "Please provide a valid Telegram Bot token in .env or environment variables."
        )
        sys.exit(1)

    await db.init_db()
    health_runner = await start_healthcheck_server()

    if settings.clean_proxy:
        logger.info(f"Using proxy for Telegram connection: {settings.clean_proxy}")
        session = AiohttpSession(proxy=settings.clean_proxy)
    else:
        session = AiohttpSession()

    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot polling started successfully!")
        await dp.start_polling(bot)
    finally:
        await health_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped gracefully.")
