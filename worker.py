#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parser worker process.

The Telegram bot writes jobs to runtime/jobs/pending. This process takes one
job at a time and runs the existing AppManager pipeline in a separate process.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from aiogram import Bot

from src.config.settings import Settings
from src.core.app_manager import AppManager
from src.core.job_queue import ParserJobQueue
from src.utils.config_loader import load_telegram_config_multi
from src.utils.logger import setup_logging

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)


class WorkerTelegramNotifier:
    def __init__(self, bot_token: str | None):
        self.bot_token = bot_token
        self.is_running = bool(bot_token)

    def send_message_sync(self, text: str, user_id: str = None) -> bool:
        if not self.bot_token or not user_id:
            return False

        async def _send():
            bot = Bot(token=self.bot_token)
            try:
                await bot.send_message(chat_id=user_id, text=text)
            finally:
                await bot.session.close()

        try:
            asyncio.run(_send())
            return True
        except Exception as exc:
            logger.error("Telegram notification failed: %s", exc)
            return False


def _result_summary(app_manager: AppManager, user_id: str | None) -> dict[str, Any]:
    results = app_manager.user_results.get(user_id, app_manager.last_results)
    if not results:
        return {}
    stats = results.get("parsing_stats", {})
    return {
        "category_url": results.get("category_url"),
        "output_folder": results.get("output_folder"),
        "total_products": results.get("total_products", 0),
        "successful_products": results.get("successful_products", 0),
        "failed_products": results.get("failed_products", 0),
        "total_sellers": results.get("total_sellers", 0),
        "successful_sellers": results.get("successful_sellers", 0),
        "parsing_stats": stats,
    }


def _watch_cancel(
    queue: ParserJobQueue,
    app_manager: AppManager,
    user_id: str | None,
    since: float,
    done_event: threading.Event,
):
    while not done_event.wait(1):
        if queue.is_cancel_requested(user_id, since=since):
            logger.info("Cancel file detected for user_id=%s", user_id)
            app_manager.stop_parsing(user_id)
            return


def process_job(queue: ParserJobQueue, job: dict[str, Any], bot_token: str | None):
    user_id = str(job.get("user_id") or "unknown")
    job_id = str(job["job_id"])
    settings = Settings()
    settings.MAX_PRODUCTS = int(job.get("max_products") or settings.MAX_PRODUCTS)

    app_manager = AppManager(settings)
    app_manager.telegram_bot = WorkerTelegramNotifier(bot_token)
    done_event = threading.Event()
    cancel_since = float(job.get("created_at") or job.get("started_at") or 0)
    cancel_thread = threading.Thread(
        target=_watch_cancel,
        args=(queue, app_manager, user_id, cancel_since, done_event),
        daemon=True,
    )

    try:
        logger.info(
            "Starting parser job: job_id=%s user_id=%s count=%s url=%s",
            job_id,
            user_id,
            settings.MAX_PRODUCTS,
            job.get("url"),
        )
        cancel_thread.start()
        started = app_manager.start_parsing(
            job["url"],
            job.get("selected_fields") or [],
            user_id,
        )
        if not started:
            raise RuntimeError("AppManager refused to start parsing")

        while app_manager.is_running and not done_event.wait(1):
            pass

        summary = _result_summary(app_manager, user_id)
        if queue.is_cancel_requested(user_id, since=cancel_since):
            queue.fail(job_id, "cancelled", summary)
            logger.info("Parser job cancelled: job_id=%s", job_id)
        else:
            queue.complete(job_id, summary)
            logger.info("Parser job completed: job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Parser job failed: job_id=%s", job_id)
        queue.fail(job_id, str(exc), _result_summary(app_manager, user_id))
    finally:
        done_event.set()
        app_manager.stop_parsing(user_id)


def main():
    setup_logging()
    queue = ParserJobQueue()
    bot_token, _ = load_telegram_config_multi()
    stop_event = threading.Event()
    poll_interval = float(os.getenv("PARSER_WORKER_POLL_INTERVAL", "2"))

    def request_shutdown(signum, frame):
        logger.info("Worker shutdown signal: %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    logger.info("Parser worker started. Queue: %s", queue.base_dir)
    while not stop_event.is_set():
        job = queue.take_next()
        if job is None:
            stop_event.wait(poll_interval)
            continue
        process_job(queue, job, bot_token)

    logger.info("Parser worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
