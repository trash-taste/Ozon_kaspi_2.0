import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from ..utils.resource_manager import resource_manager
from .ozon_listing_data import (
    extract_listing_items_from_html,
    extract_product_links_from_html,
    normalize_product_url,
)

logger = logging.getLogger(__name__)


class OzonPlaywrightParser:
    """Collects Ozon product links, titles and prices directly from listing pages."""

    def __init__(
        self,
        category_url: str,
        max_products: int = 100,
        user_id: str = None,
        headless: bool = True,
    ):
        self.category_url = category_url
        self.max_products = max_products
        self.user_id = user_id
        self.headless = headless
        self.collected_links: Dict[str, dict[str, Any]] = {}

        self.category_name = self._extract_category_name(category_url)
        self.timestamp = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
        self.output_folder = f"{self.category_name}_{self.timestamp}"
        self.output_dir: Path | None = None

    def start_parsing(self) -> Tuple[bool, Dict[str, dict[str, Any]]]:
        browser = None
        context = None

        try:
            if self.user_id:
                resource_manager.start_parsing_session(
                    self.user_id,
                    "links",
                    self.max_products,
                )

            self._create_output_folder()

            try:
                from playwright.sync_api import TimeoutError, sync_playwright
            except ImportError as exc:
                logger.warning(
                    "Playwright недоступен, используем Selenium fallback: %s",
                    exc,
                )
                return False, {}

            with sync_playwright() as playwright:
                launch_kwargs = {
                    "headless": self.headless,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1920,1080",
                    ],
                }
                chromium_path = (
                    os.getenv("PLAYWRIGHT_CHROMIUM_PATH")
                    or os.getenv("CHROME_BIN")
                )
                if chromium_path:
                    launch_kwargs["executable_path"] = chromium_path

                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    locale="ru-RU",
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    extra_http_headers={
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    },
                )
                page = context.new_page()
                page.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    """
                )

                timeout_ms = int(
                    float(os.getenv("OZON_PAGE_LOAD_TIMEOUT", "30")) * 1000
                )
                try:
                    page.goto(
                        self.category_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except TimeoutError:
                    logger.warning(
                        "Playwright: таймаут загрузки, пробуем текущий DOM"
                    )

                self._wait_for_short_redirect(page)
                self._wait_for_unblocked_page(page)
                self._collect_links(page)
                self._save_links()

            logger.info(
                "Playwright собрал ссылок Ozon: %s/%s",
                len(self.collected_links),
                self.max_products,
            )
            return bool(self.collected_links), self.collected_links

        except Exception as exc:
            logger.exception("Ошибка Playwright-сборщика Ozon: %s", exc)
            return False, {}
        finally:
            try:
                if context:
                    context.close()
                if browser:
                    browser.close()
            except Exception:
                pass
            if self.user_id:
                resource_manager.finish_parsing_session(self.user_id)

    def _wait_for_unblocked_page(self, page) -> None:
        max_wait = int(os.getenv("OZON_ANTIBOT_TIMEOUT", "45"))
        deadline = time.time() + max_wait

        while time.time() < deadline:
            if not self._is_blocked(page):
                return
            logger.info("Playwright: обнаружена блокировка, обновляем страницу")
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(5)

        logger.warning("Playwright: антибот не пройден за %s секунд", max_wait)

    def _wait_for_short_redirect(self, page) -> None:
        for _ in range(12):
            current_url = getattr(page, "url", "") or ""
            if not re.search(r"/t/[^/?#]+", current_url):
                return
            page.wait_for_timeout(1000)

        logger.warning(
            "Playwright: короткая ссылка Ozon не отдала редирект: %s",
            getattr(page, "url", ""),
        )

    def _collect_links(self, page) -> None:
        idle_scrolls = 0
        idle_limit = max(3, int(os.getenv("OZON_LINK_IDLE_SCROLLS", "6")))
        scroll_wait_ms = max(
            500,
            int(float(os.getenv("OZON_SCROLL_WAIT_SECONDS", "2")) * 1000),
        )

        for scroll_num in range(1, self.max_products * 3 + 1):
            current_items = self._extract_items_from_page(page)
            new_count = 0
            for url, payload in current_items.items():
                if (
                    url not in self.collected_links
                    and len(self.collected_links) < self.max_products
                ):
                    self.collected_links[url] = payload
                    new_count += 1

            logger.info(
                "Playwright скролл %s: +%s, всего %s/%s",
                scroll_num,
                new_count,
                len(self.collected_links),
                self.max_products,
            )

            if len(self.collected_links) >= self.max_products:
                break

            if new_count:
                idle_scrolls = 0
            else:
                idle_scrolls += 1
                if idle_scrolls >= idle_limit:
                    break

            self._scroll_for_more(page)
            page.wait_for_timeout(scroll_wait_ms)

        if not self.collected_links:
            self._save_debug_snapshot(page, "playwright_no_links")

    def _extract_items_from_page(self, page) -> Dict[str, dict[str, Any]]:
        items: Dict[str, dict[str, Any]] = {}
        try:
            dom_items = page.evaluate(
                """
                () => Array.from(
                    document.querySelectorAll('a[href*="/product/"]')
                ).map(link => {
                    const card = link.closest(
                        '[class*="tile"], [data-widget], article'
                    ) || link.parentElement;
                    const img = link.querySelector('img')
                        || (card ? card.querySelector('img') : null);
                    const text = [
                        link.getAttribute('aria-label') || '',
                        link.getAttribute('title') || '',
                        img ? (img.getAttribute('alt') || '') : '',
                        link.innerText || link.textContent || '',
                        card ? (card.innerText || card.textContent || '') : ''
                    ].filter(Boolean).join('\\n');
                    return {
                        href: link.href || link.getAttribute('href') || '',
                        text
                    };
                })
                """
            )
            for item in dom_items or []:
                self._add_product_link(
                    items,
                    item.get("href", ""),
                    item.get("text", ""),
                    getattr(page, "url", ""),
                )
        except Exception as exc:
            logger.debug("Playwright: DOM extract failed: %s", exc)

        if len(items) < self.max_products:
            for url, payload in extract_listing_items_from_html(
                page.content(),
                getattr(page, "url", "") or self.category_url,
            ).items():
                self._merge_product_payload(items, url, payload)

        return items

    def _scroll_for_more(self, page) -> None:
        try:
            page.evaluate(
                """
                () => {
                    const paginator = document.getElementById(
                        'contentScrollPaginator'
                    );
                    if (paginator) {
                        paginator.scrollIntoView({
                            behavior: 'instant',
                            block: 'end'
                        });
                    }
                    const step = Math.max(
                        Math.floor(window.innerHeight * 0.9),
                        800
                    );
                    window.scrollBy(0, step);
                    window.dispatchEvent(new Event('scroll', {bubbles: true}));
                    document.dispatchEvent(
                        new WheelEvent('wheel', {
                            deltaY: step,
                            bubbles: true,
                            cancelable: true
                        })
                    );
                }
                """
            )
        except Exception as exc:
            logger.debug("Playwright: scroll failed: %s", exc)

    def _add_product_link(
        self,
        items: Dict[str, dict[str, Any]],
        href: str,
        card_text: str,
        base_url: str = "",
    ) -> None:
        normalized = self._normalize_product_url(href, base_url)
        if not normalized or normalized in items:
            return
        items[normalized] = {
            "title": self._extract_title_from_card_text(card_text),
            "price": self._extract_price_from_card_text(card_text),
        }

    def _extract_product_links_from_html(self, page_source: str) -> list[str]:
        return extract_product_links_from_html(page_source)

    def _normalize_product_url(self, href: str, base_url: str = "") -> str:
        return normalize_product_url(href, base_url or self.category_url)

    def _merge_product_payload(
        self,
        items: Dict[str, dict[str, Any]],
        url: str,
        payload: dict[str, Any],
    ) -> None:
        if not url:
            return
        if url not in items:
            items[url] = dict(payload or {})
            return

        current = items[url]
        for key in ("title", "price", "image_url"):
            if not current.get(key) and payload.get(key):
                current[key] = payload[key]

    def _extract_title_from_card_text(self, card_text: str) -> str:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in (card_text or "").splitlines()
        ]
        ignored_markers = (
            "₸",
            "₽",
            "тг",
            "тенге",
            "%",
            "звезд",
            "отзыв",
            "балл",
            "рассроч",
            "достав",
            "в корзин",
            "осталось",
            "купить",
            "рейтинг",
            "seller",
        )
        candidates = []
        for line in lines:
            lowered = line.casefold()
            if len(line) < 5 or len(line) > 260:
                continue
            if not re.search(r"[A-Za-zА-Яа-я]", line):
                continue
            if any(marker in lowered for marker in ignored_markers):
                continue
            candidates.append(line)

        return max(candidates, key=len) if candidates else ""

    def _extract_price_from_card_text(self, card_text: str) -> int:
        values = []
        patterns = (
            r"(\d[\d\s\u00a0\u202f.,]{1,})\s*(?:₸|тг|тенге)",
            r"(?:₸|тг|тенге)\s*(\d[\d\s\u00a0\u202f.,]{1,})",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, card_text or "", re.IGNORECASE):
                cleaned = re.sub(r"[^\d]", "", match.group(1))
                if not cleaned:
                    continue
                value = int(cleaned)
                if 100 <= value <= 10_000_000 and value not in values:
                    values.append(value)
        return values[0] if values else 0

    def _is_blocked(self, page) -> bool:
        try:
            content = (page.content() or "").casefold()
            indicators = (
                "cloudflare",
                "checking your browser",
                "enable javascript",
                "access denied",
                "blocked",
                "ddos-guard",
                "проверка браузера",
                "доступ ограничен",
                "access restricted",
            )
            return any(indicator in content for indicator in indicators)
        except Exception:
            return True

    def _save_debug_snapshot(self, page, reason: str) -> None:
        if not self.output_dir:
            return
        try:
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:40]
            source_path = self.output_dir / f"{safe_reason}_page_source.html"
            screenshot_path = self.output_dir / f"{safe_reason}_screenshot.png"

            source_path.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(screenshot_path), full_page=True)
            logger.warning(
                "Playwright debug сохранен: %s, %s",
                source_path,
                screenshot_path,
            )
        except Exception as exc:
            logger.warning("Playwright debug сохранить не удалось: %s", exc)

    def _save_links(self) -> bool:
        try:
            if not self.output_dir:
                self._create_output_folder()
            filename = f"links_{self.output_folder}.json"
            file_path = self.output_dir / filename
            links_to_save = dict(
                list(self.collected_links.items())[: self.max_products]
            )
            file_path.write_text(
                json.dumps(links_to_save, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.error("Ошибка сохранения Playwright-ссылок: %s", exc)
            return False

    def _create_output_folder(self) -> None:
        base_output_dir = Path(__file__).parent.parent.parent / "output"
        self.output_dir = base_output_dir / self.output_folder
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _extract_category_name(self, url: str) -> str:
        try:
            match = re.search(r"/category/([^/]+)-(\d+)/", url)
            if match:
                return match.group(1).replace("-", "_")
            if "/search/" in url:
                return "search"
            if "/seller/" in url:
                return "seller"
            return "unknown_category"
        except Exception:
            return "unknown_category"
