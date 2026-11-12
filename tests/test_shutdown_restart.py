import http.client
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from library import Store
import run


class ShutdownRestartTests(unittest.TestCase):
    def test_real_process_pauses_download_exits_and_relaunch_preserves_pause(self):
        with tempfile.TemporaryDirectory(prefix='shutdown-restart-') as temporary:
            store = Store(Path(temporary) / 'library')
            store.upsert_book({'id': 'book', 'name': 'Offline', 'source': 'file'},
                [{'title': str(i), 'text': 'Offline body.'} for i in range(2000)])
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            def start():
                return subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(run.__file__)), '--no-browser',
                    '--data-dir', str(store.data_dir), '--port', str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            def request(path, body=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    connection.request('GET' if body is None else 'POST', path,
                        body=None if body is None else json.dumps(body),
                        headers={'Content-Type': 'application/json', **(headers or {})})
                    response = connection.getresponse(); value = response.read()
                    return response.status, dict(response.getheaders()), json.loads(value) if 'json' in response.getheader('Content-Type', '') else value
                finally: connection.close()
            def ready(process):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        if request('/api/instance')[0] == 200: return
                    except (OSError, http.client.HTTPException): pass
                    time.sleep(.025)
                self.fail('Process did not start: ' + repr(process.communicate(timeout=2)))
            def auth():
                cookie = request('/')[1]['Set-Cookie'].split(';', 1)[0]
                token = request('/api/settings', headers={'Cookie': cookie})[1]['X-Shutdown-Token']
                return {'Cookie': cookie, 'X-Shutdown-Token': token}
            process = start()
            try:
                ready(process); headers = auth()
                job = request('/api/books/book/download', {'count': 2000})[2]
                self.assertEqual(request('/api/shutdown', {'pause_downloads': True}, headers)[0], 200)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                saved = store.download_job(job['id'])
                self.assertEqual(saved['status'], 'paused')
                self.assertLess(saved['completed'], 2000)
                process = start(); ready(process)
                self.assertEqual(request('/api/jobs')[2]['jobs'][0]['status'], 'paused')
                self.assertEqual(request('/api/shutdown', {}, auth())[0], 200)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
            finally:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)

    def test_rotating_logs_are_delayed_and_bounded_to_three_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch('run.logging.basicConfig') as configure:
            run.configure_logging(Path(temporary) / 'library')
            handler = configure.call_args.kwargs['handlers'][0]
            try:
                self.assertEqual((handler.maxBytes, handler.backupCount, handler.delay), (2 * 1024 ** 2, 2, True))
                self.assertFalse((Path(temporary) / 'reader.log').exists())
                for _ in range(12):
                    handler.emit(logging.LogRecord('fixture', logging.WARNING, '', 0, 'x' * (512 * 1024), (), None))
            finally: handler.close()
            self.assertEqual({p.name for p in Path(temporary).iterdir()}, {'reader.log', 'reader.log.1', 'reader.log.2'})
            self.assertTrue(all(p.stat().st_size <= 2 * 1024 ** 2 for p in Path(temporary).iterdir()))


if __name__ == '__main__': unittest.main()
