import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from ..utils.resource_manager import resource_manager
from ..utils.selenium_manager import SeleniumManager
from .ozon_listing_data import (
    build_listing_page_url,
    extract_listing_items_from_html,
    extract_product_links_from_html,
    normalize_product_url,
)

logger = logging.getLogger(__name__)


class OzonLinkParser:
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
        self.selenium_manager = SeleniumManager(headless=headless)
        self.driver = None
        self.collected_links: Dict[str, dict[str, Any]] = {}

        self.category_name = self._extract_category_name(category_url)
        self.timestamp = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
        self.output_folder = f"{self.category_name}_{self.timestamp}"

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

    def start_parsing(self) -> Tuple[bool, Dict[str, dict[str, Any]]]:
        try:
            if self.user_id:
                resource_manager.start_parsing_session(
                    self.user_id, "links", self.max_products
                )

            self._create_output_folder()
            self.driver = self.selenium_manager.create_driver()

            if not self._load_page():
                return False, {}

            self._collect_links()
            saved = self._save_links()

            logger.info(
                f"Собрано {len(self.collected_links)} ссылок "
                f"для пользователя {self.user_id}"
            )
            return saved and bool(self.collected_links), self.collected_links

        except Exception as e:
            logger.error(f"Ошибка парсинга ссылок: {e}")
            return False, {}
        finally:
            self._cleanup()
            if self.user_id:
                resource_manager.finish_parsing_session(self.user_id)

    def _load_page(self) -> bool:
        max_driver_retries = 3

        for driver_attempt in range(max_driver_retries):
            try:
                logger.info(
                    f"Попытка загрузки страницы с драйвером "
                    f"#{driver_attempt + 1}/{max_driver_retries}"
                )

                if driver_attempt > 0:
                    logger.info(
                        f"Пересоздание драйвера после блокировки "
                        f"(драйвер #{driver_attempt + 1})"
                    )
                    self.selenium_manager.close()
                    time.sleep(3)
                    self.driver = self.selenium_manager.create_driver()

                if not self.selenium_manager.navigate_to_url(self.category_url):
                    if self._recover_links_from_current_page(
                        "после неудачной загрузки"
                    ):
                        return True
                    if driver_attempt < max_driver_retries - 1:
                        logger.warning(
                            f"Не удалось загрузить страницу с драйвером "
                            f"#{driver_attempt + 1}, пробуем новый..."
                        )
                        continue
                    return False

                self._wait_for_product_content()
                logger.info(
                    f"Страница успешно загружена с драйвером "
                    f"#{driver_attempt + 1}"
                )
                return True

            except TimeoutException:
                logger.error(
                    f"Контент товаров не найден "
                    f"(драйвер #{driver_attempt + 1})"
                )
                if self._recover_links_from_current_page(
                    "после таймаута ожидания контента"
                ):
                    return True
                if driver_attempt < max_driver_retries - 1:
                    continue
                self._save_debug_snapshot("content_timeout")
                return False

            except Exception as e:
                if "Access blocked" in str(e):
                    if driver_attempt < max_driver_retries - 1:
                        logger.warning(
                            f"Драйвер #{driver_attempt + 1} заблокирован, "
                            "пробуем новый..."
                        )
                        continue
                    logger.error("Все драйверы были заблокированы")
                    self._save_debug_snapshot("access_blocked")
                    return False

                logger.error(
                    f"Ошибка загрузки страницы "
                    f"(драйвер #{driver_attempt + 1}): {e}"
                )
                if driver_attempt >= max_driver_retries - 1:
                    self._save_debug_snapshot("load_error")
                    return False

        return False

    def _wait_for_product_content(self):
        WebDriverWait(self.driver, 60).until(
            lambda driver: driver.find_elements(
                By.CSS_SELECTOR, 'a[href*="/product/"]'
            )
            or driver.find_elements(By.ID, "contentScrollPaginator")
        )

    def _collect_links(self):
        seen_urls = set()
        no_new_limit = max(
            3,
            int(os.getenv("OZON_LINK_IDLE_SCROLLS", "6")),
        )
        scroll_wait = max(
            1,
            int(os.getenv("OZON_SCROLL_WAIT_SECONDS", "4")),
        )
        max_pages = max(1, int(os.getenv("OZON_MAX_PAGES", "10")))
        base_url = getattr(self.driver, "current_url", "") or self.category_url

        for page_num in range(1, max_pages + 1):
            if page_num > 1:
                next_url = build_listing_page_url(base_url, page_num)
                logger.info("Переход на страницу Ozon %s: %s", page_num, next_url)
                if not self.selenium_manager.navigate_to_url(next_url):
                    logger.warning("Не удалось загрузить страницу Ozon %s", page_num)
                    break

            no_new_items_count = 0
            page_new_count = 0
            scroll_num = 0

            while len(self.collected_links) < self.max_products:
                scroll_num += 1
                current_items = self._extract_all_links()

                new_count = 0
                for url, img_url in current_items.items():
                    if (
                        url not in seen_urls
                        and len(self.collected_links) < self.max_products
                    ):
                        seen_urls.add(url)
                        self.collected_links[url] = img_url
                        new_count += 1
                        page_new_count += 1

                logger.info(
                    "Страница %s, скролл %s: +%s, всего %s/%s",
                    page_num,
                    scroll_num,
                    new_count,
                    len(self.collected_links),
                    self.max_products,
                )

                if new_count == 0:
                    no_new_items_count += 1
                    if no_new_items_count >= no_new_limit:
                        break
                else:
                    no_new_items_count = 0

                if len(self.collected_links) >= self.max_products:
                    break

                self._scroll_for_more()
                time.sleep(scroll_wait)

            if len(self.collected_links) >= self.max_products:
                break
            if page_num > 1 and page_new_count == 0:
                logger.info("Страница %s не дала новых товаров", page_num)
                break

        if not self.collected_links:
            self._save_debug_snapshot("no_links")

    def _recover_links_from_current_page(self, reason: str) -> bool:
        if not self.driver:
            return False

        items: Dict[str, dict[str, Any]] = {}
        current_url = getattr(self.driver, "current_url", "") or ""
        page_title = getattr(self.driver, "title", "") or ""

        self._add_product_link(items, current_url, page_title)
        for url, payload in self._extract_product_items_from_html().items():
            self._merge_product_payload(items, url, payload)

        if not items:
            logger.warning(
                "Не удалось восстановить ссылки %s: current_url=%s title=%r",
                reason,
                current_url,
                page_title,
            )
            return False

        for url, payload in items.items():
            if (
                url not in self.collected_links
                and len(self.collected_links) < self.max_products
            ):
                self.collected_links[url] = payload

        logger.warning(
            "Ссылки восстановлены %s: +%s, всего %s/%s, current_url=%s",
            reason,
            len(items),
            len(self.collected_links),
            self.max_products,
            current_url,
        )
        return bool(self.collected_links)

    def _scroll_for_more(self):
        try:
            self.driver.execute_script(
                """
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
                """
            )
        except Exception as e:
            logger.debug("Не удалось выполнить JS-scroll: %s", e)

    def _extract_all_links(self) -> Dict[str, dict[str, Any]]:
        try:
            items = {}
            dom_items = self.driver.execute_script(
                """
                return Array.from(
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
                        text: text
                    };
                });
                """
            )
            for item in dom_items or []:
                self._add_product_link(
                    items,
                    item.get("href", ""),
                    item.get("text", ""),
                )

            if len(items) < self.max_products:
                for url, payload in self._extract_product_items_from_html().items():
                    self._merge_product_payload(items, url, payload)

            logger.debug(f"Извлечено ссылок на текущем экране: {len(items)}")
            return items
        except Exception as e:
            logger.warning(f"Ошибка извлечения ссылок: {e}")
            return {}

    def _get_products_container(self):
        try:
            return self.driver.find_element(By.ID, "contentScrollPaginator")
        except Exception:
            return None

    def _extract_product_links_from_html(self):
        return extract_product_links_from_html(self.driver.page_source)

    def _extract_product_items_from_html(self) -> Dict[str, dict[str, Any]]:
        current_url = getattr(self.driver, "current_url", "") or ""
        return extract_listing_items_from_html(
            self.driver.page_source,
            current_url or self.category_url,
        )

    def _add_product_link(
        self,
        items: Dict[str, dict[str, Any]],
        href: str,
        card_text: str = "",
    ):
        normalized = self._normalize_product_url(href)
        if not normalized or normalized in items:
            return
        items[normalized] = {
            "title": self._extract_title_from_card_text(card_text),
            "price": self._extract_price_from_card_text(card_text),
        }

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
        if not isinstance(current, dict):
            items[url] = dict(payload or {})
            return

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

        if not candidates:
            return ""

        return max(candidates, key=len)

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

    def _normalize_product_url(self, href: str) -> str:
        if not href:
            return ""

        current_url = getattr(self.driver, "current_url", "") or ""
        return normalize_product_url(href, current_url or self.category_url)

    def _save_debug_snapshot(self, reason: str = "debug"):
        try:
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:40]
            source_path = self.output_dir / f"{safe_reason}_page_source.html"
            screenshot_path = self.output_dir / f"{safe_reason}_screenshot.png"

            with open(source_path, "w", encoding="utf-8") as file:
                file.write(self.driver.page_source)
            self.driver.save_screenshot(str(screenshot_path))

            logger.warning(
                f"Ссылки не найдены. Debug сохранен: "
                f"{source_path}, {screenshot_path}"
            )
        except Exception as e:
            logger.warning(f"Не удалось сохранить debug страницы: {e}")

    def _create_output_folder(self):
        base_output_dir = Path(__file__).parent.parent.parent / "output"
        self.output_dir = base_output_dir / self.output_folder
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _save_links(self) -> bool:
        try:
            filename = f"links_{self.output_folder}.json"
            file_path = self.output_dir / filename
            links_to_save = dict(
                list(self.collected_links.items())[: self.max_products]
            )

            with open(file_path, "w", encoding="utf-8") as file:
                json.dump(links_to_save, file, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения ссылок: {e}")
            return False

    def _cleanup(self):
        if self.selenium_manager:
            self.selenium_manager.close()

    def get_article_from_url(self, url: str) -> str:
        try:
            match = re.search(r"/product/(?:[^/]+-)?(\d+)/?", url)
            return match.group(1) if match else ""
        except Exception:
            return ""
