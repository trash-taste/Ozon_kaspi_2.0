import json
import logging
import os
import re
import time
import html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote

from ..utils.resource_manager import resource_manager
from ..utils.selenium_manager import SeleniumManager
from .ozon_listing_data import (
    build_listing_page_url,
    extract_listing_items_from_html,
    extract_price_from_card_text,
    extract_product_links_from_html,
    extract_title_from_card_text,
    normalize_product_url,
)

logger = logging.getLogger(__name__)

TARGET_CARD_DISCOUNT_MIN = 0.095
TARGET_CARD_DISCOUNT_MAX = 0.115


def _is_target_card_discount_price(
    existing_price: Any,
    new_price: Any,
) -> bool:
    try:
        existing = int(existing_price or 0)
        candidate = int(new_price or 0)
    except (TypeError, ValueError):
        return False

    if existing <= 0 or candidate <= 0 or candidate >= existing:
        return False

    discount_rate = (existing - candidate) / existing
    return TARGET_CARD_DISCOUNT_MIN <= discount_rate <= TARGET_CARD_DISCOUNT_MAX


class OzonLinkParser:
    """Selenium-stealth listing collector for Ozon category/search pages."""

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
        self.selenium_manager = SeleniumManager(headless=headless)
        self.driver = None
        self.collected_links: Dict[str, dict[str, Any]] = {}

        self.category_name = self._extract_category_name(category_url)
        self.timestamp = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
        self.output_folder = f"{self.category_name}_{self.timestamp}"
        self.output_dir: Path | None = None

    def start_parsing(self) -> Tuple[bool, Dict[str, dict[str, Any]]]:
        try:
            if self.user_id:
                resource_manager.start_parsing_session(
                    self.user_id,
                    "links",
                    self.max_products,
                )

            self._create_output_folder()
            self.driver = self.selenium_manager.create_driver()

            if not self._open_page(self.category_url):
                self._save_debug_snapshot("initial_load_failed")
                return False, {}

            if not self._wait_for_short_redirect():
                if not self._open_short_link_fallback_search():
                    self._save_debug_snapshot("short_link_no_redirect")
                    self._save_links()
                    return False, {}
            self._collect_products()
            self._save_links()

            logger.info(
                "Selenium stealth собрал товаров Ozon: %s/%s",
                len(self.collected_links),
                self.max_products,
            )
            return bool(self.collected_links), self.collected_links

        except Exception as exc:
            logger.exception("Ошибка Selenium stealth-сборщика Ozon: %s", exc)
            return False, {}
        finally:
            self._cleanup()
            if self.user_id:
                resource_manager.finish_parsing_session(self.user_id)

    def _open_page(self, url: str) -> bool:
        if not self.driver:
            return False
        try:
            if not self.selenium_manager.navigate_to_url(url):
                return False
            self._wait_for_listing_or_product(timeout=20)
            return True
        except Exception as exc:
            logger.warning("Страница Ozon не загрузилась: %s", exc)
            return bool(self._extract_product_items_from_html())

    def _wait_for_listing_or_product(self, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.driver:
                return
            try:
                if self.driver.find_elements("css selector", 'a[href*="/product/"]'):
                    return
                if "ozon." in (self.driver.current_url or ""):
                    items = self._extract_product_items_from_html()
                    if items:
                        return
            except Exception:
                pass
            time.sleep(0.5)

    def _wait_for_short_redirect(self) -> bool:
        if not self.driver:
            return False
        for _ in range(12):
            current_url = getattr(self.driver, "current_url", "") or ""
            if not self._is_short_link(current_url):
                return True
            time.sleep(1)
        logger.warning(
            "Короткая ссылка Ozon не отдала редирект: %s",
            getattr(self.driver, "current_url", ""),
        )
        return False

    def _open_short_link_fallback_search(self) -> bool:
        fallback_url = self._build_search_url_from_og_title()
        if not fallback_url:
            return False

        logger.warning(
            "Пробуем открыть поиск Ozon вместо короткой ссылки: %s",
            fallback_url,
        )
        self.category_url = fallback_url
        return self._open_page(fallback_url)

    def _build_search_url_from_og_title(self) -> str:
        if not self.driver:
            return ""

        source = getattr(self.driver, "page_source", "") or ""
        for tag in re.findall(r"<meta\b[^>]*>", source, flags=re.IGNORECASE):
            if "og:title" not in tag:
                continue
            match = re.search(
                r"\bcontent=(['\"])(.*?)\1",
                tag,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                continue

            title = html.unescape(match.group(2))
            query = self._clean_og_title_query(title)
            if query:
                return f"https://www.ozon.kz/search/?text={quote(query)}"

        return ""

    @staticmethod
    def _clean_og_title_query(title: str) -> str:
        query = re.sub(r"\s+", " ", title or "").strip()
        query = re.sub(
            r"\s*[-–—]\s*купить\b.*$",
            "",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"\s*[-–—]\s*OZON\b.*$",
            "",
            query,
            flags=re.IGNORECASE,
        )
        if len(query) < 2 or "ozon" == query.casefold():
            return ""
        return query

    @staticmethod
    def _is_short_link(url: str) -> bool:
        return bool(re.search(r"/t/[^/?#]+", url or ""))

    def _collect_products(self) -> None:
        idle_limit = max(3, int(os.getenv("OZON_LINK_IDLE_SCROLLS", "7")))
        scroll_wait = max(
            0.8,
            float(os.getenv("OZON_SCROLL_WAIT_SECONDS", "1.5")),
        )
        max_pages = max(1, int(os.getenv("OZON_MAX_PAGES", "10")))
        base_url = getattr(self.driver, "current_url", "") or self.category_url

        for page_num in range(1, max_pages + 1):
            if page_num > 1:
                next_url = build_listing_page_url(base_url, page_num)
                logger.info("Переход на страницу Ozon %s: %s", page_num, next_url)
                if not self._open_page(next_url):
                    break

            idle_scrolls = 0
            page_new_count = 0
            max_scrolls = max(
                8,
                int(os.getenv("OZON_MAX_SCROLLS", str(self.max_products * 3))),
            )

            for scroll_num in range(1, max_scrolls + 1):
                new_count = self._merge_new_items(self._extract_items_from_page())
                page_new_count += new_count

                logger.info(
                    "Selenium страница %s, скролл %s: +%s, всего %s/%s",
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

                self._scroll_for_more()
                time.sleep(scroll_wait)

            if page_num > 1 and page_new_count == 0:
                break

        if not self.collected_links:
            self._save_debug_snapshot("no_products")

    def _merge_new_items(self, items: Dict[str, dict[str, Any]]) -> int:
        new_count = 0
        for url, payload in items.items():
            if len(self.collected_links) >= self.max_products:
                break
            if url not in self.collected_links:
                self.collected_links[url] = dict(payload or {})
                new_count += 1
            else:
                self._merge_product_payload(
                    self.collected_links,
                    url,
                    payload,
                )
        return new_count

    def _extract_items_from_page(self) -> Dict[str, dict[str, Any]]:
        items: Dict[str, dict[str, Any]] = {}

        if not self.driver:
            return items

        try:
            dom_items = self.driver.execute_script(_DOM_CARD_EXTRACTOR)
            for item in dom_items or []:
                self._add_product_link(
                    items,
                    str(item.get("href") or ""),
                    str(item.get("text") or ""),
                    str(item.get("image_url") or ""),
                )
        except Exception as exc:
            logger.debug("DOM extraction failed: %s", exc)

        for url, payload in self._extract_product_items_from_html().items():
            self._merge_product_payload(items, url, payload)

        return items

    def _extract_product_items_from_html(self) -> Dict[str, dict[str, Any]]:
        if not self.driver:
            return {}
        current_url = getattr(self.driver, "current_url", "") or ""
        return extract_listing_items_from_html(
            self.driver.page_source,
            current_url or self.category_url,
        )

    def _extract_product_links_from_html(self, page_source: str | None = None):
        if page_source is None:
            page_source = self.driver.page_source if self.driver else ""
        return extract_product_links_from_html(page_source)

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
            logger.warning("Не удалось восстановить ссылки %s", reason)
            return False

        self._merge_new_items(items)
        logger.warning(
            "Ссылки восстановлены %s: всего %s/%s",
            reason,
            len(self.collected_links),
            self.max_products,
        )
        return bool(self.collected_links)

    def _add_product_link(
        self,
        items: Dict[str, dict[str, Any]],
        href: str,
        card_text: str = "",
        image_url: str = "",
    ) -> None:
        normalized = self._normalize_product_url(href)
        if not normalized or normalized in items:
            return

        items[normalized] = {
            "title": extract_title_from_card_text(card_text),
            "price": extract_price_from_card_text(card_text),
            "image_url": image_url,
        }

    def _extract_title_from_card_text(self, card_text: str) -> str:
        return extract_title_from_card_text(card_text)

    def _extract_price_from_card_text(self, card_text: str) -> int:
        return extract_price_from_card_text(card_text)

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
        if not current.get("title") and payload.get("title"):
            current["title"] = payload["title"]
        if not current.get("image_url") and payload.get("image_url"):
            current["image_url"] = payload["image_url"]
        # Ozon card price is usually about 10-11% below the visible price.
        existing_price = current.get("price") or 0
        new_price = payload.get("price") or 0
        if new_price and (
            not existing_price
            or _is_target_card_discount_price(existing_price, new_price)
        ):
            current["price"] = new_price

    def _scroll_for_more(self) -> None:
        if not self.driver:
            return
        try:
            self.driver.execute_script(
                """
                const step = Math.max(Math.floor(window.innerHeight * 1.4), 1200);
                const paginator = document.getElementById('contentScrollPaginator');
                if (paginator) {
                    paginator.scrollIntoView({behavior: 'instant', block: 'end'});
                }
                window.scrollBy(0, step);
                window.scrollTo({
                    top: Math.min(document.body.scrollHeight, window.scrollY + step),
                    behavior: 'instant'
                });
                window.dispatchEvent(new Event('scroll', {bubbles: true}));
                document.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: step,
                    bubbles: true,
                    cancelable: true
                }));
                """
            )
        except Exception as exc:
            logger.debug("Scroll failed: %s", exc)

    def _normalize_product_url(self, href: str, base_url: str = "") -> str:
        current_url = ""
        if self.driver:
            current_url = getattr(self.driver, "current_url", "") or ""
        return normalize_product_url(
            href,
            base_url or current_url or self.category_url,
        )

    def get_article_from_url(self, url: str) -> str:
        return self._extract_article_from_url(url)

    def _extract_article_from_url(self, url: str) -> str:
        match = re.search(r"/product/(?:[^/]+-)?(\d+)/?", url or "")
        return match.group(1) if match else ""

    def _extract_category_name(self, url: str) -> str:
        try:
            match = re.search(r"/category/([^/]+)-(\d+)/", url)
            if match:
                return match.group(1).replace("-", "_")
            if "/search/" in url or "/s/" in url:
                return "search"
            if "/seller/" in url:
                return "seller"
            return "unknown_category"
        except Exception:
            return "unknown_category"

    def _create_output_folder(self) -> None:
        base_output_dir = Path(__file__).parent.parent.parent / "output"
        self.output_dir = base_output_dir / self.output_folder
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _save_links(self) -> bool:
        try:
            if not self.output_dir:
                self._create_output_folder()
            file_path = self.output_dir / f"links_{self.output_folder}.json"
            file_path.write_text(
                json.dumps(
                    dict(list(self.collected_links.items())[: self.max_products]),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Ссылки Ozon сохранены: %s", file_path.resolve())
            return True
        except Exception as exc:
            logger.error("Ошибка сохранения ссылок Ozon: %s", exc)
            return False

    def _save_debug_snapshot(self, reason: str) -> None:
        if not self.driver:
            return
        try:
            if not self.output_dir:
                self._create_output_folder()
            safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason)[:40]
            source_path = self.output_dir / f"{safe_reason}_page_source.html"
            screenshot_path = self.output_dir / f"{safe_reason}_screenshot.png"
            source_path.write_text(self.driver.page_source, encoding="utf-8")
            self.driver.save_screenshot(str(screenshot_path))
            logger.warning(
                "Debug Ozon сохранен: %s, %s",
                source_path.resolve(),
                screenshot_path.resolve(),
            )
        except Exception as exc:
            logger.warning("Не удалось сохранить debug Ozon: %s", exc)

    def _cleanup(self) -> None:
        self.selenium_manager.close()


_DOM_CARD_EXTRACTOR = """
return Array.from(document.querySelectorAll('a[href*="/product/"]')).map(link => {
    let card = link.closest('[class*="tile"], [data-widget], article')
        || link.parentElement;
    let root = link;
    for (let i = 0; i < 10 && root.parentElement; i += 1) {
        root = root.parentElement;
        const text = (root.innerText || root.textContent || '')
            .replace(/\\s+/g, ' ')
            .trim();
        const productLinks = root.querySelectorAll('a[href*="/product/"]').length;
        const hasPrice = /(₸|тг|тенге)/i.test(text);
        if (hasPrice && text.length >= 20 && text.length <= 3500 && productLinks <= 10) {
            card = root;
            break;
        }
    }
    const img = link.querySelector('img') || (card ? card.querySelector('img') : null);
    const text = [
        link.getAttribute('aria-label') || '',
        link.getAttribute('title') || '',
        img ? (img.getAttribute('alt') || '') : '',
        link.innerText || link.textContent || '',
        card ? (card.innerText || card.textContent || '') : ''
    ].filter(Boolean).join('\\n');
    return {
        href: link.href || link.getAttribute('href') || '',
        image_url: img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '',
        text
    };
});
"""
