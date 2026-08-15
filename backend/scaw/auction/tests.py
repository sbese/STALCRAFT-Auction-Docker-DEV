"""
Тесты нового lifecycle фоновых задач: TaskRunner, cron/health-эндпоинты,
автостарт сборщика. Сетевые вызовы и advisory-блокировка подменяются фейками.
"""

import json
import os
import sys
import threading
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from auction import taskrunner
from auction.taskrunner import COLLECTOR_TASK_NAME, TaskRunner


class FakeLock:
    """Замена PostgresAdvisoryLock: без базы, всегда доступна."""

    def __init__(self, acquirable=True):
        self.acquirable = acquirable
        self.acquired = False
        self.released = False

    def try_acquire(self):
        if self.acquirable:
            self.acquired = True
        return self.acquirable

    def is_healthy(self):
        return self.acquired

    def release(self):
        self.acquired = False
        self.released = True


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_runner(registry=None, lock_factory=FakeLock):
    runner = TaskRunner()
    runner.get_registry = lambda: (registry or {})
    runner._make_collector_lock = lambda: lock_factory()
    return runner


class TaskRunnerManualTasksTests(SimpleTestCase):
    def test_same_task_is_single_flight(self):
        release = threading.Event()
        started = threading.Event()

        def slow_task():
            started.set()
            release.wait(5)

        runner = make_runner({'slow': slow_task})

        first = runner.start_task('slow')
        self.assertTrue(started.wait(5))
        second = runner.start_task('slow')

        try:
            self.assertFalse(first['already_running'])
            self.assertTrue(second['already_running'])
            self.assertEqual(first['task']['id'], second['task']['id'])
            self.assertEqual(len(runner.running_tasks()), 1)
        finally:
            release.set()

        self.assertTrue(wait_until(lambda: not runner.running_tasks()))

    def test_unknown_task_raises(self):
        runner = make_runner({})
        with self.assertRaises(KeyError):
            runner.start_task('nope')

    def test_manual_task_is_not_stoppable(self):
        release = threading.Event()
        runner = make_runner({'slow': lambda: release.wait(5)})

        result = runner.start_task('slow')
        try:
            with self.assertRaises(ValueError):
                runner.stop_task(result['task']['id'])
        finally:
            release.set()

        self.assertTrue(wait_until(lambda: not runner.running_tasks()))


class TaskRunnerCollectorTests(SimpleTestCase):
    def test_ensure_twice_starts_single_collector(self):
        runner = make_runner()

        def fake_cycle(stop_event=None, on_progress=None):
            stop_event.wait(5)

        with mock.patch('auction.tasks.collect_history_cycle', side_effect=fake_cycle):
            first = runner.ensure_collector_running()
            second = runner.ensure_collector_running()

            try:
                self.assertFalse(first['already_running'])
                self.assertTrue(second['already_running'])
                self.assertEqual(first['task']['id'], second['task']['id'])
                collectors = [t for t in runner.running_tasks() if t['name'] == COLLECTOR_TASK_NAME]
                self.assertEqual(len(collectors), 1)
            finally:
                runner.stop_task(first['task']['id'])

            self.assertTrue(wait_until(lambda: not runner.running_tasks()))

    def test_stop_removes_collector_from_registry(self):
        runner = make_runner()

        def fake_cycle(stop_event=None, on_progress=None):
            stop_event.wait(5)

        with mock.patch('auction.tasks.collect_history_cycle', side_effect=fake_cycle):
            started = runner.ensure_collector_running()
            self.assertTrue(wait_until(lambda: runner.collector_status()['state'] == 'collecting'))

            stopped = runner.stop_task(started['task']['id'])
            self.assertTrue(stopped['stop_requested'])

            self.assertTrue(wait_until(lambda: not runner.running_tasks()))
            status = runner.collector_status()
            self.assertFalse(status['alive'])
            self.assertEqual(status['state'], 'stopped')

    def test_ensure_ignores_stopping_collector_and_starts_new_one(self):
        runner = make_runner()
        wind_down = threading.Event()

        def fake_cycle(stop_event=None, on_progress=None):
            stop_event.wait(5)
            # Имитируем медленное сворачивание цикла после запроса остановки
            wind_down.wait(5)

        with mock.patch('auction.tasks.collect_history_cycle', side_effect=fake_cycle):
            first = runner.ensure_collector_running()
            self.assertTrue(wait_until(lambda: runner.collector_status()['state'] == 'collecting'))

            runner.stop_task(first['task']['id'])

            second = runner.ensure_collector_running()
            try:
                self.assertFalse(second['already_running'])
                self.assertNotEqual(first['task']['id'], second['task']['id'])
            finally:
                wind_down.set()
                runner.stop_task(second['task']['id'])

            self.assertTrue(wait_until(lambda: not runner.running_tasks()))

    def test_collector_waits_in_standby_without_lock(self):
        runner = make_runner(lock_factory=lambda: FakeLock(acquirable=False))
        cycle = mock.Mock()

        with mock.patch('auction.tasks.collect_history_cycle', cycle):
            started = runner.ensure_collector_running()
            try:
                self.assertTrue(wait_until(lambda: runner.collector_status()['state'] == 'waiting_lock'))
                cycle.assert_not_called()
            finally:
                runner.stop_task(started['task']['id'])

            self.assertTrue(wait_until(lambda: not runner.running_tasks()))


