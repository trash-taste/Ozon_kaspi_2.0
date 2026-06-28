import logging
from typing import Any

from .job_queue import ParserJobQueue
from ..config.settings import Settings

logger = logging.getLogger(__name__)


class QueuedAppManager:
    """Bot-side AppManager facade that submits parser work to a worker process."""

    def __init__(self, settings: Settings, queue: ParserJobQueue | None = None):
        self.settings = settings
        self.queue = queue or ParserJobQueue()
        self.telegram_bot = None

    @property
    def is_running(self) -> bool:
        return bool(self.queue.active_jobs())

    @property
    def last_results(self) -> dict[str, Any]:
        return self.queue.latest_result() or {}

    def start_parsing(
        self,
        category_url: str,
        selected_fields: list[str] | None = None,
        user_id: str | None = None,
    ) -> bool:
        if self.queue.user_has_active_job(user_id):
            logger.warning("User %s already has an active parser job", user_id)
            return False

        job = self.queue.submit(
            url=category_url,
            selected_fields=selected_fields or [],
            user_id=user_id,
            max_products=self.settings.MAX_PRODUCTS,
        )
        logger.info(
            "Parser job queued: job_id=%s user_id=%s url=%s count=%s",
            job["job_id"],
            user_id,
            category_url,
            self.settings.MAX_PRODUCTS,
        )
        return True

    def stop_parsing(self, user_id: str | None = None):
        logger.info("Cancel requested for parser job: user_id=%s", user_id)
        self.queue.request_cancel(user_id)

    def restart_parsing(
        self,
        category_url: str,
        selected_fields: list[str] | None = None,
        user_id: str | None = None,
    ) -> bool:
        self.stop_parsing(user_id)
        return self.start_parsing(category_url, selected_fields, user_id)

    def get_status(self) -> dict[str, Any]:
        active_jobs = self.queue.active_jobs()
        latest = self.queue.latest_result() or {}
        return {
            "is_running": bool(active_jobs),
            "active_users_count": len(
                {str(job.get("user_id")) for job in active_jobs}
            ),
            "active_users": [
                str(job.get("user_id"))
                for job in active_jobs
                if job.get("user_id")
            ],
            "telegram_bot_active": bool(
                self.telegram_bot
                and getattr(self.telegram_bot, "is_running", False)
            ),
            "last_results": latest.get("summary", latest),
            "settings": {
                "max_products": self.settings.MAX_PRODUCTS,
                "max_workers": self.settings.MAX_WORKERS,
            },
            "total_active_users": len(active_jobs),
            "total_allocated_workers": 0,
            "sessions": {
                str(job.get("user_id")): {
                    "stage": job.get("status", "pending"),
                    "workers": 0,
                    "progress": job.get("summary", {}).get("progress", "queued"),
                    "duration": "",
                }
                for job in active_jobs
            },
        }

    def get_user_results(self, user_id: str):
        job = self.queue.latest_result(user_id)
        return (job or {}).get("summary")

    def start_telegram_bot(self, bot_token: str, user_ids) -> bool:
        from ..telegram.bot_manager import TelegramBotManager

        self.telegram_bot = TelegramBotManager(bot_token, user_ids, self)
        return self.telegram_bot.start()

    def stop_telegram_bot(self):
        if self.telegram_bot:
            self.telegram_bot.stop()
            self.telegram_bot = None

    def shutdown(self):
        self.stop_parsing()
