"""Bounded cached-image reads and HTTP validators on temporary loopback servers."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
from pathlib import Path
import socket
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from server import make_server


class ImageStreamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.server = make_server(temporary.name, port=0)
        self.server.app._asset_stop.set()
        self.server.app._asset_worker.join(2)
        self.app = self.server.app
        self.urls = [f'https://cdn.example/large-{i}.png' for i in range(4)]
        self.app.store.upsert_book({'id': 'images', 'name': 'Image fixture', 'source': 'full'},
                                  [{'title': 'One', 'text': 'Cached body.', 'html': '<p>Cached body.</p>', 'images': self.urls}])
        while not self.app.store.asset_index_ready():
            self.app.store.index_assets()
        self.size = 8 * 1024 ** 2 + 13
        digest = hashlib.sha256()
        self.paths = []
        for url in self.urls:
            path = self.app.image_dir / hashlib.sha256(url.encode()).hexdigest()
            with path.open('wb') as output:
                output.write(b'\x89PNG\r\n\x1a\n')
                remaining = self.size - 8
                while remaining:
                    chunk = b'x' * min(65536, remaining)
                    output.write(chunk)
                    remaining -= len(chunk)
            self.app.store.touch_image(path, self.size, legacy=False)
            self.paths.append(path)
        with self.paths[0].open('rb') as source:
            while chunk := source.read(65536):
                digest.update(chunk)
        self.digest = digest.hexdigest()
        while self.app.store.inventory_assets():
            pass
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self, index=0, method='GET', headers=None, cover=False):
        path = '/api/books/images/cover' if cover else '/api/image?' + urlencode({'url': self.urls[index]})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=8)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            digest, size = hashlib.sha256(), 0
            while chunk := response.read(65536):
                digest.update(chunk); size += len(chunk)
            return response.status, dict(response.getheaders()), size, digest.hexdigest()
        finally:
            connection.close()

    @contextlib.contextmanager
    def reads(self, before_read=None):
        original = Path.open
        reads, closed = [], []
        class Reader:
            def __init__(self, file): self.file = file
            def __enter__(self): return self
            def __exit__(self, *args): self.file.close(); closed.append(True)
            def read(self, size=-1):
                reads.append(size)
                if before_read:
                    before_read(size)
                return self.file.read(size)
            def __getattr__(self, name): return getattr(self.file, name)
        def opened(path, *args, **kwargs):
            file = original(path, *args, **kwargs)
            return Reader(file) if path in self.paths and (args[0] if args else kwargs.get('mode')) == 'rb' else file
        with patch.object(Path, 'open', opened):
            yield reads, closed

    def test_cached_image_and_cover_never_read_the_whole_file(self):
        self.app.store.update_book('images', {'cover': self.urls[0]})
        with self.reads() as (reads, closed), patch.object(Path, 'read_bytes', side_effect=AssertionError('Whole cached file loaded')), patch('server.safe_fetch_bytes', side_effect=AssertionError('Cached request used network')):
            for cover in (False, True):
                status, headers, size, digest = self.request(cover=cover)
                self.assertEqual((status, size, digest), (200, self.size, self.digest))
                self.assertEqual(headers['Content-Type'], 'image/png')
                self.assertEqual(int(headers['Content-Length']), self.size)
                self.assertIn('ETag', headers); self.assertIn('Last-Modified', headers)
                self.assertIn('private', headers['Cache-Control'])
                self.assertNotIn('Transfer-Encoding', headers)
        self.assertTrue(closed)
        self.assertTrue(reads)
        self.assertTrue(all(0 < count <= 65536 for count in reads), reads)

    def test_head_and_conditional_requests_only_sniff_the_image_header(self):
        _, headers, _, _ = self.request(method='HEAD')
        tag, modified = headers['ETag'], headers['Last-Modified']
        for request_headers in ({'If-None-Match': tag}, {'If-None-Match': '*'},
                {'If-None-Match': '"other,opaque", ' + tag.removeprefix('W/')}, {'If-Modified-Since': modified}):
            with self.subTest(headers=request_headers), self.reads() as (reads, closed):
                status, actual, size, _ = self.request(headers=request_headers)
                self.assertEqual((status, size), (304, 0))
                self.assertEqual(actual['ETag'], tag)
                self.assertTrue(all(0 < count <= 12 for count in reads), reads)
        with self.reads() as (reads, closed):
            status, actual, size, _ = self.request(method='HEAD')
            self.assertEqual((status, size, int(actual['Content-Length'])), (200, 0, self.size))
            self.assertTrue(all(0 < count <= 12 for count in reads), reads)
        for request_headers in ({'If-None-Match': '"different"', 'If-Modified-Since': modified},
                                {'If-Modified-Since': 'invalid date'},
                                {'If-Modified-Since': 'Mon, 01 Jan 0001 00:00:00 GMT'}):
            self.assertEqual(self.request(headers=request_headers)[0], 200)
        self.paths[0].write_bytes(b'\x89PNG\r\n\x1a\nchanged')
        status, actual, size, _ = self.request(headers={'If-None-Match': tag})
        self.assertEqual((status, size), (200, 15))
        self.assertNotEqual(actual['ETag'], tag)

    def test_active_stream_keeps_its_lease_until_the_last_chunk(self):
        entered, release = threading.Event(), threading.Event()
        def block(size):
            if size > 12 and not entered.is_set():
                entered.set()
                if not release.wait(4): raise TimeoutError('Stream not released')
        with self.reads(block), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.request)
            try:
                self.assertTrue(entered.wait(3))
                self.app.store.set_image_cache_limit(0)
                self.app.store.queue_cache_evictions()
                self.app.store.cleanup_files()
                self.assertTrue(self.paths[0].exists())
                self.assertIn(self.paths[0], self.app.store._active_assets)
            finally:
                release.set()
            self.assertEqual(pending.result(5)[2:], (self.size, self.digest))
        self.assertNotIn(self.paths[0], self.app.store._active_assets)

    def test_consumer_error_does_not_trigger_cover_recovery(self):
        self.app.store.update_book('images', {'cover': self.urls[0]})
        with patch.object(self.app.sources, 'cover') as refresh, self.assertRaisesRegex(ValueError, 'consumer'):
            with self.app.open_cover('images'):
                raise ValueError('consumer')
        refresh.assert_not_called()
        self.assertEqual(self.app.store._active_assets, {})

    def test_offline_download_pins_cached_images_without_reading_their_bodies(self):
        with self.reads() as (reads, closed), patch('server.safe_fetch_bytes', side_effect=AssertionError('Cached download used network')):
            job = self.app.download('images')
            for _ in range(300):
                saved = self.app.store.download_job(job['id'])
                if saved['status'] in ('completed', 'failed'):
                    break
                time.sleep(.01)
        self.assertEqual(saved['status'], 'completed', saved.get('error'))
        self.assertEqual(reads, [12] * 4)
        self.assertEqual(self.app.store.image_cache_stats()['protected_bytes'], self.size * 4)

    def test_stream_failure_closes_the_response_without_appending_json_error(self):
        def fail(size):
            if size > 12:
                raise ValueError('Synthetic stream read failure')
        with self.reads(fail) as (reads, closed):
            status, headers, size, digest = self.request()
        self.assertEqual((status, size, digest), (200, 0, hashlib.sha256().hexdigest()))
        self.assertEqual(int(headers['Content-Length']), self.size)
        self.assertTrue(closed)
        self.assertEqual(self.app.store._active_assets, {})

    def test_client_disconnect_releases_the_file_and_asset_lease(self):
        entered, release = threading.Event(), threading.Event()
        def block(size):
            if size > 12 and not entered.is_set():
                entered.set()
                release.wait(4)
        with self.reads(block) as (reads, closed):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            try:
                connection.request('GET', '/api/image?' + urlencode({'url': self.urls[0]}))
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(entered.wait(3))
                connection.sock.shutdown(socket.SHUT_RDWR)
            finally:
                connection.close(); release.set()
            for _ in range(300):
                if closed and not self.app.store._active_assets:
                    break
                time.sleep(.01)
        self.assertTrue(closed)
        self.assertEqual(self.app.store._active_assets, {})

    def test_parallel_streams_have_bounded_peak_python_memory(self):
        barrier, seen, lock = threading.Barrier(4), set(), threading.Lock()
        def start_together(size):
            if size <= 12:
                return
            ident = threading.get_ident()
            with lock:
                first = ident not in seen
                seen.add(ident)
            if first:
                barrier.wait(5)
        started = time.perf_counter()
        tracemalloc.start()
        try:
            with self.reads(start_together) as (reads, closed), ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(self.request, range(4)))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(all(result[2:] == (self.size, self.digest) for result in results))
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(0 < count <= 65536 for count in reads), reads[:12])
        self.assertLess(peak, 4 * 1024 ** 2, f'Cached streams retained {peak} Python bytes')
        print({'cached_images': 4, 'bytes_per_image': self.size, 'cached_chapter_bodies': 1,
               'parallel_requests': 4, 'peak_python_bytes': peak, 'elapsed_ms': (time.perf_counter() - started) * 1000})


if __name__ == '__main__':
    unittest.main()
