import asyncio
import json
import logging
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

YANDEX_SEARCH_URL = "https://yandex.kz/search/?text={query}"
MATCH_THRESHOLD = 72.0
MODEL_MATCH_BONUS = 15.0
SEARCH_RETRIES = 3
PAGE_RETRIES = 2
MAX_SEARCH_RESULTS = 12
MAX_CONCURRENT_PAGES = int(os.getenv("INTERNET_PAGE_CONCURRENCY", "2"))
MAX_CONCURRENT_PRODUCTS = int(
    os.getenv("INTERNET_PRODUCT_CONCURRENCY", "2")
)
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("INTERNET_REQUEST_TIMEOUT", "15")
)
PRODUCT_TIMEOUT_SECONDS = float(
    os.getenv("INTERNET_PRODUCT_TIMEOUT", "60")
)
MONEY_QUANT = Decimal("0.01")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
EXCLUDED_DOMAINS = {
    "ozon.kz",
    "ozon.ru",
    "yandex.kz",
    "yandex.ru",
}
BRAND_ALIASES = {
    "apple": "apple",
    "beko": "beko",
    "bosch": "bosch",
    "braun": "braun",
    "candy": "candy",
    "deerma": "deerma",
    "delonghi": "delonghi",
    "dreame": "dreame",
    "dyson": "dyson",
    "electrolux": "electrolux",
    "gorenje": "gorenje",
    "haier": "haier",
    "indesit": "indesit",
    "kitfort": "kitfort",
    "lg": "lg",
    "midea": "midea",
    "moulinex": "moulinex",
    "panasonic": "panasonic",
    "philips": "philips",
    "polaris": "polaris",
    "redmond": "redmond",
    "rowenta": "rowenta",
    "samsung": "samsung",
    "scarlett": "scarlett",
    "sokany": "sokany",
    "tefal": "tefal",
    "vitek": "vitek",
    "xiaomi": "xiaomi",
    "редмонд": "redmond",
}


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"[^\w/-]+", " ", text, flags=re.UNICODE)
    words = [
        BRAND_ALIASES.get(word, word)
        for word in re.sub(r"\s+", " ", text).strip().split()
    ]
    return " ".join(words)


def _use_kaspi_fallback() -> bool:
    return os.getenv("INTERNET_USE_KASPI_FALLBACK", "0").strip() == "1"


def _extract_model_tokens(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    text = text.replace("–", "-").replace("—", "-")
    tokens = re.findall(
        r"(?<![A-Z0-9])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:/[A-Z0-9]+)?",
        text,
    )
    return [
        token
        for token in tokens
        if len(token) >= 3 and any(char.isdigit() for char in token)
    ]


def _extract_models(value: Any) -> set[str]:
    return {
        re.sub(r"[^A-Z0-9]", "", token)
        for token in _extract_model_tokens(value)
    }


def _extract_brand(product: dict[str, Any]) -> str | None:
    explicit = _normalize_text(product.get("brand"))
    if explicit:
        return BRAND_ALIASES.get(explicit, explicit)
    for word in _normalize_text(product.get("title")).split():
        brand = BRAND_ALIASES.get(word)
        if brand:
            return brand
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = re.sub(r"[^\d.,-]", "", str(value)).replace(",", ".")
    if not normalized:
        return None
    try:
        result = Decimal(normalized)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def _to_nonnegative_decimal(value: Any, name: str) -> Decimal:
    result = _to_decimal(value)
    if result is None:
        if str(value).strip() in {"0", "0.0", "0.00"}:
            return Decimal("0")
        raise ValueError(f"{name} должен быть неотрицательным числом")
    return result


def _to_commission_decimal(value: Any) -> Decimal:
    result = _to_nonnegative_decimal(value, "commission_rate")
    if result >= Decimal("100"):
        raise ValueError("commission_rate должен быть меньше 100")
    return result


def _build_search_query(product: dict[str, Any]) -> str:
    brand = _extract_brand(product)
    models = _extract_model_tokens(product.get("title"))
    if models:
        exact = " ".join(
            part
            for part in (brand.upper() if brand else "", *models)
            if part
        )
        return f'"{exact}" цена Казахстан купить'
    title = str(product.get("title") or "").strip()
    return f'"{title[:140]}" цена Казахстан купить'


def _is_excluded_domain(host: str) -> bool:
    normalized = host.casefold().removeprefix("www.")
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in EXCLUDED_DOMAINS
    )


