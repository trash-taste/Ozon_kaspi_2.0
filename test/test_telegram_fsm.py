import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.core.app_manager import AppManager
from src.telegram.bot_manager import ScanStates, TelegramBotManager


def make_manager() -> TelegramBotManager:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager.user_ids = ["123"]
    manager.app_manager = SimpleNamespace(is_running=False)
    return manager


def make_message(text: str):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=123),
        reply=AsyncMock(),
    )


class TelegramFSMTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_url_enters_waiting_for_limit(self):
        manager = make_manager()
        message = make_message(
            "https://ozon.kz/s/firmennyy-magazin-redmond"
        )
        state = AsyncMock()

        await manager._handle_message(message, state)

        state.update_data.assert_awaited_once_with(
            ozon_url=message.text
        )
        state.set_state.assert_awaited_once_with(
            ScanStates.waiting_for_limit
        )
        self.assertIn(
            "Сколько товаров спарсить?",
            message.reply.await_args.args[0],
        )

    async def test_valid_limit_starts_parsing_and_clears_state(self):
        manager = make_manager()
        manager._start_parsing_with_count = AsyncMock()
        message = make_message("10")
        state = AsyncMock()
        state.get_data.return_value = {
            "ozon_url": "https://ozon.kz/category/elektronika-15500/"
        }

        await manager._handle_count_input(message, state)

        state.clear.assert_awaited_once()
        manager._start_parsing_with_count.assert_awaited_once_with(
            message,
            "https://ozon.kz/category/elektronika-15500/",
            10,
        )

    async def test_invalid_limit_keeps_waiting_state(self):
        manager = make_manager()

        for text in ("abc", "0", "501"):
            with self.subTest(text=text):
                message = make_message(text)
                state = AsyncMock()

                await manager._handle_count_input(message, state)

                state.clear.assert_not_awaited()
                state.get_data.assert_not_awaited()
                message.reply.assert_awaited_once_with(
                    "Введите число от 1 до 500."
                )

    def test_accepts_supported_ozon_urls(self):
        manager = make_manager()

        self.assertTrue(
            manager._is_ozon_category_url(
                "https://ozon.kz/s/firmennyy-magazin-redmond"
            )
        )
        self.assertTrue(
            manager._is_ozon_category_url(
                "https://www.ozon.ru/category/elektronika-15500/"
            )
        )
        self.assertFalse(
            manager._is_ozon_category_url(
                "https://example.com/category/elektronika-15500/"
            )
        )


class TelegramKaspiIntegrationTests(unittest.TestCase):
    def test_kaspi_report_is_created_and_sent(self):
        app_manager = AppManager.__new__(AppManager)
        app_manager.last_results = {}
        app_manager.user_results = {
            "123": {
                "category_url": "https://ozon.kz/category/test/",
                "links": {
                    "https://ozon.kz/product/test-12345/": "",
                },
                "products": [
                    SimpleNamespace(
                        success=True,
                        article="12345",
                        name="Test product",
                        price=5000,
                        card_price=0,
                    )
                ],
            }
        }
        app_manager._notify_user = Mock()
        app_manager._send_files_to_telegram = Mock()
        comparison = [{"roi": 25, "profit": 4000}]

        with (
            patch(
                "services.kaspi_compare.compare_with_kaspi",
                new=AsyncMock(return_value=comparison),
            ) as compare_mock,
            patch(
                "services.report.save_arbitrage_report",
                return_value="/tmp/arbitrage.xlsx",
            ) as report_mock,
        ):
            app_manager._compare_with_kaspi_and_send("123")

        ozon_products = compare_mock.await_args.args[0]
        self.assertEqual(len(ozon_products), 1)
        self.assertEqual(ozon_products[0]["article"], None)
        self.assertEqual(
            ozon_products[0]["url"],
            "https://ozon.kz/product/test-12345/",
        )
        report_mock.assert_called_once_with(comparison)
        app_manager._send_files_to_telegram.assert_called_once()
        send_args = app_manager._send_files_to_telegram.call_args
        self.assertEqual(send_args.args[:2], ("/tmp/arbitrage.xlsx", "123"))
        self.assertIn("Подходящих товаров: 1", send_args.kwargs["caption"])


if __name__ == "__main__":
    unittest.main()
