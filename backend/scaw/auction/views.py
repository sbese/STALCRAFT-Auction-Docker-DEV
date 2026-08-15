import datetime
import hmac
import json
import os
from django.db import connection, models
from django.utils import timezone
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.conf import settings

from .models import SaleHistory, Item
from .constants import RANK_COLORS
from .taskrunner import COLLECTOR_TASK_NAME, runner


DEFAULT_LIMIT = 100
MAX_LIMIT = 2000

# Периодические запуски выполняет внешний cron (например, cron-job.org),
# дергая /auction/api/cron/<task>/?token=<CRON_SECRET> по этому расписанию.
SCHEDULE_HINTS = [
    {
        'name': 'sync-github-items-everyday',
        'task': 'sync_github_items_daily',
        'cron': '0 16 * * *',
        'description': 'Ежедневная синхронизация предметов из GitHub (16:00 UTC).',
    },
    {
        'name': 'delete-old-sales-weekly',
        'task': 'delete_old_sales',
        'cron': '0 3 * * 1',
        'description': 'Еженедельное удаление старых продаж (понедельник, 03:00 UTC).',
    },
    {
        'name': 'keep-alive-and-collector',
        'task': COLLECTOR_TASK_NAME,
        'cron': '*/10 * * * *',
        'description': 'Пинг каждые 10 минут: не дает бесплатному инстансу заснуть и перезапускает сборщик, если он упал.',
    },
]


def _serialize_item(item):
    return {
        'id': item.id,
        'item_id': item.item_id,
        'name': item.name,
        'category': item.category,
        'color': RANK_COLORS.get(item.color, RANK_COLORS['DEFAULT']),
        'rank': item.color,
        'has_sales': bool(getattr(item, 'has_sales', False)),
        'icon_url': f"https://github.com/EXBO-Studio/stalcraft-database/raw/main/ru/icons/{item.category}/{item.item_id}.png",
    }


def _with_sales_flag(queryset):
    return queryset.annotate(
        has_sales=models.Exists(
            SaleHistory.objects.filter(item_id=models.OuterRef('pk'))
        )
    )


def _build_category_tree(items_data):
    tree = {}

    for item in items_data:
        category_parts = item['category'].split('/')
        node = tree

        for part in category_parts:
            node.setdefault(part, {'_children': {}, '_count': 0})
            node[part]['_count'] += 1
            node = node[part]['_children']

    def pack(node):
        packed = []

        for name in sorted(node.keys()):
            data = node[name]
            packed.append({
                'name': name,
                'count': data['_count'],
                'children': pack(data['_children']),
            })

        return packed

    return pack(tree)


def _get_json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _ensure_staff(request):
    if not request.user.is_authenticated:
        return JsonResponse({'detail': 'Authentication required'}, status=401)

    if not request.user.is_staff:
        return JsonResponse({'detail': 'Admin access required'}, status=403)

    return None


def _tail_lines(file_path, lines=200):
    if not os.path.exists(file_path):
        return []

    # Read only the file tail to avoid expensive full-file reads on frequent polling.
    max_bytes = 512 * 1024
    chunk_size = 8192

    with open(file_path, 'rb') as fp:
        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        read_size = min(file_size, max_bytes)
        data = b''

        while read_size > 0 and data.count(b'\n') <= lines:
            current_chunk = min(chunk_size, read_size)
            read_size -= current_chunk
            fp.seek(read_size)
            data = fp.read(current_chunk) + data

    content = data.decode('utf-8', errors='replace').splitlines()
    return content[-lines:]


@require_GET
def api_auth_me(request):
    user = request.user

    if not user.is_authenticated:
        return JsonResponse(
            {
                'authenticated': False,
                'is_staff': False,
                'is_superuser': False,
                'username': None,
            }
        )

    return JsonResponse(
        {
            'authenticated': True,
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser,
            'username': user.username,
        }
    )


@csrf_exempt
@require_POST
def api_auth_login(request):
    body = _get_json_body(request)
    username = body.get('username', '').strip()
    password = body.get('password', '')

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({'detail': 'Invalid username or password'}, status=400)

    if not user.is_staff:
        return JsonResponse({'detail': 'Admin access required'}, status=403)

    login(request, user)

    return JsonResponse(
        {
            'ok': True,
            'username': user.username,
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser,
        }
    )