def _parse_search_results(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in soup.select("li.serp-item"):
        link = item.select_one("a.OrganicHost-Link[href]")
        if link is None:
            continue
        url = str(link.get("href") or "").strip()
        parsed = urlparse(url)
        normalized_host = parsed.netloc.casefold().removeprefix("www.")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not normalized_host.endswith(".kz")
            or _is_excluded_domain(parsed.netloc)
            or url in seen
        ):
            continue
        seen.add(url)
        results.append(
            {
                "url": url,
                "source": parsed.netloc.removeprefix("www."),
                "snippet": item.get_text(" ", strip=True),
            }
        )
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    return results


async def search_internet_sources(
    session: aiohttp.ClientSession,
    query: str,
) -> list[dict[str, str]]:
    """Ищет страницы казахстанских магазинов через Yandex.kz."""
    url = YANDEX_SEARCH_URL.format(query=quote_plus(query))
    for attempt in range(1, SEARCH_RETRIES + 1):
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                html = await response.text(errors="replace")
            lowered = html.casefold()
            if "showcaptcha" in str(response.url) or "проверка браузера" in lowered:
                raise RuntimeError("Yandex запросил проверку браузера")
            results = _parse_search_results(html)
            if results:
                return results
            logger.info(
                "Интернет-поиск не вернул магазинов: query=%r attempt=%s/%s",
                query,
                attempt,
                SEARCH_RETRIES,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            logger.warning(
                "Ошибка интернет-поиска: query=%r attempt=%s/%s: %s",
                query,
                attempt,
                SEARCH_RETRIES,
                exc,
            )
        if attempt < SEARCH_RETRIES:
            await asyncio.sleep(attempt * 2)
    return []


def _type_contains(value: Any, expected: str) -> bool:
    if isinstance(value, list):
        return any(_type_contains(item, expected) for item in value)
    return str(value or "").casefold() == expected.casefold()


def _availability_text(value: Any) -> str:
    raw = str(value or "").rstrip("/").rsplit("/", 1)[-1]
    mapping = {
        "instock": "В наличии",
        "limitedavailability": "Ограниченное наличие",
        "onlineonly": "Только онлайн",
        "preorder": "Предзаказ",
        "outofstock": "Нет в наличии",
        "discontinued": "Снят с продажи",
    }
    return mapping.get(raw.casefold(), raw or "Не указано")


def _offer_candidates(
    offers: Any,
    fallback_url: str,
) -> list[dict[str, Any]]:
    if isinstance(offers, list):
        result: list[dict[str, Any]] = []
        for offer in offers:
            result.extend(_offer_candidates(offer, fallback_url))
        return result
    if not isinstance(offers, dict):
        return []

    availability = _availability_text(offers.get("availability"))
    if availability in {"Нет в наличии", "Снят с продажи"}:
        return []
    currency = str(
        offers.get("priceCurrency")
        or offers.get("currency")
        or "KZT"
    ).upper()
    if currency not in {"KZT", "₸", "ТГ", "ТЕНГЕ"}:
        return []

    prices = [
        offers.get("price"),
        offers.get("lowPrice"),
        offers.get("salePrice"),
    ]
    result = []
    for raw_price in prices:
        price = _to_decimal(raw_price)
        if price is not None:
            result.append(
                {
                    "price": price,
                    "url": urljoin(
                        fallback_url,
                        str(offers.get("url") or fallback_url),
                    ),
                    "availability": availability,
                }
            )
    return result


def _walk_json_products(
    value: Any,
    fallback_url: str,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            products.extend(_walk_json_products(item, fallback_url))
        return products
    if not isinstance(value, dict):
        return products

    if _type_contains(value.get("@type"), "Product"):
        title = str(value.get("name") or "").strip()
        brand_value = value.get("brand")
        if isinstance(brand_value, dict):
            brand = brand_value.get("name")
        else:
            brand = brand_value
        for offer in _offer_candidates(value.get("offers"), fallback_url):
            products.append(
                {
                    "title": title,
                    "brand": brand,
                    **offer,
                }
            )

    for child in value.values():
        if isinstance(child, (dict, list)):
            products.extend(_walk_json_products(child, fallback_url))
    return products


def _parse_store_page(
    html: str,
    page_url: str,
    source: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        if not text.strip():
            continue
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        products.extend(_walk_json_products(payload, page_url))

    if not products:
        price_meta = soup.select_one(
            'meta[property="product:price:amount"],'
            'meta[itemprop="price"],'
            '[itemprop="price"][content]'
        )
        price = _to_decimal(
            price_meta.get("content") if price_meta else None
        )
        if price is not None:
            title_meta = soup.select_one(
                'meta[property="og:title"], meta[name="title"]'
            )
            title = (
                title_meta.get("content")
                if title_meta
                else soup.title.get_text(" ", strip=True)
                if soup.title
                else ""
            )
            products.append(
                {
                    "title": title,
                    "brand": None,
                    "price": price,
                    "url": page_url,
                    "availability": "Не указано",
                }
            )

    for product in products:
        product["source"] = source
    return products


async def _fetch_store_page(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    result: dict[str, str],
) -> list[dict[str, Any]]:
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            async with semaphore:
                async with session.get(result["url"]) as response:
                    if response.status >= 400:
                        return []
                    html = await response.text(errors="replace")
            return _parse_store_page(
                html,
                str(response.url),
                result["source"],
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                "Ошибка чтения магазина: url=%s attempt=%s/%s: %s",
                result["url"],
                attempt,
                PAGE_RETRIES,
                exc,
            )
            if attempt < PAGE_RETRIES:
                await asyncio.sleep(attempt)
    return []


def _calculate_match_score(
    ozon_product: dict[str, Any],
    internet_product: dict[str, Any],
) -> float | None:
    ozon_title = _normalize_text(ozon_product.get("title"))
    internet_title = _normalize_text(internet_product.get("title"))
    if not ozon_title or not internet_title:
        return None

    ozon_models = _extract_models(ozon_product.get("title"))
    internet_models = _extract_models(internet_product.get("title"))
    common_models = ozon_models & internet_models
    if ozon_models and (not internet_models or not common_models):
        return None

    ozon_brand = _extract_brand(ozon_product)
    internet_brand = _extract_brand(internet_product)
    if ozon_brand and internet_brand and ozon_brand != internet_brand:
        return None

    token_score = float(fuzz.token_set_ratio(ozon_title, internet_title))
    ratio_score = float(fuzz.ratio(ozon_title, internet_title))
    score = token_score * 0.7 + ratio_score * 0.3
    if common_models:
        score += MODEL_MATCH_BONUS
    return min(100.0, score)


def _select_candidate(
    ozon_product: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], float, int] | None:
    accepted: list[tuple[dict[str, Any], float, Decimal]] = []
    sources: set[str] = set()
    for candidate in candidates:
        price = _to_decimal(candidate.get("price"))
        score = _calculate_match_score(ozon_product, candidate)
        if price is None or score is None or score < MATCH_THRESHOLD:
            continue
        accepted.append((candidate, score, price))
        sources.add(str(candidate.get("source") or ""))
    if not accepted:
        return None
    candidate, score, _ = min(
        accepted,
        key=lambda item: (item[2], -item[1]),
    )
    return candidate, score, len(sources)


def _calculate_economics(
    ozon_product: dict[str, Any],
    internet_product: dict[str, Any],
    match_score: float,
    sources_count: int,
    min_roi: Decimal | None = None,
    commission_rate: Decimal = Decimal("16"),
) -> dict[str, Any] | None:
    ozon_price = _to_decimal(ozon_product.get("price"))
    internet_price = _to_decimal(internet_product.get("price"))
    if ozon_price is None or internet_price is None:
        return None

    delivery = (
        Decimal("950")
        if ozon_price <= Decimal("10000")
        else Decimal("2000")
    )
    commission = internet_price * commission_rate / Decimal("100")
    net_revenue = internet_price - commission
    total_cost = ozon_price + delivery
    price_difference = internet_price - ozon_price
    profit = net_revenue - total_cost
    roi = profit / total_cost * Decimal("100")
    if min_roi is not None and roi < min_roi:
        return None

    def money(value: Decimal) -> float:
        return float(
            value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        )

    models = sorted(
        _extract_models(ozon_product.get("title"))
        & _extract_models(internet_product.get("title"))
    )
    brand = _extract_brand(ozon_product)
    return {
        "ozon_title": str(ozon_product.get("title") or ""),
        "internet_title": str(internet_product.get("title") or ""),
        "brand": brand.upper() if brand else None,
        "model": ", ".join(models) or None,
        "source": internet_product.get("source"),
        "sources_count": sources_count,
        "ozon_price": money(ozon_price),
        "internet_price": money(internet_price),
        "commission_rate": money(commission_rate),
        "commission": money(commission),
        "net_revenue": money(net_revenue),
        "delivery": money(delivery),
        "total_cost": money(total_cost),
        "price_difference": money(price_difference),
        "profit": money(profit),
        "roi": money(roi),
        "match_score": round(match_score, 2),
        "availability": internet_product.get("availability"),
        "ozon_url": str(ozon_product.get("url") or ""),
        "internet_url": str(internet_product.get("url") or ""),
    }


def _build_unmatched_result(product: dict[str, Any]) -> dict[str, Any]:
    ozon_price = _to_decimal(product.get("price"))

    def money(value: Decimal | None) -> float | None:
        if value is None:
            return None
        return float(
            value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        )

    brand = _extract_brand(product)
    return {
        "matched": False,
        "ozon_title": str(product.get("title") or ""),
        "internet_title": "Не найдено точное совпадение",
        "brand": brand.upper() if brand else None,
        "model": ", ".join(sorted(_extract_models(product.get("title")))) or None,
        "source": "",
        "sources_count": 0,
        "ozon_price": money(ozon_price),
        "internet_price": None,
        "commission_rate": None,
        "commission": None,
        "net_revenue": None,
        "delivery": None,
        "total_cost": None,
        "price_difference": None,
        "profit": None,
        "roi": None,
        "match_score": None,
        "availability": "Не найдено",
        "ozon_url": str(product.get("url") or ""),
        "internet_url": "",
    }


async def _compare_one(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    product: dict[str, Any],
    min_roi: Decimal | None,
    commission_rate: Decimal,
) -> dict[str, Any] | None:
    query = _build_search_query(product)
    search_results = await search_internet_sources(session, query)
    pages = (
        await asyncio.gather(
            *(
                _fetch_store_page(session, semaphore, result)
                for result in search_results
            )
        )
        if search_results
        else []
    )
    candidates = [
        candidate
        for page_candidates in pages
        for candidate in page_candidates
    ]
    selected = _select_candidate(product, candidates)
    if selected is None and _use_kaspi_fallback():
        try:
            from services.kaspi_compare import search_kaspi_product

            kaspi_candidates = await search_kaspi_product(
                str(product.get("title") or "")
            )
            candidates = [
                {
                    **candidate,
                    "source": "kaspi.kz",
                    "availability": "В наличии",
                }
                for candidate in kaspi_candidates
            ]
            selected = _select_candidate(product, candidates)
        except Exception:
            logger.exception(
                "Ошибка резервного поиска Kaspi: %s",
                product.get("title"),
            )
    elif selected is None:
        logger.debug(
            "Резервный поиск Kaspi отключен для интернет-сравнения: %s",
            product.get("title"),
        )
    if selected is None:
        if min_roi is None:
            return _build_unmatched_result(product)
        return None
    internet_product, score, sources_count = selected
    result = _calculate_economics(
        product,
        internet_product,
        score,
        sources_count,
        min_roi=min_roi,
        commission_rate=commission_rate,
    )
    if result is not None:
        result["matched"] = True
    return result


async def compare_with_internet(
    ozon_products: list[dict],
    min_roi: Decimal | float | int | str | None = None,
    commission_rate: Decimal | float | int | str = 16,
) -> list[dict]:
    """Сравнивает товары Ozon с ценами интернет-магазинов Казахстана."""
    min_roi_decimal = (
        None
        if min_roi is None
        else _to_nonnegative_decimal(min_roi, "min_roi")
    )
    commission_decimal = _to_commission_decimal(commission_rate)
    logger.info(
        "Получено товаров Ozon для интернет-сравнения: %s",
        len(ozon_products),
    )
    if min_roi_decimal is not None:
        logger.info(
            "Фильтр интернет-отчета: ROI >= %s%%",
            min_roi_decimal,
        )
    logger.info(
        "Комиссия интернет-сравнения: %s%%",
        commission_decimal,
    )
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    page_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
    product_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PRODUCTS)

    async def compare_one_product(
        index: int,
        product: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with product_semaphore:
            logger.info(
                "Интернет-поиск %s/%s: %s",
                index,
                len(ozon_products),
                product.get("title"),
            )
            try:
                return await asyncio.wait_for(
                    _compare_one(
                        session,
                        page_semaphore,
                        product,
                        min_roi_decimal,
                        commission_decimal,
                    ),
                    timeout=PRODUCT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Таймаут интернет-поиска товара после %s сек: %s",
                    PRODUCT_TIMEOUT_SECONDS,
                    product.get("title"),
                )
                if min_roi_decimal is None:
                    return _build_unmatched_result(product)
            except Exception:
                logger.exception(
                    "Ошибка интернет-сравнения: %s",
                    product.get("title"),
                )
                if min_roi_decimal is None:
                    return _build_unmatched_result(product)
            return None

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        compared = await asyncio.gather(
            *(
                compare_one_product(index, product)
                for index, product in enumerate(ozon_products, 1)
            )
        )

    results = [item for item in compared if item is not None]

    results.sort(
        key=lambda item: (
            bool(item.get("matched")),
            item.get("roi") if item.get("roi") is not None else -10**9,
            item.get("profit") if item.get("profit") is not None else -10**9,
        ),
        reverse=True,
    )
    matched_count = len([item for item in results if item.get("matched")])
    logger.info(
        "Интернет-отчет: строк=%s, точных совпадений=%s",
        len(results),
        matched_count,
    )
    return results
