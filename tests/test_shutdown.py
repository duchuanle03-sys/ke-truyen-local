import http.client
import http.server
import json
import queue
import tempfile
import socket
import shutil
import threading
import time
import unittest
from unittest.mock import Mock, patch

from library import Store
from qt import QtError
from server import App, make_server


class ShutdownLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Mock()
        self.app = App(self.temp.name, self.source)
        self.addCleanup(self.app.close)

    def test_cancellation_requested_before_close_still_closes_all_resources(self):
        self.app.begin_shutdown(pause_downloads=True)
        self.assertTrue(self.app._download_stop.is_set())
        self.assertTrue(self.app.close(timeout=2))
        self.assertTrue(self.app.close(timeout=2))
        self.source.close.assert_called_once()
        with self.assertRaises(QtError): self.app.qt.status()
        self.assertFalse(self.app._asset_worker.is_alive())
        self.assertFalse(self.app._download_worker.is_alive())

    def test_busy_critical_operation_refuses_shutdown_without_closing_admission(self):
        with self.app.critical_operation('dictionary import'):
            with self.assertRaises(ValueError) as error: self.app.begin_shutdown(True)
            self.assertEqual(error.exception.code, 'busy_operation')
            self.assertIn('dictionary import', str(error.exception))
        self.assertFalse(self.app._shutting_down)
        self.assertFalse(self.app._download_stop.is_set())

    def test_pause_intent_is_durable_for_jobs_not_loaded_in_ram(self):
        self.app._download_stop.set(); self.app._download_wake.set(); self.app._download_worker.join(2)
        self.app.store.upsert_book({'id': 'book', 'name': 'Book'}, [{'text': 'Body'}])
        for i, status in enumerate(('queued', 'running', 'verification_required')):
            book_id = 'book' if i == 0 else 'other' + str(i)
            if i: self.app.store.upsert_book({'id': book_id, 'name': book_id}, [{'text': 'Body'}])
            self.app.store.create_download({'id': str(i), 'book_id': book_id,
                'status': status, 'cancelled': False, 'name': 'Book', 'start': 0, 'end': 1, 'total': 1, 'completed': 0})
        with self.assertRaises(ValueError) as error: self.app.begin_shutdown(False)
        self.assertEqual(error.exception.code, 'active_downloads')
        self.app.begin_shutdown(True)
        self.assertTrue(self.app.close(timeout=2))
        restored = Store(self.temp.name); restored.recover_downloads()
        self.assertEqual([restored.download_job(str(i))['status'] for i in range(3)], ['paused'] * 3)

    def test_bounded_drain_does_not_close_qt_under_stalled_operation_and_can_retry(self):
        entered, release = threading.Event(), threading.Event()
        connection = socket.socket()
        self.addCleanup(connection.close)
        def request():
            with self.app.request_operation(connection):
                entered.set(); release.wait(4)
                self.assertTrue(self.app.qt.status() is not None)
        reader = threading.Thread(target=request); reader.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            self.assertFalse(self.app.close(timeout=.2))
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(self.app._resources_closed)
        finally:
            release.set(); reader.join(2)
        self.assertTrue(self.app.close(timeout=2))

    def test_failed_resource_close_is_retryable_not_successfully_closed(self):
        self.source.close.side_effect = [OSError('Synthetic close failure'), None]
        self.assertFalse(self.app.close(timeout=2))
        self.assertFalse(self.app._resources_closed)
        self.assertTrue(self.app.close(timeout=2))

    def test_library_lock_is_held_until_cleanup_finishes(self):
        with self.assertRaises(OSError): App(self.temp.name)
        self.assertTrue(self.app.close(timeout=2))
        other = App(self.temp.name)
        self.assertTrue(other.close(timeout=2))

    def test_failed_thread_start_cleans_unstarted_workers_and_library_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            partial = App.__new__(App)
            with patch('server.threading.Thread.start', side_effect=RuntimeError('Cannot start thread')):
                with self.assertRaisesRegex(RuntimeError, 'Cannot start thread'): partial.__init__(temporary)
            self.assertTrue(partial._library_lock.closed)
            with self.assertRaises(QtError): partial.qt.status()
            replacement = App(temporary)
            self.assertTrue(replacement.close(timeout=2))

    def test_failed_second_thread_start_retains_resources_until_first_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        from library import Store
        original_start, original_maintain = threading.Thread.start, Store.maintain_chapter_cache
        def start(worker):
            if worker.name == 'download-queue': raise RuntimeError('Second thread failed')
            return original_start(worker)
        def maintain(store):
            entered.set(); release.wait(4)
            return original_maintain(store)
        with tempfile.TemporaryDirectory() as temporary:
            partial = App.__new__(App)
            try:
                with patch('server.threading.Thread.start', start), patch.object(Store, 'maintain_chapter_cache', maintain):
                    with self.assertRaisesRegex(RuntimeError, 'Second thread failed'): partial.__init__(temporary)
                self.assertTrue(entered.is_set())
                self.assertFalse(partial._library_lock.closed)
            finally:
                release.set(); partial._asset_worker.join(2)
                partial._cleanup_failed_start()
            self.assertTrue(partial._library_lock.closed)

    def test_automatic_cleanup_retries_failed_abort_while_a_request_is_still_waiting(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def abort():
            if not failures:
                failures.append(True)
                raise OSError('First abort failed')
            release.set()
        self.source.close.side_effect = abort
        connection = socket.socket()
        self.addCleanup(connection.close)
        def reader():
            with self.app.request_operation(connection): entered.set(); release.wait(8)
        thread = threading.Thread(target=reader); thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertFalse(self.app.close(timeout=.2))
            self.app._close_wake.set()
            deadline = time.monotonic() + 3
            while not self.app._resources_closed and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(self.app._resources_closed)
        finally: release.set(); thread.join(2)

    def test_maintenance_backoff_and_duplicate_tracebacks(self):
        fake = App.__new__(App)
        fake.store = Mock()
        fake.store._compacting = False
        fake.store.maintain_chapter_cache.side_effect = OSError('Persistent synthetic failure')
        fake._asset_stop = Mock()
        delays = []
        fake._asset_stop.is_set.side_effect = lambda: len(delays) >= 4
        fake._asset_stop.wait.side_effect = delays.append
        with patch('server.logging.exception') as errors:
            fake._maintain_assets()
        self.assertEqual(delays, [.25, 1, 5, 30])
        self.assertEqual(errors.call_count, 1)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_real_node_processes_exit_after_pause_phase_has_already_run(self):
        from sources import SourceService
        from source_errors import SourceError
        with tempfile.TemporaryDirectory() as temporary:
            app = App(temporary, SourceService())
            try:
                stv = app.sources._stv()
                for lane in ('reading', 'metadata'):
                    with self.assertRaises(SourceError): stv._request('/not-a-source-route', session=lane)
                processes = [stv.http.process, stv.metadata_http.process]
                self.assertTrue(all(p.poll() is None for p in processes))
                app.begin_shutdown(True)
                self.assertTrue(app.close(timeout=3))
                self.assertTrue(all(p.poll() is not None for p in processes))
            finally: app.close()


class ShutdownHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(self.temp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.cookie = ''
        self.token = ''

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET' if data is None else 'POST', path,
                body=None if data is None else json.dumps(data),
                headers={'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse(); body = response.read()
            return response.status, dict(response.getheaders()), json.loads(body) if 'json' in response.getheader('Content-Type', '') else body
        finally: connection.close()

    def login_ui(self):
        status, headers, _ = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]
        self.token = self.request('/api/settings', headers={'Cookie': self.cookie})[1]['X-Shutdown-Token']

    def test_shutdown_auth_requires_ui_cookie_token_and_same_origin(self):
        self.assertNotIn('X-Shutdown-Token', self.request('/api/settings')[1])
        self.assertEqual(self.request('/api/shutdown', {})[0], 403)
        self.login_ui()
        for headers in ({'Cookie': self.cookie}, {'X-Shutdown-Token': self.token},
                        {'Cookie': self.cookie, 'X-Shutdown-Token': 'wrong'},
                        {'Cookie': self.cookie, 'X-Shutdown-Token': 'ö'},
                        {'Cookie': self.cookie, 'X-Shutdown-Token': self.token, 'Content-Type': 'text/plain'},
                        {'Cookie': self.cookie, 'X-Shutdown-Token': self.token, 'Origin': 'https://evil.example'}):
            self.assertEqual(self.request('/api/shutdown', {}, headers)[0], 403)
        self.assertFalse(self.server.app._shutting_down)

    def test_shutdown_response_is_flushed_and_instance_admission_immediately_closes(self):
        self.login_ui()
        with patch.object(self.server, 'shutdown') as stop:
            status, headers, body = self.request('/api/shutdown', {'pause_downloads': True},
                {'Cookie': self.cookie, 'X-Shutdown-Token': self.token})
            self.assertEqual((status, body['accepted']), (200, True))
            self.assertEqual(headers['Connection'], 'close')
            self.assertEqual(self.request('/api/instance')[0], 503)
            self.assertEqual(self.request('/api/books')[0], 503)
            self.assertTrue(self.server.app._download_stop.is_set())
        self.server.shutdown()
        self.assertTrue(self.server.server_close())

    def test_encoded_mutation_paths_are_still_critical(self):
        from server import Handler
        handler = Handler.__new__(Handler)
        handler.command = 'POST'
        for path in ('/api/%69mport/file', '/api/books/b/chapters/%30/edit',
                     '/api/books//b/chapters/%2B0/restore', '/api/cache/chapters/%63ompact'):
            handler.path = path
            self.assertTrue(handler._critical_name(), path)

    def test_busy_response_names_operation_before_acquiring_app_write_lock(self):
        self.login_ui()
        with self.server.app.critical_operation('synthetic import'), self.server.app.lock:
            status, _, error = self.request('/api/shutdown', {}, {'Cookie': self.cookie, 'X-Shutdown-Token': self.token})
        self.assertEqual((status, error['code']), (409, 'busy_operation'))
        self.assertIn('synthetic import', error['error'])

    def test_stalled_inbound_body_is_aborted_before_resources_close(self):
        self.login_ui()
        client = socket.create_connection(('127.0.0.1', self.server.server_port))
        try:
            client.sendall((f'POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
                'Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{').encode())
            deadline = time.monotonic() + 2
            while not self.server.app._in_flight_requests and time.monotonic() < deadline: time.sleep(.01)
            self.assertGreater(self.server.app._in_flight_requests, 0)
            self.assertEqual(self.request('/api/shutdown', {}, {'Cookie': self.cookie, 'X-Shutdown-Token': self.token})[0], 200)
            self.server.shutdown()
            started = time.monotonic()
            self.assertTrue(self.server.server_close())
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(self.server.app._in_flight_requests, 0)
            self.assertFalse(self.server.app._connections)
        finally: client.close()


class UpstreamCancellationTests(unittest.TestCase):
    def test_close_aborts_a_stalled_tls_handshake(self):
        from sources import _PublicFetcher, SourceError
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
        entered, release = threading.Event(), threading.Event()
        def handshake():
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096); entered.set(); release.wait(4)
        serving = threading.Thread(target=handshake); serving.start()
        fetcher, failures = _PublicFetcher(), []
        def read():
            try: fetcher('https://public.example/slow')
            except SourceError as error: failures.append(error)
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', listener.getsockname())
        try:
            with patch('sources.validate_public_url', side_effect=lambda url, **_: url), patch('sources._public_addresses', return_value=[address]):
                reader = threading.Thread(target=read); reader.start()
                self.assertTrue(entered.wait(2))
                fetcher.close(); reader.join(2)
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(failures), 1)
        finally:
            release.set(); fetcher.close(); listener.close(); serving.join(2)
    def test_node_child_reference_is_retained_when_termination_fails(self):
        from stv_http import StvHttp
        http = StvHttp()
        process, alive, failed = Mock(), [True], []
        process.poll.side_effect = lambda: None if alive[0] else 0
        def terminate():
            if not failed:
                failed.append(True); raise OSError('Terminate failed')
            alive[0] = False
        process.terminate.side_effect = terminate
        http.process, http.output = process, queue.Queue()
        with self.assertRaises(OSError): http.close()
        self.assertIs(http.process, process)
        http.close()
        self.assertIsNone(http.process)

    def test_both_stv_channels_close_even_when_one_fails(self):
        from stv_source import StvSource
        reading, metadata = Mock(), Mock()
        reading.close.side_effect = OSError('Reading close failed')
        with self.assertRaises(OSError): StvSource(reading, metadata).close()
        metadata.close.assert_called_once()

    def test_close_aborts_an_owned_stalled_http_body_without_external_network(self):
        from sources import _PublicFetcher, SourceError
        entered, release = threading.Event(), threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(200); handler.send_header('Content-Length', '10000'); handler.end_headers()
                entered.set(); release.wait(4)
            def log_message(self, *_): pass
        remote = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        remote.daemon_threads = True
        serving = threading.Thread(target=remote.serve_forever, daemon=True); serving.start()
        fetcher = _PublicFetcher()
        failures = []
        def read():
            try: fetcher('http://public.example/slow')
            except SourceError as error: failures.append(error)
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', remote.server_port))
        try:
            with patch('sources.validate_public_url', side_effect=lambda url, **_: url), patch('sources._public_addresses', return_value=[address]):
                reader = threading.Thread(target=read); reader.start()
                self.assertTrue(entered.wait(2))
                fetcher.close(); reader.join(2)
                self.assertFalse(reader.is_alive())
                self.assertEqual(len(failures), 1)
                with self.assertRaises(SourceError): fetcher('http://public.example/other')
        finally:
            release.set(); fetcher.close(); remote.shutdown(); remote.server_close(); serving.join(2)


if __name__ == '__main__': unittest.main()
