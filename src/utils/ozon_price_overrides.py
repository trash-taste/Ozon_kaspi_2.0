import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..parsers.ozon_listing_data import extract_product_id, normalize_product_url

logger = logging.getLogger(__name__)

DEFAULT_OVERRIDES_FILENAME = "ozon_price_overrides.json"


def load_ozon_price_overrides(base_dir: Path) -> dict[str, Any]:
    override_path = Path(
        os.getenv("OZON_PRICE_OVERRIDES_FILE", "")
        or base_dir / DEFAULT_OVERRIDES_FILENAME
    )
    if not override_path.exists():
        return {}

    try:
        payload = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось прочитать ручные цены Ozon %s: %s", override_path, exc)
        return {}

    if not isinstance(payload, dict):
        logger.warning("Ручные цены Ozon должны быть JSON-объектом: %s", override_path)
        return {}
    return payload


def find_ozon_price_override(
    overrides: dict[str, Any],
    article: str = "",
    url: str = "",
    title: str = "",
) -> tuple[int, str]:
    if not overrides:
        return 0, ""

    normalized_url = normalize_product_url(url or "")
    product_id = article or extract_product_id(normalized_url)
    lookup_keys = [
        key
        for key in (normalized_url, url, product_id)
        if key
    ]

    prices = overrides.get("prices", overrides)
    if isinstance(prices, dict):
        for key in lookup_keys:
            price = _extract_override_price(prices.get(key))
            if price:
                return price, key

    title_prices = overrides.get("title_contains", {})
    if isinstance(title_prices, dict) and title:
        lowered_title = title.casefold()
        for marker, value in title_prices.items():
            if str(marker).casefold() in lowered_title:
                price = _extract_override_price(value)
                if price:
                    return price, f"title_contains:{marker}"

    return 0, ""


def _extract_override_price(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("price")
    if isinstance(value, (int, float)):
        price = int(value)
    else:
        cleaned = re.sub(r"[^\d]", "", str(value or ""))
        price = int(cleaned) if cleaned else 0
    return price if 100 <= price <= 10_000_000 else 0
