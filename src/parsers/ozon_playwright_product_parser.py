import asyncio
import logging
import os
import random
import re
import threading
from typing import Any, Dict, List

from .product_parser import (
    ProductInfo,
    ProductWorker,
    extract_product_page_fallback,
    _is_plausible_page_price,
)

logger = logging.getLogger(__name__)


class OzonPlaywrightProductParser:
    """Playwright product-card parser returning the existing ProductInfo model."""

    def __init__(
        self,
        max_workers: int = 5,
        user_id: str = None,
        headless: bool = True,
    ):
        self.max_workers = max_workers
        self.user_id = user_id
        self.headless = headless
        logger.info(
            "OzonPlaywrightProductParser включен (max_workers=%s user=%s)",
            max_workers,
            user_id,
        )

    def parse_products(self, product_links: Dict[str, Any]) -> List[ProductInfo]:
        try:
            return self._run_async(self._parse_products_async(product_links))
        except ImportError as exc:
            logger.warning(
                "Playwright недоступен для карточек, fallback на Selenium: %s",
                exc,
            )
            from .product_parser import OzonProductParser

            return OzonProductParser(
                self.max_workers,
                self.user_id,
                self.headless,
            ).parse_products(product_links)
        except Exception as exc:
            logger.exception(
                "Ошибка Playwright-парсинга карточек, fallback на Selenium: %s",
                exc,
            )
            from .product_parser import OzonProductParser

            return OzonProductParser(
                self.max_workers,
                self.user_id,
                self.headless,
            ).parse_products(product_links)

    async def _parse_products_async(
        self,
        product_links: Dict[str, Any],
    ) -> List[ProductInfo]:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = None
        context = None
        try:
            browser = await self._launch_browser(playwright)
            context = await self._create_context(browser)
            page = await context.new_page()
            page.set_default_timeout(
                int(os.getenv("OZON_PLAYWRIGHT_TIMEOUT", "15000"))
            )
            page.set_default_navigation_timeout(
                int(os.getenv("OZON_PLAYWRIGHT_NAV_TIMEOUT", "25000"))
            )

            results: List[ProductInfo] = []
            for index, (url, metadata) in enumerate(product_links.items(), 1):
                article = self._extract_article_from_url(url)
                if not article:
                    continue
                logger.info(
                    "Playwright карточка Ozon %s/%s: %s",
                    index,
                    len(product_links),
                    url,
                )
                results.append(
                    await self._parse_single_product(page, article, url, metadata)
                )
                await asyncio.sleep(random.uniform(0.8, 1.7))
            return results
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            await playwright.stop()

    async def _launch_browser(self, playwright):
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-gpu",
                "--no-sandbox",
                "--window-size=1920,1080",
            ],
        }
        chrome_bin = os.getenv("CHROME_BIN")
        if chrome_bin:
            launch_kwargs["executable_path"] = chrome_bin
        return await playwright.chromium.launch(**launch_kwargs)

    async def _create_context(self, browser):
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            timezone_id="Asia/Almaty",
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});
            window.chrome = window.chrome || {runtime: {}};
            """
        )
        return context

    async def _parse_single_product(
        self,
        page,
        article: str,
        url: str,
        metadata: Any,
    ) -> ProductInfo:
        product = ProductWorker(
            0,
            headless=self.headless,
            page_mode="off",
        )._build_from_listing(article, metadata or {})

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(os.getenv("OZON_PLAYWRIGHT_NAV_TIMEOUT", "25000")),
            )
            await asyncio.sleep(random.uniform(1.5, 2.8))
            try:
                await page.wait_for_selector("h1", timeout=8000)
            except Exception:
                logger.warning("Playwright h1 не найден в карточке: %s", url)

            page_data = extract_product_page_fallback(await page.content())
            if page_data["title"]:
                product.name = str(page_data["title"])
            if page_data["image_url"] and not product.image_url:
                product.image_url = str(page_data["image_url"])

            prices = page_data["prices"]
            if prices:
                page_price = int(prices[0])
                listing_price = product.price
                if _is_plausible_page_price(page_price, listing_price):
                    product.card_price = page_price
                    product.price = page_price
                    higher = [price for price in prices[1:] if price > product.price]
                    product.original_price = max(higher) if higher else 0
                    logger.info(
                        "Playwright цена Ozon из карточки %s: %s",
                        article,
                        product.price,
                    )
                elif product.success:
                    logger.warning(
                        "Playwright подозрительная цена карточки %s: %s, "
                        "оставлена цена из листинга: %s",
                        article,
                        page_price,
                        listing_price,
                    )
            elif product.success:
                logger.warning(
                    "Playwright не нашел цену в карточке %s, оставлена "
                    "цена из листинга: %s",
                    article,
                    product.price,
                )

            product.success = bool(product.name and product.price)
            product.error = "" if product.success else "Неполные данные товара"
            return product
        except Exception as exc:
            if product.success:
                logger.warning(
                    "Ошибка Playwright карточки %s, оставлена цена из листинга: %s",
                    article,
                    exc,
                )
            else:
                product.error = f"Ошибка Playwright карточки товара: {exc}"
            return product

    def _extract_article_from_url(self, url: str) -> str:
        match = re.search(r"/product/(?:[^/]+-)?(\d+)/?", url or "")
        return match.group(1) if match else ""

    def _run_async(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=runner)
        thread.start()
        thread.join()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def cleanup(self) -> None:
        logger.debug("OzonPlaywrightProductParser cleanup: browser closes per run")
