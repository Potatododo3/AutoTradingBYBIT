"""
Bitunix Futures Telegram Trading Bot
Entry point — run with: python main.py
Cross-platform (Windows + Linux/Mac)
"""
import asyncio
import logging
import platform
import signal
import sys

from telegram.ext import Application

import config
from bybit_client import BybitClient
from database import Database
from debug_handler import DebugHandler
from journal import Journal
from order_manager import OrderManager
from risk_manager import RiskManager
from settings_handler import SettingsHandler
from telegram_handlers import BotHandlers
from utils import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()

    mode = "🟡 DEBUG (dry-run active)" if config.DEBUG_MODE else "🔴 LIVE"
    logger.info(f"Starting Bybit Trading Bot — Mode: {mode}")
    logger.info(f"Authorized user ID: {config.AUTHORIZED_USER_ID}")
    logger.info(f"Bybit base URL: {config.BYBIT_BASE_URL}")
    logger.info(f"Database path: {config.DB_PATH}")

    # Initialise database
    db = Database()
    await db.connect()

    # Initialise shared components
    client = BybitClient()
    journal = Journal()
    order_manager = OrderManager(client, db, journal)
    risk_manager = RiskManager(client)

    # Restore active trades from DB on startup.
    # load_from_db() also reconciles any trades whose limit entries
    # filled while the bot was offline (places TPs automatically).
    await order_manager.load_from_db()

    # Build Telegram application
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    # Register handlers — order matters!
    # BotHandlers first (trade wizard + all main commands)
    bot_handlers = BotHandlers(client, order_manager, risk_manager, journal, db)
    bot_handlers.register(app)

    # SettingsHandler second
    SettingsHandler().register(app)

    # DebugHandler third (must come before the catch-all unknown handler)
    DebugHandler(client, order_manager, risk_manager, journal, db).register(app)

    # Catch-all unknown command handler LAST so it never swallows real commands
    bot_handlers.register_unknown_handler(app)

    logger.info("All handlers registered.")

    if config.DEBUG_MODE:
        logger.info("⚠️  DEBUG_MODE=true — trade orders will NOT be placed on the exchange.")

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutdown signal received.")
        stop_event.set()

    if platform.system() != "Windows":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Give bot_handlers a reference to the running app so the
        # soft SL monitor can push alerts via app.bot.send_message()
        bot_handlers._app = app

        # Start the soft SL candle-close monitor
        bot_handlers.start_monitor()
        logger.info("✅ Bot is running. Press Ctrl+C to stop.")

        if platform.system() == "Windows":
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
        else:
            await stop_event.wait()

        logger.info("Shutting down...")
        await bot_handlers.stop_monitor()
        await app.updater.stop()
        await app.stop()

    await client.close()
    await journal.close()
    await db.close()
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)