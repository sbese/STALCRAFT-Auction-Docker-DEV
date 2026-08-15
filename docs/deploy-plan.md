# План раскатки на бесплатный стек

Цель: постоянно доступный демо-стенд проекта без затрат — бэкенд со сборщиком истории работает 24/7, фронтенд раздаётся статикой, периодика и keep-alive живут на внешнем cron-сервисе.

План опирается на архитектуру из PR #1 (фоновые задачи в веб-процессе вместо Celery/Redis): раскатывать можно только после его мержа.

## Целевая схема

| Компонент | Сервис | Тариф | Роль |
|---|---|---|---|
| Frontend (React/Vite) | [Vercel](https://vercel.com) | Hobby (free) | статика после `npm run build` |
| Backend (Django + сборщик) | [Render](https://render.com) | Free Web Service | API + фоновый сборщик в одном процессе |
| PostgreSQL | [Neon](https://neon.tech) | Free | база данных |
| Периодика и keep-alive | [cron-job.org](https://cron-job.org) | free | три задания по расписанию |
| Мониторинг | [UptimeRobot](https://uptimerobot.com) | free | алерт при смерти сборщика |

## Предусловия

- [ ] PR #1 (замена Celery на in-process раннер) смержен в `main`.
- [ ] На руках `STALCRAFT_CLIENT_ID` и `STALCRAFT_CLIENT_SECRET` (выдаются разработчиками игры).
- [ ] Сгенерирован `CRON_SECRET` — длинная случайная строка (например, `openssl rand -hex 32`).
- [ ] Придуманы прод-значения `SECRET_KEY`, `DJANGO_SUPERUSER_USERNAME/PASSWORD` (не из README).

## Этап 1. База данных на Neon

1. Создать проект в Neon (регион ближе к Render-региону бэкенда).
2. Взять **direct (unpooled) connection string** — хост без суффикса `-pooler`.
   Это важно: сборщик держит session-level advisory-блокировку PostgreSQL на долгоживущем соединении; через транзакционный pooler (pgbouncer) session-блокировки не работают. Прямых соединений при одном инстансе и `--workers 1` нужно меньше десятка — лимитов Neon хватает с запасом.
3. Разложить connection string на переменные: `POSTGRES_DATABASE_NAME`, `POSTGRES_USERNAME`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` (5432).

Приёмка: `psql` (или любой клиент) подключается по этим параметрам.

Известное ограничение: сборщик активен круглосуточно, поэтому compute Neon почти не будет засыпать — основной расход бесплатной квоты compute-часов. Актуальные лимиты проверить на [neon.tech/pricing](https://neon.tech/pricing); если квоты не хватит на месяц — запасной вариант тем же интерфейсом: Supabase (Postgres, free tier).

## Этап 2. Бэкенд на Render

1. New → Web Service → подключить репозиторий `sbese/STALCRAFT-Auction-Docker-DEV`, ветка `main`.
2. Настройки: Environment **Docker**, Root Directory **`backend`**. `PORT` Render задаёт сам — entrypoint его уважает; переменную `RENDER` Render выставляет автоматически, по ней entrypoint включает прод-режим (collectstatic + gunicorn --workers 1).
3. Переменные окружения:
   - `SECRET_KEY`, `DEBUG=False`
   - `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_PASSWORD`
   - `STALCRAFT_CLIENT_ID`, `STALCRAFT_CLIENT_SECRET`, `STALCRAFT_DATABASE_LISTING`
   - `POSTGRES_*` из этапа 1
   - `CRON_SECRET`
   - `CORS_ALLOWED_ORIGINS` — пока `http://localhost:5173`, дополним на этапе 3
4. Health Check Path: `/auction/api/health/`.

Приёмка:
- `GET https://<render-домен>/auction/api/health/` → `{"status": "ok", "collector_alive": true}`;
- `GET https://<render-домен>/auction/api/health/collector/` → `200 {"status": "collecting"}`;
- в Render Logs видно `Блокировка сборщика получена - начинаю сбор` и строки `СОХРАНЕНО N записей`.

## Этап 3. Фронтенд на Vercel

1. New Project → тот же репозиторий, Root Directory **`frontend`**, Framework **Vite** (build `npm run build`, output `dist`).
2. Переменная окружения: `VITE_API_BASE_URL=https://<render-домен>/auction/api`.
3. После первого деплоя вписать vercel-домен в `CORS_ALLOWED_ORIGINS` на Render (через запятую, с `https://`) и передеплоить бэкенд.

Приёмка: открыть `https://<vercel-домен>/items` — список предметов грузится, график продаж предмета открывается, в консоли браузера нет CORS-ошибок.

## Этап 4. Периодика на cron-job.org

Три задания, у каждого HTTP-заголовок `X-Cron-Token: <CRON_SECRET>` (не query-параметр — секрет в URL попадает в логи):

| URL | Расписание | Зачем |
|---|---|---|
| `https://<render-домен>/auction/api/cron/history_collector/` | `*/10 * * * *` | keep-alive: не даёт инстансу заснуть и перезапускает сборщик после падения |
| `https://<render-домен>/auction/api/cron/sync_github_items_daily/` | `0 16 * * *` | ежедневная синхронизация предметов |
| `https://<render-домен>/auction/api/cron/delete_old_sales/` | `0 3 * * 1` | еженедельная чистка старых продаж |

Приёмка: ручной запуск каждого задания в cron-job.org возвращает 200 `{"ok": true, ...}`; без заголовка тот же URL отвечает 403.

## Этап 5. Мониторинг на UptimeRobot

1. Монитор HTTP(s) на `https://<render-домен>/auction/api/health/collector/`, интервал 5 минут.
2. Алерт на email при статусе != 200.

Семантика ответов: `200 collecting` — всё работает; `200 standby` — короткое штатное состояние во время деплоя; `503 dead` / `503 lock_error` — тревога (сборщик мёртв или база недоступна). Ложных алертов на деплой не будет — standby отдаёт 200.

Приёмка: монитор зелёный; тестовая пауза сервиса на Render (Suspend) приводит к алерту.

## Этап 6. Приёмочный smoke всего стека

- [ ] `/items` на Vercel показывает предметы, график цен строится.
- [ ] `/control-center` пускает под прод-суперпользователем; страница «Фоновые задачи» показывает сборщик в статусе «работает», прогресс `N/…` растёт.
- [ ] Ручной запуск `sync_github_items_daily` из админки завершается `FINISH` в логах.
- [ ] «Остановить» сборщик → readiness через ~минуту отдаёт `503 dead` → cron-пинг (или кнопка «Запустить») возвращает `collecting`.
- [ ] Через сутки: в UptimeRobot нет алертов, история продаж пополняется, cron-задания в cron-job.org зелёные.

## Откат

- **Render**: Rollback на предыдущий деплой в один клик (Deploys → Rollback); переменные окружения деплоем не затрагиваются.
- **Vercel**: Instant Rollback на предыдущий билд.
- **База**: миграции этого проекта аддитивные, деструктивных шагов в раскатке нет; на крайний случай у Neon есть point-in-time restore в пределах free-квоты.
- Полный откат стенда = Suspend сервиса на Render + пауза заданий cron-job.org; данные в Neon остаются.

## Известные ограничения

- Бесплатный инстанс Render засыпает без входящего трафика — от этого держим пинг раз в 10 минут; при первом запросе после сна ответ занимает десятки секунд.
- Сборщик не даёт Neon засыпать → расходуется compute-квота (см. этап 1).
- `entrypoint.sh` выполняет `makemigrations` на старте — рабочая, но нестрогая практика (миграции должны приезжать из репозитория). Осознанно не трогаем в рамках раскатки; кандидат в отдельную задачу.
- Один инстанс = один сборщик: при масштабировании больше одного инстанса лишние уходят в standby по advisory-блокировке — это штатно, но и смысла в горизонтальном масштабировании сборщика нет.