@csrf_exempt
@require_POST
def api_auth_logout(request):
    logout(request)
    return JsonResponse({'ok': True})


@require_GET
def api_admin_tasks_overview(request):
    denied = _ensure_staff(request)
    if denied:
        return denied

    return JsonResponse(
        {
            'collector': runner.collector_status(),
            'running_tasks': runner.running_tasks(),
            'manual_tasks': runner.manual_task_names(),
            'collector_task_name': COLLECTOR_TASK_NAME,
            'schedule': SCHEDULE_HINTS,
            'cron_url_template': '/auction/api/cron/<task>/',
            'cron_auth_header': 'X-Cron-Token',
            'log_sources': ['app'],
        }
    )


@csrf_exempt
@require_POST
def api_admin_tasks_start(request):
    denied = _ensure_staff(request)
    if denied:
        return denied

    body = _get_json_body(request)
    task_name = body.get('task_name')

    if task_name != COLLECTOR_TASK_NAME and task_name not in runner.get_registry():
        return JsonResponse({'detail': 'Task is not allowed for manual start'}, status=400)

    result = runner.start_task(task_name)

    return JsonResponse(
        {
            'ok': True,
            'task_id': result['task']['id'],
            'task_name': task_name,
            'already_running': result['already_running'],
        }
    )


@csrf_exempt
@require_POST
def api_admin_tasks_stop(request):
    denied = _ensure_staff(request)
    if denied:
        return denied

    body = _get_json_body(request)
    task_id = body.get('task_id')

    if not task_id:
        return JsonResponse({'detail': 'task_id is required'}, status=400)

    try:
        task = runner.stop_task(task_id)
    except KeyError:
        return JsonResponse({'detail': 'Task not found'}, status=404)
    except ValueError:
        return JsonResponse({'detail': 'Task does not support stopping'}, status=400)

    return JsonResponse({'ok': True, 'task_id': task['id'], 'task_name': task['name']})


@require_GET
def api_admin_tasks_logs(request):
    denied = _ensure_staff(request)
    if denied:
        return denied

    source = request.GET.get('source', 'app')
    try:
        lines = int(request.GET.get('lines', 250))
    except (TypeError, ValueError):
        lines = 250

    lines = min(max(lines, 20), 2000)

    log_map = {
        'app': os.path.join(settings.BASE_DIR, 'logs.log'),
    }

    selected_path = log_map.get(source)
    if not selected_path:
        return JsonResponse({'detail': 'Unknown log source'}, status=400)

    content = _tail_lines(selected_path, lines=lines)

    return JsonResponse(
        {
            'source': source,
            'path': selected_path,
            'lines': content,
            'exists': os.path.exists(selected_path),
        }
    )


@require_GET
def api_health(request):
    """
    Liveness веб-сервера: всегда 200, пока процесс отвечает.
    Используется для keep-alive пингов бесплатного хостинга.
    Состояние сборщика здесь информационное; для мониторинга сборщика
    есть отдельный /api/health/collector/.
    """
    return JsonResponse(
        {
            'status': 'ok',
            'collector_alive': runner.collector_status()['alive'],
        }
    )


@require_GET
def api_health_collector(request):
    """
    Readiness сборщика истории для внешнего мониторинга.

    503 dead - поток сборщика мертв (cron-пинг /api/cron/history_collector/
    перезапустит его сам, но мониторинг должен видеть провал).
    503 lock_error - поток жив, но lock-инфраструктура отказала (например,
    недоступна база): сбор не идет, и не факт, что идет где-то еще.
    Детали ошибки намеренно не отдаются - эндпоинт без аутентификации,
    полный текст виден в /api/admin/tasks/overview/ и логах.
    200 collecting - сборщик работает и держит advisory-блокировку.
    200 standby - поток жив, но блокировку держит другой инстанс: штатное
    короткое состояние во время zero-downtime деплоя, тревоги не требует.
    """
    status = runner.collector_status()

    if not status['alive']:
        return JsonResponse({'status': 'dead', 'collector_alive': False}, status=503)

    if status['state'] == 'lock_error':
        return JsonResponse({'status': 'lock_error', 'collector_alive': True}, status=503)

    return JsonResponse(
        {
            'status': 'standby' if status['state'] == 'waiting_lock' else 'collecting',
            'collector_alive': True,
        }
    )


