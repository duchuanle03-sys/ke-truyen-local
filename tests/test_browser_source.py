import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from browser_source import BrowserError, BrowserSource


URL = 'https://sangtacviet.vip/truyen/test/1/'


class CdpFixture:
    """A real local WebSocket endpoint; no website or browser profile is touched."""
    def __init__(self, handler):
        self.handler = handler
        self.commands = []
        self.errors = []
        self.connection = None
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.server.settimeout(6)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def send(self, value, fragmented=False):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        parts = [data[:7], data[7:]] if fragmented else [data]
        for index, part in enumerate(parts):
            opcode = 1 if index == 0 else 0
            first = (128 if index == len(parts) - 1 else 0) | opcode
            if len(part) < 126:
                header = bytes((first, len(part)))
            elif len(part) < 65536:
                header = bytes((first, 126)) + struct.pack('!H', len(part))
            else:
                header = bytes((first, 127)) + struct.pack('!Q', len(part))
            self.connection.sendall(header + part)

    def serve(self):
        try:
            self.connection, _ = self.server.accept()
            self.connection.settimeout(6)
            with self.connection.makefile('rb') as reader:
                headers = {}
                reader.readline()
                while True:
                    line = reader.readline()
                    if line == b'\r\n':
                        break
                    key, value = line.decode('ascii').split(':', 1)
                    headers[key.lower()] = value.strip()
                accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] +
                                      '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                self.connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                while True:
                    header = reader.read(2)
                    if not header or header[0] & 15 == 8:
                        break
                    length = header[1] & 127
                    if length == 126:
                        length = struct.unpack('!H', reader.read(2))[0]
                    elif length == 127:
                        length = struct.unpack('!Q', reader.read(8))[0]
                    mask = reader.read(4)
                    payload = reader.read(length)
                    message = json.loads(bytes(char ^ mask[index % 4] for index, char in enumerate(payload)))
                    self.commands.append(message)
                    reply = self.handler(message)
                    if reply is not None:
                        self.send({'method': 'Page.frameNavigated', 'params': {}}, fragmented=True)
                        self.send({'id': message['id'], 'result': reply}, fragmented=True)
        except (ConnectionError, TimeoutError, OSError):
            pass
        except Exception as error:
            self.errors.append(error)

    def close(self):
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        self.server.close()
        self.thread.join(7)


class BrowserSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.browser = BrowserSource(Path(self.temp.name))

    def test_rejects_external_and_ambiguous_urls_before_browser_start(self):
        for url in ('http://sangtacviet.vip/a', 'https://evil.test/a',
                    'https://sangtacviet.vip.evil.test/a', 'https://user@sangtacviet.vip/a',
                    'https://sangtacviet.vip:444/a', 'https://sangtacviet.vip\\@evil.test/a',
                    'https://sangtacviet.vip/a\n', '', None):
            with self.subTest(url=url), patch.object(self.browser, '_ensure_browser', side_effect=AssertionError('Must not start')):
                with self.assertRaises(BrowserError):
                    self.browser.read(url, '({text:"chapter"})')
                with self.assertRaises(BrowserError):
                    self.browser.open(url)

    def test_returns_text_and_final_url_and_never_accepts_another_page(self):
        good = {'url': URL, 'value': {'text': 'Tiếng Việt', 'url': 'forged'}}
        with patch.object(self.browser, '_ensure_browser', return_value=9222), \
             patch.object(self.browser, '_automation_target', return_value='ws://127.0.0.1:9222/devtools/page/ABC'), \
             patch.object(self.browser, '_evaluate', return_value=good):
            self.assertEqual(self.browser.read(URL, '({text:"Tiếng Việt"})'), {'url': URL, 'text': 'Tiếng Việt'})
            for result in ({'url': URL + 'other', 'value': {'text': 'Wrong chapter'}},
                           {'url': URL, 'value': []}, {'url': URL, 'value': None}):
                with patch.object(self.browser, '_evaluate', return_value=result), self.assertRaises(BrowserError):
                    self.browser.read(URL, 'null')

    def test_serializes_reads_so_downloads_cannot_mix_pages(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        errors = []

        def evaluate(_websocket, url, _expression, _deadline):
            calls.append(url)
            if len(calls) == 1:
                entered.set()
                release.wait(2)
            return {'url': url, 'value': {'text': url}}

        def read(url):
            try:
                self.browser.read(url, '({text:location.href})')
            except Exception as error:
                errors.append(error)

        with patch.object(self.browser, '_ensure_browser', return_value=9222), \
             patch.object(self.browser, '_automation_target', return_value='ws://127.0.0.1:9222/devtools/page/ABC'), \
             patch.object(self.browser, '_evaluate', side_effect=evaluate):
            first = threading.Thread(target=read, args=(URL,))
            second = threading.Thread(target=read, args=(URL + '2',))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            time.sleep(.05)
            self.assertEqual(calls, [URL])
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(errors)
        self.assertEqual(calls, [URL, URL + '2'])

    def test_waiting_for_other_read_has_its_own_bounded_timeout(self):
        self.browser._lock.acquire()
        try:
            start = time.monotonic()
            with self.assertRaises(BrowserError) as caught:
                self.browser.read(URL, 'null', timeout=.03)
            self.assertEqual(caught.exception.code, 'browser_timeout')
            self.assertLess(time.monotonic() - start, .5)
        finally:
            self.browser._lock.release()

    def test_invalid_deadlines_never_start_a_browser(self):
        for timeout in (0, -1, float('nan'), float('inf'), 301, None, 'bad'):
            with self.subTest(timeout=timeout), patch.object(self.browser, '_ensure_browser', side_effect=AssertionError('Must not start')):
                with self.assertRaises(BrowserError):
                    self.browser.read(URL, 'null', timeout=timeout)

    def test_browser_restarts_in_private_normal_profile_after_user_closes_it(self):
        executable = Path(self.temp.name) / 'programs/Google/Chrome/Application/chrome.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.browser._target_id = 'CLOSED'
        launches = []
        with patch.dict(os.environ, {'ProgramFiles': str(Path(self.temp.name) / 'programs')}), \
             patch.object(self.browser, '_active_port', side_effect=[None, 9222]), \
             patch('browser_source.subprocess.Popen', side_effect=lambda args, **kwargs: launches.append((args, kwargs))):
            self.assertEqual(self.browser._ensure_browser(time.monotonic() + 1), 9222)
        self.assertTrue(self.browser.profile.is_dir())
        self.assertEqual(self.browser.profile.parent, Path(self.temp.name).resolve())
        self.assertIn('--user-data-dir=' + str(self.browser.profile), launches[0][0])
        self.assertIn('--remote-debugging-address=127.0.0.1', launches[0][0])
        self.assertFalse(any('headless' in argument or 'disable-web-security' in argument for argument in launches[0][0]))
        self.assertIsNone(self.browser._target_id)

    def test_private_profile_permission_failure_is_a_transport_error(self):
        executable = Path(self.temp.name) / 'programs/Google/Chrome/Application/chrome.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.dict(os.environ, {'ProgramFiles': str(Path(self.temp.name) / 'programs')}), \
             patch.object(self.browser, '_active_port', return_value=None), \
             patch('browser_source.Path.mkdir', side_effect=PermissionError('secret path')), \
             self.assertRaises(BrowserError):
            self.browser._ensure_browser(time.monotonic() + 1)

    def test_discovers_standard_32_bit_chrome_installation(self):
        executable = Path(self.temp.name) / 'programs86/Google/Chrome/Application/chrome.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        launches = []
        with patch.dict(os.environ, {'ProgramFiles': str(Path(self.temp.name) / 'programs64'),
                                    'ProgramFiles(x86)': str(Path(self.temp.name) / 'programs86'),
                                    'LOCALAPPDATA': str(Path(self.temp.name) / 'local')}), \
             patch.object(self.browser, '_active_port', side_effect=[None, 9222]), \
             patch('browser_source.subprocess.Popen', side_effect=lambda args, **kwargs: launches.append(args)):
            self.assertEqual(self.browser._ensure_browser(time.monotonic() + 1), 9222)
        self.assertEqual(launches[0][0], str(executable))

    def test_manual_tab_is_separate_from_automation_and_same_browser(self):
        targets = [{'id': 'READ', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/READ'}]
        self.browser._target_id = 'READ'
        requests = []

        def request(port, path, deadline, method='GET'):
            requests.append((port, path, method))
            if path == '/json/list':
                return targets
            return {'id': 'MANUAL'}

        with patch.object(self.browser, '_ensure_browser', return_value=9222), \
             patch.object(self.browser, '_request', side_effect=request):
            self.assertEqual(self.browser.open(URL), {'opened': True, 'url': URL})
            self.assertEqual(self.browser._automation_target(9222, time.monotonic() + 1),
                             'ws://127.0.0.1:9222/devtools/page/READ')
        self.assertEqual(self.browser._target_id, 'READ')
        self.assertEqual(requests[0], (9222, '/json/new?https%3A%2F%2Fsangtacviet.vip%2Ftruyen%2Ftest%2F1%2F', 'PUT'))

    def test_closed_automation_tab_is_recreated_without_using_manual_tab(self):
        self.browser._target_id = 'CLOSED'
        replies = [[{'id': 'MANUAL', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/MANUAL'}],
                   {'id': 'NEW', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/NEW'}]
        with patch.object(self.browser, '_request', side_effect=replies):
            websocket = self.browser._automation_target(9222, time.monotonic() + 1)
        self.assertEqual(websocket, 'ws://127.0.0.1:9222/devtools/page/NEW')
        self.assertEqual(self.browser._target_id, 'NEW')

    def test_rejects_remote_or_forged_cdp_websocket(self):
        for websocket in ('ws://evil.test:9222/devtools/page/ABC',
                          'ws://localhost:9222/devtools/page/ABC',
                          'ws://127.0.0.1:9223/devtools/page/ABC',
                          'ws://user@127.0.0.1:9222/devtools/page/ABC',
                          'ws://127.0.0.1:9222/devtools/page/ABC?other=1'):
            with self.subTest(websocket=websocket), \
                 patch.object(self.browser, '_request', return_value={'id': 'ABC', 'webSocketDebuggerUrl': websocket}), \
                 self.assertRaises(BrowserError):
                self.browser._automation_target(9222, time.monotonic() + 1)

    def test_cdp_websocket_must_belong_to_the_automation_target(self):
        target = {'id': 'AUTOMATION', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/MANUAL'}
        with patch.object(self.browser, '_request', return_value=target), self.assertRaises(BrowserError):
            self.browser._automation_target(9222, time.monotonic() + 1)

    def test_script_errors_and_timeouts_do_not_become_vip(self):
        for failure in (subprocess.TimeoutExpired('powershell', .01), OSError('Secret session token')):
            with self.subTest(failure=failure), patch('browser_source.subprocess.run', side_effect=failure):
                with self.assertRaises(BrowserError) as caught:
                    self.browser._evaluate('ws://127.0.0.1:9222/devtools/page/ABC', URL, 'null', time.monotonic() + 1)
                self.assertNotIn('Secret', str(caught.exception))
                self.assertNotIn('vip', str(caught.exception).lower())
        for response in (b'{"error":"browser_timeout"}', b'not json', b'{"error":"secret message"}'):
            with self.subTest(response=response), \
                 patch('browser_source.subprocess.run', return_value=subprocess.CompletedProcess([], 0, response, b'')):
                with self.assertRaises(BrowserError) as caught:
                    self.browser._evaluate('ws://127.0.0.1:9222/devtools/page/ABC', URL, 'null', time.monotonic() + 1)
                self.assertNotIn('secret', str(caught.exception))

    def test_script_timeout_keeps_timeout_classification(self):
        process = subprocess.CompletedProcess([], 1, b'{"error":"browser_timeout"}', b'')
        with patch('browser_source.subprocess.run', return_value=process), self.assertRaises(BrowserError) as caught:
            self.browser._evaluate('ws://127.0.0.1:9222/devtools/page/ABC', URL, 'null', time.monotonic() + 1)
        self.assertEqual(caught.exception.code, 'browser_timeout')

    def test_http_probe_uses_direct_literal_loopback_and_rejects_redirects(self):
        class Response:
            status = 302
            def read(self, _limit):
                return b'{}'
            def close(self):
                pass
        class Connection:
            def __init__(self, host, port, timeout):
                self.host, self.port, self.timeout = host, port, timeout
                self.closed = False
                self.sock = None
            def request(self, method, path):
                self.method, self.path = method, path
            def getresponse(self):
                return Response()
            def close(self):
                self.closed = True
        connections = []
        def connect(*args, **kwargs):
            connection = Connection(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch('browser_source.http.client.HTTPConnection', side_effect=connect), self.assertRaises(BrowserError):
            self.browser._request(9222, '/json/version', time.monotonic() + 1)
        self.assertEqual(connections[0].host, '127.0.0.1')
        self.assertTrue(connections[0].closed)

    def test_http_deadline_covers_slow_headers_and_body_not_only_idle_sockets(self):
        for slow_headers in (False, True):
            with self.subTest(slow_headers=slow_headers):
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(2)
                port = listener.getsockname()[1]

                def serve():
                    try:
                        connection, _ = listener.accept()
                        with connection:
                            connection.recv(4096)
                            header = b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n'
                            if not slow_headers:
                                connection.sendall(header)
                            for char in (header if slow_headers else b'          '):
                                connection.sendall(bytes((char,)))
                                time.sleep(.04)
                    except OSError:
                        pass

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                start = time.monotonic()
                try:
                    with self.assertRaises(BrowserError):
                        self.browser._request(port, '/json/version', time.monotonic() + .15)
                    elapsed = time.monotonic() - start
                finally:
                    listener.close()
                    thread.join(3)
                self.assertLess(elapsed, .3)

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_waits_for_requested_document_and_awaits_promises(self):
        frames, evaluations = [], []

        def handle(message):
            if message['method'] in ('Page.bringToFront', 'Network.enable', 'Network.setCacheDisabled'):
                return {}
            if message['method'] == 'Page.navigate':
                self.assertEqual([command['method'] for command in fixture.commands[:2]],
                                 ['Page.bringToFront', 'Page.getFrameTree'])
                self.assertEqual(message['params']['url'], URL)
                return {'frameId': 'TOP', 'loaderId': 'NEW'}
            if message['method'] == 'Page.getFrameTree':
                frames.append(1)
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'OLD' if len(frames) == 1 else 'NEW',
                                               'url': URL + 'old' if len(frames) == 1 else URL}}}
            if message['method'] == 'Runtime.evaluate':
                self.assertGreaterEqual(len(frames), 2)
                self.assertTrue(message['params']['awaitPromise'])
                self.assertTrue(message['params']['returnByValue'])
                evaluations.append(1)
                value = None if len(evaluations) == 1 else {'url': URL, 'value': {'text': 'Tiếng Việt'}}
                return {'result': {'type': 'object', 'value': value}}
            self.fail('Unexpected CDP command: ' + message['method'])

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            '(async () => ({text:"Tiếng Việt"}))()', time.monotonic() + 8)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'text': 'Tiếng Việt'}})
        self.assertEqual(len(evaluations), 2)

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_reloads_same_url_and_waits_for_new_document_then_site_reload(self):
        navigated = False
        polls = 0
        current_loader = 'OLD'
        evaluated_loaders = []

        def handle(message):
            nonlocal navigated, polls, current_loader
            if message['method'] in ('Page.bringToFront', 'Network.enable', 'Network.setCacheDisabled'):
                return {}
            if message['method'] == 'Page.reload':
                self.assertEqual(message['params'], {'ignoreCache': False})
                navigated = True
                return {}
            if message['method'] == 'Page.getFrameTree':
                if navigated:
                    polls += 1
                    current_loader = 'OLD' if polls == 1 else 'NAV' if polls == 2 else 'LANGUAGE-RELOAD'
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': current_loader, 'url': URL}}}
            if message['method'] == 'Runtime.evaluate':
                self.assertNotEqual(current_loader, 'OLD', 'The previous document must never be extracted')
                evaluated_loaders.append(current_loader)
                value = {'url': URL, 'value': {'text': 'Translated chapter'}} if current_loader == 'LANGUAGE-RELOAD' else None
                return {'result': {'type': 'object', 'value': value}}
            self.fail('Unexpected CDP command: ' + message['method'])

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            '({text:document.body.innerText})', time.monotonic() + 4)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'text': 'Translated chapter'}})
        self.assertEqual(evaluated_loaders, ['NAV', 'LANGUAGE-RELOAD'])

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_reports_main_document_http_status(self):
        for status in (403, 404):
            with self.subTest(status=status):
                navigated = False

                def handle(message):
                    nonlocal navigated
                    if message['method'] in ('Page.bringToFront', 'Network.enable', 'Network.setCacheDisabled'):
                        return {}
                    if message['method'] == 'Page.getFrameTree':
                        return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'NEW' if navigated else 'OLD',
                                                       'url': URL if navigated or status == 404 else URL + 'old'}}}
                    if message['method'] in ('Page.navigate', 'Page.reload'):
                        self.assertEqual(message['method'], 'Page.reload' if status == 404 else 'Page.navigate')
                        navigated = True
                        fixture.send({'method': 'Network.responseReceived', 'params': {'type': 'Document', 'frameId': 'TOP',
                                      'loaderId': 'NEW', 'response': {'url': URL, 'status': status}}})
                        return {} if message['method'] == 'Page.reload' else {'frameId': 'TOP', 'loaderId': 'NEW'}
                    if message['method'] == 'Runtime.evaluate':
                        return {'result': {'type': 'object', 'value': None}}
                    self.fail('Unexpected CDP command: ' + message['method'])

                fixture = CdpFixture(handle)
                try:
                    result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                                    'null', time.monotonic() + 3)
                finally:
                    fixture.close()
                self.assertFalse(fixture.errors)
                self.assertEqual(result, {'url': URL, 'value': {'error': {'code': 'http_error', 'http_status': status}}})
                self.assertIn('Network.enable', [command['method'] for command in fixture.commands])

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_ignores_stale_and_unrelated_http_failures(self):
        navigated = False

        def send_status(loader='OLD', frame='TOP', url=URL, resource='Document'):
            fixture.send({'method': 'Network.responseReceived', 'params': {'type': resource, 'frameId': frame,
                          'loaderId': loader, 'response': {'url': url, 'status': 404}}})

        def handle(message):
            nonlocal navigated
            if message['method'] in ('Page.bringToFront', 'Network.setCacheDisabled'):
                return {}
            if message['method'] == 'Network.enable':
                send_status()
                return {}
            if message['method'] == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'NEW' if navigated else 'OLD', 'url': URL}}}
            if message['method'] == 'Page.reload':
                navigated = True
                send_status()
                send_status(loader='UNRELATED')
                send_status(loader='NEW', frame='SUBFRAME')
                send_status(loader='NEW', url=URL + 'different')
                send_status(loader='NEW', resource='Script')
                return {}
            if message['method'] == 'Runtime.evaluate':
                return {'result': {'type': 'object', 'value': {'url': URL, 'value': {'text': 'Readable chapter'}}}}
            self.fail('Unexpected CDP command: ' + message['method'])

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            '({text:"Readable chapter"})', time.monotonic() + 3)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'text': 'Readable chapter'}})

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_distinguishes_navigation_network_failure(self):
        def handle(message):
            if message['method'] in ('Page.bringToFront', 'Network.enable', 'Network.setCacheDisabled'):
                return {}
            if message['method'] == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'OLD', 'url': URL + 'old'}}}
            if message['method'] == 'Page.navigate':
                return {'frameId': 'TOP', 'loaderId': 'NEW', 'errorText': 'net::ERR_NAME_NOT_RESOLVED'}
            self.fail('Unexpected CDP command: ' + message['method'])

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            'null', time.monotonic() + 3)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'error': {'code': 'network_error'}}})

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_disables_cache_for_read_then_restores_it(self):
        for same_url in (False, True):
            with self.subTest(same_url=same_url):
                navigated = False
                cache_values = []

                def handle(message):
                    nonlocal navigated
                    method = message['method']
                    if method == 'Network.setCacheDisabled':
                        cache_values.append(message['params']['cacheDisabled'])
                        return {}
                    if method in ('Page.bringToFront', 'Network.enable'):
                        return {}
                    if method == 'Page.getFrameTree':
                        return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'NEW' if navigated else 'OLD',
                                                       'url': URL if same_url or navigated else URL + 'old'}}}
                    if method in ('Page.navigate', 'Page.reload'):
                        self.assertEqual(cache_values, [True])
                        self.assertEqual(method, 'Page.reload' if same_url else 'Page.navigate')
                        navigated = True
                        return {} if same_url else {'frameId': 'TOP', 'loaderId': 'NEW'}
                    if method == 'Runtime.evaluate':
                        return {'result': {'type': 'object', 'value': {'url': URL, 'value': {'text': 'Fresh bootstrap'}}}}
                    self.fail('Unexpected CDP command: ' + method)

                fixture = CdpFixture(handle)
                try:
                    result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                                    '({text:"Fresh bootstrap"})', time.monotonic() + 3)
                finally:
                    fixture.close()
                self.assertFalse(fixture.errors)
                self.assertEqual(result['value']['text'], 'Fresh bootstrap')
                self.assertEqual(cache_values, [True, False])

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_restores_cache_after_protocol_error(self):
        cache_values = []

        def handle(message):
            method = message['method']
            if method == 'Network.setCacheDisabled':
                cache_values.append(message['params']['cacheDisabled'])
                return {}
            if method in ('Page.bringToFront', 'Network.enable'):
                return {}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'OLD', 'url': URL + 'old'}}}
            if method == 'Page.navigate':
                fixture.send({'id': message['id'], 'error': {'code': -32000, 'message': 'Protocol failure'}})
                return None
            self.fail('Unexpected CDP command: ' + method)

        fixture = CdpFixture(handle)
        try:
            with self.assertRaises(BrowserError) as caught:
                self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                       'null', time.monotonic() + 3)
        finally:
            fixture.close()
        self.assertEqual(caught.exception.code, 'browser_error')
        self.assertFalse(fixture.errors)
        self.assertEqual(cache_values, [True, False])

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_correlates_reload_document_network_failure(self):
        reloaded = False
        cache_values = []

        def handle(message):
            nonlocal reloaded
            method = message['method']
            if method == 'Network.setCacheDisabled':
                cache_values.append(message['params']['cacheDisabled'])
                return {}
            if method in ('Page.bringToFront', 'Network.enable'):
                return {}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'NEW' if reloaded else 'OLD',
                                               'url': 'chrome-error://chromewebdata/' if reloaded else URL}}}
            if method == 'Page.reload':
                reloaded = True
                fixture.send({'method': 'Network.requestWillBeSent', 'params': {'requestId': 'DOC', 'type': 'Document',
                              'frameId': 'TOP', 'loaderId': 'NEW', 'request': {'url': URL}}})
                fixture.send({'method': 'Network.loadingFailed', 'params': {'requestId': 'DOC', 'type': 'Document',
                              'canceled': False, 'errorText': 'net::ERR_NAME_NOT_RESOLVED'}})
                return {}
            self.fail('Unexpected CDP command: ' + method)

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            'null', time.monotonic() + 3)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'error': {'code': 'network_error'}}})
        self.assertEqual(cache_values, [True, False])

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_ignores_stale_canceled_and_unrelated_network_failures(self):
        reloaded = False

        def request(request_id, loader='NEW', frame='TOP', url=URL, resource='Document'):
            fixture.send({'method': 'Network.requestWillBeSent', 'params': {'requestId': request_id, 'type': resource,
                          'frameId': frame, 'loaderId': loader, 'request': {'url': url}}})

        def fail(request_id, canceled=False, error='net::ERR_NAME_NOT_RESOLVED'):
            fixture.send({'method': 'Network.loadingFailed', 'params': {'requestId': request_id, 'type': 'Document',
                          'canceled': canceled, 'errorText': error}})

        def handle(message):
            nonlocal reloaded
            method = message['method']
            if method in ('Page.bringToFront', 'Network.enable', 'Network.setCacheDisabled'):
                return {}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'TOP', 'loaderId': 'NEW' if reloaded else 'OLD', 'url': URL}}}
            if method == 'Page.reload':
                reloaded = True
                request('STALE', loader='OLD'); fail('STALE')
                request('CANCELED'); fail('CANCELED', canceled=True)
                request('ABORTED'); fail('ABORTED', error='net::ERR_ABORTED')
                request('SUBFRAME', frame='OTHER'); fail('SUBFRAME')
                request('OTHERURL', url=URL + 'other'); fail('OTHERURL')
                request('SCRIPT', resource='Script'); fail('SCRIPT')
                request('REDIRECT'); request('REDIRECT', url=URL + 'redirect'); fail('REDIRECT')
                fail('UNTRACKED')
                return {}
            if method == 'Runtime.evaluate':
                return {'result': {'type': 'object', 'value': {'url': URL, 'value': {'text': 'Readable chapter'}}}}
            self.fail('Unexpected CDP command: ' + method)

        fixture = CdpFixture(handle)
        try:
            result = self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                            '({text:"Readable chapter"})', time.monotonic() + 3)
        finally:
            fixture.close()
        self.assertFalse(fixture.errors)
        self.assertEqual(result, {'url': URL, 'value': {'text': 'Readable chapter'}})

    @unittest.skipUnless(os.name == 'nt', 'The Windows transport uses system PowerShell')
    def test_real_powershell_transport_cancels_a_stalled_cdp_reply(self):
        fixture = CdpFixture(lambda message: None)
        start = time.monotonic()
        try:
            with self.assertRaises(BrowserError) as caught:
                self.browser._evaluate(f'ws://127.0.0.1:{fixture.port}/devtools/page/ABC', URL,
                                       'null', time.monotonic() + 2)
        finally:
            fixture.close()
        self.assertEqual(caught.exception.code, 'browser_timeout')
        self.assertLess(time.monotonic() - start, 4)


if __name__ == '__main__':
    unittest.main()
