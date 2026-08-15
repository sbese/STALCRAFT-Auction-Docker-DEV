"""
In-process замена Celery: фоновые задачи выполняются в потоках веб-процесса.

Ограничение: раннер живет внутри одного процесса, поэтому сервер должен
запускаться в один процесс (gunicorn --workers 1 или runserver). Периодические
запуски выполняет внешний cron (например, cron-job.org) через /auction/api/cron/.
"""

import os
import sys
import threading
import uuid

from django.db import close_old_connections
from django.utils import timezone

from auction.logger import log


COLLECTOR_TASK_NAME = 'history_collector'
COLLECTOR_CYCLE_PAUSE_SECONDS = 1  # Пауза между циклами сборщика (как countdown=1 у Celery-версии)


def _isoformat(value):
    return value.isoformat() if value else None


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
        """Запускает задачу из реестра. Одна задача одного имени за раз."""
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

    def ensure_collector_running(self) -> dict:
        """Запускает вечный цикл сборщика истории, если он еще не работает."""
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

        state = self._collector_state

        def on_progress(done: int, total: int) -> None:
            state['progress_done'] = done
            state['progress_total'] = total

        try:
            while not task.stop_event.is_set():
                state['last_cycle_started_at'] = timezone.now()
                state['progress_done'] = 0
                state['progress_total'] = 0
                try:
                    close_old_connections()
                    task_module.collect_history_cycle(stop_event=task.stop_event, on_progress=on_progress)
                    state['cycles_completed'] += 1
                    state['last_error'] = None
                except Exception as exc:
                    state['last_error'] = str(exc)
                    log(f"ERROR: Цикл сборщика истории завершился с ошибкой: {exc}", save=True)
                finally:
                    close_old_connections()
                    state['last_cycle_finished_at'] = timezone.now()

                task.stop_event.wait(COLLECTOR_CYCLE_PAUSE_SECONDS)
        finally:
            with self._lock:
                self._tasks.pop(task.id, None)
            log("INFO: Сборщик истории аукциона остановлен.", save=True)

    # -------------------------------- СТАТУС --------------------------------

    def running_tasks(self) -> list:
        with self._lock:
            tasks = list(self._tasks.values())
        return [task.as_dict() for task in sorted(tasks, key=lambda item: item.started_at)]

    def collector_status(self) -> dict:
        with self._lock:
            task = self._find_by_name_locked(COLLECTOR_TASK_NAME)

        state = self._collector_state
        return {
            'alive': task is not None,
            'task': task.as_dict() if task else None,
            'cycles_completed': state['cycles_completed'],
            'progress_done': state['progress_done'],
            'progress_total': state['progress_total'],
            'last_cycle_started_at': _isoformat(state['last_cycle_started_at']),
            'last_cycle_finished_at': _isoformat(state['last_cycle_finished_at']),
            'last_error': state['last_error'],
        }

    def _find_by_name_locked(self, name: str):
        for task in self._tasks.values():
            if task.name == name:
                return task
        return None


runner = TaskRunner()


def maybe_autostart_collector() -> None:
    """
    Автостарт сборщика при старте сервера.

    Включается переменной окружения COLLECTOR_AUTOSTART=1 (её выставляет
    entrypoint.sh только для серверного процесса, чтобы manage.py migrate и
    прочие команды не запускали сборщик).
    """
    if os.getenv('COLLECTOR_AUTOSTART') != '1':
        return

    # runserver с автоперезагрузкой поднимает два процесса; рабочий - с RUN_MAIN=true
    if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
        return

    runner.ensure_collector_running()
