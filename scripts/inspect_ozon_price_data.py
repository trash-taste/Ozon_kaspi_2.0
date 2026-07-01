from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.parsers.ozon_listing_data import (  # noqa: E402
    PRICE_WITH_CURRENCY_PATTERN,
    decode_ozon_source,
    extract_price_from_card_text,
)
from src.parsers.product_parser import (  # noqa: E402
    _extract_prices_for_keys,
    _extract_product_page_prices,
    _extract_visible_product_prices,
    extract_product_page_fallback,
)
from src.utils.selenium_manager import SeleniumManager  # noqa: E402


SALE_KEYS = (
    "cardPrice",
    "finalPrice",
    "currentPrice",
    "salePrice",
    "discountPrice",
    "discountedPrice",
    "priceWithCard",
    "minPrice",
    "price",
)

ORIGINAL_KEYS = (
    "originalPrice",
    "oldPrice",
    "crossedPrice",
    "basePrice",
    "priceWithoutDiscount",
)


def _short_text(value: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _ordered_unique(values: list[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def collect_price_debug(page_source: str) -> dict[str, Any]:
    soup = BeautifulSoup(page_source or "", "html.parser")
    decoded_source = decode_ozon_source(page_source or "")
    page_data = extract_product_page_fallback(page_source)
    title = str(page_data.get("title") or "")

    widgets = []
    for widget in soup.select('[data-widget*="webPrice"], [data-widget*="price"]'):
        text = widget.get_text("\n", strip=True)
        selected = extract_price_from_card_text(text)
        if selected:
            widgets.append(
                {
                    "selected": selected,
                    "data_widget": widget.get("data-widget"),
                    "text": _short_text(text),
                }
            )

    sale_by_key = {
        key: _extract_prices_for_keys(decoded_source, (key,))
        for key in SALE_KEYS
    }
    original_by_key = {
        key: _extract_prices_for_keys(decoded_source, (key,))
        for key in ORIGINAL_KEYS
    }

    all_currency_prices: list[int] = []
    visible_text = soup.get_text("\n")
    for match in PRICE_WITH_CURRENCY_PATTERN.finditer(visible_text):
        digits = re.sub(r"[^\d]", "", match.group(1) or match.group(2) or "")
        if not digits:
            continue
        price = int(digits)
        if 100 <= price <= 10_000_000:
            all_currency_prices.append(price)

    return {
        "title": title,
        "fallback_prices": page_data.get("prices", []),
        "product_page_prices": _extract_product_page_prices(soup, decoded_source),
        "visible_product_prices": _extract_visible_product_prices(soup, title),
        "widget_prices": widgets,
        "sale_prices_by_key": {
            key: values for key, values in sale_by_key.items() if values
        },
        "original_prices_by_key": {
            key: values for key, values in original_by_key.items() if values
        },
        "all_visible_currency_prices_first_80": _ordered_unique(
            all_currency_prices
        )[:80],
    }


def load_page_source_from_url(url: str, headless: bool) -> str:
    manager = SeleniumManager(headless=headless)
    driver = manager.create_driver()
    try:
        if not manager.navigate_to_url(url):
            raise RuntimeError(f"Failed to open {url}")
        return driver.page_source
    finally:
        manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect raw Ozon price candidates for a product page."
    )
    parser.add_argument("target", help="Ozon product URL or local HTML file")
    parser.add_argument("--html", action="store_true", help="Read target as HTML file")
    parser.add_argument("--headful", action="store_true", help="Show browser window")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    if args.html:
        page_source = Path(args.target).read_text(encoding="utf-8")
    else:
        page_source = load_page_source_from_url(
            args.target,
            headless=not args.headful,
        )

    debug_data = collect_price_debug(page_source)
    output = json.dumps(debug_data, ensure_ascii=False, indent=2)
    print(output)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
