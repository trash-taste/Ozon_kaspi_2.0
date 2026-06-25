import json
import logging
import os
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from ..utils.selenium_manager import SeleniumManager
from .ozon_listing_data import decode_ozon_source, extract_price_from_card_text

logger = logging.getLogger(__name__)

PRODUCT_PAGE_MODES = {"always", "missing", "off"}


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


def extract_product_page_fallback(page_source: str) -> Dict[str, object]:
    """Extract minimal product data from an already loaded product page."""
    soup = BeautifulSoup(page_source or "", "html.parser")
    decoded_source = decode_ozon_source(page_source or "")
    title = ""
    image_url = ""
    prices: list[int] = []

    heading = soup.select_one("h1")
    if heading:
        title = heading.get_text(" ", strip=True)

    if not title:
        meta = soup.select_one('meta[property="og:title"], meta[name="title"]')
        if meta:
            title = str(meta.get("content") or "").strip()

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
        price = _extract_price_number(price_meta.get("content"))
        if price:
            prices.append(price)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(unescape(script.string or script.get_text() or ""))
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
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                for key in ("price", "lowPrice", "salePrice"):
                    price = _extract_price_number(offer.get(key))
                    if price and price not in prices:
                        prices.append(price)

    targeted_prices = _extract_product_page_prices(soup, decoded_source)
    if targeted_prices:
        prices = _ordered_unique([*targeted_prices, *prices])

    if not prices:
        page_text_price = extract_price_from_card_text(soup.get_text("\n"))
        if page_text_price:
            prices.append(page_text_price)

    return {
        "title": _clean_text(unescape(title)),
        "image_url": image_url,
        "prices": prices,
    }


