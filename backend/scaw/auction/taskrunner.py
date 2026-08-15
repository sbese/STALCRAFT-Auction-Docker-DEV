"""
In-process замена Celery: фоновые задачи выполняются в потоках веб-процесса.

Ограничения и гарантии:
- сервер запускается в один процесс (gunicorn --workers 1 или runserver);
- одновременная работа двух сборщиков (например, при zero-downtime деплое
  Render, когда старый и новый инстансы живут параллельно) исключается
  advisory-блокировкой PostgreSQL: инстанс без блокировки остается в standby
  и ждет, пока держатель не умрет;
- периодические запуски выполняет внешний cron (например, cron-job.org)
  через /auction/api/cron/.
"""

import os
import sys
import threading
import uuid

import psycopg2
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from auction.logger import log


COLLECTOR_TASK_NAME = 'history_collector'
COLLECTOR_CYCLE_PAUSE_SECONDS = 1  # Пауза между циклами сборщика (как countdown=1 у Celery-версии)
COLLECTOR_LOCK_KEY = 741_852_963  # Ключ advisory-блокировки "ровно один сборщик на базу"
COLLECTOR_LOCK_RETRY_SECONDS = 15  # Как часто standby-инстанс пробует забрать блокировку


def _isoformat(value):
    return value.isoformat() if value else None


class PostgresAdvisoryLock:
    """
    Session-level advisory lock на выделенном соединении.

    Соединение держится открытым на весь срок жизни сборщика: если процесс
    умирает, PostgreSQL сам освобождает блокировку, и ее забирает standby.
    """

    def __init__(self, key: int):
        self.key = key
        self._conn = None

    def try_acquire(self) -> bool:
        if self._conn is not None:
            return True

        conn = None
        try:
            db = settings.DATABASES['default']
            conn = psycopg2.connect(
                dbname=db['NAME'],
                user=db['USER'],
                password=db['PASSWORD'],
                host=db['HOST'],
                port=db['PORT'],
                connect_timeout=10,
            )
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (self.key,))
                acquired = bool(cursor.fetchone()[0])

            if acquired:
                self._conn = conn
                return True

            conn.close()
            return False
        except Exception as exc:
            log(f"ERROR: Не удалось получить блокировку сборщика: {exc}", save=True)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            return False

    def is_healthy(self) -> bool:
        """Проверяет, что соединение с блокировкой еще живо."""
        if self._conn is None:
            return False
        try:
            with self._conn.cursor() as cursor:
                cursor.execute('SELECT 1')
            return True
        except Exception:
            self.release()
            return False

    def release(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class BackgroundTask:
    """Одна запущенная фоновая задача."""

    def __init__(self, name: str, stoppable: bool):
        self.id = uuid.uuid4().hex
        self.name = name
        self.stoppable = stoppable
        self.stop_event = threading.Event()
        self.started_at = timezone.now()
        self.thread = None

    def as_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'stoppable': self.stoppable,
            'started_at': _isoformat(self.started_at),
            'runtime_seconds': int((timezone.now() - self.started_at).total_seconds()),
            'stop_requested': self.stop_event.is_set(),
        }


