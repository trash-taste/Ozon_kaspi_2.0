import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook

from app import run_internet_comparison
from services import internet_compare
from services.report import (
    INTERNET_REPORT_COLUMNS,
    save_internet_comparison_report,
)


class InternetSearchTests(unittest.TestCase):
    def test_builds_exact_model_query(self):
        product = {
            "title": "REDMOND Мультиварка RMC-M52",
            "brand": None,
        }
        self.assertEqual(
            internet_compare._build_search_query(product),
            '"REDMOND RMC-M52" цена Казахстан купить',
        )

    def test_parses_kazakhstan_results_and_excludes_ozon(self):
        html = """
        <ul>
          <li class="serp-item">
            <a class="OrganicHost-Link"
               href="https://ozon.kz/product/test">Ozon</a>
          </li>
          <li class="serp-item">
            <a class="OrganicHost-Link"
               href="https://shop.example.kz/product/1">Shop</a>
            REDMOND RMC-M52 29 990 тг
          </li>
          <li class="serp-item">
            <a class="OrganicHost-Link"
               href="https://example.com/product/1">Foreign</a>
          </li>
        </ul>
        """
        results = internet_compare._parse_search_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "shop.example.kz")

    def test_parses_google_result_links(self):
        html = """
        <html><body>
          <a href="/url?q=https://kz.multivarka.pro/catalog/redmond-rka-pm7">
            REDMOND RKA-PM7
          </a>
          <a href="/url?q=https://ozon.kz/product/test">Ozon</a>
          <a href="/url?q=https://example.com/product/test">Foreign</a>
        </body></html>
        """
        results = internet_compare._parse_google_search_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "kz.multivarka.pro")
        self.assertEqual(
            results[0]["url"],
            "https://kz.multivarka.pro/catalog/redmond-rka-pm7",
        )

    def test_parses_google_ad_links(self):
        html = """
        <html><body>
          <a href="/aclk?sa=l&adurl=https://shop.example.kz/p/redmond">
            Реклама REDMOND RKA-PM7
          </a>
        </body></html>
        """
        results = internet_compare._parse_google_search_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "shop.example.kz")

    def test_adds_multivarka_direct_source_for_spice_grinder(self):
        results = internet_compare._direct_source_results(
            '"REDMOND RKA-PM7" цена Казахстан купить'
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "kz.multivarka.pro")

    def test_parses_multivarka_catalog_cards(self):
        html = """
        <div class="product-item">
          <a itemprop="name"
             href="/catalog/drugaya_kukhonnaya_tekhnika/melnitsa-dlya-spetsiy-redmond-rka-pm7/">
            Мельница для специй REDMOND <span>RKA-PM7</span>
          </a>
          <div itemprop="price" class="product-item-price-new">
            <span>4 990</span><span> т</span>
            <div>-84%</div>
          </div>
          <a class="btn-report-stock">Сообщить о поступлении</a>
        </div>
        """
        products = internet_compare._parse_store_page(
            html,
            "https://kz.multivarka.pro/catalog/melnitsy-dlya-spetsiy/",
            "kz.multivarka.pro",
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(
            products[0]["title"],
            "Мельница для специй REDMOND RKA-PM7",
        )
        self.assertEqual(products[0]["price"], 4990)
        self.assertEqual(products[0]["availability"], "Нет в наличии")
        self.assertEqual(
            products[0]["url"],
            "https://kz.multivarka.pro/catalog/drugaya_kukhonnaya_tekhnika/melnitsa-dlya-spetsiy-redmond-rka-pm7/",
        )

    def test_parses_multiple_json_ld_offers(self):
        html = """
        <html><head>
          <script type="application/ld+json">
          [
            {
              "@context": "https://schema.org",
              "@type": "Product",
              "name": "Мультиварка Redmond RMC-M52",
              "brand": {"@type": "Brand", "name": "Redmond"},
              "offers": {
                "@type": "Offer",
                "price": "29990",
                "priceCurrency": "KZT",
                "availability": "https://schema.org/InStock",
                "url": "/product/1"
              }
            },
            {
              "@type": "Product",
              "name": "Мультиварка Redmond RMC-M52",
              "offers": {
                "@type": "Offer",
                "price": "25000",
                "priceCurrency": "KZT",
                "availability": "https://schema.org/OutOfStock"
              }
            }
          ]
          </script>
        </head></html>
        """
        products = internet_compare._parse_store_page(
            html,
            "https://shop.kz/list",
            "shop.kz",
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["price"], 29990)
        self.assertEqual(
            products[0]["url"],
            "https://shop.kz/product/1",
        )
        self.assertEqual(products[0]["availability"], "В наличии")

    def test_uses_product_price_meta_as_fallback(self):
        html = """
        <html><head>
          <title>REDMOND RMC-M52 купить</title>
          <meta property="product:price:amount" content="42 990">
        </head></html>
        """
        products = internet_compare._parse_store_page(
            html,
            "https://shop.kz/product/1",
            "shop.kz",
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["price"], 42990)


class InternetMatchingTests(unittest.TestCase):
    def test_accepts_exact_model_and_rejects_different_model(self):
        ozon = {
            "title": "REDMOND Мультиварка RMC-M52",
            "price": 29715,
        }
        exact = {
            "title": "Мультиварка Redmond RMC-M52 черная",
            "price": 33290,
        }
        wrong = {
            "title": "Мультиварка Redmond MC108",
            "price": 25000,
        }
        self.assertGreaterEqual(
            internet_compare._calculate_match_score(ozon, exact),
            internet_compare.MATCH_THRESHOLD,
        )
        self.assertIsNone(
            internet_compare._calculate_match_score(ozon, wrong)
        )

    def test_rejects_unrelated_product_without_model(self):
        ozon = {
            "title": "REDMOND Fast Chef MP114 Мультиварка",
            "price": 57598,
        }
        unrelated = {
            "title": "DADU 20W FAST Charge USB",
            "price": 4990,
        }
        self.assertIsNone(
            internet_compare._calculate_match_score(ozon, unrelated)
        )

    def test_selects_lowest_exact_price(self):
        ozon = {"title": "REDMOND RMC-M52", "price": 29715}
        candidates = [
            {
                "title": "REDMOND RMC-M52",
                "price": 42990,
                "source": "one.kz",
            },
            {
                "title": "Мультиварка REDMOND RMC-M52",
                "price": 27990,
                "source": "two.kz",
            },
        ]
        selected = internet_compare._select_candidate(ozon, candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0]["price"], 27990)
        self.assertEqual(selected[2], 2)


