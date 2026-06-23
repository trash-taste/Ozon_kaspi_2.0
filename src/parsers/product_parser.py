
import logging
import json
import re
import time
import concurrent.futures
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from ..utils.selenium_manager import SeleniumManager
from ..utils.resource_manager import resource_manager

logger = logging.getLogger(__name__)

@dataclass
class ProductInfo:
    article: str
    name: str = ""
    company_name: str = ""
    company_inn: str = ""
    image_url: str = ""
    card_price: int = 0
    price: int = 0
    original_price: int = 0
    seller_id: str = ""
    seller_link: str = ""
    success: bool = False
    error: str = ""

class ProductWorker:
    
    def __init__(self, worker_id: int, headless: bool = True):
        self.worker_id = worker_id
        self.selenium_manager = SeleniumManager(headless=headless)
        self.driver = None
        logger.info(f"Воркер {worker_id} инициализирован")
    
    def initialize(self):
        try:
            self.driver = self.selenium_manager.create_driver()
            logger.info(f"Воркер {self.worker_id} готов к работе")
        except Exception as e:
            logger.error(f"Ошибка инициализации воркера {self.worker_id}: {e}")
            raise
    
    def parse_products(self, articles: List[str], product_links: Dict[str, str]) -> List[ProductInfo]:
        results = []
        
        for article in articles:
            try:
                # Находим ссылку и изображение для артикула
                product_url = ""
                image_from_links = ""
                
                for url, img_url in product_links.items():
                    if article in url:
                        product_url = url
                        image_from_links = img_url
                        break
                
                result = self._parse_single_product(article, product_url)
                
                # Используем изображение из ссылок вместо API
                if result.success and image_from_links:
                    result.image_url = image_from_links
                
                results.append(result)
                
                if result.success:
                    logger.info(f"Воркер {self.worker_id}: Товар {article} обработан успешно")
                else:
                    logger.warning(f"Воркер {self.worker_id}: Ошибка товара {article}: {result.error}")
                    
            except Exception as e:
                logger.error(f"Воркер {self.worker_id}: Критическая ошибка товара {article}: {e}")
                results.append(ProductInfo(article=article, error=str(e)))
            
            time.sleep(1.5)
        
        return results
    
    def _parse_single_product(self, article: str, product_url: str) -> ProductInfo:
        max_retries = 2
        
        for attempt in range(max_retries):
            try:
                if not product_url:
                    return ProductInfo(article=article, error="Не найдена ссылка товара")

                if not self.selenium_manager.navigate_to_url(product_url):
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                    return ProductInfo(article=article, error="Не удалось загрузить карточку товара")

                WebDriverWait(self.driver, 15).until(
                    lambda driver: driver.find_elements(By.CSS_SELECTOR, "h1")
                )

                product_info = self._parse_product_page(article)
                
                if product_info.success:
                    return product_info
                elif attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                else:
                    return product_info
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.debug(f"Попытка {attempt + 1} неудачна для товара {article}: {e}")
                    time.sleep(5)
                    continue
                else:
                    return ProductInfo(article=article, error=f"Ошибка парсинга: {str(e)}")
        
        return ProductInfo(article=article, error="Превышено количество попыток")

    def _parse_product_page(self, article: str) -> ProductInfo:
        try:
            product_info = ProductInfo(article=article)
            headings = self.driver.find_elements(By.CSS_SELECTOR, "h1")
            if headings:
                product_info.name = self._fix_text_encoding(
                    headings[0].text.strip()
                )

            seller_name, seller_link = self._extract_seller()
            product_info.company_name = seller_name
            product_info.seller_link = seller_link

            images = self.driver.find_elements(
                By.CSS_SELECTOR, 'meta[property="og:image"]'
            )
            if images:
                product_info.image_url = images[0].get_attribute("content") or ""

            seller_id = re.search(
                r"/seller/(?:[^/]*-)?(\d+)/?",
                product_info.seller_link,
            )
            if seller_id:
                product_info.seller_id = seller_id.group(1)

            price_widgets = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-widget="webPrice"]'
            )
            price_text = price_widgets[0].text if price_widgets else ""
            prices = self._extract_prices(price_text)
            if prices:
                product_info.card_price = prices[0]
                product_info.price = prices[0]
                if len(prices) > 1 and prices[1] > prices[0]:
                    product_info.original_price = prices[1]

            if product_info.name:
                product_info.success = True
            else:
                product_info.error = "Не найдено название товара в карточке"

            return product_info
        except Exception as e:
            return ProductInfo(
                article=article,
                error=f"Ошибка обработки карточки: {e}",
            )

    def _extract_seller(self) -> Tuple[str, str]:
        seller_links = self.driver.find_elements(
            By.CSS_SELECTOR, 'a[href*="/seller/"]'
        )
        fallback = ("", "")

        for seller in seller_links:
            href = seller.get_attribute("href") or ""
            title = seller.get_attribute("title") or ""
            text = seller.text or ""
            value = title.strip() or text.strip()

            if not value:
                try:
                    value = seller.find_element(By.XPATH, "..").text.strip()
                except Exception:
                    value = ""

            value = self._clean_seller_name(
                self._fix_text_encoding(value)
            )
            if value:
                return value, href
            if href and not fallback[1]:
                fallback = ("", href)

        return fallback

    def _fix_text_encoding(self, value: str) -> str:
        value = value or ""
        for encoding in ("cp1251", "latin1"):
            try:
                fixed = value.encode(encoding).decode("utf-8")
                if fixed != value:
                    return fixed
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return value

    def _clean_seller_name(self, value: str) -> str:
        value = re.sub(r"\s+", " ", value or "").strip()
        for suffix in ("Перейти в магазин", "В магазин"):
            value = value.replace(suffix, "").strip()
        return value

    def _extract_prices(self, price_text: str) -> List[int]:
        values = []
        for line in (price_text or "").splitlines():
            number = self._extract_price_number(line)
            if number and number not in values:
                values.append(number)
        return values
    
    def _parse_json_response(self, article: str, json_content: str) -> ProductInfo:
        try:
            data = json.loads(json_content)
            
            if 'widgetStates' not in data:
                return ProductInfo(article=article, error="Отсутствует widgetStates в ответе")
            
            widget_states = data['widgetStates']
            product_info = ProductInfo(article=article)
            
            # Ищем информацию о товаре в webStickyProducts
            sticky_product_data = self._find_sticky_product_data(widget_states)
            if sticky_product_data:
                product_info.name = sticky_product_data.get('name', '')
                product_info.image_url = sticky_product_data.get('coverImageUrl', '')
                
                # Информация о продавце
                seller_info = sticky_product_data.get('seller', {})
                product_info.company_name = seller_info.get('name', '')
                product_info.company_inn = seller_info.get('inn', '')
                
                # Извлекаем ID и ссылку продавца
                seller_link = seller_info.get('link', '')
                if seller_link:
                    # Ищем seller_id в разных форматах: /seller/123456/ или /seller/name-123456/
                    seller_id = re.search(r'/seller/(?:[^/]*-)?(\d+)/?', seller_link)
                    if seller_id:
                        product_info.seller_id = seller_id.group(1)
                        product_info.seller_link = f"https://ozon.ru/seller/{seller_id.group(1)}"
                        logger.debug(f"Найден seller_id из sticky_product_data: {product_info.seller_id}")
                    else:
                        logger.debug(f"Не удалось извлечь seller_id из ссылки: {seller_link}")
            
            # Резервный поиск seller_id во всём JSON, если не нашли в sticky_product_data
            if not product_info.seller_id:
                raw_data = json.dumps(widget_states)
                # Ищем все возможные варианты seller ссылок
                seller_matches = re.findall(r'/seller/(?:[^/]*-)?(\d+)/?', raw_data)
                if seller_matches:
                    # Берём первый найденный seller_id
                    product_info.seller_id = seller_matches[0]
                    product_info.seller_link = f"https://ozon.ru/seller/{seller_matches[0]}"
                    logger.info(f"Найден seller_id через резервный поиск для товара {article}: {product_info.seller_id} (всего найдено: {len(seller_matches)})")
                else:
                    logger.warning(f"seller_id не найден ни в sticky_product_data, ни в резервном поиске для товара {article}")
            
            # Ищем информацию о ценах в webPrice
            price_data = self._find_price_data(widget_states)
            if price_data:
                product_info.card_price = self._extract_price_number(price_data.get('cardPrice', ''))
                product_info.price = self._extract_price_number(price_data.get('price', ''))
                product_info.original_price = self._extract_price_number(price_data.get('originalPrice', ''))
            
            # Проверяем, что получили основную информацию
            if product_info.name or product_info.card_price:
                product_info.success = True
            else:
                product_info.error = "Не найдена основная информация о товаре"
            
            return product_info
            
        except json.JSONDecodeError as e:
            return ProductInfo(article=article, error=f"Ошибка парсинга JSON: {str(e)}")
        except Exception as e:
            return ProductInfo(article=article, error=f"Ошибка обработки данных: {str(e)}")
    
    def _find_sticky_product_data(self, widget_states: Dict) -> Optional[Dict]:
        for key, value in widget_states.items():
            if key.startswith('webStickyProducts-') and isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    continue
        return None
    
    def _find_price_data(self, widget_states: Dict) -> Optional[Dict]:
        for key, value in widget_states.items():
            if key.startswith('webPrice-') and isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    continue
        return None
    
    def _extract_price_number(self, price_str: str) -> int:
        if not price_str:
            return 0
        try:
            cleaned = re.sub(r'[^\d]', '', str(price_str))
            return int(cleaned) if cleaned else 0
        except:
            return 0
    
    def close(self):
        if self.selenium_manager:
            self.selenium_manager.close()
        logger.info(f"Воркер {self.worker_id} закрыт")

