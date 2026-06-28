#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Консольный запуск Ozon Parser.
Запуск: python app.py "https://ozon.ru/category/..." --count 50
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config.settings import Settings
from src.core.app_manager import AppManager
from src.utils.logger import setup_logging

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_FIELDS = ["name", "company_name", "product_url"]
ALL_FIELDS = [
    "article",
    "name",
    "seller_name",
    "company_name",
    "inn",
    "card_price",
    "price",
    "original_price",
    "product_url",
    "orders_count",
    "reviews_count",
    "average_rating",
    "working_time",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Консольный парсер товаров Ozon")
    parser.add_argument("url", nargs="?", help="Ссылка на категорию, поиск или продавца Ozon")
    parser.add_argument("-c", "--count", type=int, help="Количество товаров для парсинга")
    parser.add_argument("-w", "--workers", type=int, help="Количество воркеров")
    parser.add_argument(
        "-f",
        "--fields",
        default=",".join(DEFAULT_FIELDS),
        help="Поля через запятую или all. По умолчанию: name,company_name,product_url",
    )
    parser.add_argument("--headed", action="store_true", help="Показывать окно Chrome")
    parser.add_argument(
        "--compare-kaspi",
        action="store_true",
        help="Сравнить результаты с Kaspi и создать арбитражный Excel-отчет",
    )
    parser.add_argument(
        "--compare-json",
        type=Path,
        help="Сравнить с Kaspi товары из сохраненного JSON без нового парсинга Ozon",
    )
    parser.add_argument(
        "--compare-internet",
        action="store_true",
        help="Найти цены в интернет-магазинах Казахстана и создать Excel-отчет",
    )
    parser.add_argument(
        "--compare-internet-json",
        type=Path,
        help="Найти интернет-цены для товаров из сохраненного JSON",
    )
    parser.add_argument(
        "--min-roi",
        type=float,
        default=25.0,
        help="Минимальный ROI в процентах для отчета (по умолчанию: 25)",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=3000.0,
        help="Минимальная прибыль в тенге, строго больше значения (по умолчанию: 3000)",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=16.0,
        help="Комиссия от найденной цены в процентах (по умолчанию: 16)",
    )
    parser.add_argument(
        "--all-internet-matches",
        action="store_true",
        help="Включить все точные интернет-совпадения без фильтра ROI",
    )
    return parser.parse_args()


def parse_fields(fields_arg: str):
    fields_arg = (fields_arg or "").strip()
    if not fields_arg:
        return DEFAULT_FIELDS
    if fields_arg.lower() == "all":
        return ALL_FIELDS

    fields = [field.strip() for field in fields_arg.split(",") if field.strip()]
    unknown = [field for field in fields if field not in ALL_FIELDS]
    if unknown:
        raise ValueError(f"Неизвестные поля: {', '.join(unknown)}")
    return fields or DEFAULT_FIELDS


def ask_url(url: str = None):
    if url:
        return url.strip()

    while True:
        value = input("Вставь ссылку Ozon: ").strip()
        if value:
            return value
        print("Ссылка не может быть пустой.")


def ask_count(count: int = None, default: int = 50):
    if count is not None:
        if count < 1 or count > 10000:
            raise ValueError("Количество товаров должно быть от 1 до 10000")
        return count

    value = input(f"Количество товаров [{default}]: ").strip()
    if not value:
        return default

    count = int(value)
    if count < 1 or count > 10000:
        raise ValueError("Количество товаров должно быть от 1 до 10000")
    return count


def print_summary(app_manager: AppManager, settings: Settings):
    results = app_manager.last_results or {}
    if not results:
        print("\nПарсинг завершился без результатов. Проверь logs/errors_*.log")
        return 1

    folder_name = results.get("output_folder", "unknown")
    output_dir = settings.OUTPUT_DIR / folder_name
    stats = results.get("parsing_stats", {})

    print("\nГотово.")
    print(f"Всего товаров: {results.get('total_products', 0)}")
    print(f"Успешно: {results.get('successful_products', 0)}")
    print(f"Неудачно: {results.get('failed_products', 0)}")
    print(f"Время: {int(stats.get('total_time', 0))} сек.")
    print(f"Папка результата: {output_dir}")
    return 0


def build_ozon_products_for_comparison(
    app_manager: AppManager,
) -> list[dict]:
    results = app_manager.last_results or {}
    product_links = results.get("links", {})
    normalized_products = []

    for product in results.get("products", []):
        if not getattr(product, "success", False):
            continue

        price = getattr(product, "price", 0) or getattr(
            product,
            "card_price",
            0,
        )
        if not price:
            continue

        product_url = next(
            (
                url
                for url in product_links
                if getattr(product, "article", "") in url
            ),
            "",
        )
        normalized_products.append(
            {
                "title": getattr(product, "name", ""),
                "price": price,
                "url": product_url,
                "brand": None,
                # Текущий ProductInfo.article является ID карточки Ozon.
                "article": None,
                "category": None,
            }
        )

    return normalized_products


def load_ozon_products_from_json(json_path: Path) -> list[dict]:
    if not json_path.exists():
        raise FileNotFoundError(f"JSON-файл не найден: {json_path}")

    with json_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    products = payload.get("products", []) if isinstance(payload, dict) else []
    category = (
        payload.get("category_url")
        if isinstance(payload, dict)
        else None
    )
    normalized_products = []

    for product in products:
        if not isinstance(product, dict) or not product.get("success"):
            continue
        price = product.get("price") or product.get("card_price")
        title = str(product.get("name") or "").strip()
        product_url = str(product.get("product_url") or "").strip()
        if not title or not price:
            continue
        normalized_products.append(
            {
                "title": title,
                "price": price,
                "url": product_url,
                "brand": None,
                # article в сохраненном JSON является ID карточки Ozon.
                "article": None,
                "category": category,
            }
        )

    return normalized_products


def run_kaspi_comparison(
    app_manager: AppManager | None = None,
    ozon_products: list[dict] | None = None,
    min_roi: float = 25.0,
    min_profit: float = 3000.0,
) -> tuple[int, str]:
    from services.kaspi_compare import compare_with_kaspi
    from services.report import save_arbitrage_report

    if ozon_products is None:
        if app_manager is None:
            raise ValueError(
                "Нужен app_manager или готовый список ozon_products"
            )
        ozon_products = build_ozon_products_for_comparison(app_manager)

    arbitrage_items = asyncio.run(
        compare_with_kaspi(
            ozon_products,
            min_roi=min_roi,
            min_profit=min_profit,
        )
    )
    report_path = save_arbitrage_report(arbitrage_items)
    return len(arbitrage_items), report_path


def run_internet_comparison(
    app_manager: AppManager | None = None,
    ozon_products: list[dict] | None = None,
    min_roi: float | None = None,
    commission_rate: float = 16.0,
) -> tuple[int, str]:
    from services.internet_compare import compare_with_internet
    from services.report import save_internet_comparison_report

    if ozon_products is None:
        if app_manager is None:
            raise ValueError(
                "Нужен app_manager или готовый список ozon_products"
            )
        ozon_products = build_ozon_products_for_comparison(app_manager)

    comparison_items = asyncio.run(
        compare_with_internet(
            ozon_products,
            min_roi=min_roi,
            commission_rate=commission_rate,
        )
    )
    report_path = save_internet_comparison_report(comparison_items)
    matched_count = len([item for item in comparison_items if item.get("matched")])
    return matched_count, report_path


def main():
    args = parse_args()
    setup_logging()
    logger = logging.getLogger(__name__)

    settings = Settings()
    app_manager = AppManager(settings)

    def stop_handler(signum, frame):
        print("\nОстанавливаю парсинг...")
        app_manager.shutdown()
        sys.exit(130)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    try:
        if args.min_roi < 0 or args.min_profit < 0:
            raise ValueError(
                "--min-roi и --min-profit должны быть неотрицательными"
            )
        if args.commission < 0 or args.commission >= 100:
            raise ValueError(
                "--commission должен быть не меньше 0 и меньше 100"
            )
        if args.compare_json and args.compare_internet_json:
            raise ValueError(
                "Используйте только один режим: "
                "--compare-json или --compare-internet-json"
            )

        if args.compare_json:
            ozon_products = load_ozon_products_from_json(args.compare_json)
            print(
                f"Загружено товаров Ozon из JSON: {len(ozon_products)}"
            )
            print(
                f"Фильтры: ROI >= {args.min_roi:g}%, "
                f"прибыль > {args.min_profit:g} ₸"
            )
            opportunities, report_path = run_kaspi_comparison(
                ozon_products=ozon_products,
                min_roi=args.min_roi,
                min_profit=args.min_profit,
            )
            print(f"Арбитражных возможностей: {opportunities}")
            print(f"Отчет Kaspi: {report_path}")
            return 0

        if args.compare_internet_json:
            ozon_products = load_ozon_products_from_json(
                args.compare_internet_json
            )
            print(
                f"Загружено товаров Ozon из JSON: {len(ozon_products)}"
            )
            print("Ищу цены в интернет-магазинах Казахстана...")
            internet_min_roi = (
                None if args.all_internet_matches else args.min_roi
            )
            if internet_min_roi is None:
                print("Фильтр ROI отключен: показываю все совпадения")
            else:
                print(f"Фильтр: ROI >= {internet_min_roi:g}%")
            print(f"Комиссия: {args.commission:g}%")
            matches, report_path = run_internet_comparison(
                ozon_products=ozon_products,
                min_roi=internet_min_roi,
                commission_rate=args.commission,
            )
            print(f"Товаров с найденной интернет-ценой: {matches}")
            print(f"Интернет-отчет: {report_path}")
            return 0

        url = ask_url(args.url)
        count = ask_count(args.count, settings.MAX_PRODUCTS)
        selected_fields = parse_fields(args.fields)

        settings.MAX_PRODUCTS = count
        if args.workers:
            settings.MAX_WORKERS = args.workers
        if args.headed:
            settings.HEADLESS = False

        print("\nСтартую парсинг.")
        print(f"URL: {url}")
        print(f"Товаров: {settings.MAX_PRODUCTS}")
        print(f"Воркеров: {settings.MAX_WORKERS}")
        print(f"Поля: {', '.join(selected_fields)}")
        print("Логи идут ниже. Для остановки нажми Ctrl+C.\n")

        if not app_manager.start_parsing(url, selected_fields, user_id="console"):
            print("Не удалось запустить парсинг.")
            return 1

        last_status_at = 0
        while app_manager.is_running:
            now = time.time()
            if now - last_status_at >= 15:
                status = app_manager.get_status()
                print(f"Работаю... активных задач: {status.get('active_users_count', 0)}")
                last_status_at = now
            time.sleep(1)

        exit_code = print_summary(app_manager, settings)
        if exit_code == 0 and args.compare_kaspi:
            print("\nСравниваю товары с Kaspi...")
            opportunities, report_path = run_kaspi_comparison(
                app_manager=app_manager,
                min_roi=args.min_roi,
                min_profit=args.min_profit,
            )
            print(f"Арбитражных возможностей: {opportunities}")
            print(f"Отчет Kaspi: {report_path}")

        if exit_code == 0 and args.compare_internet:
            print("\nИщу цены в интернет-магазинах Казахстана...")
            internet_min_roi = (
                None if args.all_internet_matches else args.min_roi
            )
            matches, report_path = run_internet_comparison(
                app_manager=app_manager,
                min_roi=internet_min_roi,
                commission_rate=args.commission,
            )
            print(f"Товаров с найденной интернет-ценой: {matches}")
            print(f"Интернет-отчет: {report_path}")

        return exit_code

    except Exception as e:
        logger.error(f"Ошибка консольного запуска: {e}")
        print(f"Ошибка: {e}")
        return 1
    finally:
        app_manager.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