class InternetEconomicsTests(unittest.TestCase):
    def test_calculates_with_sixteen_percent_commission(self):
        result = internet_compare._calculate_economics(
            {
                "title": "REDMOND RMC-M52",
                "price": 10000,
                "url": "ozon",
            },
            {
                "title": "REDMOND RMC-M52",
                "price": 15000,
                "source": "shop.kz",
                "url": "internet",
                "availability": "В наличии",
            },
            95,
            3,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["delivery"], 950.0)
        self.assertEqual(result["total_cost"], 10950.0)
        self.assertEqual(result["commission_rate"], 16.0)
        self.assertEqual(result["commission"], 2400.0)
        self.assertEqual(result["net_revenue"], 11650.0)
        self.assertEqual(result["profit"], 1650.0)
        self.assertEqual(result["roi"], 16.5)
        self.assertEqual(result["sources_count"], 3)

    def test_keeps_negative_profit(self):
        result = internet_compare._calculate_economics(
            {"title": "REDMOND RMC-M52", "price": 20000},
            {
                "title": "REDMOND RMC-M52",
                "price": 15000,
                "source": "shop.kz",
            },
            90,
            1,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["profit"], -9400.0)

    def test_includes_roi_exactly_twenty_five(self):
        internet_price = (
            (Decimal("10000") * Decimal("1.25") + Decimal("950"))
            / Decimal("0.84")
        )
        result = internet_compare._calculate_economics(
            {"title": "REDMOND RMC-M52", "price": 10000},
            {
                "title": "REDMOND RMC-M52",
                "price": internet_price,
                "source": "shop.kz",
            },
            90,
            1,
            min_roi=Decimal("25"),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["roi"], 25.0)

    def test_validates_commission_range(self):
        with self.assertRaises(ValueError):
            internet_compare._to_commission_decimal(-1)
        with self.assertRaises(ValueError):
            internet_compare._to_commission_decimal(100)
        self.assertEqual(
            internet_compare._to_commission_decimal(0),
            Decimal("0"),
        )


class InternetAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_kaspi_as_fallback_internet_source(self):
        product = {
            "title": "REDMOND RMC-M52",
            "price": 10000,
            "url": "ozon",
        }
        with patch(
            "services.internet_compare.search_internet_sources",
            new=AsyncMock(return_value=[]),
        ), patch(
            "services.kaspi_compare.search_kaspi_product",
            new=AsyncMock(
                return_value=[
                    {
                        "title": "Мультиварка REDMOND RMC-M52",
                        "price": 20000,
                        "url": "https://kaspi.kz/shop/p/1",
                        "brand": "REDMOND",
                        "article": None,
                    }
                ]
            ),
        ), patch.dict("os.environ", {"INTERNET_USE_KASPI_FALLBACK": "1"}):
            results = await internet_compare.compare_with_internet(
                [product],
                commission_rate=16,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "kaspi.kz")
        self.assertEqual(results[0]["commission"], 3200.0)

    async def test_kaspi_fallback_is_disabled_by_default(self):
        product = {
            "title": "REDMOND RMC-M52",
            "price": 10000,
            "url": "ozon",
        }
        kaspi_search = AsyncMock(return_value=[])
        with patch(
            "services.internet_compare.search_internet_sources",
            new=AsyncMock(return_value=[]),
        ), patch(
            "services.kaspi_compare.search_kaspi_product",
            new=kaspi_search,
        ), patch.dict("os.environ", {"INTERNET_USE_KASPI_FALLBACK": "0"}):
            results = await internet_compare.compare_with_internet(
                [product],
                commission_rate=16,
            )

        kaspi_search.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["matched"])

    async def test_compare_uses_search_pages_and_returns_result(self):
        product = {
            "title": "REDMOND RMC-M52",
            "price": 10000,
            "url": "ozon",
        }
        page_product = {
            "title": "REDMOND RMC-M52",
            "price": 20000,
            "source": "shop.kz",
            "url": "https://shop.kz/product",
            "availability": "В наличии",
        }
        with patch(
            "services.internet_compare.search_internet_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "url": "https://shop.kz/product",
                        "source": "shop.kz",
                    }
                ]
            ),
        ), patch(
            "services.internet_compare._fetch_store_page",
            new=AsyncMock(return_value=[page_product]),
        ):
            results = await internet_compare.compare_with_internet(
                [product],
                min_roi=25,
                commission_rate=16,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["internet_price"], 20000.0)

    async def test_filters_roi_below_twenty_five(self):
        product = {
            "title": "REDMOND RMC-M52",
            "price": 10000,
            "url": "ozon",
        }
        page_product = {
            "title": "REDMOND RMC-M52",
            "price": 13000,
            "source": "shop.kz",
            "url": "https://shop.kz/product",
            "availability": "В наличии",
        }
        with patch(
            "services.internet_compare.search_internet_sources",
            new=AsyncMock(
                return_value=[
                    {
                        "url": "https://shop.kz/product",
                        "source": "shop.kz",
                    }
                ]
            ),
        ), patch(
            "services.internet_compare._fetch_store_page",
            new=AsyncMock(return_value=[page_product]),
        ):
            results = await internet_compare.compare_with_internet(
                [product],
                min_roi=25,
                commission_rate=16,
            )
        self.assertEqual(results, [])

    async def test_includes_unmatched_rows_when_roi_filter_disabled(self):
        product = {
            "title": "REDMOND RMC-M52",
            "price": 10000,
            "url": "ozon",
        }
        with patch(
            "services.internet_compare.search_internet_sources",
            new=AsyncMock(return_value=[]),
        ), patch(
            "services.kaspi_compare.search_kaspi_product",
            new=AsyncMock(return_value=[]),
        ):
            results = await internet_compare.compare_with_internet(
                [product],
                min_roi=None,
                commission_rate=16,
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["matched"])
        self.assertEqual(
            results[0]["internet_title"],
            "Не найдено точное совпадение",
        )
        self.assertEqual(results[0]["ozon_price"], 10000.0)


class InternetReportAndCliTests(unittest.TestCase):
    def test_report_contains_decision_columns_links_and_manual_review_comment(self):
        items = [
            {
                "ozon_title": "REDMOND RMC-M52",
                "internet_title": "REDMOND RMC-M52",
                "source": "shop.kz",
                "sources_count": 2,
                "ozon_price": 30000,
                "internet_price": 50000,
                "commission_rate": 16,
                "commission": 8000,
                "net_revenue": 40000,
                "delivery": 2000,
                "total_cost": 32000,
                "price_difference": 20000,
                "profit": 10000,
                "roi": 33.33,
                "match_score": 95,
                "availability": "В наличии",
                "ozon_url": "https://ozon.kz/product/1",
                "internet_url": "https://shop.kz/product/1",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "services.report.REPORTS_DIR",
                Path(temp_dir),
            ):
                report_path = save_internet_comparison_report(items)
                workbook = load_workbook(report_path)
                sheet = workbook.active

        self.assertEqual(
            [cell.value for cell in sheet[1]],
            [title for title, _ in INTERNET_REPORT_COLUMNS],
        )
        self.assertEqual(sheet["E2"].value, "Shop.kz")
        self.assertIsNotNone(sheet["E2"].comment)
        self.assertEqual(sheet["G2"].value, 10000)
        self.assertEqual(
            sheet["I2"].hyperlink.target,
            items[0]["ozon_url"],
        )
        self.assertEqual(
            sheet["J2"].hyperlink.target,
            items[0]["internet_url"],
        )

    def test_cli_calls_internet_services(self):
        products = [{"title": "Товар", "price": 10000}]
        items = [{"matched": True, "profit": 5000}]
        with patch(
            "services.internet_compare.compare_with_internet",
            new=AsyncMock(return_value=items),
        ) as compare_mock, patch(
            "services.report.save_internet_comparison_report",
            return_value="reports/internet.xlsx",
        ) as report_mock:
            count, path = run_internet_comparison(
                ozon_products=products,
                min_roi=25,
                commission_rate=16,
            )
        self.assertEqual(count, 1)
        self.assertEqual(path, "reports/internet.xlsx")
        compare_mock.assert_awaited_once_with(
            products,
            min_roi=25,
            commission_rate=16,
        )
        report_mock.assert_called_once_with(items)


if __name__ == "__main__":
    unittest.main()
