import asyncio
import logging
import os
import re
import unicodedata
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote

import aiohttp
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

KASPI_SEARCH_URL = "https://kaspi.kz/yml/product-view/pl/filters"
KASPI_BASE_URL = "https://kaspi.kz"
DEFAULT_CITY_ID = "750000000"
DEFAULT_CITY_SLUG = "almaty"
MATCH_THRESHOLD = 72.0
BRAND_MISMATCH_PENALTY = 15.0
NEAR_BEST_SCORE_DELTA = 5.0
MAX_CONCURRENT_SEARCHES = 5
SEARCH_PAGES = (0, 1)
REQUEST_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = float(
    os.getenv("KASPI_REQUEST_TIMEOUT", "12")
)
COMPARE_TIMEOUT_SECONDS = float(
    os.getenv("KASPI_COMPARE_TIMEOUT", "180")
)
MONEY_QUANT = Decimal("0.01")


@dataclass
class _SearchContext:
    session: aiohttp.ClientSession
    semaphore: asyncio.Semaphore
    tasks: dict[str, asyncio.Task[list[dict[str, Any]]]] = field(
        default_factory=dict
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_search_context: ContextVar[_SearchContext | None] = ContextVar(
    "kaspi_search_context",
    default=None,
)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_article(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", _normalize_text(value))


def _extract_model_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    text = text.replace("–", "-").replace("—", "-")
    tokens = re.findall(
        r"(?<![A-Z0-9])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:/[A-Z0-9]+)?",
        text,
    )
    return {
        re.sub(r"[^A-Z0-9]", "", token)
        for token in tokens
        if len(token) >= 3 and any(char.isdigit() for char in token)
    }


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def _to_nonnegative_decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} должен быть числом") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} должен быть неотрицательным числом")
    return result


def _to_optional_nonnegative_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    return _to_nonnegative_decimal(value, name)


def _build_search_query(product: dict[str, Any]) -> str:
    title = str(product.get("title") or "").strip()
    brand = str(product.get("brand") or "").strip()
    article = str(product.get("article") or "").strip()

    parts = [part for part in (brand, article, title) if part]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _kaspi_city_id() -> str:
    return os.getenv("KASPI_CITY_ID", DEFAULT_CITY_ID).strip() or DEFAULT_CITY_ID


def _kaspi_city_slug() -> str:
    slug = os.getenv("KASPI_CITY_SLUG", DEFAULT_CITY_SLUG).strip().strip("/")
    return slug or DEFAULT_CITY_SLUG


def _kaspi_proxy_url() -> str | None:
    proxy = (
        os.getenv("KASPI_PROXY_URL")
        or os.getenv("OZON_PROXY_URL")
        or ""
    ).strip()
    return proxy or None


def _kaspi_search_referer(query: str) -> str:
    return (
        f"{KASPI_BASE_URL}/shop/{_kaspi_city_slug()}/search/"
        f"?text={quote(query)}"
    )


def _normalize_kaspi_url(shop_link: str) -> str:
    city_slug = _kaspi_city_slug()
    if shop_link.startswith("/p/"):
        return f"{KASPI_BASE_URL}/shop/{city_slug}{shop_link}"
    if shop_link.startswith("/shop/p/"):
        return f"{KASPI_BASE_URL}/shop/{city_slug}{shop_link[5:]}"
    if shop_link.startswith("/"):
        return f"{KASPI_BASE_URL}{shop_link}"
    return shop_link.replace("/shop/p/", f"/shop/{city_slug}/p/")


def _brands_differ(ozon_brand: Any, kaspi_brand: Any) -> bool:
    left = _normalize_text(ozon_brand)
    right = _normalize_text(kaspi_brand)
    if not left or not right:
        return False
    if left in right or right in left:
        return False
    return fuzz.ratio(left, right) < 85


def _calculate_match_score(
    ozon_product: dict[str, Any],
    kaspi_product: dict[str, Any],
) -> float | None:
    ozon_article = _normalize_article(ozon_product.get("article"))
    kaspi_article = _normalize_article(kaspi_product.get("article"))
    if ozon_article and kaspi_article and ozon_article != kaspi_article:
        return None

    ozon_title = _normalize_text(ozon_product.get("title"))
    kaspi_title = _normalize_text(kaspi_product.get("title"))
    if not ozon_title or not kaspi_title:
        return None

    ozon_models = _extract_model_tokens(ozon_product.get("title"))
    kaspi_models = _extract_model_tokens(kaspi_product.get("title"))
    if ozon_models and kaspi_models and not (ozon_models & kaspi_models):
        return None

    score = float(fuzz.WRatio(ozon_title, kaspi_title))
    if _brands_differ(
        ozon_product.get("brand"),
        kaspi_product.get("brand"),
    ):
        score -= BRAND_MISMATCH_PENALTY
    return max(0.0, score)


