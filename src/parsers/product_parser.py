
import logging
import json
import os
import re
import time
import concurrent.futures
from html import unescape
from typing import Any, List, Dict, Optional, Tuple
from dataclasses import dataclass
from bs4 import BeautifulSoup
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from urllib.parse import urlencode, urlsplit
from ..utils.selenium_manager import SeleniumManager
from ..utils.resource_manager import resource_manager

logger = logging.getLogger(__name__)


def _decode_ozon_json_string(value: str) -> Optional[Dict[str, Any]]:
    text = unescape(value or "").strip()
    for _ in range(4):
        if not text:
            return None

        candidates = [text]
        if '\\"' in text:
            candidates.append(text.replace('\\"', '"'))

        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            return decoded if isinstance(decoded, dict) else None

        try:
            decoded_text = json.loads(f'"{text}"')
        except (json.JSONDecodeError, TypeError):
            return None
        if decoded_text == text:
            return None
        text = unescape(decoded_text).strip()
    return None


def _walk_json_values(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_values(child)


def _looks_like_product_title(value: str) -> bool:
    text = re.sub(r"\s+", " ", value or "").strip()
    lowered = text.casefold()
    if len(text) < 5 or len(text) > 300:
        return False
    if not re.search(r"[A-Za-zА-Яа-я]", text):
        return False
    blocked = (
        "ozon",
        "в корзин",
        "купить",
        "доставка",
        "отзывы",
        "рейтинг",
        "характеристики",
        "описание",
    )
    return not any(marker in lowered for marker in blocked)


def _extract_titles_from_json(value: Any) -> List[str]:
    titles = []
    for item in _walk_json_values(value):
        for key in ("title", "name", "productName", "heading"):
            raw = item.get(key)
            if isinstance(raw, str):
                title = re.sub(r"\s+", " ", unescape(raw)).strip()
                if _looks_like_product_title(title) and title not in titles:
                    titles.append(title)
        text_atom = item.get("textAtom")
        if isinstance(text_atom, dict):
            raw = text_atom.get("text")
            if isinstance(raw, str):
                title = re.sub(r"\s+", " ", unescape(raw)).strip()
                if _looks_like_product_title(title) and title not in titles:
                    titles.append(title)
    return titles


def _extract_price_number(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        price = int(value)
        return price if 100 <= price <= 10_000_000 else 0
    cleaned = re.sub(r"[^\d]", "", str(value))
    if not cleaned:
        return 0
    price = int(cleaned)
    return price if 100 <= price <= 10_000_000 else 0


def _extract_prices_from_json(value: Any) -> List[int]:
    prices = []

    def add_price(raw: Any):
        if isinstance(raw, (dict, list)):
            for nested_price in _extract_prices_from_json(raw):
                if nested_price not in prices:
                    prices.append(nested_price)
            return
        price = _extract_price_number(raw)
        if price and price not in prices:
            prices.append(price)

    preferred_keys = (
        "cardPrice",
        "finalPrice",
        "currentPrice",
        "salePrice",
        "discountPrice",
        "lowPrice",
        "price",
    )

    for item in _walk_json_values(value):
        for key in preferred_keys:
            if key in item:
                add_price(item.get(key))
        for key, raw in item.items():
            if "price" in str(key).casefold():
                add_price(raw)

    if isinstance(value, str):
        for match in re.finditer(
            r"(\d[\d\s\u00a0\u202f.,]{1,})\s*(?:₸|тг|тенге)",
            value,
            re.IGNORECASE,
        ):
            add_price(match.group(1))

    return prices


def _pick_sale_price(value: Any) -> int:
    candidates = []
    preferred_keys = (
        "cardPrice",
        "finalPrice",
        "currentPrice",
        "salePrice",
        "discountPrice",
        "lowPrice",
        "price",
    )
    if not isinstance(value, dict):
        return 0
    for key in preferred_keys:
        price = _extract_price_number(value.get(key))
        if price and price not in candidates:
            candidates.append(price)
    return min(candidates) if candidates else 0


def _pick_original_price(value: Any, sale_price: int = 0) -> int:
    if not isinstance(value, dict):
        return 0
    candidates = []
    for key in ("originalPrice", "oldPrice", "basePrice"):
        price = _extract_price_number(value.get(key))
        if price and price not in candidates:
            candidates.append(price)
    if sale_price:
        candidates = [price for price in candidates if price > sale_price]
    return max(candidates) if candidates else 0


def _extract_ozon_widget_payloads(page_source: str) -> List[Tuple[str, Dict[str, Any]]]:
    source = (
        unescape(page_source or "")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
    )
    payloads: List[Tuple[str, Dict[str, Any]]] = []
    pattern = re.compile(
        r'"(?P<key>web(?:ProductHeading|StickyProducts|Price)[^"]*)"\s*:\s*"'
        r'(?P<value>(?:\\.|[^"\\])*)"',
        re.DOTALL,
    )
    for match in pattern.finditer(source):
        payload = _decode_ozon_json_string(match.group("value"))
        if payload:
            payloads.append((match.group("key"), payload))
    return payloads


def _walk_json_ld_products(value) -> List[Dict]:
    products = []
    if isinstance(value, list):
        for item in value:
            products.extend(_walk_json_ld_products(item))
        return products
    if not isinstance(value, dict):
        return products

    value_type = value.get("@type")
    types = value_type if isinstance(value_type, list) else [value_type]
    if any(str(item).casefold() == "product" for item in types):
        products.append(value)
    for child in value.values():
        if isinstance(child, (dict, list)):
            products.extend(_walk_json_ld_products(child))
    return products


def extract_product_page_fallback(page_source: str) -> Dict[str, object]:
    """Извлекает карточку из meta и JSON-LD при изменении DOM Ozon."""
    soup = BeautifulSoup(page_source or "", "html.parser")
    title = ""
    image_url = ""
    prices: List[int] = []

    heading = soup.select_one("h1")
    if heading:
        title = heading.get_text(" ", strip=True)

    if not title:
        title_meta = soup.select_one(
            'meta[property="og:title"], meta[name="title"]'
        )
        if title_meta:
            title = str(title_meta.get("content") or "").strip()
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    image_meta = soup.select_one('meta[property="og:image"]')
    if image_meta:
        image_url = str(image_meta.get("content") or "").strip()

    price_meta = soup.select_one(
        'meta[property="product:price:amount"],'
        'meta[itemprop="price"],'
        '[itemprop="price"][content]'
    )
    if price_meta:
        raw_price = str(price_meta.get("content") or "")
        cleaned = re.sub(r"[^\d]", "", raw_price)
        if cleaned:
            prices.append(int(cleaned))

    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text()
        if not raw_json.strip():
            continue
        try:
            payload = json.loads(unescape(raw_json))
        except (json.JSONDecodeError, TypeError):
            continue
        for product in _walk_json_ld_products(payload):
            if not title:
                title = str(product.get("name") or "").strip()
            if not image_url:
                image = product.get("image")
                if isinstance(image, list):
                    image = image[0] if image else ""
                if isinstance(image, dict):
                    image = image.get("url")
                image_url = str(image or "").strip()

            offers = product.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                continue
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                for key in ("price", "lowPrice", "salePrice"):
                    cleaned = re.sub(
                        r"[^\d]",
                        "",
                        str(offer.get(key) or ""),
                    )
                    if cleaned:
                        prices.append(int(cleaned))

    for key, payload in _extract_ozon_widget_payloads(page_source):
        if not title and (
            key.startswith("webProductHeading")
            or key.startswith("webStickyProducts")
        ):
            titles = _extract_titles_from_json(payload)
            if titles:
                title = titles[0]
        if not image_url:
            for item in _walk_json_values(payload):
                for image_key in ("coverImageUrl", "imageUrl", "image", "url"):
                    image_value = item.get(image_key)
                    if isinstance(image_value, str) and image_value.startswith(
                        ("http://", "https://", "//")
                    ):
                        image_url = image_value
                        break
                if image_url:
                    break
        if key.startswith("webPrice") or "Price" in key:
            prices.extend(_extract_prices_from_json(payload))

    return {
        "title": re.sub(r"\s+", " ", unescape(title)).strip(),
        "image_url": image_url,
        "prices": list(dict.fromkeys(price for price in prices if price > 0)),
    }


@dataclass
class ProductInfo:
    article: str
    name: str = ""
    company_name: str = ""
    company_inn: str = ""
    image_url: str = ""
    card_price: int = 0
    price: int = 0
    original_price: int = 0
    seller_id: str = ""
    seller_link: str = ""
    success: bool = False
    error: str = ""

class ProductWorker:
    
    def __init__(self, worker_id: int, headless: bool = True):
        self.worker_id = worker_id
        self.selenium_manager = SeleniumManager(headless=headless)
        self.driver = None
        logger.info(f"Воркер {worker_id} инициализирован")
    
    def initialize(self):
        try:
            self._ensure_driver()
        except Exception as e:
            logger.error(f"Ошибка инициализации воркера {self.worker_id}: {e}")
            raise

    def _ensure_driver(self):
        if self.driver:
            return
        self.driver = self.selenium_manager.create_driver()
        logger.info(f"Воркер {self.worker_id} готов к работе")
    
    def parse_products(self, articles: List[str], product_links: Dict[str, Any]) -> List[ProductInfo]:
        results = []
        
        for article in articles:
            try:
                # Находим ссылку и изображение для артикула
                product_url = ""
                link_metadata: Dict[str, Any] = {}
                
                for url, payload in product_links.items():
                    if article in url:
                        product_url = url
                        link_metadata = self._normalize_link_metadata(payload)
                        break
                
                result = self._parse_single_product(
                    article,
                    product_url,
                    link_metadata,
                )
                
                # Используем изображение из ссылок вместо API
                if result.success and link_metadata.get("image_url"):
                    result.image_url = str(link_metadata["image_url"])
                
                results.append(result)
                
                if result.success:
                    logger.info(f"Воркер {self.worker_id}: Товар {article} обработан успешно")
                else:
                    logger.warning(f"Воркер {self.worker_id}: Ошибка товара {article}: {result.error}")
                    
            except Exception as e:
                logger.error(f"Воркер {self.worker_id}: Критическая ошибка товара {article}: {e}")
                results.append(ProductInfo(article=article, error=str(e)))
            
            time.sleep(1.5)
        
        return results
    
    def _parse_single_product(
        self,
        article: str,
        product_url: str,
        link_metadata: Optional[Dict[str, Any]] = None,
    ) -> ProductInfo:
        max_retries = 1
        link_metadata = link_metadata or {}
        
        for attempt in range(max_retries):
            try:
                if not product_url:
                    return self._build_from_link_metadata(
                        article,
                        link_metadata,
                        "Не найдена ссылка товара",
                    )

                api_product_info = self._parse_product_api(
                    article,
                    product_url,
                )
                self._apply_link_metadata(api_product_info, link_metadata)
                if api_product_info.success:
                    return api_product_info

                self._ensure_driver()
                if not self.selenium_manager.navigate_to_url(product_url):
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    return self._build_from_link_metadata(
                        article,
                        link_metadata,
                        "Не удалось загрузить карточку товара",
                    )

                WebDriverWait(self.driver, 10).until(
                    lambda driver: (
                        driver.find_elements(By.CSS_SELECTOR, "h1")
                        or driver.find_elements(
                            By.CSS_SELECTOR,
                            'meta[property="og:title"]',
                        )
                        or driver.title
                    )
                )

                product_info = self._parse_product_page(article)
                self._apply_link_metadata(product_info, link_metadata)
                
                if product_info.success:
                    return product_info
                elif attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                else:
                    return self._build_from_link_metadata(
                        article,
                        link_metadata,
                        product_info.error,
                    )
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.debug(f"Попытка {attempt + 1} неудачна для товара {article}: {e}")
                    time.sleep(5)
                    continue
                else:
                    return self._build_from_link_metadata(
                        article,
                        link_metadata,
                        f"Ошибка парсинга: {str(e)}",
                    )
        
        return ProductInfo(article=article, error="Превышено количество попыток")

    def _parse_product_api(
        self,
        article: str,
        product_url: str,
    ) -> ProductInfo:
        last_error = ""

        json_content = self._fetch_product_api_json_http(product_url)
        if json_content:
            product_info = self._parse_json_response(article, json_content)
            if product_info.success:
                logger.info("Товар %s получен через Ozon composer API", article)
                return product_info
            last_error = product_info.error

        if os.getenv("OZON_PRODUCT_API_SELENIUM", "1") != "0":
            json_content = self._fetch_product_api_json_browser(product_url)
            if json_content:
                product_info = self._parse_json_response(article, json_content)
                if product_info.success:
                    logger.info(
                        "Товар %s получен через Ozon composer API в браузере",
                        article,
                    )
                    return product_info
                last_error = product_info.error

        return ProductInfo(
            article=article,
            error=last_error or "Не получен JSON Ozon composer API",
        )

    def _fetch_product_api_json_http(self, product_url: str) -> str:
        api_url = self._build_product_api_url(product_url)
        if not api_url:
            return ""

        try:
            response = requests.get(
                api_url,
                headers=self._build_product_api_headers(product_url),
                timeout=float(os.getenv("OZON_PRODUCT_API_TIMEOUT", "12")),
            )
            if response.status_code != 200:
                logger.debug(
                    "Ozon composer API HTTP %s: %s",
                    response.status_code,
                    api_url,
                )
                return ""
            return self._extract_json_content(response.text)
        except requests.RequestException as exc:
            logger.debug("Ozon composer API HTTP error: %s", exc)
            return ""

    def _fetch_product_api_json_browser(self, product_url: str) -> str:
        api_url = self._build_product_api_url(product_url)
        if not api_url:
            return ""

        try:
            self._ensure_driver()
            if not self.selenium_manager.navigate_to_url(api_url):
                return ""
            return self.selenium_manager.wait_for_json_response(
                timeout=int(os.getenv("OZON_PRODUCT_API_BROWSER_TIMEOUT", "15"))
            ) or ""
        except Exception as exc:
            logger.debug("Ozon composer API browser error: %s", exc)
            return ""

    def _build_product_api_url(self, product_url: str) -> str:
        try:
            parsed = urlsplit(product_url)
        except ValueError:
            return ""

        host = (parsed.hostname or "").casefold()
        if host not in {"ozon.ru", "www.ozon.ru", "ozon.kz", "www.ozon.kz"}:
            return ""

        product_path = parsed.path
        if parsed.query:
            product_path = f"{product_path}?{parsed.query}"

        api_host = "www.ozon.kz" if host.endswith("ozon.kz") else "www.ozon.ru"
        params = urlencode(
            {
                "url": product_path,
                "layout_container": "pdpPage2column",
                "layout_page_index": "2",
            }
        )
        return f"https://{api_host}/api/composer-api.bx/page/json/v2?{params}"

    def _build_product_api_headers(self, product_url: str) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": product_url,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }

    def _extract_json_content(self, raw_content: str) -> str:
        text = unescape(raw_content or "").strip()
        if not text:
            return ""

        if text.startswith("{") and text.endswith("}"):
            return text

        pre_match = re.search(
            r"<pre[^>]*>(.*?)</pre>",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if pre_match:
            return unescape(pre_match.group(1)).strip()

        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
            return text[first_brace:last_brace + 1]

        return ""

    def _normalize_link_metadata(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            return {
                "image_url": str(payload.get("image_url") or ""),
                "title": str(payload.get("title") or ""),
                "price": _extract_price_number(payload.get("price")),
            }
        return {"image_url": str(payload or ""), "title": "", "price": 0}

    def _apply_link_metadata(
        self,
        product_info: ProductInfo,
        metadata: Dict[str, Any],
    ) -> None:
        if not metadata:
            return
        if (
            metadata.get("title")
            and (
                not product_info.name
                or self._is_generic_ozon_title(product_info.name)
            )
        ):
            product_info.name = self._fix_text_encoding(
                str(metadata["title"])
            )
        if not product_info.image_url and metadata.get("image_url"):
            product_info.image_url = str(metadata["image_url"])
        if not product_info.price and metadata.get("price"):
            product_info.card_price = int(metadata["price"])
            product_info.price = int(metadata["price"])
        if product_info.name and product_info.price:
            product_info.success = True
            product_info.error = ""

    def _is_generic_ozon_title(self, value: str) -> bool:
        text = re.sub(r"\s+", " ", value or "").strip().casefold()
        if not text:
            return True
        generic_markers = (
            "ozon интернет-магазин",
            "интернет-магазин ozon",
            "ozon marketplace",
            "ozon казахстан",
        )
        return any(marker in text for marker in generic_markers)

    def _build_from_link_metadata(
        self,
        article: str,
        metadata: Dict[str, Any],
        error: str,
    ) -> ProductInfo:
        product_info = ProductInfo(article=article, error=error)
        self._apply_link_metadata(product_info, metadata)
        if product_info.success:
            logger.info(
                "Товар %s восстановлен из данных категории Ozon",
                article,
            )
        return product_info

    def _parse_product_page(self, article: str) -> ProductInfo:
        try:
            product_info = ProductInfo(article=article)
            headings = self.driver.find_elements(By.CSS_SELECTOR, "h1")
            if headings:
                product_info.name = self._fix_text_encoding(
                    headings[0].text.strip()
                )

            seller_name, seller_link = self._extract_seller()
            product_info.company_name = seller_name
            product_info.seller_link = seller_link

            images = self.driver.find_elements(
                By.CSS_SELECTOR, 'meta[property="og:image"]'
            )
            if images:
                product_info.image_url = images[0].get_attribute("content") or ""

            seller_id = re.search(
                r"/seller/(?:[^/]*-)?(\d+)/?",
                product_info.seller_link,
            )
            if seller_id:
                product_info.seller_id = seller_id.group(1)

            price_widgets = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-widget="webPrice"]'
            )
            price_text = price_widgets[0].text if price_widgets else ""
            prices = self._extract_prices(price_text)
            if prices:
                product_info.card_price = prices[0]
                product_info.price = prices[0]
                if len(prices) > 1 and prices[1] > prices[0]:
                    product_info.original_price = prices[1]

            fallback = extract_product_page_fallback(
                self.driver.page_source
            )
            if not product_info.name:
                product_info.name = self._fix_text_encoding(
                    str(fallback["title"])
                )
            if not product_info.image_url:
                product_info.image_url = str(fallback["image_url"])
            if not product_info.price and fallback["prices"]:
                fallback_prices = fallback["prices"]
                product_info.card_price = fallback_prices[0]
                product_info.price = fallback_prices[0]
                if (
                    len(fallback_prices) > 1
                    and fallback_prices[1] > fallback_prices[0]
                ):
                    product_info.original_price = fallback_prices[1]

            if product_info.name and product_info.price:
                product_info.success = True
            elif product_info.name:
                product_info.error = "Не найдена цена товара в карточке"
            else:
                product_info.error = "Не найдено название товара в карточке"

            return product_info
        except Exception as e:
            return ProductInfo(
                article=article,
                error=f"Ошибка обработки карточки: {e}",
            )

    def _extract_seller(self) -> Tuple[str, str]:
        seller_links = self.driver.find_elements(
            By.CSS_SELECTOR, 'a[href*="/seller/"]'
        )
        fallback = ("", "")

        for seller in seller_links:
            href = seller.get_attribute("href") or ""
            title = seller.get_attribute("title") or ""
            text = seller.text or ""
            value = title.strip() or text.strip()

            if not value:
                try:
                    value = seller.find_element(By.XPATH, "..").text.strip()
                except Exception:
                    value = ""

            value = self._clean_seller_name(
                self._fix_text_encoding(value)
            )
            if value:
                return value, href
            if href and not fallback[1]:
                fallback = ("", href)

        return fallback

    def _fix_text_encoding(self, value: str) -> str:
        value = value or ""
        for encoding in ("cp1251", "latin1"):
            try:
                fixed = value.encode(encoding).decode("utf-8")
                if fixed != value:
                    return fixed
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return value

    def _clean_seller_name(self, value: str) -> str:
        value = re.sub(r"\s+", " ", value or "").strip()
        for suffix in ("Перейти в магазин", "В магазин"):
            value = value.replace(suffix, "").strip()
        return value

    def _extract_prices(self, price_text: str) -> List[int]:
        values = []
        for line in (price_text or "").splitlines():
            number = self._extract_price_number(line)
            if number and number not in values:
                values.append(number)
        return values
    
    def _parse_json_response(self, article: str, json_content: str) -> ProductInfo:
        try:
            data = json.loads(json_content)
            
            if 'widgetStates' not in data:
                return ProductInfo(article=article, error="Отсутствует widgetStates в ответе")
            
            widget_states = data['widgetStates']
            product_info = ProductInfo(article=article)
            
            # Ищем информацию о товаре в webStickyProducts
            sticky_product_data = self._find_sticky_product_data(widget_states)
            if sticky_product_data:
                titles = _extract_titles_from_json(sticky_product_data)
                product_info.name = (
                    sticky_product_data.get('name', '')
                    or sticky_product_data.get('title', '')
                    or (titles[0] if titles else '')
                )
                product_info.image_url = (
                    sticky_product_data.get('coverImageUrl', '')
                    or sticky_product_data.get('imageUrl', '')
                )
                
                # Информация о продавце
                seller_info = sticky_product_data.get('seller', {})
                product_info.company_name = seller_info.get('name', '')
                product_info.company_inn = seller_info.get('inn', '')
                
                # Извлекаем ID и ссылку продавца
                seller_link = seller_info.get('link', '')
                if seller_link:
                    # Ищем seller_id в разных форматах: /seller/123456/ или /seller/name-123456/
                    seller_id = re.search(r'/seller/(?:[^/]*-)?(\d+)/?', seller_link)
                    if seller_id:
                        product_info.seller_id = seller_id.group(1)
                        product_info.seller_link = f"https://ozon.ru/seller/{seller_id.group(1)}"
                        logger.debug(f"Найден seller_id из sticky_product_data: {product_info.seller_id}")
                    else:
                        logger.debug(f"Не удалось извлечь seller_id из ссылки: {seller_link}")
            
            # Резервный поиск seller_id во всём JSON, если не нашли в sticky_product_data
            if not product_info.seller_id:
                raw_data = json.dumps(widget_states)
                # Ищем все возможные варианты seller ссылок
                seller_matches = re.findall(r'/seller/(?:[^/]*-)?(\d+)/?', raw_data)
                if seller_matches:
                    # Берём первый найденный seller_id
                    product_info.seller_id = seller_matches[0]
                    product_info.seller_link = f"https://ozon.ru/seller/{seller_matches[0]}"
                    logger.info(f"Найден seller_id через резервный поиск для товара {article}: {product_info.seller_id} (всего найдено: {len(seller_matches)})")
                else:
                    logger.debug(f"seller_id не найден ни в sticky_product_data, ни в резервном поиске для товара {article}")
            
            # Ищем информацию о ценах в webPrice
            price_data = self._find_price_data(widget_states)
            if price_data:
                sale_price = _pick_sale_price(price_data)
                if sale_price:
                    product_info.card_price = sale_price
                    product_info.price = sale_price
                product_info.original_price = _pick_original_price(
                    price_data,
                    sale_price,
                )

            if not product_info.name:
                titles = []
                for value in widget_states.values():
                    decoded = self._decode_widget_state(value)
                    if decoded:
                        titles.extend(_extract_titles_from_json(decoded))
                if titles:
                    product_info.name = titles[0]

            if not product_info.image_url:
                product_info.image_url = self._find_image_url(widget_states)

            if not product_info.price:
                prices = []
                for value in widget_states.values():
                    decoded = self._decode_widget_state(value)
                    if decoded:
                        sale_price = _pick_sale_price(decoded)
                        if sale_price:
                            prices.append(sale_price)
                        else:
                            prices.extend(_extract_prices_from_json(decoded))
                prices = list(dict.fromkeys(price for price in prices if price))
                if prices:
                    product_info.card_price = min(prices)
                    product_info.price = min(prices)

            if product_info.price and not product_info.original_price:
                for value in widget_states.values():
                    decoded = self._decode_widget_state(value)
                    if decoded:
                        product_info.original_price = _pick_original_price(
                            decoded,
                            product_info.price,
                        )
                        if product_info.original_price:
                            break
            
            # Проверяем, что получили основную информацию
            if product_info.name and product_info.price:
                product_info.success = True
            elif product_info.name or product_info.card_price:
                product_info.error = "Неполные данные товара в JSON Ozon"
            else:
                product_info.error = "Не найдена основная информация о товаре"
            
            return product_info
            
        except json.JSONDecodeError as e:
            return ProductInfo(article=article, error=f"Ошибка парсинга JSON: {str(e)}")
        except Exception as e:
            return ProductInfo(article=article, error=f"Ошибка обработки данных: {str(e)}")
    
    def _find_sticky_product_data(self, widget_states: Dict) -> Optional[Dict]:
        for key, value in widget_states.items():
            if key.startswith('webStickyProducts-') and isinstance(value, str):
                decoded = self._decode_widget_state(value)
                if decoded:
                    return decoded
        return None
    
    def _find_price_data(self, widget_states: Dict) -> Optional[Dict]:
        for key, value in widget_states.items():
            if key.startswith('webPrice-') and isinstance(value, str):
                decoded = self._decode_widget_state(value)
                if decoded:
                    return decoded
        return None

    def _decode_widget_state(self, value: Any) -> Optional[Dict[str, Any]]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return None
        return _decode_ozon_json_string(value)

    def _find_image_url(self, widget_states: Dict) -> str:
        for value in widget_states.values():
            decoded = self._decode_widget_state(value)
            if not decoded:
                continue
            for item in _walk_json_values(decoded):
                for key in ("coverImageUrl", "imageUrl", "image", "url"):
                    image_value = item.get(key)
                    if isinstance(image_value, str) and image_value.startswith(
                        ("http://", "https://", "//")
                    ):
                        return image_value
        return ""
    
    def _extract_price_number(self, price_str: str) -> int:
        return _extract_price_number(price_str)
    
    def close(self):
        if self.selenium_manager:
            self.selenium_manager.close()
        logger.info(f"Воркер {self.worker_id} закрыт")

class OzonProductParser:
    
    def __init__(self, max_workers: int = 5, user_id: str = None, headless: bool = True):
        self.max_workers = max_workers
        self.user_id = user_id
        self.headless = headless
        self.results: List[ProductInfo] = []
        logger.info(f"Парсер товаров инициализирован с макс {max_workers} воркерами для пользователя {user_id}")
    
    def parse_products(
        self,
        product_links: Dict[str, Any],
    ) -> List[ProductInfo]:
        # Сохраняем ссылки для использования в воркерах
        self.product_links = product_links
        
        articles = []
        for url in product_links.keys():
            article = self._extract_article_from_url(url)
            if article:
                articles.append(article)
        
        if not articles:
            logger.error("Не найдено артикулов для парсинга")
            return []

        listing_results = self._parse_products_from_listing(
            product_links,
            articles,
        )
        complete_listing_results = [
            product for product in listing_results if product.success
        ]
        if len(complete_listing_results) == len(articles):
            logger.info(
                "Все %s товаров получены из карточек категории Ozon, "
                "переходы в карточки товаров пропущены",
                len(articles),
            )
            return listing_results

        listing_result_by_article = {
            product.article: product
            for product in listing_results
            if product.success
        }
        articles_to_parse = [
            article
            for article in articles
            if article not in listing_result_by_article
        ]
        if listing_result_by_article:
            logger.info(
                "Переходы в карточки Ozon нужны только для %s/%s товаров",
                len(articles_to_parse),
                len(articles),
            )
            self.product_links = {
                url: payload
                for url, payload in product_links.items()
                if self._extract_article_from_url(url) in articles_to_parse
            }
        
        # Получаем количество воркеров от менеджера ресурсов
        if self.user_id:
            allocated_workers = resource_manager.start_parsing_session(
                self.user_id, 'products', len(articles_to_parse)
            )
        else:
            allocated_workers = self._calculate_optimal_workers(
                len(articles_to_parse)
            )

        worker_limit = max(
            1,
            int(os.getenv("OZON_PRODUCT_WORKERS", "2")),
        )
        allocated_workers = min(
            allocated_workers,
            self.max_workers,
            worker_limit,
            len(articles_to_parse),
        )
        
        logger.info(
            f"Начало парсинга {len(articles_to_parse)} товаров "
            f"с {allocated_workers} воркерами для пользователя {self.user_id}"
        )
        
        if allocated_workers == 1:
            parsed_results = self._parse_single_worker(articles_to_parse)
        else:
            parsed_results = self._parse_multiple_workers(
                articles_to_parse,
                allocated_workers,
            )

        if not listing_result_by_article:
            return parsed_results

        parsed_result_by_article = {
            product.article: product for product in parsed_results
        }
        return [
            listing_result_by_article.get(article)
            or parsed_result_by_article.get(article)
            or ProductInfo(article=article, error="Не обработан")
            for article in articles
        ]
    
    def _extract_article_from_url(self, url: str) -> str:
        try:
            match = re.search(r'/product/(?:[^/]+-)?(\d+)/?', url)
            return match.group(1) if match else ""
        except Exception:
            return ""

    def _parse_products_from_listing(
        self,
        product_links: Dict[str, Any],
        articles: List[str],
    ) -> List[ProductInfo]:
        metadata_by_article: Dict[str, Dict[str, Any]] = {}
        for url, payload in product_links.items():
            article = self._extract_article_from_url(url)
            if not article:
                continue
            metadata_by_article[article] = self._normalize_listing_metadata(
                payload,
            )

        results = []
        for article in articles:
            metadata = metadata_by_article.get(article, {})
            title = str(metadata.get("title") or "").strip()
            price = int(metadata.get("price") or 0)
            image_url = str(metadata.get("image_url") or "")
            if title and price:
                results.append(
                    ProductInfo(
                        article=article,
                        name=title,
                        image_url=image_url,
                        card_price=price,
                        price=price,
                        success=True,
                    )
                )
            else:
                results.append(
                    ProductInfo(
                        article=article,
                        image_url=image_url,
                        error="Нет названия или цены в карточке категории",
                    )
                )

        successful = len([product for product in results if product.success])
        logger.info(
            "Из карточек категории Ozon получено товаров с названием и ценой: "
            "%s/%s",
            successful,
            len(results),
        )
        return results

    def _normalize_listing_metadata(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            return {
                "image_url": str(payload.get("image_url") or ""),
                "title": str(payload.get("title") or ""),
                "price": _extract_price_number(payload.get("price")),
            }
        return {"image_url": str(payload or ""), "title": "", "price": 0}
    
    def _parse_single_worker(self, articles: List[str]) -> List[ProductInfo]:
        worker = ProductWorker(1, headless=self.headless)
        try:
            return worker.parse_products(articles, self.product_links)
        finally:
            worker.close()
    
    def _calculate_optimal_workers(self, total_links: int) -> int:
        if total_links <= 10:
            return 1
        elif total_links <= 25:
            return 2
        elif total_links <= 50:
            return 3
        else:
            return min(5, self.max_workers)  # Максимум 5 воркеров
    
    def _parse_multiple_workers(self, articles: List[str], num_workers: int) -> List[ProductInfo]:
        chunks = self._distribute_articles(articles, num_workers)
        
        # Логируем распределение
        for i, chunk in enumerate(chunks):
            if chunk:
                logger.info(f"Воркер {i+1}: {len(chunk)} товаров")
        
        all_results = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_worker = {}
            
            for i, chunk in enumerate(chunks):
                if chunk:
                    future = executor.submit(self._worker_task_with_retry, i + 1, chunk)
                    future_to_worker[future] = i + 1
            
            for future in concurrent.futures.as_completed(future_to_worker):
                worker_id = future_to_worker[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    logger.info(f"Воркер {worker_id} завершил работу с {len(results)} товарами")
                except Exception as e:
                    logger.error(f"Ошибка воркера {worker_id}: {e}")
        
        return self._sort_results_by_original_order(all_results, articles)
    
    def _distribute_articles(self, articles: List[str], num_workers: int) -> List[List[str]]:
        chunks = [[] for _ in range(num_workers)]
        
        for i, article in enumerate(articles):
            worker_index = i % num_workers
            chunks[worker_index].append(article)
        
        return chunks
    
    def _worker_task_with_retry(self, worker_id: int, articles: List[str]) -> List[ProductInfo]:
        max_worker_retries = 3
        for attempt in range(max_worker_retries):
            worker = ProductWorker(worker_id, headless=self.headless)
            try:
                results = worker.parse_products(articles, self.product_links)
                return results
            except Exception as e:
                if "Access blocked" in str(e) and attempt < max_worker_retries - 1:
                    logger.warning(
                        f"Воркер {worker_id} заблокирован, пересоздаем (попытка {attempt + 1}/3)"
                    )
                    time.sleep(15)     
                    continue
                else:
                    raise
            finally:
                # Гарантируем закрытие воркера в любом случае
                worker.close()
        return []
    
    def _sort_results_by_original_order(self, results: List[ProductInfo], original_articles: List[str]) -> List[ProductInfo]:
        result_dict = {result.article: result for result in results}
        return [result_dict.get(article, ProductInfo(article=article, error="Не обработан")) 
                for article in original_articles]
    
    def cleanup(self):
        """Принудительная очистка всех ресурсов парсера"""
        logger.info("Очистка ресурсов парсера товаров...")
        # Даем время на завершение всех потоков
        time.sleep(2)
        logger.info("Ресурсы парсера товаров очищены")
