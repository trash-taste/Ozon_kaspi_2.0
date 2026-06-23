import unittest
from types import SimpleNamespace

from src.parsers.link_parser import OzonLinkParser


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


if __name__ == "__main__":
    unittest.main()