def _normalize_kaspi_card(card: dict[str, Any]) -> dict[str, Any] | None:
    title = str(card.get("title") or "").strip()
    shop_link = str(card.get("shopLink") or "").strip()
    price = _to_decimal(card.get("unitSalePrice"))
    if price is None:
        price = _to_decimal(card.get("unitPrice"))
    if not title or not shop_link or price is None:
        return None

    shop_link = _normalize_kaspi_url(shop_link)

    return {
        "title": title,
        "price": float(price),
        "url": shop_link,
        "brand": card.get("brand") or None,
        # Kaspi ID is a marketplace ID, not a manufacturer article.
        "article": None,
    }


async def _request_kaspi_page(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    query: str,
    page: int,
) -> list[dict[str, Any]]:
    city_id = _kaspi_city_id()
    params = {
        "text": query,
        "page": page,
        "all": "false",
        "fl": "true",
        "ui": "d",
        "q": "",
        "i": "-1",
        "c": city_id or DEFAULT_CITY_ID,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": _kaspi_search_referer(query),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0 Safari/537.36"
        ),
    }
    request_kwargs = {
        "params": params,
        "headers": headers,
    }
    proxy = _kaspi_proxy_url()
    if proxy:
        request_kwargs["proxy"] = proxy

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            async with semaphore:
                async with session.get(
                    KASPI_SEARCH_URL,
                    **request_kwargs,
                ) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            cards = data.get("cards", []) if isinstance(data, dict) else []
            return cards if isinstance(cards, list) else []
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                "Ошибка поиска Kaspi: query=%r page=%s attempt=%s/%s: %s",
                query,
                page,
                attempt,
                REQUEST_RETRIES,
                exc,
            )
            if attempt < REQUEST_RETRIES:
                await asyncio.sleep(attempt)

    return []


async def _fetch_kaspi_products(
    context: _SearchContext,
    query: str,
) -> list[dict[str, Any]]:
    pages = await asyncio.gather(
        *(
            _request_kaspi_page(
                context.session,
                context.semaphore,
                query,
                page,
            )
            for page in SEARCH_PAGES
        )
    )

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cards in pages:
        for card in cards:
            if not isinstance(card, dict):
                continue
            candidate = _normalize_kaspi_card(card)
            if not candidate:
                continue
            key = candidate["url"]
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if len(candidates) >= 24:
                return candidates
    return candidates


async def _search_with_context(
    context: _SearchContext,
    query: str,
) -> list[dict[str, Any]]:
    cache_key = _normalize_text(query)
    if not cache_key:
        return []

    async with context.lock:
        task = context.tasks.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                _fetch_kaspi_products(context, query.strip())
            )
            context.tasks[cache_key] = task
    return await task


async def search_kaspi_product(query: str) -> list[dict]:
    """Ищет до 24 кандидатов Kaspi по текстовому запросу."""
    context = _search_context.get()
    if context is not None:
        return await _search_with_context(context, query)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        temporary = _SearchContext(
            session=session,
            semaphore=asyncio.Semaphore(MAX_CONCURRENT_SEARCHES),
        )
        token = _search_context.set(temporary)
        try:
            return await _search_with_context(temporary, query)
        finally:
            _search_context.reset(token)


def _select_candidate(
    ozon_product: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], float] | None:
    ozon_price = _to_decimal(ozon_product.get("price"))
    if ozon_price is None:
        return None

    accepted: list[tuple[dict[str, Any], float, Decimal]] = []
    for candidate in candidates:
        kaspi_price = _to_decimal(candidate.get("price"))
        if kaspi_price is None or kaspi_price > ozon_price * Decimal("3"):
            continue

        score = _calculate_match_score(ozon_product, candidate)
        if score is None or score < MATCH_THRESHOLD:
            continue
        accepted.append((candidate, score, kaspi_price))

    if not accepted:
        return None

    best_score = max(item[1] for item in accepted)
    near_best = [
        item
        for item in accepted
        if item[1] >= best_score - NEAR_BEST_SCORE_DELTA
    ]
    candidate, score, _ = min(
        near_best,
        key=lambda item: (item[2], -item[1]),
    )
    return candidate, score


