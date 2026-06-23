import html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from ..utils.resource_manager import resource_manager
from ..utils.selenium_manager import SeleniumManager

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
        self.collected_links = {}

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

    def start_parsing(self) -> Tuple[bool, Dict[str, str]]:
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
                if driver_attempt < max_driver_retries - 1:
                    continue
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
                    return False

                logger.error(
                    f"Ошибка загрузки страницы "
                    f"(драйвер #{driver_attempt + 1}): {e}"
                )
                if driver_attempt >= max_driver_retries - 1:
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
        scroll_num = 0
        no_new_items_count = 0

        while len(self.collected_links) < self.max_products:
            scroll_num += 1
            current_items = self._extract_all_links()

            new_count = 0
            for url, img_url in current_items.items():
                if url not in seen_urls and len(self.collected_links) < self.max_products:
                    seen_urls.add(url)
                    self.collected_links[url] = img_url
                    new_count += 1

            logger.info(
                f"Скролл {scroll_num}: +{new_count}, "
                f"всего {len(self.collected_links)}/{self.max_products}"
            )

            if new_count == 0:
                no_new_items_count += 1
                if no_new_items_count >= 3:
                    break
            else:
                no_new_items_count = 0

            if len(self.collected_links) >= self.max_products:
                break

            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(8)

        if not self.collected_links:
            self._save_debug_snapshot()

    def _extract_all_links(self) -> Dict[str, str]:
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
                    return {
                        href: link.href || link.getAttribute('href') || '',
                        image: img
                            ? (
                                img.currentSrc
                                || img.src
                                || img.getAttribute('data-src')
                                || img.getAttribute('srcset')
                                || ''
                            )
                            : ''
                    };
                });
                """
            )
            for item in dom_items or []:
                self._add_product_link(
                    items,
                    item.get("href", ""),
                    item.get("image", ""),
                )

            if len(items) < self.max_products:
                for href in self._extract_product_links_from_html():
                    self._add_product_link(items, href, "")

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

    def _extract_link_from_tile(self, element) -> Tuple[str, str]:
        try:
            link_element = element.find_element(
                By.CSS_SELECTOR,
                'a[href*="/product/"], a[data-prerender="true"]',
            )
            href = link_element.get_attribute("href")
            img_url = (
                self._extract_image_from_element(element)
                or self._extract_image_near_link(link_element)
            )
            return href, img_url
        except Exception:
            return "", ""

    def _extract_image_near_link(self, link_element) -> str:
        try:
            img_url = self._extract_image_from_element(link_element)
            if img_url:
                return img_url

            return (
                self.driver.execute_script(
                    """
                    const link = arguments[0];
                    const card = link.closest(
                        '[class*="tile"], [data-widget], div'
                    ) || link.parentElement;
                    const img = card ? card.querySelector('img') : null;
                    if (!img) return '';
                    return img.currentSrc
                        || img.src
                        || img.getAttribute('data-src')
                        || img.getAttribute('srcset')
                        || '';
                    """,
                    link_element,
                )
                or ""
            )
        except Exception:
            return ""

    def _extract_image_from_element(self, element) -> str:
        try:
            img_element = element.find_element(By.CSS_SELECTOR, "img")
            return (
                img_element.get_attribute("currentSrc")
                or img_element.get_attribute("src")
                or img_element.get_attribute("data-src")
                or img_element.get_attribute("srcset")
                or ""
            )
        except Exception:
            return ""

    def _extract_product_links_from_html(self):
        page_source = html.unescape(self.driver.page_source)
        page_source = page_source.replace("\\u002F", "/").replace("\\/", "/")
        pattern = (
            r'(?:https?:)?//(?:www\.)?ozon\.(?:ru|kz)/product/'
            r'[^"\'<>\s\\]+'
            r'|/product/[^"\'<>\s\\]+'
        )
        return re.findall(pattern, page_source)

    def _add_product_link(
        self, items: Dict[str, str], href: str, img_url: str
    ):
        normalized = self._normalize_product_url(href)
        if not normalized or normalized in items:
            return
        items[normalized] = self._normalize_image_url(img_url)

    def _normalize_product_url(self, href: str) -> str:
        if not href:
            return ""

        href = (
            html.unescape(href)
            .replace("\\u002F", "/")
            .replace("\\/", "/")
            .strip()
        )
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            current_url = getattr(self.driver, "current_url", "") or ""
            href = urljoin(current_url or "https://www.ozon.ru", href)

        try:
            parsed = urlsplit(href)
        except ValueError:
            return ""

        host = (parsed.hostname or "").casefold()
        if host not in {"ozon.ru", "www.ozon.ru", "ozon.kz", "www.ozon.kz"}:
            return ""
        if not re.fullmatch(
            r"/product/(?:[^/]+-)?\d+/?",
            parsed.path,
        ):
            return ""

        return urlunsplit(
            (
                parsed.scheme or "https",
                parsed.netloc,
                parsed.path,
                "",
                "",
            )
        )

    def _normalize_image_url(self, img_url: str) -> str:
        if not img_url:
            return ""

        img_url = html.unescape(img_url).strip()
        if "," in img_url and " " in img_url:
            img_url = img_url.split(",", 1)[0].split(" ", 1)[0]
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        return img_url

    def _save_debug_snapshot(self):
        try:
            source_path = self.output_dir / "debug_page_source.html"
            screenshot_path = self.output_dir / "debug_screenshot.png"

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