@csrf_exempt
def api_cron_task(request, task_name):
    """
    Запуск задачи внешним cron-сервисом (cron-job.org и т.п.).

    Защищен токеном CRON_SECRET: основной способ - заголовок X-Cron-Token,
    ?token=... поддерживается как fallback для сервисов без кастомных
    заголовков (секрет в URL попадает в логи - использовать осознанно).
    Для history_collector гарантирует, что сборщик запущен (перезапуск после падений).
    Задачи выполняются в фоне, ответ возвращается сразу.
    """
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'detail': 'Method not allowed'}, status=405)

    secret = settings.CRON_SECRET
    token = request.headers.get('X-Cron-Token') or request.GET.get('token', '')

    if not secret or not hmac.compare_digest(str(token), str(secret)):
        return JsonResponse({'detail': 'Invalid or missing token'}, status=403)

    if task_name != COLLECTOR_TASK_NAME and task_name not in runner.get_registry():
        return JsonResponse({'detail': 'Unknown task'}, status=404)

    result = runner.start_task(task_name)

    return JsonResponse(
        {
            'ok': True,
            'task_name': task_name,
            'task_id': result['task']['id'],
            'already_running': result['already_running'],
            'collector_alive': runner.collector_status()['alive'],
        }
    )


@require_GET
def api_items(request):
    search = request.GET.get('search', '').strip()
    category_prefix = request.GET.get('category', '').strip()
    page = request.GET.get('page', 1)
    try:
        page_size = int(request.GET.get('page_size', 120))
    except (TypeError, ValueError):
        page_size = 120

    page_size = min(max(page_size, 1), 500)

    items = _with_sales_flag(Item.objects.all()).order_by('name')

    if search:
        items = items.filter(
            models.Q(name__icontains=search)
            | models.Q(category__icontains=search)
            | models.Q(item_id__icontains=search)
        ).distinct()

    if category_prefix:
        items = items.filter(category__startswith=category_prefix)

    paginator = Paginator(items, page_size)
    page_obj = paginator.get_page(page)

    return JsonResponse(
        {
            'items': [_serialize_item(item) for item in page_obj],
            'has_next': page_obj.has_next(),
            'page': page_obj.number,
            'page_size': page_size,
            'total': paginator.count,
        }
    )


@require_GET
def api_items_all(request):
    items = _with_sales_flag(Item.objects.all()).order_by('name')

    return JsonResponse(
        {
            'items': [_serialize_item(item) for item in items],
            'categories': _build_category_tree(items.values('category')),
        }
    )


@require_GET
def api_item_suggest(request):
    query = request.GET.get('q', '').strip()
    try:
        limit = int(request.GET.get('limit', 8))
    except (TypeError, ValueError):
        limit = 8

    limit = min(max(limit, 1), 20)

    if not query:
        return JsonResponse({'items': []})

    items = (
        _with_sales_flag(Item.objects.filter(name__icontains=query))
        .order_by('name')
        .values('item_id', 'name', 'category', 'color', 'has_sales')[:limit]
    )

    return JsonResponse(
        {
            'items': [
                {
                    'item_id': item['item_id'],
                    'name': item['name'],
                    'category': item['category'],
                    'rank': item['color'],
                    'color': RANK_COLORS.get(item['color'], RANK_COLORS['DEFAULT']),
                    'has_sales': bool(item.get('has_sales', False)),
                }
                for item in items
            ]
        }
    )


@require_GET
def api_item_detail(request, item_id):
    item = get_object_or_404(Item, item_id=item_id)

    return JsonResponse(
        {
            'item': _serialize_item(item),
            'max_limit': MAX_LIMIT,
        }
    )