def _calculate_economics(
    ozon_product: dict[str, Any],
    kaspi_product: dict[str, Any],
    match_score: float,
    min_roi: Decimal | None = None,
    min_profit: Decimal | None = None,
) -> dict[str, Any] | None:
    ozon_price = _to_decimal(ozon_product.get("price"))
    kaspi_price = _to_decimal(kaspi_product.get("price"))
    if ozon_price is None or kaspi_price is None:
        return None

    delivery = (
        Decimal("950")
        if ozon_price <= Decimal("10000")
        else Decimal("2000")
    )
    fee_rate = Decimal("0.16")
    net_revenue = kaspi_price * (Decimal("1") - fee_rate) - delivery
    total_cost = ozon_price + delivery
    profit = net_revenue - ozon_price
    roi = profit / ozon_price * Decimal("100")

    if min_roi is not None and roi < min_roi:
        return None
    if min_profit is not None and profit <= min_profit:
        return None

    money = lambda value: float(
        value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    )
    return {
        "ozon_title": str(ozon_product.get("title") or ""),
        "ozon_price": money(ozon_price),
        "ozon_url": str(ozon_product.get("url") or ""),
        "brand": ozon_product.get("brand") or None,
        "ozon_article": ozon_product.get("article") or None,
        "ozon_category": ozon_product.get("category") or None,
        "kaspi_title": str(kaspi_product.get("title") or ""),
        "kaspi_price": money(kaspi_price),
        "kaspi_url": str(kaspi_product.get("url") or ""),
        "kaspi_brand": kaspi_product.get("brand") or None,
        "kaspi_article": kaspi_product.get("article") or None,
        "delivery": money(delivery),
        "total_cost": money(total_cost),
        "net_revenue": money(net_revenue),
        "profit": money(profit),
        "roi": money(roi),
        "match_score": round(match_score, 2),
    }


def _build_unmatched_result(product: dict[str, Any]) -> dict[str, Any]:
    ozon_price = _to_decimal(product.get("price"))

    def money(value: Decimal | None) -> float | None:
        if value is None:
            return None
        return float(
            value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        )

    return {
        "matched": False,
        "ozon_title": str(product.get("title") or ""),
        "ozon_price": money(ozon_price),
        "ozon_url": str(product.get("url") or ""),
        "brand": product.get("brand") or None,
        "ozon_article": product.get("article") or None,
        "ozon_category": product.get("category") or None,
        "kaspi_title": "Не найдено точное совпадение",
        "kaspi_price": None,
        "kaspi_url": "",
        "kaspi_brand": None,
        "kaspi_article": None,
        "delivery": None,
        "total_cost": None,
        "net_revenue": None,
        "profit": None,
        "roi": None,
        "match_score": None,
    }


async def _compare_one(
    product: dict[str, Any],
    min_roi: Decimal | None,
    min_profit: Decimal | None,
) -> tuple[bool, dict[str, Any] | None]:
    try:
        query = _build_search_query(product)
        if not query:
            return False, _build_unmatched_result(product)

        candidates = await search_kaspi_product(query)
        selected = _select_candidate(product, candidates)
        if selected is None:
            return False, _build_unmatched_result(product)

        kaspi_product, match_score = selected
        item = _calculate_economics(
            product,
            kaspi_product,
            match_score,
            min_roi=min_roi,
            min_profit=min_profit,
        )
        return True, item
    except Exception:
        logger.exception(
            "Неожиданная ошибка сравнения с Kaspi: product=%r",
            product.get("title") if isinstance(product, dict) else product,
        )
        return False, _build_unmatched_result(product)


async def compare_with_kaspi(
    ozon_products: list[dict],
    min_roi: Decimal | float | int | str | None = None,
    min_profit: Decimal | float | int | str | None = None,
) -> list[dict]:
    """Сравнивает товары Ozon с Kaspi и возвращает строки отчета."""
    min_roi_decimal = _to_optional_nonnegative_decimal(min_roi, "min_roi")
    min_profit_decimal = _to_optional_nonnegative_decimal(
        min_profit,
        "min_profit",
    )
    logger.info("Получено товаров Ozon для сравнения: %s", len(ozon_products))
    if min_roi_decimal is not None or min_profit_decimal is not None:
        logger.info(
            "Финансовые фильтры: ROI >= %s%%, прибыль > %s ₸",
            min_roi_decimal if min_roi_decimal is not None else "не задан",
            min_profit_decimal if min_profit_decimal is not None else "не задана",
        )
    else:
        logger.info("Финансовые фильтры отключены: в отчет попадут все товары")

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    context_token: Token[_SearchContext | None] | None = None
    matched_count = 0
    report_items: list[dict[str, Any]] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        context = _SearchContext(
            session=session,
            semaphore=asyncio.Semaphore(MAX_CONCURRENT_SEARCHES),
        )
        context_token = _search_context.set(context)
        try:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            _compare_one(
                                product,
                                min_roi_decimal,
                                min_profit_decimal,
                            )
                            for product in ozon_products
                        )
                    ),
                    timeout=COMPARE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Общий таймаут сравнения с Kaspi: %s секунд",
                    COMPARE_TIMEOUT_SECONDS,
                )
                results = []
        finally:
            if context_token is not None:
                _search_context.reset(context_token)

    for matched, item in results:
        if matched:
            matched_count += 1
        if item is not None:
            report_items.append(item)

    report_items.sort(
        key=lambda item: (
            item.get("roi") if item.get("roi") is not None else -10**9,
            item.get("profit") if item.get("profit") is not None else -10**9,
        ),
        reverse=True,
    )
    logger.info("Найдено допустимых совпадений Kaspi: %s", matched_count)
    logger.info(
        "Строк в Kaspi-отчете: %s",
        len(report_items),
    )
    return report_items
