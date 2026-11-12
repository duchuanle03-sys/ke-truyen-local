"""Admission, durable cancellation and bounded, retryable server cleanup."""
from collections import Counter
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import secrets
import threading
import time

from sources import _PublicFetcher, abort_socket


class ShutdownError(ValueError):
    def __init__(self, message, code='shutting_down', status=503, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class ServerLifecycle:
    @staticmethod
    def _acquire_library_lock(data_dir):
        directory = Path(data_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / '.server.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if not handle.seek(0, 2): handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BaseException:
            handle.close()
            raise OSError('Thư viện đang được một máy chủ khác sử dụng hoặc đang hoàn tất tắt.')

    def _init_lifecycle(self):
        self.session_token = secrets.token_urlsafe(32)
        self._request_cond = threading.Condition(threading.RLock())
        self._in_flight_requests = 0
        self._request_sockets = set()
        self._connections = set()
        self._critical_operations = Counter()
        self._shutting_down = False
        self._pause_persisted = False
        self._shutdown_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._close_wake = threading.Event()
        self._finalizer = None
        self._source_task = None
        self._source_error = None
        self._sources_closed = self._qt_closed = self._resources_closed = False
        self._image_fetcher = _PublicFetcher()

    @contextmanager
    def request_operation(self, connection):
        with self._request_cond:
            if self._shutting_down: raise ShutdownError('Máy chủ đang tắt; hãy mở lại ứng dụng sau khi hoàn tất.')
            self._in_flight_requests += 1
            self._request_sockets.add(connection)
        try: yield
        finally:
            with self._request_cond:
                self._request_sockets.discard(connection)
                self._in_flight_requests -= 1
                self._request_cond.notify_all()
            self._close_wake.set()

    @contextmanager
    def critical_operation(self, name):
        with self._request_cond:
            if self._shutting_down: raise ShutdownError('Máy chủ đang tắt; không nhận thao tác ghi mới.')
            self._critical_operations[name] += 1
        try: yield
        finally:
            with self._request_cond:
                self._critical_operations[name] -= 1
                if not self._critical_operations[name]: del self._critical_operations[name]
                self._request_cond.notify_all()
            self._close_wake.set()

    def begin_shutdown(self, pause_downloads=False, *, force=False, timeout=1):
        deadline = time.monotonic() + max(0, timeout)
        if not self._shutdown_lock.acquire(timeout=max(0, timeout)):
            raise ShutdownError('Đang lưu trạng thái tắt máy chủ.', 'busy_operation', 409)
        try:
            with self._request_cond:
                if self._pause_persisted: return {'accepted': True}
                if not force and (self._critical_operations or self.store._compacting):
                    names = ', '.join(self._critical_operations) or 'thu gọn SQLite'
                    raise ShutdownError('Hãy chờ thao tác kết thúc: ' + names, 'busy_operation', 409, operations=list(self._critical_operations))
                was_shutting = self._shutting_down
                self._shutting_down = True
            if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
                with self._request_cond: self._shutting_down = was_shutting
                raise ShutdownError('Thư viện đang ghi dữ liệu; hãy thử lại.', 'busy_operation', 409)
            try:
                active = self.store.active_download_count(timeout=max(0, deadline - time.monotonic()))
                if active and not (pause_downloads or force):
                    raise ShutdownError('Tạm dừng các tác vụ tải đang chạy và tắt máy chủ?', 'active_downloads', 409, active_downloads=active)
                self.store.pause_downloads(self.jobs.values(), timeout=max(0, deadline - time.monotonic()))
                for job in self.jobs.values():
                    if job['status'] in ('queued', 'running', 'verification_required'):
                        job['status'] = 'cancelled' if job['cancelled'] else 'paused'
                    job['captcha'] = None
                self._download_verifications.clear()
                self._pause_persisted = True
                self._download_stop.set(); self._download_wake.set(); self._asset_stop.set()
                return {'accepted': True}
            except BaseException:
                with self._request_cond: self._shutting_down = was_shutting
                raise
            finally: self.lock.release()
        finally: self._shutdown_lock.release()

    def _pending_operations(self):
        with self._request_cond:
            return bool(self._in_flight_requests or self._connections or self._critical_operations
                or self._asset_worker.is_alive() or self._download_worker.is_alive()
                or any(not f.done() for f in self._download_futures.values())
                or self.store._active_chapters or self.store._active_assets or self.store._database_users)

    def _start_source_close(self):
        if self._sources_closed or self._source_task is not None and self._source_task.is_alive(): return
        self._source_error = None
        def close():
            try:
                self._image_fetcher.close()
                if callable(getattr(self.sources, 'close', None)): self.sources.close()
                self._sources_closed = True
            except Exception as error:
                self._source_error = error
                logging.exception('Cannot close source transport; cleanup remains incomplete.')
            finally: self._close_wake.set()
        self._source_task = threading.Thread(target=close, name='source-close', daemon=True)
        self._source_task.start()

    def _retry_cleanup(self):
        if self._finalizer is not None and self._finalizer.is_alive(): return
        self._close_wake.clear()
        def finish():
            while not self._resources_closed:
                self._close_wake.wait(5); self._close_wake.clear()
                if self.close(): return
        self._finalizer = threading.Thread(target=finish, name='shutdown-cleanup', daemon=True)
        self._finalizer.start()

    def close(self, timeout=15):
        deadline = time.monotonic() + max(0, timeout)
        if not self._close_lock.acquire(timeout=max(0, timeout)): return self._resources_closed
        try:
            if self._resources_closed: return True
            try: self.begin_shutdown(True, force=True, timeout=min(1, max(0, deadline - time.monotonic())))
            except Exception:
                logging.exception('Cannot persist shutdown pause; cleanup remains incomplete.')
                with self._request_cond: self._shutting_down = True
                self._download_stop.set(); self._download_wake.set(); self._asset_stop.set()
                return False
            self.executor.shutdown(wait=False, cancel_futures=True)
            with self._request_cond: idle = self._connections - self._request_sockets
            for connection in idle: abort_socket(connection)
            grace = min(deadline, time.monotonic() + min(2, timeout * .4))
            while self._pending_operations() and time.monotonic() < grace:
                with self._request_cond: self._request_cond.wait(min(.05, max(0, grace - time.monotonic())))
            if self._source_task is not None and self._source_error is not None:
                self._source_task.join(timeout=max(0, deadline - time.monotonic()))
            self._start_source_close()
            if self._pending_operations():
                with self._request_cond: active = tuple(self._request_sockets | self._connections)
                for connection in active: abort_socket(connection)
            while (self._pending_operations() or not self._sources_closed) and time.monotonic() < deadline:
                if self._source_error is not None: return False
                with self._request_cond: self._request_cond.wait(min(.05, max(0, deadline - time.monotonic())))
            if self._pending_operations() or not self._sources_closed:
                logging.error('Shutdown drain deadline exceeded; live operations retain their resources. Cleanup will retry.')
                return False
            self._source_task.join(timeout=max(0, deadline - time.monotonic()))
            if self._source_task.is_alive(): return False
            if not self._qt_closed:
                if not self.qt._write_lock.acquire(timeout=max(0, deadline - time.monotonic())): return False
                try: self.qt.close(); self._qt_closed = True
                finally: self.qt._write_lock.release()
            self.store.close_inventory()
            self.store.checkpoint(timeout=min(1, max(0, deadline - time.monotonic())))
            self._library_lock.close()
            self._resources_closed = True
            return True
        except Exception:
            logging.exception('Shutdown cleanup incomplete; remaining resources will be retried.')
            return False
        finally:
            self._close_lock.release()
            if not self._resources_closed: self._retry_cleanup()