class TaskRunner:
    """Реестр и запуск фоновых задач в потоках текущего процесса."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}  # id -> BackgroundTask
        self._collector_state = {
            'lock_acquired': False,
            'cycles_completed': 0,
            'progress_done': 0,
            'progress_total': 0,
            'last_cycle_started_at': None,
            'last_cycle_finished_at': None,
            'last_error': None,
        }

    # ------------------------------- РЕЕСТР -------------------------------

    def get_registry(self) -> dict:
        # Импорт внутри метода, чтобы не создавать циклический импорт с tasks.py
        from auction import tasks as task_module

        return {
            'sync_github_items_daily': task_module.sync_github_items_daily,
            'delete_old_sales': task_module.delete_old_sales,
        }

    def manual_task_names(self) -> list:
        return sorted(self.get_registry().keys())

    # ------------------------------- ЗАПУСК -------------------------------

    def start_task(self, name: str) -> dict:
        """Запускает задачу из реестра. Одна активная задача одного имени за раз."""
        registry = self.get_registry()
        if name == COLLECTOR_TASK_NAME:
            return self.ensure_collector_running()

        if name not in registry:
            raise KeyError(name)

        with self._lock:
            running = self._find_by_name_locked(name)
            if running is not None:
                return {'task': running.as_dict(), 'already_running': True}

            task = BackgroundTask(name, stoppable=False)
            task.thread = threading.Thread(
                target=self._run_manual_task,
                args=(task, registry[name]),
                name=f'task-{name}',
                daemon=True,
            )
            self._tasks[task.id] = task

        task.thread.start()
        log(f"INFO: Задача {name} [{task.id}] запущена.", save=True)
        return {'task': task.as_dict(), 'already_running': False}

    def _run_manual_task(self, task: BackgroundTask, target) -> None:
        try:
            close_old_connections()
            target()
        except Exception as exc:
            log(f"ERROR: Задача {task.name} [{task.id}] завершилась с ошибкой: {exc}", save=True)
        finally:
            close_old_connections()
            with self._lock:
                self._tasks.pop(task.id, None)

    # ------------------------------ ОСТАНОВКА ------------------------------

    def stop_task(self, task_id: str) -> dict:
        """Кооперативная остановка: задача завершится на ближайшей проверке stop_event."""
        with self._lock:
            task = self._tasks.get(task_id)

        if task is None:
            raise KeyError(task_id)

        if not task.stoppable:
            raise ValueError('Task does not support stopping')

        task.stop_event.set()
        log(f"INFO: Запрошена остановка задачи {task.name} [{task.id}].", save=True)
        return task.as_dict()

    # ------------------------------- СБОРЩИК -------------------------------

    def _make_collector_lock(self) -> PostgresAdvisoryLock:
        # Отдельный метод, чтобы в тестах подменять блокировку фейком
        return PostgresAdvisoryLock(COLLECTOR_LOCK_KEY)

    def ensure_collector_running(self) -> dict:
        """
        Запускает вечный цикл сборщика истории, если он еще не работает.

        Задача с уже запрошенной остановкой живой не считается: рядом с ней
        сразу стартует новый поток, а advisory-блокировка сериализует их -
        новый сборщик начнет циклы только после того, как старый отпустит лок.
        """
        with self._lock:
            running = self._find_by_name_locked(COLLECTOR_TASK_NAME)
            if running is not None:
                return {'task': running.as_dict(), 'already_running': True}

            task = BackgroundTask(COLLECTOR_TASK_NAME, stoppable=True)
            task.thread = threading.Thread(
                target=self._collector_loop,
                args=(task,),
                name='task-history-collector',
                daemon=True,
            )
            self._tasks[task.id] = task

        task.thread.start()
        log("INFO: Сборщик истории аукциона запущен.", save=True)
        return {'task': task.as_dict(), 'already_running': False}

    def _collector_loop(self, task: BackgroundTask) -> None:
        from auction import tasks as task_module

        def on_progress(done: int, total: int) -> None:
            self._update_collector_state(progress_done=done, progress_total=total)

        lock = self._make_collector_lock()
        waiting_logged = False

        try:
            while not task.stop_event.is_set():
                # Сначала блокировка: без нее инстанс остается в standby
                if not lock.is_healthy():
                    self._update_collector_state(lock_acquired=False)
                    if not lock.try_acquire():
                        if not waiting_logged:
                            log("INFO: Блокировка сборщика занята другим инстансом - ожидание в standby.", save=True)
                            waiting_logged = True
                        task.stop_event.wait(COLLECTOR_LOCK_RETRY_SECONDS)
                        continue
                    self._update_collector_state(lock_acquired=True)
                    waiting_logged = False
                    log("INFO: Блокировка сборщика получена - начинаю сбор.", save=True)

                self._update_collector_state(
                    last_cycle_started_at=timezone.now(),
                    progress_done=0,
                    progress_total=0,
                )
                try:
                    close_old_connections()
                    task_module.collect_history_cycle(stop_event=task.stop_event, on_progress=on_progress)
                    self._update_collector_state(
                        cycles_completed=self._collector_state['cycles_completed'] + 1,
                        last_error=None,
                    )
                except Exception as exc:
                    self._update_collector_state(last_error=str(exc))
                    log(f"ERROR: Цикл сборщика истории завершился с ошибкой: {exc}", save=True)
                finally:
                    close_old_connections()
                    self._update_collector_state(last_cycle_finished_at=timezone.now())

                task.stop_event.wait(COLLECTOR_CYCLE_PAUSE_SECONDS)
        finally:
            lock.release()
            with self._lock:
                self._tasks.pop(task.id, None)
                self._collector_state['lock_acquired'] = False
            log("INFO: Сборщик истории аукциона остановлен.", save=True)

    # -------------------------------- СТАТУС --------------------------------

    def running_tasks(self) -> list:
        with self._lock:
            tasks = list(self._tasks.values())
        return [task.as_dict() for task in sorted(tasks, key=lambda item: item.started_at)]

    def collector_status(self) -> dict:
        with self._lock:
            task = self._find_by_name_locked(COLLECTOR_TASK_NAME)
            if task is None:
                # Активного нет - показываем останавливающийся, если есть
                task = self._find_by_name_locked(COLLECTOR_TASK_NAME, include_stopping=True)
            state_snapshot = dict(self._collector_state)

        if task is None:
            state = 'stopped'
        elif not state_snapshot['lock_acquired']:
            state = 'waiting_lock'
        else:
            state = 'collecting'

        return {
            'alive': task is not None,
            'state': state,
            'task': task.as_dict() if task else None,
            'lock_acquired': state_snapshot['lock_acquired'],
            'cycles_completed': state_snapshot['cycles_completed'],
            'progress_done': state_snapshot['progress_done'],
            'progress_total': state_snapshot['progress_total'],
            'last_cycle_started_at': _isoformat(state_snapshot['last_cycle_started_at']),
            'last_cycle_finished_at': _isoformat(state_snapshot['last_cycle_finished_at']),
            'last_error': state_snapshot['last_error'],
        }

    def _update_collector_state(self, **fields) -> None:
        with self._lock:
            self._collector_state.update(fields)

    def _find_by_name_locked(self, name: str, include_stopping: bool = False):
        for task in self._tasks.values():
            if task.name != name:
                continue
            if not include_stopping and task.stop_event.is_set():
                continue
            return task
        return None


runner = TaskRunner()


def maybe_autostart_collector() -> None:
    """
    Автостарт сборщика при старте сервера.

    Включается переменной окружения COLLECTOR_AUTOSTART=1: entrypoint.sh
    экспортирует ее после всех manage.py-команд, непосредственно перед exec
    сервера, поэтому migrate/collectstatic и прочие management-команды
    сборщик не запускают.
    """
    if os.getenv('COLLECTOR_AUTOSTART') != '1':
        return

    # runserver с автоперезагрузкой поднимает два процесса; рабочий - с RUN_MAIN=true
    if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
        return

    runner.ensure_collector_running()
