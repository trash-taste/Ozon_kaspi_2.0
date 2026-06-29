import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.parsers.link_parser import OzonLinkParser
from src.parsers.ozon_listing_data import (
    build_listing_page_url,
    extract_listing_items_from_html,
    extract_price_from_card_text,
    extract_title_from_card_text,
)
from src.parsers.ozon_playwright_parser import OzonPlaywrightParser
from src.parsers.product_parser import (
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

    def test_extracts_single_line_card_title_and_lowest_sale_price(self):
        card_text = (
            "Мультиварка REDMOND RMC-M52, черная "
            "39 990 ₸ 34 990 ₸ 4.9 120 отзывов В корзину"
        )

        self.assertEqual(
            extract_title_from_card_text(card_text),
            "Мультиварка REDMOND RMC-M52, черная",
        )
        self.assertEqual(extract_price_from_card_text(card_text), 34990)

    def test_ignores_tiny_installment_like_price(self):
        card_text = (
            "REDMOND Мультипекарь RMB-M614/1 700 Вт, черный "
            "17 890 ₸ 1 450 ₸"
        )

        self.assertEqual(extract_price_from_card_text(card_text), 17890)

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

    def test_extracts_listing_metadata_from_embedded_json(self):
        self.parser.driver = SimpleNamespace(
            current_url="https://ozon.kz/category/test-123/",
            page_source=r'''
            <script>
            window.__data = {
              "items": [{
                "link": "https:\/\/ozon.kz\/product\/redmond-rmc-m52-4103859568\/",
                "title": "Мультиварка REDMOND RMC-M52, черная",
                "cardPrice": "34 990 ₸"
              }]
            };
            </script>
            ''',
        )

        self.assertEqual(
            self.parser._extract_product_items_from_html(),
            {
                "https://ozon.kz/product/redmond-rmc-m52-4103859568/": {
                    "title": "Мультиварка REDMOND RMC-M52, черная",
                    "price": 34990,
                }
            },
        )

    def test_builds_search_fallback_from_short_link_meta_title(self):
        self.parser.driver = SimpleNamespace(
            current_url="https://ozon.kz/t/7WjRGFO?__rr=1",
            page_source="""
            <html><head>
            <meta property="og:title"
                  content="tp link - купить на OZON в Казахстане">
            </head><body></body></html>
            """,
        )

        self.assertEqual(
            self.parser._build_search_url_from_og_title(),
            "https://www.ozon.kz/search/?text=tp%20link",
        )

    def test_listing_metadata_prefers_card_price_over_regular_price(self):
        html = r'''
        <script>
        window.__data = {
          "items": [{
            "link": "https:\/\/ozon.kz\/product\/redmond-rmc-m52-4103859568\/",
            "title": "Мультиварка REDMOND RMC-M52, черная",
            "price": "39 990 ₸",
            "cardPrice": "34 990 ₸"
          }]
        };
        </script>
        '''

        self.assertEqual(
            extract_listing_items_from_html(
                html,
                "https://ozon.kz/category/test-123/",
            ),
            {
                "https://ozon.kz/product/redmond-rmc-m52-4103859568/": {
                    "title": "Мультиварка REDMOND RMC-M52, черная",
                    "price": 34990,
                }
            },
        )

    def test_playwright_parser_normalizes_relative_product_url(self):
        parser = OzonPlaywrightParser(
            "https://ozon.kz/category/test-123/",
            max_products=1,
        )

        self.assertEqual(
            parser._normalize_product_url(
                "/product/redmond-rmc-m52-4103859568/",
                "https://ozon.kz/category/test-123/",
            ),
            "https://ozon.kz/product/redmond-rmc-m52-4103859568/",
        )

    def test_playwright_parser_extracts_urlencoded_product_links(self):
        parser = OzonPlaywrightParser(
            "https://ozon.kz/category/test-123/",
            max_products=1,
        )

        self.assertEqual(
            parser._extract_product_links_from_html(
                "https%3A%2F%2Fozon.kz%2Fproduct%2F"
                "redmond-rmc-m52-4103859568%2F"
            ),
            ["https://ozon.kz/product/redmond-rmc-m52-4103859568/"],
        )

    def test_builds_next_listing_page_url_without_losing_filters(self):
        self.assertEqual(
            build_listing_page_url(
                "https://ozon.kz/category/test-123/?seller=0&currency_price=1%3B2",
                2,
            ),
            "https://ozon.kz/category/test-123/?seller=0&currency_price=1%3B2&page=2",
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

    def test_extracts_price_from_product_page_text(self):
        html = """
        <html><body>
          <h1>REDMOND RMC-M52 мультиварка</h1>
          <div>34 990 ₸</div>
        </body></html>
        """

        result = extract_product_page_fallback(html)

        self.assertEqual(result["title"], "REDMOND RMC-M52 мультиварка")
        self.assertEqual(result["prices"], [34990])

    def test_visible_product_price_wins_over_hidden_source_price(self):
        html = """
        <html><body>
          <h1>REDMOND Мультипекарь RMB-M614/1 700 Вт, черный</h1>
          <div data-widget="webPrice">
            17 392 ₸
            30 702 ₸
            Стало дешевле
            1 450 ₸
            × 12 месяцев
          </div>
          <script>
            window.__data = {"price": "15 654 ₸", "originalPrice": "30 702 ₸"};
          </script>
        </body></html>
        """

        result = extract_product_page_fallback(html)

        self.assertEqual(result["prices"], [17392, 30702])

    def test_visible_product_price_ignores_recommendation_prices(self):
        html = """
        <html><body>
          <h1>REDMOND RV-UR370 Вертикальный беспроводной пылесос</h1>
          <div>
            REDMOND RV-UR370 Вертикальный беспроводной пылесос
            30 608 ₸
            45 000 ₸
            В корзину
          </div>
          <section>
            Рекомендуем также
            148 460 ₸
            203 321 ₸
          </section>
        </body></html>
        """

        result = extract_product_page_fallback(html)

        self.assertEqual(result["prices"], [30608, 45000])

    def test_product_worker_uses_listing_metadata(self):
        worker = ProductWorker(1)
        product = worker._build_from_listing(
            "4103859568",
            {
                "title": "REDMOND RMC-M52 мультиварка",
                "price": 34990,
                "image_url": "https://img.test/item.jpg",
            },
        )

        self.assertTrue(product.success)
        self.assertEqual(product.name, "REDMOND RMC-M52 мультиварка")
        self.assertEqual(product.price, 34990)
        self.assertEqual(product.image_url, "https://img.test/item.jpg")

    def test_product_worker_marks_incomplete_listing_data(self):
        worker = ProductWorker(1)
        product = worker._build_from_listing(
            "4103859568",
            {"title": "REDMOND RMC-M52 мультиварка", "price": 0},
        )

        self.assertFalse(product.success)
        self.assertIn("цена", product.error)

    def test_product_worker_page_price_overrides_listing_price(self):
        worker = ProductWorker(1, page_mode="always")
        worker.driver = SimpleNamespace(
            page_source="""
            <html><body>
              <h1>REDMOND RMC-M52 карточка</h1>
              <div data-widget="webPrice">22 222 ₸</div>
            </body></html>
            """
        )

        with patch.object(
            worker.selenium_manager,
            "navigate_to_url",
            return_value=True,
        ):
            product = worker._parse_single_product(
                "4103859568",
                "https://ozon.kz/product/test-4103859568/",
                {
                    "title": "REDMOND RMC-M52 листинг",
                    "price": 11111,
                    "image_url": "https://img.test/item.jpg",
                },
            )

        self.assertTrue(product.success)
        self.assertEqual(product.name, "REDMOND RMC-M52 карточка")
        self.assertEqual(product.price, 22222)

    def test_product_worker_keeps_listing_price_when_page_price_is_suspicious(self):
        worker = ProductWorker(1, page_mode="always")
        worker.driver = SimpleNamespace(
            page_source="""
            <html><body>
              <h1>REDMOND RV-UR370 карточка</h1>
              <div data-widget="webPrice">148 460 ₸</div>
            </body></html>
            """
        )

        with patch.object(
            worker.selenium_manager,
            "navigate_to_url",
            return_value=True,
        ):
            product = worker._parse_single_product(
                "1948518910",
                "https://ozon.kz/product/test-1948518910/",
                {
                    "title": "REDMOND RV-UR370 листинг",
                    "price": 30608,
                    "image_url": "https://img.test/item.jpg",
                },
            )

        self.assertTrue(product.success)
        self.assertEqual(product.price, 30608)

    def test_product_parser_opens_product_pages_by_default(self):
        parser = OzonProductParser(max_workers=2)
        product_links = {
            "https://ozon.kz/product/test-10001/": {
                "title": "REDMOND RMC-M52",
                "price": 34990,
            },
        }

        with patch.object(
            parser,
            "_parse_products_from_pages",
            return_value=[],
        ) as parse_pages:
            parser.parse_products(product_links)

        parse_pages.assert_called_once()

    def test_product_parser_uses_listing_data_when_page_mode_is_off(self):
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

        with patch.dict("os.environ", {"OZON_PRODUCT_PAGE_MODE": "off"}):
            parser = OzonProductParser(max_workers=2)
        with patch.object(parser, "_parse_incomplete_products") as fallback:
            results = parser.parse_products(product_links)

        fallback.assert_not_called()
        self.assertEqual([product.article for product in results], ["10001", "10002"])
        self.assertTrue(all(product.success for product in results))
        self.assertEqual(results[0].name, "REDMOND RMC-M52")
        self.assertEqual(results[0].price, 34990)

    def test_product_parser_does_not_open_missing_items_when_page_mode_is_off(self):
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

        with patch.dict("os.environ", {"OZON_PRODUCT_PAGE_MODE": "off"}):
            parser = OzonProductParser(max_workers=2)
        with patch.object(parser, "_parse_incomplete_products") as fallback:
            results = parser.parse_products(product_links)

        fallback.assert_not_called()
        self.assertEqual([product.article for product in results], ["10001", "10002"])
        self.assertEqual(results[0].name, "REDMOND RMC-M52")
        self.assertFalse(results[1].success)
        self.assertIn("название", results[1].error)

    def test_product_parser_can_use_missing_page_mode(self):
        product_links = {
            "https://ozon.kz/product/test-10002/": {
                "title": "",
                "price": 0,
            },
        }

        with (
            patch.dict("os.environ", {"OZON_PRODUCT_PAGE_MODE": "missing"}),
            patch.object(
                OzonProductParser,
                "_parse_products_from_pages",
            ) as parse_pages,
        ):
            parser = OzonProductParser(max_workers=2)
        with (
            patch.object(parser, "_parse_incomplete_products", return_value=[]) as fallback,
        ):
            parser.parse_products(product_links)

        parse_pages.assert_not_called()
        fallback.assert_called_once()


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
