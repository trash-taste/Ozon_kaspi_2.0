import logging
import threading
import asyncio
import os
import time
from typing import Dict, Any, List, Optional
from ..config.settings import Settings
from ..parsers.link_parser import OzonLinkParser
from ..parsers.ozon_playwright_parser import OzonPlaywrightParser
from ..parsers.ozon_playwright_product_parser import OzonPlaywrightProductParser
from ..parsers.product_parser import OzonProductParser
from ..parsers.seller_parser import OzonSellerParser
from ..utils.excel_exporter import ExcelExporter
from ..telegram.bot_manager import TelegramBotManager
from ..utils.resource_manager import resource_manager

logger = logging.getLogger(__name__)

class AppManager:
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.is_running = False  # Глобальный флаг для совместимости
        self.active_parsing_users = set()  # Множество активных пользователей
        self.parsing_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_results = {}  # Глобальные результаты для совместимости
        self.user_results = {}  # Результаты по пользователям: {user_id: results}
        self.telegram_bot: Optional[TelegramBotManager] = None
    
    def start_parsing(self, category_url: str, selected_fields: list = None, user_id: str = None) -> bool:
        with self.parsing_lock:
            # Проверяем, не парсит ли уже этот пользователь
            if user_id and user_id in self.active_parsing_users:
                logger.warning(f"Пользователь {user_id} уже запустил парсинг")
                return False
            
            # Добавляем пользователя в активные
            if user_id:
                self.active_parsing_users.add(user_id)
            
            # Устанавливаем глобальный флаг для первого пользователя
            if not self.is_running:
                self.stop_event.clear()
                self.is_running = True
        
        try:
            # Запускаем парсинг в отдельном потоке
            parsing_thread = threading.Thread(
                target=self._parsing_task_wrapper,
                args=(category_url, selected_fields, user_id),
                daemon=True
            )
            parsing_thread.start()
            return True
        except Exception as e:
            logger.error(f"Ошибка запуска парсинга для пользователя {user_id}: {e}")
            # Убираем пользователя из активных при ошибке
            with self.parsing_lock:
                if user_id and user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                # Если это был последний пользователь, сбрасываем глобальный флаг
                if not self.active_parsing_users:
                    self.is_running = False
            return False
    
    def _parsing_task_wrapper(self, category_url: str, selected_fields: list = None, user_id: str = None):
        """Wrapper для парсинга с правильной очисткой ресурсов"""
        try:
            self._parsing_task(category_url, selected_fields, user_id)
        except Exception as e:
            logger.error(f"Ошибка в парсинге для пользователя {user_id}: {e}")
            self._notify_user(user_id, "❌ Парсинг остановился из-за ошибки. Подробности записаны в logs.")
        finally:
            # Убираем пользователя из активных
            with self.parsing_lock:
                if user_id and user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                    logger.info(f"Пользователь {user_id} завершил парсинг")
                
                # Если это был последний пользователь, сбрасываем глобальный флаг
                if not self.active_parsing_users:
                    self.is_running = False
                    logger.info("Все пользователи завершили парсинг")
    
    def stop_parsing(self, user_id: str = None):
        """Останавливает парсинг для конкретного пользователя или всех"""
        with self.parsing_lock:
            if user_id:
                # Останавливаем парсинг для конкретного пользователя
                if user_id in self.active_parsing_users:
                    self.active_parsing_users.remove(user_id)
                    logger.info(f"Остановлен парсинг для пользователя {user_id}")
            else:
                # Останавливаем все парсинги
                self.active_parsing_users.clear()
                logger.info("Остановлен парсинг для всех пользователей")
            
            # Если нет активных пользователей, сбрасываем глобальный флаг
            if not self.active_parsing_users:
                self.stop_event.set()
                self.is_running = False
    
    def _parsing_task(self, category_url: str, selected_fields: list = None, user_id: str = None):
        # Поля, которые требуют парсинга селлера
        SELLER_FIELDS = {'inn', 'orders_count', 'reviews_count', 'average_rating', 'working_time'}
        
        # Проверяем, нужен ли парсинг селлеров
        needs_seller_parsing = False
        if selected_fields:
            needs_seller_parsing = any(field in SELLER_FIELDS for field in selected_fields)
        else:
            # Если поля не указаны, по умолчанию парсим селлеров
            needs_seller_parsing = True
        
        start_time = time.time()
        
        try:
            # Начинаем сессию парсинга для пользователя
            if user_id:
                resource_manager.start_parsing_session(user_id, 'full_parsing', 0)
                self._notify_user(user_id, "🔎 Собираю ссылки товаров с Ozon...")
            
            link_parser = self._create_link_parser(category_url, user_id)
            success, product_links = link_parser.start_parsing()
            
            if self.stop_event.is_set():
                return
            
            if not success or not product_links:
                logger.error("Не удалось собрать ссылки товаров")
                self._notify_user(user_id, "❌ Не удалось собрать ссылки товаров. Проверьте ссылку на категорию Ozon и попробуйте ещё раз.")
                return
            
            self._notify_user(user_id, f"✅ Собрано ссылок: {len(product_links)}. Начинаю обработку товаров...")
            
            if self.stop_event.is_set():
                return
            
            product_parser = self._create_product_parser(user_id)
            product_results = product_parser.parse_products(product_links)
            
            # Принудительно закрываем все воркеры продуктов перед началом парсинга продавцов
            product_parser.cleanup()

            parsed_products = len(
                [product for product in product_results if product.success]
            )
            logger.info(
                "Обработка карточек завершена: успешно=%s, ошибок=%s",
                parsed_products,
                len(product_results) - parsed_products,
            )
            self._notify_user(
                user_id,
                "Обработка карточек завершена: "
                f"{parsed_products}/{len(product_results)} успешно. "
                "Продолжаю подготовку отчета.",
            )
            
            if self.stop_event.is_set():
                return
            
            seller_results = []
            
            if needs_seller_parsing:
                seller_ids = []
                total_products = len(product_results)
                successful_products = len([p for p in product_results if p.success])
                products_with_seller_id = 0
                
                for product in product_results:
                    if product.success:
                        if product.seller_id:
                            seller_ids.append(product.seller_id)
                            products_with_seller_id += 1
                        else:
                            logger.warning(f"Товар {product.article} ({product.name[:50]}) не имеет seller_id")
                
                unique_seller_ids = list(set(seller_ids))
                logger.info(f"Статистика seller_id: всего товаров={total_products}, успешных={successful_products}, с seller_id={products_with_seller_id}, уникальных селлеров={len(unique_seller_ids)}")
                
                if unique_seller_ids:
                    logger.info(f"Начинаем парсинг {len(unique_seller_ids)} продавцов (поля: {selected_fields})")
                    self._notify_user(
                        user_id,
                        "Проверяю дополнительные данные продавцов: "
                        f"{len(unique_seller_ids)}.",
                    )
                    seller_parser = OzonSellerParser(self.settings.MAX_WORKERS, user_id, headless=self.settings.HEADLESS)
                    seller_results = seller_parser.parse_sellers(unique_seller_ids)
                    logger.info(f"✓ Парсинг селлеров завершен. Получено: {len(seller_results)}, успешных: {len([s for s in seller_results if s.success])}")
                    # Закрываем воркеры продавцов после завершения
                    seller_parser.cleanup()
                else:
                    logger.info("Нет ID селлеров для парсинга")
            else:
                logger.info(f"Парсинг селлеров пропущен: в selected_fields ({selected_fields}) нет полей селлера")
            
            if self.stop_event.is_set():
                return
            
            seller_data = {}
            for seller in seller_results:
                if seller.success:
                    seller_data[seller.seller_id] = seller
            
            end_time = time.time()
            total_time = end_time - start_time
            successful_products = len([p for p in product_results if p.success])
            failed_products = len([p for p in product_results if not p.success])
            avg_time_per_product = total_time / len(product_results) if product_results else 0
            
            # Сохраняем результаты для конкретного пользователя
            user_results = {
                'links': product_links,
                'products': product_results,
                'sellers': seller_results,
                'category_url': category_url,
                'total_products': len(product_results),
                'successful_products': successful_products,
                'failed_products': failed_products,
                'total_sellers': len(seller_results),
                'successful_sellers': len([s for s in seller_results if s.success]),
                'output_folder': getattr(link_parser, 'output_folder', 'unknown'),
                'seller_data': seller_data,
                'selected_fields': selected_fields,
                'parsing_stats': {
                    'total_time': total_time,
                    'successful_products': successful_products,
                    'failed_products': failed_products,
                    'average_time_per_product': avg_time_per_product
                }
            }
            
            # Сохраняем результаты для пользователя
            if user_id:
                self.user_results[user_id] = user_results
            
            # Обновляем глобальные результаты для совместимости
            self.last_results = user_results
            
            self._save_results_to_file(user_id)
            excel_created = self._export_to_excel(user_id)
            if self.telegram_bot and user_id and not excel_created:
                self._notify_user(
                    user_id,
                    "Не удалось создать технический Ozon-отчет. "
                    "Проверьте logs и папку output.",
                )
            if self.telegram_bot and user_id:
                self._compare_with_internet_and_send(user_id)
            if self.telegram_bot and user_id:
                self._send_report_to_telegram(user_id)
            
        finally:
            # Завершаем сессию парсинга для пользователя
            if user_id:
                resource_manager.finish_parsing_session(user_id)
    
    def _create_link_parser(self, category_url: str, user_id: str = None):
        parser_name = os.getenv("OZON_LINK_PARSER", "playwright").strip().casefold()
        parser_cls = OzonPlaywrightParser if parser_name == "playwright" else OzonLinkParser
        logger.info("Используется Ozon link parser: %s", parser_cls.__name__)
        return parser_cls(
            category_url,
            self.settings.MAX_PRODUCTS,
            user_id,
            headless=self.settings.HEADLESS,
        )

    def _create_product_parser(self, user_id: str = None):
        parser_name = os.getenv("OZON_PRODUCT_PARSER", "playwright").strip().casefold()
        parser_cls = (
            OzonPlaywrightProductParser
            if parser_name == "playwright"
            else OzonProductParser
        )
        logger.info("Используется Ozon product parser: %s", parser_cls.__name__)
        return parser_cls(
            self.settings.MAX_WORKERS,
            user_id,
            headless=self.settings.HEADLESS,
        )


    def _save_results_to_file(self, user_id: str = None):
        try:
            import json
            from datetime import datetime
            from pathlib import Path
            
            folder_name = self.last_results.get('output_folder', 'unknown')
            filename = f"category_{folder_name}.json"
            current_timestamp = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
            
            output_dir = self.settings.OUTPUT_DIR / folder_name
            filepath = output_dir / filename
            
            # Получаем результаты для конкретного пользователя
            results = self.user_results.get(user_id, self.last_results) if user_id else self.last_results
            
            save_data = {
                'timestamp': current_timestamp,
                'category_url': results.get('category_url', ''),
                'total_products': results.get('total_products', 0),
                'successful_products': results.get('successful_products', 0),
                'total_sellers': results.get('total_sellers', 0),
                'successful_sellers': results.get('successful_sellers', 0),
                'products': []
            }
            
            for product in results.get('products', []):
                product_url = ""
                for url in results.get('links', {}).keys():
                    if product.article in url:
                        product_url = url
                        break
                
                seller_info = results.get('seller_data', {}).get(product.seller_id, None)
                
                seller_data = {
                    'name': product.company_name,
                    'id': product.seller_id,
                    'link': product.seller_link,
                    'inn': '',
                    'company_name': product.company_name
                }
                
                if seller_info:
                    company_name = seller_info.company_name.replace('\\"', '"').replace('\"', '"').replace('"', '"')
                    
                    seller_data.update({
                        'inn': seller_info.inn,
                        'company_name': company_name,
                        'orders_count': seller_info.orders_count,
                        'reviews_count': seller_info.reviews_count,
                        'working_time': seller_info.working_time,
                        'average_rating': seller_info.average_rating
                    })
                
                if 'name' in seller_data:
                    seller_data['name'] = seller_data['name'].replace('\\"', '"').replace('\"', '"').replace('"', '"')
                
                save_data['products'].append({
                    'article': product.article,
                    'name': product.name,
                    'seller': seller_data,
                    'image_url': product.image_url,
                    'card_price': product.card_price,
                    'price': product.price,
                    'original_price': product.original_price,
                    'product_url': product_url,
                    'success': product.success,
                    'error': product.error
                })
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            logger.error(f"Ошибка сохранения результатов: {e}")
    
    def _export_to_excel(
        self,
        user_id: str = None,
        send_to_telegram: bool = False,
    ) -> bool:
        try:
            # Получаем результаты для конкретного пользователя
            results = self.user_results.get(user_id, self.last_results) if user_id else self.last_results
            
            folder_name = results.get('output_folder', 'unknown')
            output_dir = self.settings.OUTPUT_DIR / folder_name
            
            exporter = ExcelExporter(output_dir, f"category_{folder_name}")
            selected_fields = results.get('selected_fields', [])
            
            export_data = {'products': []}
            
            for product in results.get('products', []):
                product_url = ""
                for url in results.get('links', {}).keys():
                    if product.article in url:
                        product_url = url
                        break
                
                seller_info = results.get('seller_data', {}).get(product.seller_id, None)
                
                seller_data = {
                    'name': product.company_name,
                    'inn': '',
                    'company_name': product.company_name,
                    'orders_count': '',
                    'reviews_count': '',
                    'average_rating': '',
                    'working_time': ''
                }
                
                if seller_info:
                    seller_data.update({
                        'inn': seller_info.inn,
                        'company_name': seller_info.company_name.replace('\"', '"').replace('"', '"'),
                        'orders_count': seller_info.orders_count,
                        'reviews_count': seller_info.reviews_count,
                        'average_rating': seller_info.average_rating,
                        'working_time': seller_info.working_time
                    })
                
                export_data['products'].append({
                    'article': product.article,
                    'name': product.name,
                    'seller': seller_data,
                    'image_url': product.image_url,
                    'card_price': product.card_price,
                    'price': product.price,
                    'original_price': product.original_price,
                    'product_url': product_url,
                    'success': product.success,
                    'error': product.error
                })
            
            if not exporter.export_results(export_data, selected_fields):
                return False

            file_size = exporter.filepath.stat().st_size
            logger.info(
                "Excel готов к отправке: path=%s size=%s bytes",
                exporter.filepath.resolve(),
                file_size,
            )
            if send_to_telegram and self.telegram_bot and user_id:
                return self._send_files_to_telegram(
                    str(exporter.filepath),
                    user_id,
                )
            return True
            
        except Exception as e:
            logger.exception("Ошибка экспорта в Excel: %s", e)
            return False

    def _build_ozon_products_for_comparison(
        self,
        user_id: str,
    ) -> list[dict]:
        results = self.user_results.get(user_id, self.last_results)
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
            title = str(getattr(product, "name", "") or "").strip()
            if not title or not price:
                continue

            ozon_article = str(
                getattr(product, "article", "") or ""
            ).strip()
            product_url = next(
                (
                    url
                    for url in product_links
                    if ozon_article and ozon_article in url
                ),
                "",
            )
            normalized_products.append(
                {
                    "title": title,
                    "price": price,
                    "url": product_url,
                    "brand": None,
                    # ProductInfo.article is an Ozon card ID, not a model.
                    "article": None,
                    "category": results.get("category_url"),
                }
            )

        return normalized_products

    def _compare_with_kaspi_and_send(self, user_id: str):
        try:
            from services.kaspi_compare import compare_with_kaspi
            from services.report import save_arbitrage_report

            ozon_products = self._build_ozon_products_for_comparison(user_id)
            logger.info(
                "Запуск сравнения с Kaspi для пользователя %s: %s товаров",
                user_id,
                len(ozon_products),
            )
            self._notify_user(
                user_id,
                "Сравниваю найденные товары с Kaspi...",
            )

            arbitrage_items = asyncio.run(
                compare_with_kaspi(ozon_products)
            )
            report_path = save_arbitrage_report(arbitrage_items)
            caption = (
                "Сравнение с Kaspi завершено.\n"
                f"Строк в отчете: {len(arbitrage_items)}.\n"
                "Excel-файл готов."
            )
            self._send_files_to_telegram(
                report_path,
                user_id,
                caption=caption,
            )
        except Exception as e:
            logger.exception(
                "Ошибка сравнения с Kaspi для пользователя %s: %s",
                user_id,
                e,
            )
            self._notify_user(
                user_id,
                "Не удалось завершить сравнение с Kaspi. "
                "Ozon-отчет уже сохранен.",
            )

    def _compare_with_internet_and_send(self, user_id: str):
        try:
            from services.internet_compare import compare_with_internet
            from services.report import save_internet_comparison_report

            ozon_products = self._build_ozon_products_for_comparison(user_id)
            logger.info(
                "Запуск интернет-сравнения для пользователя %s: "
                "%s товаров",
                user_id,
                len(ozon_products),
            )
            if not ozon_products:
                self._notify_user(
                    user_id,
                    "Не удалось получить названия и цены товаров Ozon. "
                    "Итоговый отчет не создан.",
                )
                return False

            self._notify_user(
                user_id,
                "Ищу цены в интернет-магазинах Казахстана. "
                "Комиссия: 16%.",
            )
            comparison_items = asyncio.run(
                compare_with_internet(
                    ozon_products,
                    min_roi=None,
                    commission_rate=16,
                )
            )
            if not comparison_items:
                logger.warning(
                    "Интернет-сравнение не нашло точных совпадений: "
                    "user=%s products=%s",
                    user_id,
                    len(ozon_products),
                )
                self._notify_user(
                    user_id,
                    "Точные совпадения цен в интернете не найдены. "
                    "Пустой Excel не отправляю.",
                )
                return False

            report_path = save_internet_comparison_report(
                comparison_items
            )
            matched_count = len(
                [item for item in comparison_items if item.get("matched")]
            )
            caption = (
                "Сравнение с интернет-магазинами завершено.\n"
                f"Строк в отчете: {len(comparison_items)}.\n"
                f"Точных совпадений: {matched_count}.\n"
                "Комиссия: 16%. В файле показаны прибыль и ROI."
            )
            return self._send_files_to_telegram(
                report_path,
                user_id,
                caption=caption,
            )
        except Exception as e:
            logger.exception(
                "Ошибка интернет-сравнения для пользователя %s: %s",
                user_id,
                e,
            )
            self._notify_user(
                user_id,
                "Не удалось завершить поиск интернет-цен. "
                "Подробности записаны в logs.",
            )
            return False
    
    def start_telegram_bot(self, bot_token: str, user_ids) -> bool:
        try:
            if self.telegram_bot:
                self.telegram_bot.stop()
            
            # Поддерживаем как строку, так и массив для обратной совместимости
            if isinstance(user_ids, str):
                user_ids = [user_ids]
            elif not isinstance(user_ids, list):
                user_ids = list(user_ids)
            
            self.telegram_bot = TelegramBotManager(bot_token, user_ids, self)
            return self.telegram_bot.start()
        except Exception as e:
            logger.error(f"Ошибка запуска Telegram бота: {e}")
            return False
    
    def stop_telegram_bot(self):
        if self.telegram_bot:
            self.telegram_bot.stop()
            self.telegram_bot = None
    
    def restart_parsing(self, category_url: str, selected_fields: list = None, user_id: str = None) -> bool:
        self.stop_parsing(user_id)
        time.sleep(1)
        return self.start_parsing(category_url, selected_fields, user_id)
    
    def get_status(self):
        with self.parsing_lock:
            status = {
                'is_running': self.is_running,
                'active_users_count': len(self.active_parsing_users),
                'active_users': list(self.active_parsing_users),
                'telegram_bot_active': self.telegram_bot and hasattr(self.telegram_bot, 'is_running') and self.telegram_bot.is_running,
                'last_results': self.last_results,
                'settings': {
                    'max_products': self.settings.MAX_PRODUCTS,
                    'max_workers': self.settings.MAX_WORKERS
                }
            }
        
        # Добавляем информацию о ресурсах
        resource_status = resource_manager.get_status()
        status.update(resource_status)
        
        return status
    
    def get_user_results(self, user_id: str):
        """Получает результаты парсинга для конкретного пользователя"""
        with self.parsing_lock:
            return self.user_results.get(user_id, None)
    
    def _notify_user(self, user_id: str, text: str):
        if not user_id or not self.telegram_bot:
            return
        
        try:
            self.telegram_bot.send_message_sync(text, user_id)
        except Exception as e:
            logger.error(f"Ошибка уведомления пользователя {user_id}: {e}")
    
    def _send_report_to_telegram(self, user_id: str = None):
        return self._send_via_temp_bot(
            report_only=True,
            target_user_id=user_id,
        )
    
    def _send_files_to_telegram(
        self,
        excel_path: str,
        user_id: str = None,
        caption: str = None,
    ) -> bool:
        return self._send_via_temp_bot(
            excel_path=excel_path,
            target_user_id=user_id,
            caption=caption,
        )

    def _send_via_temp_bot(
        self,
        excel_path: str = None,
        report_only: bool = False,
        target_user_id: str = None,
        caption: str = None,
    ) -> bool:
        try:
            from ..utils.config_loader import load_telegram_config
            from pathlib import Path
            
            config_token, config_user_ids = load_telegram_config()
            bot_token = getattr(
                self.telegram_bot,
                "bot_token",
                None,
            ) or config_token
            
            if not bot_token:
                logger.error("Нет TELEGRAM_BOT_TOKEN в config.txt")
                return False
            
            # Определяем целевого пользователя
            if target_user_id:
                # Отправляем конкретному пользователю
                target_users = [target_user_id]
            else:
                # Отправляем всем пользователям из конфига (для обратной совместимости)
                if not config_user_ids:
                    logger.error("Нет TELEGRAM_CHAT_ID в config.txt")
                    return False
                target_users = config_user_ids.split(',') if isinstance(config_user_ids, str) else [config_user_ids]

            if excel_path:
                report_file = Path(excel_path)
                if not report_file.is_file():
                    logger.error(
                        "Excel-файл для отправки не найден: %s",
                        report_file.resolve(),
                    )
                    return False
                logger.info(
                    "Отправка Excel в Telegram: path=%s size=%s "
                    "user=%s",
                    report_file.resolve(),
                    report_file.stat().st_size,
                    target_user_id,
                )
            
            from aiogram import Bot
            from aiogram.types import FSInputFile
            
            async def send_files():
                temp_bot = Bot(token=bot_token)
                sent = True
                
                try:
                    for target_user in target_users:
                        target_user = target_user.strip()
                        
                        if report_only:
                            # Получаем результаты для конкретного пользователя
                            results = self.user_results.get(target_user_id, self.last_results) if target_user_id else self.last_results
                            
                            stats = results.get('parsing_stats', {})
                            total_time = stats.get('total_time', 0)
                            successful = stats.get('successful_products', 0)
                            failed = stats.get('failed_products', 0)
                            avg_time = stats.get('average_time_per_product', 0)
                            
                            hours = int(total_time // 3600)
                            minutes = int((total_time % 3600) // 60)
                            seconds = int(total_time % 60)
                            
                            if hours > 0:
                                time_str = f"{hours}ч {minutes}м {seconds}с"
                            elif minutes > 0:
                                time_str = f"{minutes}м {seconds}с"
                            else:
                                time_str = f"{seconds}с"
                            
                            success_rate = (successful / (successful + failed) * 100) if (successful + failed) > 0 else 0
                            
                            report = (
                                "📈 <b>Отчет о парсинге</b>\n\n"
                                f"⏱️ <b>Общее время:</b> {time_str}\n"
                                f"⚡ <b>Среднее время на товар:</b> {avg_time:.1f}с\n\n"
                                f"📦 <b>Всего товаров:</b> {successful + failed}\n"
                                f"✅ <b>Успешно:</b> {successful}\n"
                                f"❌ <b>Неудачно:</b> {failed}\n"
                                f"📊 <b>Успешность:</b> {success_rate:.1f}%"
                            )
                            
                            await temp_bot.send_message(chat_id=target_user, text=report, parse_mode="HTML")
                        
                        if excel_path:
                            document_caption = caption or (
                                "🎉 <b>Парсинг успешно завершен!</b>\n\n"
                                "📊 <b>Ваш Excel файл готов!</b>\n"
                                "💎 Данные отформатированы и готовы к использованию\n\n"
                                "📥 Скачайте файл ниже ⬇️"
                            )
                            
                            document = FSInputFile(excel_path)
                            await temp_bot.send_document(
                                chat_id=target_user,
                                document=document,
                                caption=document_caption,
                                parse_mode="HTML" if not caption else None,
                            )
                            logger.info(
                                "Excel успешно отправлен в Telegram: "
                                "path=%s user=%s",
                                excel_path,
                                target_user,
                            )
                        
                finally:
                    await temp_bot.session.close()
                return sent
            
            return asyncio.run(send_files())
            
        except Exception as e:
            logger.exception(
                "Ошибка отправки через временный бот: %s",
                e,
            )
            return False
    
    def _delete_output_folder(self):
        try:
            import shutil
            import os
            import stat
            
            folder_name = self.last_results.get('output_folder', '')
            if folder_name:
                output_dir = self.settings.OUTPUT_DIR / folder_name
                if output_dir.exists():
                    def handle_remove_readonly(func, path, exc):
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    
                    shutil.rmtree(output_dir, onerror=handle_remove_readonly)
        except Exception as e:
            logger.error(f"Ошибка удаления папки: {e}")
    
    def shutdown(self):
        # non-blocking wrapper
        threading.Thread(target=self._do_shutdown, daemon=True).start()

    def _do_shutdown(self):
        self.stop_parsing()
        self.stop_telegram_bot()
