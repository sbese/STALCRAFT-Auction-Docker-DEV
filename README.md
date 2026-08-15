# STALCRAFT-Auction-Docker

## Оглавление
1. [Описание проекта](#описание-проекта)
2. [Функционал](#функционал)
3. [Доступные страницы](#доступные-страницы)
4. [Требования](#требования)
5. [Установка и запуск](#установка-и-запуск)
   1. [Клонирование репозитория](#1-клонирование-репозитория)
   2. [Настройка переменных окружения](#2-настройка-переменных-окружения)
   3. [Запуск с помощью Docker](#3-запуск-с-помощью-docker)
   4. [Миграции базы данных и collectstatic](#4-миграции-базы-данных-и-collectstatic)
   5. [Фоновые задачи](#5-фоновые-задачи)
6. [Остановка проекта](#остановка-проекта)
7. [Структура проекта](#структура-проекта)
8. [API интеграция](#api-интеграция)

---

## Описание проекта

**STALCRAFT-Auction-Docker** — это веб-приложение для управления аукционом, интегрированное с игровым проектом STALCRAFT.  
Проект разделен на два независимых слоя:
- **Backend (Django)**: API + сбор и хранение данных.
- **Frontend (React + Vite)**: современный интерфейс для работы с предметами и графиками.

---

### Функционал:
- **Синхронизация данных**: Автоматическое получение данных о продажах из API игрового проекта.
- **Просмотр списка предметов**: Удобный интерфейс для просмотра всех доступных предметов.
- **Поиск и фильтрация**: Возможность искать предметы по названию, категории и другим параметрам.
- **История продаж**: Отслеживание истории продаж предметов. Просмотр до 5 000 последних продаж для одного предмета с удобным фильтром по цене и редкости предмета.
- **Сжатие ответов**: Включено gzip-сжатие API-ответов (Django `GZipMiddleware`) и brotli/gzip для статических файлов через WhiteNoise.
- **Интеграция цен в игровой интерфейс**: Пользователь может загрузить файл предметов из игры (`ru.lang`) и получает изменённый файл, где для каждого предмета указана средняя цена за выбранный период. Эти цены будут отображаться рядом с предметами в игре.

---

### Доступные страницы:
- [http://localhost:5173/items](http://localhost:5173/items) - React-страница списка предметов с поиском, категориями и подкатегориями.
- [http://localhost:5173/items/<item_id>](http://localhost:5173/items/9mmq) - React-страница графика истории продаж предмета.
- [http://localhost:5173/upload-lang](http://localhost:5173/upload-lang) - Загрузка ru.lang и получение файла со средними ценами.
- [http://localhost:5173/control-center](http://localhost:5173/control-center) - Админ-панель фоновых задач (сборщик истории, запуск и остановка задач, live-логи, расписание).

---

## Требования

Для запуска проекта вам понадобятся:
- [**Docker**](https://www.docker.com/get-started/)

---

## Установка и запуск

### 1. Клонирование репозитория
Склонируйте репозиторий на ваш локальный компьютер:
```bash
git clone https://github.com/AkihiroAck/STALCRAFT-Auction-Docker.git
cd STALCRAFT-Auction-Docker
```

### 2. Настройка переменных окружения
Создайте файл `.env` в корневой папке (рядом с `docker-compose.yml`) и настройте его под ваши нужды:
```
# Django
SECRET_KEY=django-insecure-key
DEBUG = False

DJANGO_SUPERUSER_USERNAME=admin
DJANGO_SUPERUSER_PASSWORD=1234

# STALCRAFT
STALCRAFT_CLIENT_ID=SECRET_ID
STALCRAFT_CLIENT_SECRET=SECRET_KEY
STALCRAFT_DATABASE_LISTING=https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main/ru/listing.json

# PostgreSQL
POSTGRES_DATABASE_NAME=POSTGRESQL_DATABASE_NAME
POSTGRES_USERNAME=db_user
POSTGRES_PASSWORD=db_password
POSTGRES_HOST=db
POSTGRES_PORT=5432

# pgAdmin
PGADMIN_EMAIL=admin@admin.com
PGADMIN_PASSWORD=1234

# Секрет для запуска задач внешним cron (например, cron-job.org)
CRON_SECRET=any-long-random-string
```

Примечание:
- `STALCRAFT_CLIENT_ID` и `STALCRAFT_CLIENT_SECRET` - Получаются от разработчиков игры. Нужны для получения историй продаж (`history_collector`).
- `STALCRAFT_DATABASE_LISTING` - URL до [listing.json](https://github.com/EXBO-Studio/stalcraft-database/blob/main/ru/listing.json) (список предметов). Рекомендуется `raw .../main/ru/listing.json`, чтобы всегда получать актуальные категории и пути.

### 3. Запуск с помощью Docker
Для запуска всех сервисов выполните:
```bash
docker-compose build
docker-compose up
```

После успешного запуска:
- Frontend будет доступен по адресу: [localhost:5173](http://localhost:5173)
- Backend API будет доступен по адресу: [localhost:8000/auction/api](http://localhost:8000/auction/api/items/)
- pgAdmin: [localhost:5050](http://localhost:5050) (логин и пароль указаны в `.env`)

Примечание для разработки фронта:
- Изменения в файлах `frontend/src/*` применяются автоматически в Docker (hot-reload) без пересборки образа.
- Если контейнер `frontend` запущен, достаточно просто сохранить файл — страница обновится сама.

Доступ в админ-панель:
- Выполняется через Django-пользователя с правами `is_staff`/`superuser`.
- Учетная запись создается из переменных `DJANGO_SUPERUSER_USERNAME` и `DJANGO_SUPERUSER_PASSWORD` при старте backend.

Мониторинг фоновых задач в админ-панели (`/control-center`):
- Отображает состояние сборщика истории (прогресс цикла, количество циклов, ошибки), активные задачи и список задач для ручного запуска.
- Поддерживает кооперативную остановку сборщика (задача завершается на ближайшей контрольной точке).
- Логи показываются в реальном времени.

### 4. Миграции базы данных и collectstatic
Миграции выполняются автоматически с помощью [`backend/entrypoint.sh`](backend/entrypoint.sh).
В прод-режиме (`RENDER` или `USE_GUNICORN=1`) дополнительно выполняется collectstatic и запускается gunicorn:
```bash
python manage.py makemigrations
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn scaw.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 8
```

### 5. Фоновые задачи
Фоновые задачи выполняются в потоках веб-процесса без Celery и Redis (см. `backend/scaw/auction/taskrunner.py`).
Поэтому сервер должен работать в один процесс: `gunicorn --workers 1` (уже настроено в `entrypoint.sh`).

- **Сборщик истории (`history_collector`)** - Вечный цикл: делает запросы на сервер игры и получает историю последних 200 продаж каждого предмета, сохраняет только новые продажи. Стартует автоматически при запуске backend (переменная `COLLECTOR_AUTOSTART=1` в `entrypoint.sh`).
- **Синхронизация данных (`sync_github_items_daily`)** - Проверяет наличие новых или обновленных предметов с помощью api запроса `STALCRAFT_DATABASE_LISTING` и сохраняет их в базу данных.
- **Удаление старых продаж (`delete_old_sales`)** - Чистит устаревшие записи истории продаж.

Периодический запуск выполняет внешний cron-сервис (например, [cron-job.org](https://cron-job.org)) через защищенный эндпоинт. Токен передается заголовком:
```
GET /auction/api/cron/<task>/
X-Cron-Token: <CRON_SECRET>
```
(Query-параметр `?token=<CRON_SECRET>` поддерживается как fallback для cron-сервисов без кастомных заголовков, но секрет в URL попадает в access-логи - используйте заголовок, где возможно.)

Рекомендуемое расписание:
- `sync_github_items_daily` - ежедневно в 16:00 UTC (`0 16 * * *`);
- `delete_old_sales` - по понедельникам в 03:00 UTC (`0 3 * * 1`);
- `history_collector` - каждые 10 минут (`*/10 * * * *`): пинг не дает бесплатному хостингу заснуть и перезапускает сборщик, если тот упал.

Health-эндпоинты:
- `GET /auction/api/health/` - liveness веб-сервера, всегда 200: подходит для keep-alive пингов;
- `GET /auction/api/health/collector/` - readiness сборщика для мониторинга (UptimeRobot и т.п.): `503 dead` - поток сборщика мертв; `503 lock_error` - отказ lock-инфраструктуры (например, недоступна база), сбор не идет; `200 standby` - штатное короткое состояние во время деплоя, когда блокировку еще держит старый инстанс; `200 collecting` - все работает.

Одновременная работа двух сборщиков (например, при zero-downtime деплое Render, когда старый и новый инстансы живут параллельно) исключена advisory-блокировкой PostgreSQL: сборщик без блокировки ждет в standby и подхватывает работу, когда держатель умирает. Штатное ожидание (лок занят) и отказ инфраструктуры (базу не достать) - разные состояния: первое отдает 200, второе - 503.

Тесты бэкенда (`backend/scaw/auction/tests.py`, включая интеграционные тесты advisory-блокировки на реальном PostgreSQL) и сборка фронтенда гоняются в CI на каждый PR ([.github/workflows/ci.yml](.github/workflows/ci.yml)); локально: `docker compose exec backend sh -c "cd scaw && python manage.py test auction"`.

### Деплой на Render (бесплатный тариф)
1. Создайте Web Service из этого репозитория (Environment: Docker, Root Directory: `backend`).
2. Задайте переменные окружения из раздела выше (`SECRET_KEY`, `DEBUG=False`, `POSTGRES_*` от внешней базы, например [Neon](https://neon.tech), `STALCRAFT_*`, `DJANGO_SUPERUSER_*`, `CRON_SECRET`, `CORS_ALLOWED_ORIGINS` с адресом фронтенда).
3. Фронтенд задеплойте на Vercel/Netlify (`npm run build` в `frontend/`, переменная `VITE_API_BASE_URL=https://<ваш-сервис>.onrender.com/auction/api`).
4. На cron-job.org настройте три задания из раздела [Фоновые задачи](#5-фоновые-задачи) - пинг каждые 10 минут обязателен, иначе бесплатный инстанс Render засыпает и сборщик останавливается.

---

## Остановка проекта
Для остановки выберите один из вариантов:

1. Для остановки всех контейнеров выполните:
```bash
docker-compose stop
```

2. Для остановки и удаления всех контейнеров выполните:
```bash
docker-compose down
```

3. Для остановки и удаления всех контейнеров и volume выполните:  
```bash
docker-compose down -v
```

---

## Структура проекта
- **backend/** — исходный код серверной части.
  - **scaw/** — корневая Django-папка проекта.
    - **auction/** — приложение для работы с аукционом (модели, вьюхи, логика).
    - **scaw/** — настройки Django (urls.py, settings.py, wsgi.py и др.).
    - **static/** — статические файлы проекта (CSS, JS, изображения).
    - **templates/** — HTML-шаблоны для отображения страниц.
    - **manage.py** — основной скрипт для управления Django (миграции, запуск сервера, команды).
  - **auction_item.sql** — Backup с начальными данными предметов, используется для первичного наполнения базы при первом запуске.
  - **auction_salehistory.sql** — Backup историей продаж предметов, используется для первичного наполнения базы при первом запуске.
  - **Dockerfile** — инструкция сборки Docker-образа для backend.
  - **entrypoint.sh** — скрипт запуска backend (миграции, суперпользователь, runserver/gunicorn + автостарт сборщика).
  - **requirements.txt** — список зависимостей Python-пакетов.
- **frontend/** — отдельный React-клиент.
  - **src/pages/** — страницы списка предметов, графика и загрузки ru.lang.
  - **src/components/** — переиспользуемые UI-компоненты (категории, поиск).
  - **src/api.js** — клиент для работы с Django API.
- **docker-compose.yml** — конфигурация для запуска всех сервисов (PostgreSQL, backend, frontend, pgAdmin) через Docker Compose.
- **.env** — файл с переменными окружения (секреты, ключи, настройки БД и др.).

---

## API интеграция
Приложение интегрируется с игровым проектом STALCRAFT через API. Данные о продажах собирает цикличный фоновый сборщик `history_collector`. Для настройки API используйте переменные `STALCRAFT_CLIENT_ID`, `STALCRAFT_CLIENT_SECRET` в `.env`. Без них сборщик будет получать ошибку - `{'title': 'Unauthorized', 'status': 401, 'details': {}}`

---

## Производительность API продаж

- Максимальный лимит API продаж ограничен до `5000` (backend и frontend).
- Для API-ответов включено gzip-сжатие через Django middleware.
- Для статических файлов включена поддержка gzip/brotli через WhiteNoise.
- Для reverse proxy подготовлен пример конфига с gzip+brotli: `deploy/nginx/api-proxy.conf`.

### Индексация SaleHistory

- Добавлен индекс для быстрого чтения последних продаж по предмету: `(item_id, time DESC)`.
- Миграция: `backend/scaw/auction/migrations/0001_salehistory_item_time_index.py`.