class AutostartTests(SimpleTestCase):
    def test_no_autostart_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('COLLECTOR_AUTOSTART', None)
            with mock.patch.object(taskrunner.runner, 'ensure_collector_running') as ensure:
                taskrunner.maybe_autostart_collector()
                ensure.assert_not_called()

    def test_autostart_with_env_on_server_process(self):
        with mock.patch.dict(os.environ, {'COLLECTOR_AUTOSTART': '1'}):
            with mock.patch.object(sys, 'argv', ['gunicorn']):
                with mock.patch.object(taskrunner.runner, 'ensure_collector_running') as ensure:
                    taskrunner.maybe_autostart_collector()
                    ensure.assert_called_once()

    def test_no_autostart_in_runserver_reloader_parent(self):
        with mock.patch.dict(os.environ, {'COLLECTOR_AUTOSTART': '1'}, clear=False):
            os.environ.pop('RUN_MAIN', None)
            with mock.patch.object(sys, 'argv', ['manage.py', 'runserver']):
                with mock.patch.object(taskrunner.runner, 'ensure_collector_running') as ensure:
                    taskrunner.maybe_autostart_collector()
                    ensure.assert_not_called()

    def test_autostart_in_runserver_child(self):
        with mock.patch.dict(os.environ, {'COLLECTOR_AUTOSTART': '1', 'RUN_MAIN': 'true'}):
            with mock.patch.object(sys, 'argv', ['manage.py', 'runserver']):
                with mock.patch.object(taskrunner.runner, 'ensure_collector_running') as ensure:
                    taskrunner.maybe_autostart_collector()
                    ensure.assert_called_once()


def _stub_runner(collector_status=None):
    stub = mock.Mock()
    stub.collector_status.return_value = collector_status or {
        'alive': True,
        'state': 'collecting',
        'task': {'id': 'cid'},
        'lock_acquired': True,
        'cycles_completed': 1,
        'progress_done': 0,
        'progress_total': 0,
        'last_cycle_started_at': None,
        'last_cycle_finished_at': None,
        'last_error': None,
    }
    stub.running_tasks.return_value = []
    stub.manual_task_names.return_value = ['delete_old_sales', 'sync_github_items_daily']
    stub.get_registry.return_value = {'delete_old_sales': lambda: None, 'sync_github_items_daily': lambda: None}
    stub.start_task.return_value = {'task': {'id': 'tid'}, 'already_running': False}
    return stub


