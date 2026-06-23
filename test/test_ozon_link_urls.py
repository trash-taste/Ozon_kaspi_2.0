import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.parsers.link_parser import OzonLinkParser
from src.parsers.product_parser import OzonProductParser
from src.parsers.seller_parser import OzonSellerParser


class OzonProductURLTests(unittest.TestCase):
    def setUp(self):
        self.parser = OzonLinkParser(
            "https://ozon.kz/category/test-123/",
            max_products=1,
        )
        self.parser.driver = SimpleNamespace(
            current_url="https://ozon.kz/category/test-123/"
        )

    def test_relative_product_url_keeps_kazakhstan_domain(self):
        result = self.parser._normalize_product_url(
            "/product/test-product-4103859568/?from=share"
        )

        self.assertEqual(
            result,
            "https://ozon.kz/product/test-product-4103859568/",
        )

    def test_absolute_kazakhstan_product_url_is_accepted(self):
        result = self.parser._normalize_product_url(
            "https://www.ozon.kz/product/test-product-4103859568/"
        )

        self.assertEqual(
            result,
            "https://www.ozon.kz/product/test-product-4103859568/",
        )

    def test_non_ozon_product_url_is_rejected(self):
        self.assertEqual(
            self.parser._normalize_product_url(
                "https://example.com/product/test-4103859568/"
            ),
            "",
        )


class OzonProductWorkerTests(unittest.TestCase):
    def test_uses_at_most_two_product_workers_by_default(self):
        parser = OzonProductParser(max_workers=10, user_id="123")
        product_links = {
            f"https://ozon.kz/product/test-{article}/": ""
            for article in range(10000, 10008)
        }

        with (
            patch(
                "src.parsers.product_parser.resource_manager."
                "start_parsing_session",
                return_value=5,
            ),
            patch.object(
                parser,
                "_parse_multiple_workers",
                return_value=[],
            ) as parse_multiple,
        ):
            parser.parse_products(product_links)

        parse_multiple.assert_called_once()
        self.assertEqual(parse_multiple.call_args.args[1], 2)


class OzonSellerWorkerTests(unittest.TestCase):
    def test_uses_at_most_two_seller_workers_by_default(self):
        parser = OzonSellerParser(max_workers=10, user_id="123")

        with (
            patch(
                "src.parsers.seller_parser.resource_manager."
                "start_parsing_session",
                return_value=5,
            ),
            patch.object(
                parser,
                "_parse_multiple_workers",
                return_value=[],
            ) as parse_multiple,
        ):
            parser.parse_sellers([str(value) for value in range(8)])

        parse_multiple.assert_called_once()
        self.assertEqual(parse_multiple.call_args.args[1], 2)


if __name__ == "__main__":
    unittest.main()
