# Ozon Parser

Парсер товаров Ozon.ru с GUI и Telegram-ботом. Многопоточный сбор данных о товарах и продавцах.

## Возможности

- ✅ Парсинг до 10,000 товаров из категорий
- ✅ Данные о продавцах: ИНН, рейтинг, статистика
- ✅ Многопоточность (до 5 воркеров)
- ✅ Экспорт в Excel + JSON
- ✅ Telegram бот для управления
- ✅ GUI интерфейс

## Установка

```bash
git clone https://github.com/trash-taste/Ozon_bot_kaspi.git
cd Ozon_bot_kaspi
pip install -r requirements.txt
```

**Требования**: Python 3.11, Chrome браузер

## Запуск

```bash
python main.py          # GUI
python bot.py           # Только Telegram бот
python app.py           # CLI
```

### Сравнение с Kaspi

После парсинга можно автоматически найти похожие товары на Kaspi и создать
Excel-отчет с прибылью и ROI:

```bash
python app.py "https://ozon.ru/category/..." --count 10 --headed --compare-kaspi
```

Отчеты сохраняются в папку `reports/`. По умолчанию используются цены Kaspi
для Алматы. Другой город можно задать через переменную `KASPI_CITY_ID`.

Повторное сравнение уже сохраненного JSON без нового запуска Ozon:

```bash
python app.py --compare-json "output/category.json" --min-roi 5 --min-profit 3000
```

## Конфигурация

Создайте `config.txt`:

```
TELEGRAM_BOT_TOKEN=сюда_токен_от_BotFather
TELEGRAM_CHAT_ID=your_user_id
USER_your_user_id_SELECTED_FIELDS=name,company_name,inn,price
USER_your_user_id_FIELD_ORDER=name,company_name,inn,price
USER_your_user_id_DEFAULT_COUNT=500
```

## Разделение процессов

В Docker Compose бот и парсер запускаются отдельно:

- `bot` — Telegram, FSM, настройки пользователей и постановка задач в очередь.
- `parser-worker` — отдельный процесс с Chromium/Playwright, который забирает
  задачи из `runtime/`, запускает парсер, делает Excel и сравнение цен.

Парсеры в `src/parsers/` при этом не меняются. Связь между процессами идет
через файловую очередь `runtime/`, а результаты остаются в `output/` и
`reports/`.

## Docker Compose на Ubuntu 24.04

Compose запускает два процесса: Telegram-бот и отдельный worker парсинга. GUI
`main.py` внутри контейнера не используется.

### 1. Установите Docker

Добавьте официальный Docker apt-репозиторий и установите Docker Engine с
Compose plugin по
[официальной инструкции для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
После добавления репозитория устанавливаются пакеты:

```bash
sudo apt install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo docker run hello-world
```

### 2. Клонируйте и настройте проект

```bash
git clone https://github.com/trash-taste/Ozon_bot_kaspi.git
cd Ozon_bot_kaspi

cp config.example.txt config.txt
nano config.txt
chmod 600 config.txt

mkdir -p output reports logs runtime
```

В `config.txt` укажите реальный токен BotFather и Telegram User ID. Этот файл
игнорируется Git и монтируется в контейнер с правом записи, потому что бот
сохраняет в нём пользовательские настройки.

### 3. Запустите бота

```bash
sudo docker compose up -d --build
sudo docker compose logs -f bot parser-worker
```

Порты открывать не требуется: Telegram-бот работает через исходящие запросы.
Парсер работает в отдельном контейнере и забирает задания из общей очереди.

### Обновление

```bash
git pull --ff-only
sudo docker compose up -d --build --remove-orphans
```

### Управление

```bash
sudo docker compose ps
sudo docker compose restart bot
sudo docker compose restart parser-worker
sudo docker compose stop
```

### Ручной CLI-запуск

Тот же образ можно использовать без запуска второго постоянного сервиса:

```bash
sudo docker compose run --rm parser-worker \
  python -u app.py "https://ozon.kz/category/..." --count 10
```

Результаты сохраняются в смонтированные каталоги `output/` и `reports/`,
логи — в `logs/`.

> Для VPS рекомендуется минимум 2 vCPU и 4 ГБ RAM. IP-адреса дата-центров
> могут чаще попадать под антибот-защиту Ozon.

### Мобильный прокси для Ozon

Для VPS лучше брать мобильный прокси Казахстана с доступом по whitelist IP
без логина и пароля. Тогда Chromium в контейнере подключается напрямую через
`--proxy-server`.

Создайте рядом с `compose.yaml` файл `.env`:

```bash
OZON_PROXY_URL=http://host:port
OZON_PROXY_BYPASS=localhost,127.0.0.1
```

Поддерживаются форматы `host:port`, `http://host:port`,
`https://host:port`, `socks4://host:port`, `socks5://host:port`.
Прокси с логином и паролем через `OZON_PROXY_URL` не включайте; для такого
варианта нужен отдельный механизм авторизации браузера.

После изменения `.env` перезапустите worker:

```bash
sudo docker compose up -d --build parser-worker
```

## Доступные поля

| Поле | Описание |
|------|----------|
| `article` | Артикул товара |
| `name` | Название товара |
| `seller_name` | Имя продавца |
| `company_name` | Название компании |
| `inn` | ИНН продавца |
| `card_price` | Цена по карте |
| `price` | Текущая цена |
| `original_price` | Старая цена |
| `product_url` | Ссылка на товар |
| `image_url` | Ссылка на изображение |
| `orders_count` | Количество заказов |
| `reviews_count` | Количество отзывов |
| `average_rating` | Средний рейтинг |
| `working_time` | Дата регистрации |

## Особенности

- **Обход блокировки**: 3 драйвера × 3 попытки = 9 попыток обхода антибота
- **Резервный поиск seller_id**: если не найден в основных данных, ищет по всему JSON
- **Умное управление ресурсами**: автоматическое распределение воркеров между пользователями
- **Headless режим**: настраивается в `src/config/settings.py`

## Структура вывода

```
output/
└── category_name_DD.MM.YYYY_HH-MM-SS/
    ├── links_*.json               # Собранные ссылки
    ├── category_*.json            # Данные в JSON
    └── category_*.xlsx            # Excel отчет
```

## Troubleshooting

**Парсинг селлеров не работает?**
- Проверьте что в `SELECTED_FIELDS` есть хотя бы одно поле селлера: `inn`, `company_name`, `seller_name`, `orders_count`, `reviews_count`, `average_rating`, `working_time`

**Блокировка Ozon?**
- Установите `HEADLESS = False` в `src/config/settings.py`
- Используйте прокси
- Увеличьте задержки между запросами

## Лицензия

MIT License