class ProductWorker:
    """Product-page parser with listing data as a fallback."""

    def __init__(
        self,
        worker_id: int,
        headless: bool = True,
        page_mode: str | None = None,
    ):
        self.worker_id = worker_id
        self.selenium_manager = SeleniumManager(headless=headless)
        self.driver = None
        self.page_mode = _normalize_product_page_mode(page_mode)

    def parse_products(
        self,
        articles: List[str],
        product_links: Dict[str, Any],
    ) -> List[ProductInfo]:
        results = []
        for article in articles:
            url, metadata = self._find_link(article, product_links)
            results.append(self._parse_single_product(article, url, metadata))
        return results

    def _parse_single_product(
        self,
        article: str,
        product_url: str,
        metadata: Dict[str, Any] | None = None,
    ) -> ProductInfo:
        product = self._build_from_listing(article, metadata or {})

        if self.page_mode == "off":
            product.error = product.error or "Нет названия или цены в листинге"
            return product

        if self.page_mode == "missing" and product.success:
            return product

        if not product_url:
            product.error = product.error or "Нет ссылки товара"
            return product

        try:
            if not self.driver:
                self.driver = self.selenium_manager.create_driver()
            if not self.selenium_manager.navigate_to_url(product_url):
                if not product.success:
                    product.error = "Не удалось открыть карточку товара"
                else:
                    logger.warning(
                        "Товар %s: не удалось открыть карточку, оставлена цена "
                        "из листинга",
                        article,
                    )
                return product
            page_data = extract_product_page_fallback(self.driver.page_source)
            if page_data["title"]:
                product.name = str(page_data["title"])
            if page_data["image_url"] and not product.image_url:
                product.image_url = str(page_data["image_url"])
            prices = page_data["prices"]
            if prices:
                product.card_price = int(prices[0])
                product.price = int(prices[0])
                higher = [price for price in prices[1:] if price > product.price]
                product.original_price = max(higher) if higher else 0
                logger.info(
                    "Товар %s: цена Ozon взята из карточки товара: %s",
                    article,
                    product.price,
                )
            elif product.success:
                logger.warning(
                    "Товар %s: цена в карточке не найдена, оставлена цена "
                    "из листинга: %s",
                    article,
                    product.price,
                )
            product.success = bool(product.name and product.price)
            product.error = "" if product.success else "Неполные данные товара"
            return product
        except Exception as exc:
            if product.success:
                logger.warning(
                    "Товар %s: ошибка карточки, оставлена цена из листинга: %s",
                    article,
                    exc,
                )
            else:
                product.error = f"Ошибка карточки товара: {exc}"
            return product

    def _build_from_listing(
        self,
        article: str,
        metadata: Dict[str, Any],
    ) -> ProductInfo:
        title = _clean_text(metadata.get("title"))
        price = _extract_price_number(metadata.get("price"))
        product = ProductInfo(
            article=article,
            name=title,
            image_url=str(metadata.get("image_url") or ""),
            card_price=price,
            price=price,
            success=bool(title and price),
        )
        if not product.success:
            missing = []
            if not title:
                missing.append("название")
            if not price:
                missing.append("цена")
            product.error = "Нет " + " и ".join(missing) + " в листинге"
        return product

    def _find_link(
        self,
        article: str,
        product_links: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        for url, payload in product_links.items():
            if article and article in url:
                return url, _normalize_metadata(payload)
        return "", {}

    def close(self) -> None:
        self.selenium_manager.close()


class OzonProductParser:
    def __init__(self, max_workers: int = 5, user_id: str = None, headless: bool = True):
        self.max_workers = max_workers
        self.user_id = user_id
        self.headless = headless
        self.page_mode = _normalize_product_page_mode()
        logger.info(
            "OzonProductParser работает в режиме product-page=%s "
            "(max_workers=%s user=%s)",
            self.page_mode,
            max_workers,
            user_id,
        )

    def parse_products(self, product_links: Dict[str, Any]) -> List[ProductInfo]:
        if self.page_mode == "always":
            products = self._parse_products_from_pages(product_links)
            successful = len([product for product in products if product.success])
            logger.info(
                "Из карточек Ozon получено товаров с названием и ценой: %s/%s",
                successful,
                len(products),
            )
            return products

        products = []
        incomplete: dict[str, Any] = {}

        for url, payload in product_links.items():
            article = self._extract_article_from_url(url)
            if not article:
                continue

            product = self._build_product_from_link(article, payload)
            if product.success:
                products.append(product)
            else:
                incomplete[url] = payload
                products.append(product)

        if incomplete and self.page_mode == "missing":
            fallback_by_article = {
                item.article: item
                for item in self._parse_incomplete_products(incomplete)
            }
            products = [
                fallback_by_article.get(product.article, product)
                if not product.success
                else product
                for product in products
            ]

        successful = len([product for product in products if product.success])
        logger.info(
            "Из листинга Ozon получено товаров с названием и ценой: %s/%s",
            successful,
            len(products),
        )
        return products

    def _build_product_from_link(self, article: str, payload: Any) -> ProductInfo:
        metadata = _normalize_metadata(payload)
        return ProductWorker(0, headless=self.headless)._build_from_listing(
            article,
            metadata,
        )

    def _parse_products_from_pages(
        self,
        product_links: Dict[str, Any],
    ) -> List[ProductInfo]:
        articles = [
            self._extract_article_from_url(url)
            for url in product_links
            if self._extract_article_from_url(url)
        ]
        worker = ProductWorker(
            1,
            headless=self.headless,
            page_mode=self.page_mode,
        )
        try:
            return worker.parse_products(articles, product_links)
        finally:
            worker.close()

    def _parse_incomplete_products(
        self,
        product_links: Dict[str, Any],
    ) -> List[ProductInfo]:
        articles = [
            self._extract_article_from_url(url)
            for url in product_links
            if self._extract_article_from_url(url)
        ]
        worker = ProductWorker(
            1,
            headless=self.headless,
            page_mode=self.page_mode,
        )
        try:
            return worker.parse_products(articles, product_links)
        finally:
            worker.close()

    def _extract_article_from_url(self, url: str) -> str:
        match = re.search(r"/product/(?:[^/]+-)?(\d+)/?", url or "")
        return match.group(1) if match else ""

    def cleanup(self) -> None:
        logger.debug("OzonProductParser cleanup: active browser is not kept")


def _normalize_metadata(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return {
            "title": _clean_text(payload.get("title")),
            "price": _extract_price_number(payload.get("price")),
            "image_url": str(payload.get("image_url") or ""),
        }
    return {
        "title": "",
        "price": 0,
        "image_url": str(payload or ""),
    }


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


def _extract_product_page_prices(
    soup: BeautifulSoup,
    decoded_source: str,
) -> list[int]:
    sale_keys = (
        "cardPrice",
        "finalPrice",
        "currentPrice",
        "salePrice",
        "discountPrice",
        "discountedPrice",
        "priceWithCard",
        "price",
    )
    original_keys = (
        "originalPrice",
        "oldPrice",
        "crossedPrice",
        "basePrice",
        "priceWithoutDiscount",
    )
    sale_prices: list[int] = []
    original_prices: list[int] = []

    for widget in soup.select('[data-widget*="webPrice"], [data-widget*="price"]'):
        price = extract_price_from_card_text(widget.get_text("\n"))
        if price:
            sale_prices.append(price)

    sale_prices.extend(_extract_prices_for_keys(decoded_source, sale_keys))
    original_prices.extend(_extract_prices_for_keys(decoded_source, original_keys))

    sale_prices = _ordered_unique(sale_prices)
    original_prices = [
        price
        for price in _ordered_unique(original_prices)
        if not sale_prices or price > min(sale_prices)
    ]
    if sale_prices:
        return [min(sale_prices), *original_prices]
    return original_prices


def _extract_prices_for_keys(source: str, keys: tuple[str, ...]) -> list[int]:
    prices: list[int] = []
    for key in keys:
        pattern = (
            rf'"{re.escape(key)}"\s*:\s*'
            r'(?:"((?:\\.|[^"\\]){1,120})"|(\d{3,10}))'
        )
        for match in re.finditer(pattern, source, re.IGNORECASE):
            price = _extract_price_number(match.group(1) or match.group(2))
            if price:
                prices.append(price)
    return _ordered_unique(prices)


def _ordered_unique(values: list[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _normalize_product_page_mode(value: str | None = None) -> str:
    raw = (value or os.getenv("OZON_PRODUCT_PAGE_MODE") or "").strip().casefold()
    if not raw and os.getenv("OZON_PRODUCT_PAGE_FALLBACK") == "1":
        raw = "missing"
    if not raw:
        raw = "always"
    if raw not in PRODUCT_PAGE_MODES:
        logger.warning(
            "Неизвестный OZON_PRODUCT_PAGE_MODE=%r, используется always",
            raw,
        )
        return "always"
    return raw


def _walk_json_ld_products(value: Any) -> List[Dict[str, Any]]:
    products = []
    if isinstance(value, list):
        for item in value:
            products.extend(_walk_json_ld_products(item))
        return products
    if not isinstance(value, dict):
        return products

    raw_type = value.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if any(str(item).casefold() == "product" for item in types):
        products.append(value)
    for child in value.values():
        if isinstance(child, (dict, list)):
            products.extend(_walk_json_ld_products(child))
    return products


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
