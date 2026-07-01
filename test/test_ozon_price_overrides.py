import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.app_manager import AppManager
from src.utils.ozon_price_overrides import (
    find_ozon_price_override,
    load_ozon_price_overrides,
)


class OzonPriceOverrideTests(unittest.TestCase):
    def test_finds_manual_price_by_article_url_and_title(self):
        overrides = {
            "prices": {
                "479441444": 21000,
                "https://ozon.kz/product/test-123/": {"price": "19 500 ₸"},
            },
            "title_contains": {
                "Archer AX53": 21000,
            },
        }

        self.assertEqual(
            find_ozon_price_override(overrides, article="479441444"),
            (21000, "479441444"),
        )
        self.assertEqual(
            find_ozon_price_override(
                overrides,
                url="https://ozon.kz/product/test-123/",
            ),
            (19500, "https://ozon.kz/product/test-123/"),
        )
        self.assertEqual(
            find_ozon_price_override(
                overrides,
                title="Wi-Fi Роутер TP-Link Archer AX53",
            ),
            (21000, "title_contains:Archer AX53"),
        )

    def test_loads_manual_prices_from_default_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "ozon_price_overrides.json").write_text(
                json.dumps({"prices": {"479441444": 21000}}),
                encoding="utf-8",
            )

            overrides = load_ozon_price_overrides(base_dir)

        self.assertEqual(overrides["prices"]["479441444"], 21000)

    def test_app_manager_applies_manual_price_before_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "ozon_price_overrides.json").write_text(
                json.dumps({"prices": {"479441444": 21000}}),
                encoding="utf-8",
            )
            manager = AppManager(SimpleNamespace(BASE_DIR=base_dir))
            product = SimpleNamespace(
                success=True,
                article="479441444",
                name="Wi-Fi Роутер TP-Link Archer AX53",
                price=32460,
                card_price=32460,
                original_price=52927,
            )

            applied = manager._apply_ozon_price_overrides(
                [product],
                {
                    "https://ozon.kz/product/wi-fi-router-tp-link-archer-ax53-479441444/": {
                        "price": 32460,
                    }
                },
            )

        self.assertEqual(applied, 1)
        self.assertEqual(product.price, 21000)
        self.assertEqual(product.card_price, 21000)
        self.assertEqual(product.ozon_parser_price, 32460)
        self.assertEqual(product.ozon_price_source, "manual_override")
        self.assertEqual(product.ozon_price_override_key, "479441444")


if __name__ == "__main__":
    unittest.main()
