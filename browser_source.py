"""Read STV's rendered public website in a separate, reusable browser profile."""
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit


class BrowserError(ValueError):
    def __init__(self, message='Cannot communicate with the website browser.', code='browser_error'):
        super().__init__(message)
        self.code = code


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrowserError('The website browser timed out. Retry after the page has loaded.', 'browser_timeout')
    return remaining


def _site_url(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(char.isspace() or ord(char) < 32 for char in url) or '\\' in url:
            raise ValueError()
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname != 'sangtacviet.vip'
                or parts.username is not None or parts.password is not None or parts.port not in (None, 443)):
            raise ValueError()
        return urlunsplit(('https', 'sangtacviet.vip', parts.path or '/', parts.query, parts.fragment))
    except (ValueError, TypeError):
        raise BrowserError('Only the original https://sangtacviet.vip website may be opened.') from None


def _websocket_url(url, port, kind):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'ws' or parts.netloc != f'127.0.0.1:{port}' or parts.query or parts.fragment
                or not re.fullmatch(r'/devtools/' + kind + r'/[A-Za-z0-9_-]+', parts.path)):
            raise ValueError()
        return url
    except (ValueError, TypeError, AttributeError):
        raise BrowserError('The local browser debugging address is invalid.') from None


class BrowserSource:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.profile = self.data_dir / '.browser-stv'
        if not self.profile.resolve().is_relative_to(self.data_dir):
            raise BrowserError('The private browser profile must be inside this library.')
        self._lock = threading.Lock()
        self._target_id = None

    def read(self, url, expression, timeout=60):
        url = _site_url(url)
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 2_000_000:
            raise BrowserError('The website extraction expression is invalid.')
        try:
            seconds = float(timeout)
            if not math.isfinite(seconds) or seconds <= 0 or seconds > 300:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise BrowserError('The browser timeout must be between zero and 300 seconds.') from None
        deadline = time.monotonic() + seconds
        if not self._lock.acquire(timeout=_remaining(deadline)):
            raise BrowserError('Another website read is still running. Retry shortly.', 'browser_timeout')
        try:
            port = self._ensure_browser(deadline)
            websocket = self._automation_target(port, deadline)
            result = self._evaluate(websocket, url, expression, deadline)
            if not isinstance(result, dict) or result.get('url') != url or not isinstance(result.get('value'), dict):
                raise BrowserError('The website browser did not return the requested page.')
            return dict(result['value'], url=result['url'])
        finally:
            self._lock.release()

    def open(self, url):
        url = _site_url(url)
        deadline = time.monotonic() + 20
        if not self._lock.acquire(timeout=_remaining(deadline)):
            raise BrowserError('Another website read is still running. Retry shortly.', 'browser_timeout')
        try:
            port = self._ensure_browser(deadline)
            # A manual sign-in tab shares cookies, while extraction keeps its own tab.
            target = self._request(port, '/json/new?' + quote(url, safe=''), deadline, method='PUT')
            if not isinstance(target, dict) or not target.get('id'):
                raise BrowserError('Cannot open the original website in the private browser.')
            return {'opened': True, 'url': url}
        finally:
            self._lock.release()

    def _request(self, port, path, deadline, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=min(2, _remaining(deadline)))
        timer = response = None

        def interrupt(active_socket):
            if active_socket:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        try:
            connection.request(method, path)
            # Socket idle timeouts alone allow a slow response to overrun the read deadline.
            timer = threading.Timer(_remaining(deadline), interrupt, args=(connection.sock,))
            timer.daemon = True
            timer.start()
            response = connection.getresponse()
            body = response.read(16_777_217)
            if response.status != 200 or len(body) > 16_777_216:
                raise BrowserError()
            return json.loads(body)
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            _remaining(deadline)
            raise BrowserError() from None
        finally:
            if timer:
                timer.cancel()
            if response:
                response.close()
            connection.close()

    def _active_port(self, deadline):
        try:
            with (self.profile / 'DevToolsActivePort').open('r', encoding='ascii') as handle:
                lines = handle.read(1024).splitlines()
            if len(lines) != 2 or not lines[0].isdigit() or not 0 < int(lines[0]) < 65536:
                return None
            port = int(lines[0])
            expected = _websocket_url(f'ws://127.0.0.1:{port}' + lines[1], port, 'browser')
            version = self._request(port, '/json/version', deadline)
            if isinstance(version, dict) and version.get('webSocketDebuggerUrl') == expected:
                return port
        except (OSError, ValueError, UnicodeError):
            pass
        return None

    def _ensure_browser(self, deadline):
        port = self._active_port(deadline)
        if port:
            return port
        _remaining(deadline)
        candidates = [
            Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
            Path(os.environ.get('LOCALAPPDATA', 'C:/Users/Default/AppData/Local')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe',
        ]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            raise BrowserError('Install Chrome or Edge to read the original website.', 'browser_unavailable')
        self._target_id = None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([str(executable), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                              '--user-data-dir=' + str(self.profile), '--no-first-run', '--no-default-browser-check',
                              '--new-window', 'about:blank'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        except OSError:
            raise BrowserError('Cannot start the private website browser.', 'browser_unavailable') from None
        while True:
            port = self._active_port(deadline)
            if port:
                return port
            time.sleep(min(.1, _remaining(deadline)))

    def _automation_target(self, port, deadline):
        target = None
        if self._target_id:
            targets = self._request(port, '/json/list', deadline)
            if isinstance(targets, list):
                target = next((item for item in targets if isinstance(item, dict) and item.get('id') == self._target_id), None)
        if target is None:
            target = self._request(port, '/json/new?' + quote('about:blank', safe=''), deadline, method='PUT')
        if not isinstance(target, dict) or not isinstance(target.get('id'), str):
            raise BrowserError('Cannot create the website reading tab.')
        websocket = _websocket_url(target.get('webSocketDebuggerUrl'), port, 'page')
        if websocket != f'ws://127.0.0.1:{port}/devtools/page/' + target['id']:
            raise BrowserError('The local browser returned a different reading tab.')
        self._target_id = target['id']
        return websocket

    def _evaluate(self, websocket, url, expression, deadline):
        script = Path(__file__).resolve().parent / 'windows' / 'browser-source.ps1'
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        payload = json.dumps({'websocket': websocket, 'url': url, 'expression': expression,
                              'timeout': _remaining(deadline)}, ensure_ascii=False).encode('utf-8')
        try:
            process = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                      '-File', str(script)], input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     timeout=_remaining(deadline), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = json.loads(process.stdout)
            if process.returncode != 0 or not isinstance(result, dict) or result.get('error'):
                code = 'browser_timeout' if isinstance(result, dict) and result.get('error') == 'browser_timeout' else 'browser_error'
                raise BrowserError('The website browser could not complete this read. Retry on the original website.', code)
            return result
        except subprocess.TimeoutExpired:
            raise BrowserError('The website browser timed out. Retry after the page has loaded.', 'browser_timeout') from None
        except BrowserError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise BrowserError('Cannot communicate with the private website browser.') from None
