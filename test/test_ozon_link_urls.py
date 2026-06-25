import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.parsers.link_parser import OzonLinkParser
from src.parsers.product_parser import (
    ProductInfo,
    ProductWorker,
    OzonProductParser,
    extract_product_page_fallback,
)
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

    def test_extracts_title_and_price_from_category_card_text(self):
        card_text = """
        Мультиварка REDMOND RMC-M52, черная
        34 990 ₸
        4.9 • 120 отзывов
        В корзину
        """

        self.assertEqual(
            self.parser._extract_title_from_card_text(card_text),
            "Мультиварка REDMOND RMC-M52, черная",
        )
        self.assertEqual(
            self.parser._extract_price_from_card_text(card_text),
            34990,
        )

    def test_recovers_current_product_url_when_category_wait_times_out(self):
        self.parser.driver = SimpleNamespace(
            current_url="https://ozon.kz/product/test-product-4103859568/",
            title="REDMOND RMC-M52",
            page_source="",
        )

        self.assertTrue(
            self.parser._recover_links_from_current_page("test")
        )
        self.assertIn(
            "https://ozon.kz/product/test-product-4103859568/",
            self.parser.collected_links,
        )
        self.assertEqual(
            self.parser.collected_links[
                "https://ozon.kz/product/test-product-4103859568/"
            ]["title"],
            "REDMOND RMC-M52",
        )

    def test_extracts_urlencoded_product_links_from_html(self):
        self.parser.driver = SimpleNamespace(
            page_source=(
                "https%3A%2F%2Fozon.kz%2Fproduct%2F"
                "redmond-rmc-m52-4103859568%2F"
            )
        )

        self.assertEqual(
            self.parser._extract_product_links_from_html(),
            ["https://ozon.kz/product/redmond-rmc-m52-4103859568/"],
        )


class OzonProductWorkerTests(unittest.TestCase):
    def test_extracts_title_price_and_image_from_json_ld(self):
        html = """
        <html><head>
          <meta property="og:title" content="REDMOND RMC-M52">
          <meta property="og:image" content="https://img.test/item.jpg">
          <script type="application/ld+json">
          {
            "@type": "Product",
            "name": "REDMOND RMC-M52",
            "offers": {
              "@type": "Offer",
              "price": "34 990",
              "priceCurrency": "KZT"
            }
          }
          </script>
        </head></html>
        """

        result = extract_product_page_fallback(html)

        self.assertEqual(result["title"], "REDMOND RMC-M52")
        self.assertEqual(result["prices"], [34990])
        self.assertEqual(
            result["image_url"],
            "https://img.test/item.jpg",
        )

    def test_extracts_title_and_price_from_ozon_widget_state(self):
        html = r'''
        <html><body>
          <script>
          window.__ozon = {
            "webProductHeading-123-default-1":"{\"title\":\"REDMOND RMC-M52 мультиварка\"}",
            "webPrice-123-default-1":"{\"cardPrice\":\"34 990 ₸\",\"originalPrice\":\"39 990 ₸\"}"
          };
          </script>
        </body></html>
        '''

        result = extract_product_page_fallback(html)

        self.assertEqual(result["title"], "REDMOND RMC-M52 мультиварка")
        self.assertEqual(result["prices"], [34990, 39990])

    def test_product_worker_uses_category_metadata_when_card_parse_fails(self):
        worker = ProductWorker(1)
        product = worker._build_from_link_metadata(
            "4103859568",
            {
                "title": "REDMOND RMC-M52 мультиварка",
                "price": 34990,
                "image_url": "https://img.test/item.jpg",
            },
            "Не удалось загрузить карточку товара",
        )

        self.assertTrue(product.success)
        self.assertEqual(product.name, "REDMOND RMC-M52 мультиварка")
        self.assertEqual(product.price, 34990)
        self.assertEqual(product.image_url, "https://img.test/item.jpg")

    def test_product_worker_replaces_generic_ozon_title_from_metadata(self):
        worker = ProductWorker(1)
        product = ProductInfo(
            article="4103859568",
            name="Ozon интернет-магазин",
            price=34990,
        )

        worker._apply_link_metadata(
            product,
            {
                "title": "REDMOND RMC-M52 мультиварка",
                "price": 34990,
            },
        )

        self.assertEqual(product.name, "REDMOND RMC-M52 мультиварка")
        self.assertTrue(product.success)

    def test_product_parser_skips_product_pages_when_listing_has_all_data(self):
        parser = OzonProductParser(max_workers=2)
        product_links = {
            "https://ozon.kz/product/test-10001/": {
                "title": "REDMOND RMC-M52",
                "price": 34990,
                "image_url": "https://img.test/1.jpg",
            },
            "https://ozon.kz/product/test-10002/": {
                "title": "REDMOND RK-G196",
                "price": 12990,
                "image_url": "https://img.test/2.jpg",
            },
        }

        with (
            patch.object(parser, "_parse_single_worker") as parse_single,
            patch.object(parser, "_parse_multiple_workers") as parse_multiple,
        ):
            results = parser.parse_products(product_links)

        parse_single.assert_not_called()
        parse_multiple.assert_not_called()
        self.assertEqual([product.article for product in results], ["10001", "10002"])
        self.assertTrue(all(product.success for product in results))
        self.assertEqual(results[0].name, "REDMOND RMC-M52")
        self.assertEqual(results[0].price, 34990)

    def test_product_parser_opens_only_items_missing_listing_data(self):
        parser = OzonProductParser(max_workers=2)
        product_links = {
            "https://ozon.kz/product/test-10001/": {
                "title": "REDMOND RMC-M52",
                "price": 34990,
            },
            "https://ozon.kz/product/test-10002/": {
                "title": "",
                "price": 0,
            },
        }
        parsed_missing = ProductInfo(
            article="10002",
            name="REDMOND RK-G196",
            price=12990,
            card_price=12990,
            success=True,
        )

        with patch.object(
            parser,
            "_parse_single_worker",
            return_value=[parsed_missing],
        ) as parse_single:
            results = parser.parse_products(product_links)

        parse_single.assert_called_once_with(["10002"])
        self.assertEqual([product.article for product in results], ["10001", "10002"])
        self.assertEqual(results[0].name, "REDMOND RMC-M52")
        self.assertEqual(results[1].name, "REDMOND RK-G196")

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
