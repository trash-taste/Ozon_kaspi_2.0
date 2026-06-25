import html
import json
import re
from typing import Any, Dict
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


PRODUCT_LINK_PATTERN = re.compile(
    r'(?:https?:)?//(?:www\.)?ozon\.(?:ru|kz)/product/'
    r'[^"\'<>\s\\]+'
    r'|/product/[^"\'<>\s\\]+'
)


def decode_ozon_source(page_source: str) -> str:
    source = html.unescape(page_source or "")
    source = unquote(source)
    return (
        source
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003D", "=")
    )


def normalize_product_url(href: str, base_url: str = "") -> str:
    if not href:
        return ""

    href = decode_ozon_source(href).strip()
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = urljoin(base_url or "https://ozon.kz", href)

    try:
        parsed = urlsplit(href)
    except ValueError:
        return ""

    host = (parsed.hostname or "").casefold()
    if host not in {"ozon.ru", "www.ozon.ru", "ozon.kz", "www.ozon.kz"}:
        return ""
    if not re.fullmatch(r"/product/(?:[^/]+-)?\d+/?", parsed.path):
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


def extract_product_id(url: str) -> str:
    match = re.search(r"/product/(?:[^/]+-)?(\d+)/?", url or "")
    return match.group(1) if match else ""


def extract_product_links_from_html(page_source: str) -> list[str]:
    source = decode_ozon_source(page_source)
    links = []
    for href in PRODUCT_LINK_PATTERN.findall(source):
        if href not in links:
            links.append(href)
    return links


def extract_listing_items_from_html(
    page_source: str,
    base_url: str = "",
) -> Dict[str, dict[str, Any]]:
    source = decode_ozon_source(page_source)
    items: Dict[str, dict[str, Any]] = {}

    for href in PRODUCT_LINK_PATTERN.findall(source):
        url = normalize_product_url(href, base_url)
        if not url or url in items:
            continue

        context = _context_for_product(source, url)
        items[url] = {
            "title": _extract_title_from_context(context),
            "price": _extract_price_from_context(context),
        }

    return items


def _context_for_product(source: str, url: str) -> str:
    tokens = [url]
    parsed = urlsplit(url)
    if parsed.path:
        tokens.append(parsed.path)
    article = extract_product_id(url)
    if article:
        tokens.append(article)

    windows = []
    for token in dict.fromkeys(tokens):
        for match in re.finditer(re.escape(token), source):
            left = max(0, match.start() - 5000)
            right = min(len(source), match.end() + 5000)
            windows.append(source[left:right])
            if len(windows) >= 8:
                break
        if windows:
            break

    return "\n".join(windows) if windows else source[:10000]


def _extract_title_from_context(context: str) -> str:
    candidates = []
    patterns = (
        r'"(?:title|name|productName|heading|alt|text)"\s*:\s*"'
        r'((?:\\.|[^"\\]){5,320})"',
        r"'(?:title|name|productName|heading|alt|text)'\s*:\s*'"
        r"((?:\\.|[^'\\]){5,320})'",
    )

    for pattern in patterns:
        for match in re.finditer(pattern, context, re.IGNORECASE | re.DOTALL):
            title = _decode_jsonish_string(match.group(1))
            if _looks_like_product_title(title):
                candidates.append(title)

    if not candidates:
        return ""

    return max(dict.fromkeys(candidates), key=_title_score)


def _extract_price_from_context(context: str) -> int:
    preferred_keys = (
        "cardPrice",
        "finalPrice",
        "currentPrice",
        "salePrice",
        "discountPrice",
        "price",
        "lowPrice",
    )
    for key in preferred_keys:
        pattern = (
            rf'"{key}"\s*:\s*'
            r'(?:"((?:\\.|[^"\\]){1,80})"|(\d{3,10}))'
        )
        for match in re.finditer(pattern, context, re.IGNORECASE):
            price = _extract_price_number(match.group(1) or match.group(2))
            if price:
                return price

    for match in re.finditer(
        r"(\d[\d\s\u00a0\u202f.,]{1,})\s*(?:₸|тг|тенге)",
        context,
        re.IGNORECASE,
    ):
        price = _extract_price_number(match.group(1))
        if price:
            return price

    for match in re.finditer(
        r"(?:₸|тг|тенге)\s*(\d[\d\s\u00a0\u202f.,]{1,})",
        context,
        re.IGNORECASE,
    ):
        price = _extract_price_number(match.group(1))
        if price:
            return price

    return 0


def _decode_jsonish_string(value: str) -> str:
    text = value or ""
    try:
        text = json.loads(f'"{text}"')
    except (json.JSONDecodeError, TypeError):
        pass
    text = decode_ozon_source(str(text))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_product_title(value: str) -> bool:
    text = re.sub(r"\s+", " ", value or "").strip()
    lowered = text.casefold()
    if len(text) < 5 or len(text) > 260:
        return False
    if not re.search(r"[A-Za-zА-Яа-я]", text):
        return False
    blocked = (
        "http://",
        "https://",
        "/product/",
        "ozon marketplace",
        "ozon интернет-магазин",
        "в корзин",
        "купить",
        "доставка",
        "отзывы",
        "рейтинг",
        "характеристики",
        "описание",
        "seller",
        "₸",
        "тг",
        "тенге",
    )
    return not any(marker in lowered for marker in blocked)


def _title_score(value: str) -> int:
    score = min(len(value), 180)
    if re.search(r"\d", value):
        score += 15
    if len(value.split()) >= 3:
        score += 20
    if "," in value:
        score += 5
    return score


def _extract_price_number(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        price = int(value)
        return price if 100 <= price <= 10_000_000 else 0

    decoded = _decode_jsonish_string(str(value))
    cleaned = re.sub(r"[^\d]", "", decoded)
    if not cleaned:
        return 0
    price = int(cleaned)
    return price if 100 <= price <= 10_000_000 else 0