@override_settings(CRON_SECRET='cron-secret')
class CronEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_missing_token_rejected(self):
        response = self.client.get('/auction/api/cron/sync_github_items_daily/')
        self.assertEqual(response.status_code, 403)

    def test_wrong_token_rejected(self):
        response = self.client.get(
            '/auction/api/cron/sync_github_items_daily/',
            HTTP_X_CRON_TOKEN='wrong',
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(CRON_SECRET='')
    def test_disabled_without_secret(self):
        response = self.client.get(
            '/auction/api/cron/sync_github_items_daily/',
            HTTP_X_CRON_TOKEN='',
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_task_404(self):
        with mock.patch('auction.views.runner', _stub_runner()):
            response = self.client.get(
                '/auction/api/cron/unknown_task/',
                HTTP_X_CRON_TOKEN='cron-secret',
            )
        self.assertEqual(response.status_code, 404)

    def test_valid_header_token_starts_task(self):
        stub = _stub_runner()
        with mock.patch('auction.views.runner', stub):
            response = self.client.get(
                '/auction/api/cron/sync_github_items_daily/',
                HTTP_X_CRON_TOKEN='cron-secret',
            )
        self.assertEqual(response.status_code, 200)
        stub.start_task.assert_called_once_with('sync_github_items_daily')

    def test_query_token_fallback_still_works(self):
        stub = _stub_runner()
        with mock.patch('auction.views.runner', stub):
            response = self.client.get('/auction/api/cron/history_collector/?token=cron-secret')
        self.assertEqual(response.status_code, 200)
        stub.start_task.assert_called_once_with('history_collector')


class HealthEndpointTests(TestCase):
    def test_liveness_is_200_even_with_dead_collector(self):
        dead = _stub_runner({'alive': False, 'state': 'stopped'})
        with mock.patch('auction.views.runner', dead):
            response = self.client.get('/auction/api/health/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(json.loads(response.content)['collector_alive'])

    def test_collector_readiness_503_when_dead(self):
        dead = _stub_runner({'alive': False, 'state': 'stopped'})
        with mock.patch('auction.views.runner', dead):
            response = self.client.get('/auction/api/health/collector/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)['status'], 'dead')

    def test_collector_readiness_200_standby_during_deploy_overlap(self):
        standby = _stub_runner()
        standby.collector_status.return_value.update({'alive': True, 'state': 'waiting_lock'})
        with mock.patch('auction.views.runner', standby):
            response = self.client.get('/auction/api/health/collector/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'standby')

    def test_collector_readiness_200_when_collecting(self):
        with mock.patch('auction.views.runner', _stub_runner()):
            response = self.client.get('/auction/api/health/collector/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'collecting')


class AdminTasksAuthTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user('staff', password='x', is_staff=True)
        self.regular = User.objects.create_user('regular', password='x', is_staff=False)

    def test_anonymous_gets_401(self):
        response = self.client.get('/auction/api/admin/tasks/overview/')
        self.assertEqual(response.status_code, 401)

    def test_non_staff_gets_403(self):
        self.client.force_login(self.regular)
        response = self.client.get('/auction/api/admin/tasks/overview/')
        self.assertEqual(response.status_code, 403)

    def test_staff_gets_overview(self):
        self.client.force_login(self.staff)
        with mock.patch('auction.views.runner', _stub_runner()):
            response = self.client.get('/auction/api/admin/tasks/overview/')
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIn('collector', payload)
        self.assertEqual(payload['cron_auth_header'], 'X-Cron-Token')

    def test_staff_start_rejects_unknown_task(self):
        self.client.force_login(self.staff)
        with mock.patch('auction.views.runner', _stub_runner()):
            response = self.client.post(
                '/auction/api/admin/tasks/start/',
                data=json.dumps({'task_name': 'unknown'}),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 400)