@require_GET
def api_item_sales(request, item_id):
    item = get_object_or_404(Item, item_id=item_id)

    try:
        limit = min(int(request.GET.get('limit', DEFAULT_LIMIT)), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    limit = max(2, limit)

    sales = (
        SaleHistory.objects.filter(item_id=item.id)
        .order_by('-time')
        .values('id', 'price', 'time', 'extra_data', 'item__name', 'item__color')[:limit]
    )

    return JsonResponse(
        {
            'sales': list(sales),
            'colors': RANK_COLORS,
        }
    )


@csrf_exempt
def api_process_lang_file(request):
    return process_lang_file(request)


def process_lang_file(request):
    """
    Обработка загруженного ru.lang файла:
    - берём все названия из файла
    - ищем Item по этим названиям
    - считаем среднюю цену за последние N месяцев (через SQL)
    - если продаж нет, используем последнюю доступную цену до указанного периода
    - возвращаем новый файл с добавленными строками
    """
    if request.method != "POST" or "file" not in request.FILES:
        return HttpResponse("Загрузите файл методом POST с полем 'file'", status=400)

    # Сколько месяцев брать (по умолчанию 2, максимум 3)
    months = int(request.POST.get("months", 2))
    months = max(1, min(3, months))
    date_from = timezone.now() - datetime.timedelta(days=30 * months)

    file = request.FILES["file"]
    lines = file.read().decode("utf-8").splitlines()
    output_lines = lines.copy()
    output_lines.append("")  # пустая строка-разделитель

    # все названия предметов из файла
    names = [line.split("=", 1)[1] for line in lines if "=" in line]

    # загрузка Items одним запросом
    items = list(Item.objects.filter(name__in=names).values("id", "name", "category"))
    name_to_item = {i["name"]: i for i in items}
    if not name_to_item:
        return HttpResponse("В файле нет предметов из базы", status=400)

    regular_item_ids = []
    artefact_item_ids = []
    for item in items:
        if item["category"].startswith("artefact"):
            artefact_item_ids.append(item["id"])
        else:
            regular_item_ids.append(item["id"])

    prices_map = {}
    artefact_prices_map = {}
    artefact_qlt_label_map = {
        0: 'обыч',
        1: 'необ',
        2: 'особ',
        3: 'редк',
        4: 'искл',
        5: 'лег',
    }

    # --- обычные предметы: средняя цена за N месяцев ---
    if regular_item_ids:
        query_avg_regular = """
            WITH filtered AS (
                SELECT sh.item_id, sh.price
                FROM auction_salehistory sh
                WHERE sh.item_id = ANY(%s)
                  AND sh.time >= %s
            ),
            bounds AS (
                SELECT item_id,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY price) AS q1,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY price) AS q3
                FROM filtered
                GROUP BY item_id
            )
            SELECT f.item_id, AVG(f.price) AS avg_price
            FROM filtered f
            JOIN bounds b ON f.item_id = b.item_id
            WHERE f.price BETWEEN (b.q1 - 4 * (b.q3 - b.q1))
                              AND (b.q3 + 4 * (b.q3 - b.q1))
            GROUP BY f.item_id;
        """

        with connection.cursor() as cursor:
            cursor.execute(query_avg_regular, [regular_item_ids, date_from])
            rows = cursor.fetchall()

        prices_map = {item_id: avg for item_id, avg in rows}

        # --- fallback для обычных предметов: последняя продажа до периода ---
        missing_ids = [item_id for item_id in regular_item_ids if item_id not in prices_map]
        if missing_ids:
            query_last_regular = """
                SELECT DISTINCT ON (sh.item_id) sh.item_id, sh.price
                FROM auction_salehistory sh
                WHERE sh.item_id = ANY(%s)
                  AND sh.time < %s
                ORDER BY sh.item_id, sh.time DESC;
            """
            with connection.cursor() as cursor:
                cursor.execute(query_last_regular, [missing_ids, date_from])
                rows = cursor.fetchall()
            for item_id, price in rows:
                prices_map[item_id] = price

    # --- артефакты: средняя цена отдельно для qlt=0..5 (только ptn=0 или ptn отсутствует) ---
    if artefact_item_ids:
        query_avg_artefact = """
            WITH filtered AS (
                SELECT
                    sh.item_id,
                    CASE
                        WHEN sh.extra_data IS NOT NULL
                         AND sh.extra_data ? 'qlt'
                         AND (sh.extra_data ->> 'qlt') ~ '^[0-9]+$'
                        THEN (sh.extra_data ->> 'qlt')::int
                        ELSE 0
                    END AS qlt,
                    sh.price
                FROM auction_salehistory sh
                WHERE sh.item_id = ANY(%s)
                  AND sh.time >= %s
                  AND (
                      sh.extra_data IS NULL
                      OR NOT (sh.extra_data ? 'ptn')
                      OR sh.extra_data ->> 'ptn' = '0'
                  )
            ),
            normalized AS (
                SELECT item_id, qlt, price
                FROM filtered
                WHERE qlt BETWEEN 0 AND 5
            ),
            bounds AS (
                SELECT
                    item_id,
                    qlt,
                    percentile_cont(0.25) WITHIN GROUP (ORDER BY price) AS q1,
                    percentile_cont(0.75) WITHIN GROUP (ORDER BY price) AS q3
                FROM normalized
                GROUP BY item_id, qlt
            )
            SELECT n.item_id, n.qlt, AVG(n.price) AS avg_price
            FROM normalized n
            JOIN bounds b
              ON n.item_id = b.item_id
             AND n.qlt = b.qlt
            WHERE n.price BETWEEN (b.q1 - 4 * (b.q3 - b.q1))
                              AND (b.q3 + 4 * (b.q3 - b.q1))
            GROUP BY n.item_id, n.qlt;
        """

        with connection.cursor() as cursor:
            cursor.execute(query_avg_artefact, [artefact_item_ids, date_from])
            rows = cursor.fetchall()

        for item_id, qlt, avg_price in rows:
            artefact_prices_map.setdefault(item_id, {})[qlt] = avg_price

        # --- fallback для артефактов: последняя продажа до периода для каждого qlt ---
        missing_artefact_ids = [item_id for item_id in artefact_item_ids if len(artefact_prices_map.get(item_id, {})) < 6]
        if missing_artefact_ids:
            query_last_artefact = """
                SELECT DISTINCT ON (x.item_id, x.qlt) x.item_id, x.qlt, x.price
                FROM (
                    SELECT
                        sh.item_id,
                        CASE
                            WHEN sh.extra_data IS NOT NULL
                             AND sh.extra_data ? 'qlt'
                             AND (sh.extra_data ->> 'qlt') ~ '^[0-9]+$'
                            THEN (sh.extra_data ->> 'qlt')::int
                            ELSE 0
                        END AS qlt,
                        sh.price,
                        sh.time
                    FROM auction_salehistory sh
                    WHERE sh.item_id = ANY(%s)
                      AND sh.time < %s
                      AND (
                          sh.extra_data IS NULL
                          OR NOT (sh.extra_data ? 'ptn')
                          OR sh.extra_data ->> 'ptn' = '0'
                      )
                ) AS x
                WHERE x.qlt BETWEEN 0 AND 5
                ORDER BY x.item_id, x.qlt, x.time DESC;
            """

            with connection.cursor() as cursor:
                cursor.execute(query_last_artefact, [missing_artefact_ids, date_from])
                rows = cursor.fetchall()

            for item_id, qlt, price in rows:
                if qlt not in artefact_prices_map.setdefault(item_id, {}):
                    artefact_prices_map[item_id][qlt] = price

    # --- сборка выходного файла ---
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        item = name_to_item.get(value)
        if not item:
            continue

        item_id = item["id"]
        is_artefact = item["category"].startswith("artefact")

        if is_artefact:
            qlt_prices = artefact_prices_map.get(item_id, {})

            artifact_lines = [f"{key}={value}"]
            for qlt in range(0, 6):
                avg_price = qlt_prices.get(qlt)
                qlt_label = artefact_qlt_label_map.get(qlt, str(qlt))
                if avg_price is None:
                    formatted_price = '---'
                else:
                    formatted_price = f"{int(avg_price):,}".replace(",", " ")
                artifact_lines.append(f"{formatted_price} руб ({qlt_label})")

            if len(artifact_lines) > 1:
                output_lines.append("\\n".join(artifact_lines))
        else:
            avg_price = prices_map.get(item_id)
            if avg_price:
                formatted_price = f"{int(avg_price):,}".replace(",", " ")
                output_lines.append(f"{key}={value}\\n{formatted_price} руб.")

    response = HttpResponse("\n".join(output_lines), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="ru.lang"'
    return response