class OzonProductParser:
    
    def __init__(self, max_workers: int = 5, user_id: str = None, headless: bool = True):
        self.max_workers = max_workers
        self.user_id = user_id
        self.headless = headless
        self.results: List[ProductInfo] = []
        logger.info(f"Парсер товаров инициализирован с макс {max_workers} воркерами для пользователя {user_id}")
    
    def parse_products(self, product_links: Dict[str, str]) -> List[ProductInfo]:
        # Сохраняем ссылки для использования в воркерах
        self.product_links = product_links
        
        articles = []
        for url in product_links.keys():
            article = self._extract_article_from_url(url)
            if article:
                articles.append(article)
        
        if not articles:
            logger.error("Не найдено артикулов для парсинга")
            return []
        
        # Получаем количество воркеров от менеджера ресурсов
        if self.user_id:
            allocated_workers = resource_manager.start_parsing_session(
                self.user_id, 'products', len(articles)
            )
        else:
            allocated_workers = self._calculate_optimal_workers(len(articles))

        # Несколько параллельных Chrome-сессий быстро вызывают блокировку Ozon.
        allocated_workers = 1
        
        logger.info(f"Начало парсинга {len(articles)} товаров с {allocated_workers} воркерами для пользователя {self.user_id}")
        
        if allocated_workers == 1:
            return self._parse_single_worker(articles)
        else:
            return self._parse_multiple_workers(articles, allocated_workers)
    
    def _extract_article_from_url(self, url: str) -> str:
        try:
            match = re.search(r'/product/(?:[^/]+-)?(\d+)/?', url)
            return match.group(1) if match else ""
        except Exception:
            return ""
    
    def _parse_single_worker(self, articles: List[str]) -> List[ProductInfo]:
        worker = ProductWorker(1, headless=self.headless)
        try:
            worker.initialize()
            return worker.parse_products(articles, self.product_links)
        finally:
            worker.close()
    
    def _calculate_optimal_workers(self, total_links: int) -> int:
        if total_links <= 10:
            return 1
        elif total_links <= 25:
            return 2
        elif total_links <= 50:
            return 3
        else:
            return min(5, self.max_workers)  # Максимум 5 воркеров
    
    def _parse_multiple_workers(self, articles: List[str], num_workers: int) -> List[ProductInfo]:
        chunks = self._distribute_articles(articles, num_workers)
        
        # Логируем распределение
        for i, chunk in enumerate(chunks):
            if chunk:
                logger.info(f"Воркер {i+1}: {len(chunk)} товаров")
        
        all_results = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_worker = {}
            
            for i, chunk in enumerate(chunks):
                if chunk:
                    future = executor.submit(self._worker_task_with_retry, i + 1, chunk)
                    future_to_worker[future] = i + 1
            
            for future in concurrent.futures.as_completed(future_to_worker):
                worker_id = future_to_worker[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    logger.info(f"Воркер {worker_id} завершил работу с {len(results)} товарами")
                except Exception as e:
                    logger.error(f"Ошибка воркера {worker_id}: {e}")
        
        return self._sort_results_by_original_order(all_results, articles)
    
    def _distribute_articles(self, articles: List[str], num_workers: int) -> List[List[str]]:
        chunks = [[] for _ in range(num_workers)]
        
        for i, article in enumerate(articles):
            worker_index = i % num_workers
            chunks[worker_index].append(article)
        
        return chunks
    
    def _worker_task_with_retry(self, worker_id: int, articles: List[str]) -> List[ProductInfo]:
        max_worker_retries = 3
        for attempt in range(max_worker_retries):
            worker = ProductWorker(worker_id, headless=self.headless)
            try:
                worker.initialize()
                results = worker.parse_products(articles, self.product_links)
                return results
            except Exception as e:
                if "Access blocked" in str(e) and attempt < max_worker_retries - 1:
                    logger.warning(
                        f"Воркер {worker_id} заблокирован, пересоздаем (попытка {attempt + 1}/3)"
                    )
                    time.sleep(15)     
                    continue
                else:
                    raise
            finally:
                # Гарантируем закрытие воркера в любом случае
                worker.close()
        return []
    
    def _sort_results_by_original_order(self, results: List[ProductInfo], original_articles: List[str]) -> List[ProductInfo]:
        result_dict = {result.article: result for result in results}
        return [result_dict.get(article, ProductInfo(article=article, error="Не обработан")) 
                for article in original_articles]
    
    def cleanup(self):
        """Принудительная очистка всех ресурсов парсера"""
        logger.info("Очистка ресурсов парсера товаров...")
        # Даем время на завершение всех потоков
        time.sleep(2)
        logger.info("Ресурсы парсера товаров очищены")
