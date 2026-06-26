import asyncio
import json
import logging
import os
import random
import re
import threading
from typing import Any, Dict, Tuple

from ..utils.resource_manager import resource_manager
from .link_parser import OzonLinkParser, _DOM_CARD_EXTRACTOR
from .ozon_listing_data import (
    build_listing_page_url,
    extract_listing_items_from_html,
)

logger = logging.getLogger(__name__)


class OzonPlaywrightParser(OzonLinkParser):
    """Playwright listing collector for Ozon category/search pages."""

    def start_parsing(self) -> Tuple[bool, Dict[str, dict[str, Any]]]:
        try:
            return self._run_async(self._start_async())
        except ImportError as exc:
            logger.warning(
                "Playwright недоступен, fallback на Selenium parser: %s",
                exc,
            )
            return super().start_parsing()
        except Exception as exc:
            logger.exception(
                "Ошибка Playwright-сборщика Ozon, fallback на Selenium: %s",
                exc,
            )
            return super().start_parsing()

    async def _start_async(self) -> Tuple[bool, Dict[str, dict[str, Any]]]:
        from playwright.async_api import async_playwright

        if self.user_id:
            resource_manager.start_parsing_session(
                self.user_id,
                "links",
                self.max_products,
            )

        playwright = None
        browser = None
        context = None
        try:
            self._create_output_folder()
            playwright = await async_playwright().start()
            browser = await self._launch_browser(playwright)
            context = await self._create_context(browser)
            self.page = await context.new_page()
            self.page.set_default_timeout(
                int(os.getenv("OZON_PLAYWRIGHT_TIMEOUT", "15000"))
            )
            self.page.set_default_navigation_timeout(
                int(os.getenv("OZON_PLAYWRIGHT_NAV_TIMEOUT", "25000"))
            )

            if not await self._open_page_async(self.category_url):
                await self._save_debug_snapshot_async("initial_load_failed")
                return False, {}

            await self._wait_for_short_redirect_async()
            await self._collect_products_async()
            self._save_links()

            logger.info(
                "Playwright собрал товаров Ozon: %s/%s",
                len(self.collected_links),
                self.max_products,
            )
            return bool(self.collected_links), self.collected_links
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            if playwright:
                await playwright.stop()
            if self.user_id:
                resource_manager.finish_parsing_session(self.user_id)

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

    async def _open_page_async(self, url: str) -> bool:
        try:
            logger.info("Playwright открывает Ozon: %s", url)
            await self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(os.getenv("OZON_PLAYWRIGHT_NAV_TIMEOUT", "25000")),
            )
            await self._human_delay(1.5, 2.8)
            await self._wait_for_listing_or_product_async(timeout=18)
            return True
        except Exception as exc:
            logger.warning("Playwright не загрузил страницу Ozon: %s", exc)
            return bool(await self._extract_product_items_from_html_async())

    async def _wait_for_listing_or_product_async(self, timeout: int) -> None:
        selectors = (
            'a[href*="/product/"]',
            "[data-widget='searchResults']",
            "[data-widget*='search']",
            ".widget-search-result-container",
            ".tile-root",
            "h1",
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                try:
                    if await self.page.query_selector(selector):
                        return
                except Exception:
                    continue
            if await self._extract_product_items_from_html_async():
                return
            await asyncio.sleep(0.5)

    async def _wait_for_short_redirect_async(self) -> None:
        for _ in range(12):
            current_url = getattr(self.page, "url", "") or ""
            if not re.search(r"/t/[^/?#]+", current_url):
                return
            await asyncio.sleep(1)
        logger.warning("Короткая ссылка Ozon не отдала редирект: %s", self.page.url)

    async def _collect_products_async(self) -> None:
        idle_limit = max(3, int(os.getenv("OZON_LINK_IDLE_SCROLLS", "7")))
        scroll_wait = max(
            0.8,
            float(os.getenv("OZON_SCROLL_WAIT_SECONDS", "1.5")),
        )
        max_pages = max(1, int(os.getenv("OZON_MAX_PAGES", "10")))
        base_url = getattr(self.page, "url", "") or self.category_url

        for page_num in range(1, max_pages + 1):
            if page_num > 1:
                next_url = build_listing_page_url(base_url, page_num)
                logger.info("Playwright переходит на страницу Ozon %s: %s", page_num, next_url)
                if not await self._open_page_async(next_url):
                    break

            idle_scrolls = 0
            page_new_count = 0
            max_scrolls = max(
                8,
                int(os.getenv("OZON_MAX_SCROLLS", str(self.max_products * 3))),
            )

            for scroll_num in range(1, max_scrolls + 1):
                new_count = self._merge_new_items(
                    await self._extract_items_from_page_async()
                )
                page_new_count += new_count

                logger.info(
                    "Playwright страница %s, скролл %s: +%s, всего %s/%s",
                    page_num,
                    scroll_num,
                    new_count,
                    len(self.collected_links),
                    self.max_products,
                )

                if len(self.collected_links) >= self.max_products:
                    return

                if new_count:
                    idle_scrolls = 0
                else:
                    idle_scrolls += 1
                    if idle_scrolls >= idle_limit:
                        break

                await self._scroll_for_more_async()
                await asyncio.sleep(scroll_wait)

            if page_num > 1 and page_new_count == 0:
                break

        if not self.collected_links:
            await self._save_debug_snapshot_async("no_products")

    async def _extract_items_from_page_async(self) -> Dict[str, dict[str, Any]]:
        items: Dict[str, dict[str, Any]] = {}
        try:
            dom_items = await self.page.evaluate(_DOM_CARD_EXTRACTOR)
            for item in dom_items or []:
                self._add_product_link(
                    items,
                    str(item.get("href") or ""),
                    str(item.get("text") or ""),
                    str(item.get("image_url") or ""),
                )
        except Exception as exc:
            logger.debug("Playwright DOM extraction failed: %s", exc)

        for url, payload in (await self._extract_product_items_from_html_async()).items():
            self._merge_product_payload(items, url, payload)

        return items

    async def _extract_product_items_from_html_async(self) -> Dict[str, dict[str, Any]]:
        try:
            content = await self.page.content()
        except Exception:
            return {}
        return extract_listing_items_from_html(
            content,
            getattr(self.page, "url", "") or self.category_url,
        )

    async def _scroll_for_more_async(self) -> None:
        try:
            await self.page.evaluate(
                """
                const step = Math.max(Math.floor(window.innerHeight * 1.4), 1200);
                const paginator = document.getElementById('contentScrollPaginator');
                if (paginator) {
                    paginator.scrollIntoView({behavior: 'instant', block: 'end'});
                }
                window.scrollBy(0, step);
                window.dispatchEvent(new Event('scroll', {bubbles: true}));
                document.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: step,
                    bubbles: true,
                    cancelable: true
                }));
                """
            )
            await self.page.mouse.wheel(0, random.randint(900, 1500))
        except Exception as exc:
            logger.debug("Playwright scroll failed: %s", exc)

    async def _save_debug_snapshot_async(self, reason: str) -> None:
        try:
            if not self.output_dir:
                self._create_output_folder()
            snapshot_path = self.output_dir / f"debug_playwright_{reason}.html"
            snapshot_path.write_text(await self.page.content(), encoding="utf-8")
            meta_path = self.output_dir / f"debug_playwright_{reason}.json"
            meta_path.write_text(
                json.dumps(
                    {"url": getattr(self.page, "url", ""), "reason": reason},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.warning("Playwright debug Ozon сохранен: %s", snapshot_path.resolve())
        except Exception as exc:
            logger.warning("Не удалось сохранить Playwright debug Ozon: %s", exc)

    async def _human_delay(self, min_sec: float = 1.0, max_sec: float = 2.5) -> None:
        await asyncio.sleep(random.uniform(min_sec, max_sec))

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
