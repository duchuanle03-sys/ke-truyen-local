import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import run
from server import make_server


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.logging = patch('run.logging.basicConfig')
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'library'
        self.server = make_server(self.data, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_address[1]

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_book_shortcut_reuses_running_library_and_encodes_book_id(self):
        book_id = 'book & question? \u0111\u1ecdc'
        self.server.app.store.upsert_book({'id': book_id, 'name': 'Keep'}, [{'title': 'One', 'text': 'Keep'}])
        self.server.app.store.progress(book_id, 0, .6)
        with patch('run.make_server', side_effect=AssertionError('Must reuse the running app')), patch('run.open_window') as opened:
            result = run.main(['--data-dir', str(self.data), '--port', str(self.port), '--book', book_id])
        self.assertEqual(result, 0)
        url = urlsplit(opened.call_args.args[0])
        self.assertEqual((url.hostname, url.port), ('127.0.0.1', self.port))
        self.assertEqual(parse_qs(url.query), {'book': [book_id]})
        self.assertEqual(self.server.app.store.get_book(book_id)['position'], .6)

    def test_standard_launcher_reuses_running_library(self):
        for options in ([], ['--no-browser']):
            with self.subTest(options=options):
                with patch('run.make_server', side_effect=AssertionError('Must reuse the running app')), \
                     patch('run.open_window') as opened:
                    result = run.main(['--data-dir', str(self.data), '--port', str(self.port)] + options)
                self.assertEqual(result, 0)
                if options:
                    opened.assert_not_called()
                else:
                    opened.assert_called_once_with(f'http://127.0.0.1:{self.port}')

    def test_standard_launcher_reuses_instance_that_starts_during_bind(self):
        with patch('run.running_instance', side_effect=[False, True]), \
             patch('run.make_server', side_effect=OSError('Another launch acquired the port')), \
             patch('run.open_window') as opened:
            result = run.main(['--data-dir', str(self.data), '--port', str(self.port)])
        self.assertEqual(result, 0)
        opened.assert_called_once_with(f'http://127.0.0.1:{self.port}')

    def test_instance_probe_requires_same_app_and_same_library(self):
        self.assertTrue(run.running_instance(self.data, self.port))
        self.assertFalse(run.running_instance(self.data.parent / 'other-library', self.port))
        self.assertFalse((self.data.parent / 'other-library').exists())

    def test_instance_probe_rejects_other_services_and_redirects(self):
        class ForeignHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(handler.server.reply_status)
                handler.send_header('Location', f'http://127.0.0.1:{self.port}/api/instance')
                handler.end_headers()
                handler.wfile.write(json.dumps(handler.server.reply).encode())

            def log_message(handler, *_):
                pass
        foreign = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ForeignHandler)
        thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        try:
            for status, identity in [(302, {}), (200, {'app': 'another-service', 'data_directory': str(self.data)}),
                                     (200, {'app': 'ke-truyen-local', 'data_directory': None})]:
                with self.subTest(status=status, identity=identity):
                    foreign.reply_status, foreign.reply = status, identity
                    self.assertFalse(run.running_instance(self.data, foreign.server_address[1]))
        finally:
            foreign.shutdown()
            foreign.server_close()
            thread.join(3)

    def test_failed_start_does_not_open_another_service(self):
        for options in ([], ['--book=keep']):
            with self.subTest(options=options):
                with patch('run.make_server', side_effect=OSError('busy')), patch('run.running_instance', return_value=False), \
                     patch('run.time.sleep'), patch('run.open_window') as opened, patch('run.report_error') as error:
                    result = run.main(['--data-dir', str(self.data.parent / 'other'), '--port', str(self.port)] + options)
                self.assertEqual(result, 1)
                opened.assert_not_called()
                self.assertTrue(error.call_args.args[0])

    def test_shortcut_starts_server_when_app_is_not_running(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        data = Path(self.temp.name) / 'cold-library'
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(run.__file__)),
            '--data-dir', str(data), '--port', str(port), '--book=cold-book', '--no-browser'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            deadline = time.monotonic() + 10
            instance = None
            while time.monotonic() < deadline and process.poll() is None:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
                try:
                    connection.request('GET', '/api/instance')
                    response = connection.getresponse()
                    if response.status == 200:
                        instance = json.loads(response.read())
                        break
                except (OSError, http.client.HTTPException):
                    time.sleep(.05)
                finally:
                    connection.close()
            self.assertIsNotNone(instance, 'The cold-start launcher did not expose the local app')
            self.assertEqual(instance['app'], 'ke-truyen-local')
            self.assertEqual(Path(instance['data_directory']), data.resolve())
            self.assertEqual(instance['pid'], process.pid)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
