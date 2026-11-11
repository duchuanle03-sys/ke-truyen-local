"""Priority and session-isolation checks without contacting Sáng Tác Việt."""
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urljoin

from source_errors import SourceError, SourceCaptchaError
from stv_http import StvHttp
from stv_source import StvSource
from server import App
from library import Store
from test_stv_http import BOOK, CHAPTER, IMAGE, HttpFixture, chapter


class BlockingHttp:
    generation = 1
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        self.closed = False

    def request(self, path, **options):
        name = threading.current_thread().name
        self.calls.append((name, path, options))
        if options.get('session') == 'reading' and options.get('navigation') and path == CHAPTER:
            self.entered.set()
            if not self.release.wait(4):
                raise TimeoutError('Active chapter was not released')
        if self.closed:
            raise SourceError('Synthetic transport closed', code='network_error')
        if path.startswith('/io/searchtp/'):
            value = '<a class="booksearch" href="' + BOOK + '"><span class="searchbooktitle">Found</span></a>'
        elif 'sajax=readchapter' in path:
            value = json.dumps(chapter(parse_qs(path.split('?', 1)[1])['c'][0]), ensure_ascii=False)
        else:
            value = '<div>Ready</div>'
        return {'text': value, 'request_url': urljoin(BOOK, path), 'request_method': options.get('method', 'GET')}

    def close(self):
        self.closed = True
        self.release.set()


class StvSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.http = BlockingHttp()
        self.source = StvSource(http=self.http)
        self.source.tocs[('qidian', '123')] = {'901', '902', '903', '904'}
        self.addCleanup(self.source.close)

    def wait(self, test):
        for _ in range(200):
            if test(): return
            time.sleep(.005)
        self.fail('Scheduling state did not advance')

    def test_interactive_chapter_overtakes_queued_background_at_atomic_boundary(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            active = executor.submit(self.source.chapter, CHAPTER, background=True)
            self.assertTrue(self.http.entered.wait(2))
            queued = executor.submit(self.source.chapter, BOOK + '902/', background=True)
            self.wait(lambda: len(self.source._waiting[True]) == 1)
            interactive = executor.submit(self.source.chapter, BOOK + '903/')
            self.wait(lambda: len(self.source._waiting[False]) == 1)
            self.http.release.set()
            for future in (active, queued, interactive):
                self.assertIn('Tiếng Việt', future.result(3)['text'])
        navigation = [path for _, path, options in self.http.calls if options.get('navigation')]
        self.assertEqual(navigation, [CHAPTER, BOOK + '903/', BOOK + '902/'])
        self.assertEqual([options.get('method') for _, _, options in self.http.calls], [None, 'POST'] * 3)

    def test_metadata_finishes_while_a_reading_chapter_is_blocked(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            active = executor.submit(self.source.chapter, CHAPTER, background=True)
            self.assertTrue(self.http.entered.wait(2))
            search = executor.submit(self.source.search, 'Found', 1)
            try:
                self.assertEqual(search.result(.7)['items'][0]['name'], 'Found')
                self.assertFalse(active.done())
            finally:
                self.http.release.set()
            active.result(3)

    def test_waiting_background_request_can_be_cancelled_without_sending_any_http(self):
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            active = executor.submit(self.source.chapter, CHAPTER)
            self.assertTrue(self.http.entered.wait(2))
            waiting = executor.submit(self.source.chapter, BOOK + '902/', background=True, cancelled=cancel.is_set)
            try:
                self.wait(lambda: len(self.source._waiting[True]) == 1)
                cancel.set()
                with self.assertRaises(SourceError) as caught:
                    waiting.result(.7)
                self.assertEqual(caught.exception.code, 'request_cancelled')
                self.assertFalse(any('/902/' in path for _, path, _ in self.http.calls))
            finally:
                self.http.release.set()
            active.result(3)

    def test_close_wakes_queued_reading_requests_without_starting_more_http(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            active = executor.submit(self.source.chapter, CHAPTER)
            self.assertTrue(self.http.entered.wait(2))
            waiting = executor.submit(self.source.chapter, BOOK + '902/', background=True)
            self.wait(lambda: len(self.source._waiting[True]) == 1)
            self.source.close()
            for future in (active, waiting):
                with self.assertRaises(SourceError): future.result(2)
        self.assertFalse(any('/902/' in path for _, path, _ in self.http.calls))

    def test_metadata_transport_changes_do_not_expire_a_reading_captcha(self):
        reading = HttpFixture(IMAGE, 'success')
        reading.generation = 4
        metadata = HttpFixture(SourceError('metadata timeout', code='source_timeout'))
        metadata.generation = 1
        source = StvSource(http=reading, metadata_http=metadata)
        self.addCleanup(source.close)
        with self.assertRaises(SourceCaptchaError) as challenge:
            source._error({'code': 21}, CHAPTER)
        token = challenge.exception.captcha['id']
        with self.assertRaises(SourceError): source.search('test', 1)
        metadata.generation += 1
        self.assertTrue(source.captcha('image', token)['image'])
        self.assertTrue(source.captcha('verify', token, 'manual-answer')['verified'])
        self.assertTrue(all(options['session'] == 'reading' and options['require_session'] for _, options in reading.calls))
        self.assertEqual(len(reading.calls), 2)

    def test_close_between_read_response_and_captcha_publication_leaves_no_challenge(self):
        from stv_source import _json
        original = self.http.request
        def request(path, **options):
            result = original(path, **options)
            if 'sajax=readchapter' in path:
                result['text'] = '{"code":21}'
            return result
        def parse(value):
            payload = _json(value)
            self.source.close()
            return payload
        self.http.release.set()
        with patch.object(self.http, 'request', side_effect=request), patch('stv_source._json', side_effect=parse):
            with self.assertRaises(SourceError) as caught:
                self.source.chapter(CHAPTER)
        self.assertEqual(caught.exception.code, 'network_error')
        self.assertIsNone(self.source.pending)

    def test_close_after_verify_response_does_not_revive_verification(self):
        source = StvSource(http=HttpFixture(IMAGE, 'success'))
        self.addCleanup(source.close)
        with self.assertRaises(SourceCaptchaError) as challenge:
            source._error({'code': 21}, CHAPTER)
        verification = challenge.exception.verification
        token = challenge.exception.captcha['id']
        source.captcha('image', token)
        original = source._request
        def request(path, **options):
            result = original(path, **options)
            source.close()
            return result
        with patch.object(source, '_request', side_effect=request):
            with self.assertRaises(SourceError) as caught:
                source.captcha('verify', token, 'manual-answer')
        self.assertEqual(caught.exception.code, 'network_error')
        self.assertIsNone(source.pending)
        self.assertTrue(verification['event'].is_set())
        self.assertFalse(verification['verified'])

    def test_wait_queue_is_bounded_without_disrupting_existing_requests(self):
        with patch('stv_source._MAX_WAITERS', 2), ThreadPoolExecutor(max_workers=3) as executor:
            active = executor.submit(self.source.chapter, CHAPTER)
            self.assertTrue(self.http.entered.wait(2))
            background = executor.submit(self.source.chapter, BOOK + '902/', background=True)
            interactive = executor.submit(self.source.chapter, BOOK + '903/')
            try:
                self.wait(lambda: sum(map(len, self.source._waiting.values())) == 2)
                with self.assertRaises(SourceError) as caught:
                    self.source.chapter(BOOK + '904/')
                self.assertEqual(caught.exception.code, 'source_busy')
            finally:
                self.http.release.set()
            for future in (active, background, interactive): future.result(3)


class StvTransportCloseTests(unittest.TestCase):
    def test_close_interrupts_an_active_transport_wait_and_disallows_restart(self):
        http = StvHttp()
        output = queue.Queue()
        sent, done = threading.Event(), threading.Event()
        process = Mock()
        terminated = []
        process.poll.side_effect = lambda: 0 if terminated else None
        process.stdin.flush.side_effect = sent.set
        process.terminate.side_effect = lambda: (terminated.append(True), output.put(None))
        http.process, http.output = process, output
        errors = []
        def request():
            try: http.request('/index.php')
            except SourceError as error: errors.append(error)
        def close():
            http.close(); done.set()
        caller = threading.Thread(target=request); caller.start()
        self.assertTrue(sent.wait(2))
        closer = threading.Thread(target=close); closer.start()
        try:
            self.assertTrue(done.wait(.7), 'close() waited behind an active worker request')
        finally:
            output.put(None); caller.join(3); closer.join(3)
        self.assertEqual(len(errors), 1)
        with self.assertRaises(SourceError): http.request('/index.php')


class StvQueueScaleTests(unittest.TestCase):
    def test_admission_skips_stv_without_loading_queue_history_or_chapter_bodies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            with store.db() as db:
                db.executemany('INSERT INTO books(id,name,source,added_at,updated_at) VALUES(?,?,?,?,?)',
                    ((str(i), str(i), 'stv' if i < 10000 else 'full', i, i) for i in range(10001)))
                db.executemany('INSERT INTO download_jobs(id,book_id,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                    ((str(i), str(i), 'queued', '{}', i, i) for i in range(10001)))
                db.executemany('INSERT INTO download_jobs(id,book_id,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                    ((f'history-{i}', '0', 'completed', '{}', i, i) for i in range(50000)))
                db.executemany('INSERT INTO chapters(book_id,idx,text,downloaded) VALUES(?,0,?,1)',
                    ((str(i), 'Cached text.') for i in range(3)))
            statements = []
            original_db = store.db
            @contextmanager
            def measured_db():
                with original_db() as db:
                    db.set_trace_callback(statements.append)
                    yield db
            with patch.object(store, 'db', measured_db), patch.object(store, '_download_row', wraps=store._download_row) as decode:
                started = time.perf_counter()
                selected = store.queued_downloads(exclude_stv=True, limit=1)
                elapsed_ms = (time.perf_counter() - started) * 1000
                self.assertEqual([job['book_id'] for job in selected], ['10000'])
                self.assertEqual(selected[0]['source'], 'full')
                self.assertEqual(decode.call_count, 1)
            queries = [sql for sql in statements if sql.startswith('SELECT')]
            self.assertEqual(len(queries), 1)
            self.assertNotIn('chapters', queries[0])
            with store.db() as db:
                plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + queries[0]))
            self.assertIn('download_queue', plan)
            self.assertNotIn('TEMP B-TREE', plan)
            print(json.dumps({'stv_queued': 10000, 'other_source_queued': 1, 'history': 50000,
                              'cached_chapter_bodies': 3, 'admission_ms': round(elapsed_ms, 2), 'decoded_jobs': 1}))


class StvDownloadAdmissionTests(unittest.TestCase):
    def test_pausing_a_job_waiting_behind_interactive_reading_releases_its_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            http = BlockingHttp()
            source = StvSource(http=http)
            source.tocs[('qidian', '123')] = {'901', '902'}
            app = App(temporary)
            app.sources._stv_source = source
            app.store.upsert_book({'id': 'waiting', 'name': 'Waiting', 'source': 'stv', 'path': BOOK},
                                  [{'title': 'Two', 'url': BOOK + '902/'}])
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    active = executor.submit(source.chapter, CHAPTER)
                    self.assertTrue(http.entered.wait(2))
                    job = app.download('waiting')
                    try:
                        for _ in range(200):
                            if source._waiting[True]: break
                            time.sleep(.005)
                        self.assertTrue(source._waiting[True])
                        app.job_action(job['id'], 'pause')
                        for _ in range(200):
                            if not source._waiting[True] and all(f.done() for f in app._download_futures.values()): break
                            time.sleep(.005)
                        self.assertFalse(source._waiting[True])
                        self.assertTrue(all(f.done() for f in app._download_futures.values()))
                        saved = app.store.download_job(job['id'])
                        self.assertEqual((saved['status'], saved['completed']), ('paused', 0))
                        self.assertFalse(app.store.content('waiting', 0)['downloaded'])
                        self.assertFalse(any('/902/' in path for _, path, _ in http.calls))
                        self.assertEqual(app.job_action(job['id'], 'resume')['id'], job['id'])
                    finally:
                        http.release.set()
                    active.result(3)
                    for _ in range(200):
                        if app.store.download_job(job['id'])['status'] == 'completed': break
                        time.sleep(.005)
                    self.assertEqual(app.store.download_job(job['id'])['status'], 'completed')
            finally:
                http.release.set(); app.close()


if __name__ == '__main__': unittest.main()
