import tempfile
import time
import unittest
from types import SimpleNamespace

from src.core.job_queue import ParserJobQueue
from src.core.queued_app_manager import QueuedAppManager


class ParserJobQueueTests(unittest.TestCase):
    def test_submit_take_complete_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = ParserJobQueue(temp_dir)
            job = queue.submit(
                url="https://ozon.kz/s/test",
                selected_fields=["name"],
                user_id="123",
                max_products=5,
            )

            self.assertTrue(queue.user_has_active_job("123"))

            running = queue.take_next()
            self.assertEqual(running["job_id"], job["job_id"])
            self.assertEqual(running["status"], "running")

            queue.complete(job["job_id"], {"successful_products": 5})
            self.assertFalse(queue.user_has_active_job("123"))
            latest = queue.latest_result("123")
            self.assertEqual(latest["status"], "done")
            self.assertEqual(latest["summary"]["successful_products"], 5)

    def test_cancel_flag_is_user_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = ParserJobQueue(temp_dir)

            queue.request_cancel("123")

            self.assertTrue(queue.is_cancel_requested("123"))
            self.assertFalse(queue.is_cancel_requested("456"))

    def test_stale_cancel_before_job_is_ignored_by_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = ParserJobQueue(temp_dir)

            queue.request_cancel("123")
            cutoff = time.time()

            self.assertTrue(queue.is_cancel_requested("123"))
            self.assertFalse(queue.is_cancel_requested("123", since=cutoff))

            time.sleep(0.01)
            queue.request_cancel("123")

            self.assertTrue(queue.is_cancel_requested("123", since=cutoff))


class QueuedAppManagerTests(unittest.TestCase):
    def test_start_parsing_submits_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = ParserJobQueue(temp_dir)
            manager = QueuedAppManager(
                SimpleNamespace(MAX_PRODUCTS=7, MAX_WORKERS=2),
                queue,
            )

            result = manager.start_parsing(
                "https://ozon.kz/s/test",
                ["name"],
                "123",
            )

            self.assertTrue(result)
            self.assertTrue(manager.is_running)
            status = manager.get_status()
            self.assertEqual(status["active_users_count"], 1)
            self.assertEqual(status["settings"]["max_products"], 7)

    def test_rejects_second_active_job_for_same_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = ParserJobQueue(temp_dir)
            manager = QueuedAppManager(
                SimpleNamespace(MAX_PRODUCTS=7, MAX_WORKERS=2),
                queue,
            )

            self.assertTrue(
                manager.start_parsing("https://ozon.kz/s/test", [], "123")
            )
            self.assertFalse(
                manager.start_parsing("https://ozon.kz/s/other", [], "123")
            )


if __name__ == "__main__":
    unittest.main()
