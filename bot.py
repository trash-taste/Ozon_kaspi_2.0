#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запуск Telegram бота
Запуск: python bot.py
"""

import logging
import os
import signal
import sys
import threading
from src.config.settings import Settings
from src.core.app_manager import AppManager
from src.core.queued_app_manager import QueuedAppManager
from src.telegram.bot_manager import TelegramBotManager
from src.utils.logger import setup_logging
from src.utils.config_loader import load_telegram_config_multi

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    stop_event = threading.Event()
    app_manager = None
    bot_manager = None

    def request_shutdown(signum, frame):
        logger.info("Получен сигнал остановки: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    
    try:
        bot_token, chat_ids = load_telegram_config_multi()
        
        if not bot_token:
            print("❌ Укажите TELEGRAM_BOT_TOKEN в config.txt")
            return 1
            
        if not chat_ids:
            print("❌ Укажите TELEGRAM_CHAT_ID в config.txt")
            return 1
        
        settings = Settings()
        manager_mode = os.getenv("APP_MANAGER_MODE", "direct").strip().lower()
        if manager_mode == "queue":
            app_manager = QueuedAppManager(settings)
            logger.info("App manager mode: queue")
        else:
            app_manager = AppManager(settings)
            logger.info("App manager mode: direct")
        
        bot_manager = TelegramBotManager(bot_token, chat_ids, app_manager)
        app_manager.telegram_bot = bot_manager
        
        print("🤖 Запуск Telegram бота...")
        
        if bot_manager.start():
            print("✅ Telegram бот запущен успешно")
            while not stop_event.wait(1):
                if not bot_manager.is_running:
                    logger.error("Telegram polling неожиданно остановлен")
                    return 1
        else:
            print("❌ Ошибка запуска бота")
            return 1
            
    except Exception as e:
        logger.exception("Критическая ошибка запуска бота")
        print(f"❌ Ошибка: {e}")
        return 1
    finally:
        if bot_manager is not None:
            print("\n🛑 Остановка бота...")
            bot_manager.stop()
        if app_manager is not None:
            app_manager.stop_parsing()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
