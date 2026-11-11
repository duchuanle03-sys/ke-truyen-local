"""Loopback-only application API; original exports are never served as files."""
import base64
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import documents
import legacy_formats
import pdfocr
from htmltools import html_to_text, sanitize_html, safe_raster_data_url
from library import Store, image_urls
from qt import QtDictionary
from sources import SourceService, validate_public_url, safe_fetch_bytes
from source_errors import SourceError, SourceAccessError, SourceCaptchaError

ROOT = Path(__file__).resolve().parent
MAX_BODY = 128 * 1024 * 1024

def public_content(value):
    if isinstance(value, list):
        return [public_content(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: public_content(item) for key, item in value.items()}
    cover = result.get('cover')
    if isinstance(cover, str):
        if cover.startswith(('https://', 'http://')):
            result['cover'] = ('/api/books/' + quote(str(result['id']), safe='') + '/cover?v=' + hashlib.sha256(cover.encode()).hexdigest()
                               if result.get('id') and 'source' in result else
                               '/api/image?' + urlencode({'url': cover, 'referer': result.get('path', '')}))
        else:
            result['cover'] = safe_raster_data_url(cover)
    if isinstance(result.get('html'), str):
        markup = sanitize_html(result['html'], allow_data_images=True)
        result['html'] = re.sub(r'(<img\b[^>]*\bsrc=")(https?://[^"]+)(")',
            lambda match: match[1] + html.escape('/api/image?' + urlencode({'url': html.unescape(match[2])}), quote=True) + match[3], markup)
    return result

class App:
    def __init__(self, data_dir, source_service=None):
        self.store = Store(data_dir)
        self.sources = source_service or SourceService()
        self.qt = QtDictionary(self.store.data_dir)
        self.uploads = self.store.data_dir / "files"
        self.uploads.mkdir(exist_ok=True)
        self.image_dir = self.store.data_dir / "images"
        self.image_dir.mkdir(exist_ok=True)
        self.jobs = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chapter-download")
        self._download_futures = {}
        self._download_verifications = {}
        self._download_stop = threading.Event()
        self._download_wake = threading.Event()
        self.store.recover_downloads()
        self._asset_stop = threading.Event()
        self._next_trash_cleanup = 0
        self.cleanup_trash()
        self._asset_worker = threading.Thread(target=self._maintain_assets, name='asset-maintenance', daemon=True)
        self._asset_worker.start()
        self._download_worker = threading.Thread(target=self._maintain_downloads, name='download-queue', daemon=True)
        self._download_worker.start()
        self._download_wake.set()

    def _cache_job(self, job):
        self.jobs[job['id']] = job
        # SQLite owns the full queue/history; retain only a bounded live view in RAM.
        for key in list(self.jobs):
            if len(self.jobs) <= 100:
                break
            if key not in self._download_futures:
                del self.jobs[key]
        return job

    def _schedule_downloads(self):
        with self.lock:
            if self._download_stop.is_set():
                return
            self._resume_verified_downloads()
            self._download_futures = {key: future for key, future in self._download_futures.items() if not future.done()}
            slots = 2 - len(self._download_futures)
            if slots:
                for saved in self.store.queued_downloads(tuple(self._download_futures), slots):
                    job = self._cache_job(self.jobs.get(saved['id'], saved))
                    future = self.executor.submit(self._download, job)
                    self._download_futures[job['id']] = future
                    future.add_done_callback(lambda _: self._download_wake.set())

    def _resume_verified_downloads(self):
        ready = []
        for job_id, verification in self._download_verifications.items():
            if verification['event'].is_set():
                ready.append((job_id, verification))
                if len(ready) == 32:
                    break
        saved = self.store.download_jobs_by_ids([job_id for job_id, _ in ready if job_id not in self.jobs])
        resolved = []
        for job_id, verification in ready:
            current = self.jobs.get(job_id) or saved.get(job_id)
            if current is None:
                del self._download_verifications[job_id]
                continue
            job = dict(current)
            job['captcha'] = None
            if not job['cancelled']:
                if verification['verified']:
                    job.update(status='paused' if job['status'] == 'paused' else 'queued', error='')
                    job.pop('error_code', None)
                    job.pop('access', None)
                else:
                    job.update(status='paused' if job['status'] == 'paused' else 'failed',
                               error='Đã hủy xác minh. Các chương đã tải vẫn được giữ; có thể thử lại phần còn lại.',
                               error_code='captcha_cancelled')
            resolved.append(job)
        if resolved:
            self.store.save_downloads(resolved)
            for job in resolved:
                if job['id'] in self.jobs:
                    self.jobs[job['id']].update(job)
                else:
                    self._cache_job(job)
                del self._download_verifications[job['id']]
        if len(ready) == 32:
            self._download_wake.set()

    def _maintain_downloads(self):
        while not self._download_stop.is_set():
            self._download_wake.wait()
            self._download_wake.clear()
            try:
                self._schedule_downloads()
            except Exception:
                logging.exception('Cannot dispatch persisted downloads; will retry.')
                if not self._download_stop.wait(1):
                    self._download_wake.set()

    def list_jobs(self, offset=0):
        with self.lock:
            page = self.store.download_jobs(offset)
            page['jobs'] = [dict(self.jobs.get(job['id'], job)) for job in page['jobs']]
            for job in page['jobs']:
                if verification := self._download_verifications.get(job['id']):
                    job['captcha'] = verification['captcha']
            return page

    def _maintain_assets(self):
        try:
            while not self._asset_stop.is_set():
                try:
                    assets = self.store.index_assets(stop=self._asset_stop)
                    search = self.store.index_search(stop=self._asset_stop)
                    inventory = self.store.inventory_assets(stop=self._asset_stop)
                    if self._asset_stop.is_set():
                        break
                    if self.store.asset_index_ready():
                        self.store.queue_cache_evictions(stop=self._asset_stop)
                        if self.store.cleanup_files(self._asset_stop):
                            continue
                    if assets or search or inventory:
                        self._asset_stop.wait(.01)
                        continue
                except Exception:
                    logging.exception('Cannot maintain asset index; will retry.')
                self._asset_stop.wait(.25)
        finally:
            self.store.close_inventory()

    def close(self):
        with self.lock:
            if self._download_stop.is_set():
                return
            self._download_stop.set()
            self._download_wake.set()
            self.store.save_downloads(self.jobs.values())
        self._download_worker.join(timeout=1)
        self._asset_stop.set()
        self._asset_worker.join(timeout=1)
        self.executor.shutdown(wait=False, cancel_futures=True)
        if callable(getattr(self.sources, 'close', None)):
            self.sources.close()
        wait(tuple(self._download_futures.values()), timeout=1)
        self.qt.close()

    def chapter(self, book_id, index):
        book = self.store.get_book(book_id)
        chapter = self._chapter_content(book, index)
        chapter["images"] = ["/api/image?" + urlencode({"url": url, "referer": book["path"]})
                             if url.startswith(("http://", "https://")) else url for url in chapter["images"]]
        return chapter

    def _chapter_content(self, book, index):
        book_id = book['id']
        chapter = self.store.content(book_id, index)
        if not chapter["downloaded"]:
            if book["source"] not in ("qq", "full", "hako", "stv"):
                raise ValueError("Chương chưa có nội dung. Hãy nhập tệp hoặc chọn lại nguồn tương ứng.")
            try:
                content = self.sources.chapter(book["source"], chapter["url"])
            except SourceAccessError as error:
                self.store.chapter_access(book_id, index, error.access)
                raise
            chapter = self.store.save_content(book_id, index, content)
        return chapter

    def add_source(self, source, url):
        detail = self.sources.detail(source, url)
        chapters = detail.pop("chapters")
        detail["source"] = source
        book = self.store.upsert_book(detail, chapters)
        return self.store.restore_book(book['id']) if book['trashed_at'] else book

    def import_file(self, name, body):
        clean_name = Path(name.replace("\\", "/")).name
        extension = Path(clean_name).suffix.lower()
        if len(clean_name) > 240 or not extension:
            raise ValueError("Tên tệp không hợp lệ.")
        key = hashlib.sha256(body).hexdigest()
        path = self.uploads / (key + extension)
        created = False
        try:
            with self.store.use_asset(path):
                created = not path.exists()
                if created:
                    self.store.queue_assets((path,))
                    path.write_bytes(body)
                if extension == '.pdf':
                    result = pdfocr.extract_pdf(path)
                elif extension in legacy_formats.SUPPORTED_EXTENSIONS:
                    result = legacy_formats.import_legacy(path)
                else:
                    result = documents.import_document(path)
                book = dict(result["book"], source="file", path=str(path))
                if book.get("name") in (key, path.stem) or not book.get("name"):
                    book["name"] = Path(clean_name).stem
                saved = self.store.upsert_book(book, result["chapters"])
                saved = self.store.restore_book(saved['id']) if saved['trashed_at'] else saved
                if result.get('warnings'):
                    saved['warnings'] = result['warnings']
                return saved
        except Exception:
            # Another import may already be using or have saved the same content-addressed file.
            if created:
                self.store.cleanup_file(path)
            raise

    def trash_book(self, book_id):
        with self.lock:
            book = self.store.trash_book(book_id)
            saved = self.store.active_download(book_id)
            if saved:
                job = self._cache_job(self.jobs.get(saved['id'], saved))
                self._cancel_download(job)
        return book

    def purge_book(self, book_id):
        with self.lock:
            if self.store.active_download(book_id):
                raise ValueError('Đang dừng tác vụ tải của truyện. Hãy thử lại sau khi tác vụ dừng.')
            self.store.purge_book(book_id)
            self.jobs = {key: job for key, job in self.jobs.items() if job['book_id'] != book_id}
        return {'id': book_id, 'deleted': True}

    def cleanup_trash(self):
        if time.monotonic() < self._next_trash_cleanup:
            return
        self._next_trash_cleanup = time.monotonic() + 60
        with self.lock:
            active = self.store.active_download_books()
            removed = set(self.store.purge_expired(skip_ids=active))
            self.jobs = {key: job for key, job in self.jobs.items() if job['book_id'] not in removed}

    def image(self, url, referer="", offline_owner=None):
        url = validate_public_url(url, resolve=False)
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.image_dir / key
        with self.store.use_asset(path):
            if path.exists():
                body = path.read_bytes()
                self.store.touch_image(path, len(body))
                if offline_owner is not None:
                    self.store.pin_image(path, *offline_owner)
            else:
                if offline_owner is not None:
                    self.store.pin_image(path, *offline_owner)
                if referer:
                    referer = validate_public_url(referer, resolve=False)
                try:
                    body = safe_fetch_bytes(url, headers={"Referer": referer, "Accept": "image/*"})
                except SourceError as error:
                    if (error.code != 'http_error' or error.http_status != 403
                            or (urlsplit(referer).hostname or '').removeprefix('www.') not in ('sangtacviet.com', 'sangtacviet.vip')):
                        raise
                    # STV's cover uses no-referrer; browsers also upgrade its mixed-content image to HTTPS.
                    body = safe_fetch_bytes(urlsplit(url)._replace(scheme='https').geturl(), headers={"Accept": "image/*"})
                if len(body) > 20 * 1024 * 1024:
                    raise ValueError("Ảnh vượt giới hạn 20 MiB.")
                content_type = image_type(body)
                if content_type == "application/octet-stream":
                    raise ValueError("Nguồn trả về dữ liệu không phải ảnh được hỗ trợ.")
                try:
                    with self.store.cache_image_write(path, len(body)) as admitted:
                        if admitted:
                            temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                            self.store.queue_assets((path, temporary))
                            try:
                                temporary.write_bytes(body)
                                temporary.replace(path)
                            finally:
                                temporary.unlink(missing_ok=True)
                except OSError:
                    if offline_owner is not None:
                        raise
                    logging.warning('Image could not be cached; serving the fetched response.', exc_info=True)
        return body, image_type(body)

    def cover(self, book_id):
        book = self.store.get_book(book_id)
        try:
            return self.image(book['cover'], book['path'])
        except ValueError as original:
            # Mobile exports retain old CDN URLs; refresh only the cover, never the reading state or TOC.
            try:
                metadata = self.sources.cover(book['source'], book['path'])
                url = validate_public_url(metadata['cover'], resolve=False)
                with self.store.use_asset(self.image_dir / hashlib.sha256(url.encode()).hexdigest()):
                    body = self.image(url, metadata['path'])
                    self.store.update_book(book_id, {'cover': metadata['cover']})
            except ValueError as error:
                raise ValueError('Không tải được bìa cũ hoặc bìa trên trang nguồn: ' + str(error)) from original
            return body

    def shortcut(self, book_id, port):
        from shortcuts import create_shortcut
        book = self.store.get_book(book_id)
        if book['trashed_at']:
            raise ValueError('Hãy khôi phục truyện khỏi thùng rác trước khi tạo lối tắt.')
        cover_bytes = b''
        if book['cover']:
            embedded = safe_raster_data_url(book['cover'])
            if embedded:
                cover_bytes = base64.b64decode(embedded.partition(',')[2], validate=True)
            else:
                try:
                    cover_bytes, _ = self.cover(book_id)
                except (ValueError, OSError):
                    pass
        result = create_shortcut(book_id=book['id'], title=book['name'], cover_bytes=cover_bytes,
                                 data_dir=self.store.data_dir, port=port)
        if not result['used_cover']:
            result['warning'] = 'Chưa lấy được bìa hợp lệ; lối tắt dùng biểu tượng mặc định.'
        return result

    def download(self, book_id, start=0, count=-1):
        book = self.store.get_book(book_id)
        start, count = int(start), int(count)
        if start < 0 or start >= book["chapter_count"] or count == 0 or count < -1:
            raise ValueError("Khoảng chương không hợp lệ.")
        end = book["chapter_count"] if count == -1 else min(book["chapter_count"], start + count)
        with self.lock:
            if self.store.get_book(book_id)['trashed_at']:
                raise ValueError('Hãy khôi phục truyện khỏi thùng rác trước khi tải.')
            if self._download_stop.is_set():
                raise ValueError('Máy chủ đang dừng; tác vụ đã lưu sẽ tiếp tục khi mở lại.')
            if self.store.active_download(book_id):
                raise ValueError("Truyện đang có trong hàng đợi tải.")
            job = {"id": uuid.uuid4().hex, "book_id": book_id, "name": book["name"],
                   "start": start, "end": end, "total": end - start, "completed": 0,
                   "status": "queued", "error": "", "cancelled": False, "captcha": None}
            self.store.create_download(job)
            self._cache_job(job)
            self._schedule_downloads()
        return dict(job)

    def _download(self, job):
        checkpoint_completed, checkpoint_time = job['completed'], time.monotonic()
        try:
            with self.lock:
                if self._download_stop.is_set():
                    return
                if job['cancelled']:
                    job['status'] = 'cancelled'
                    return
                job.update(status='running', error='', captcha=None)
                job.pop('error_code', None)
                job.pop('access', None)
                self.store.save_download(job)
            for index in range(job["start"] + job['completed'], job["end"]):
                with self.lock:
                    if self._download_stop.is_set() or job['status'] != 'running' or job['cancelled']:
                        return
                book = self.store.get_book(job["book_id"])
                try:
                    raw = self._chapter_content(book, index)
                except SourceCaptchaError as error:
                    if not error.verification:
                        raise
                    with self.lock:
                        if self._download_stop.is_set() or job['cancelled']:
                            return
                        self._download_verifications[job['id']] = error.verification
                        job.update(status='paused' if job['status'] == 'paused' else 'verification_required',
                                   error=str(error), captcha=error.captcha)
                    # Keep the HTTP verification context, not an occupied download thread.
                    return
                for url in image_urls(raw["images"], (raw['html'], raw.get('original_html')), book['path']):
                    if self._download_stop.is_set():
                        return
                    self.image(url, book["path"], offline_owner=(book['id'], index))
                with self.lock:
                    if self._download_stop.is_set():
                        return
                    job["completed"] += 1
                    if job['completed'] - checkpoint_completed >= 10 or time.monotonic() - checkpoint_time >= 2:
                        self.store.save_download(job)
                        checkpoint_completed, checkpoint_time = job['completed'], time.monotonic()
                    if job['status'] != 'running' or job['cancelled']:
                        return
            with self.lock:
                if not self._download_stop.is_set():
                    job["status"] = "cancelled" if job['cancelled'] else "completed"
        except Exception as error:
            with self.lock:
                if self._download_stop.is_set():
                    return
                job["status"] = "failed"
                job["error"] = str(error)
                if isinstance(error, SourceError):
                    job['error_code'] = error.code
                    if isinstance(error, SourceAccessError):
                        job['access'] = error.access
        finally:
            with self.lock:
                # close() persisted the last snapshot; a late request must not overwrite a new server's recovery.
                if not self._download_stop.is_set():
                    if job['cancelled']:
                        job.update(status='cancelled', captcha=None)
                        self._download_verifications.pop(job['id'], None)
                    self.store.save_download(job)

    def _cancel_download(self, job):
        job['cancelled'] = True
        job['captcha'] = None
        self._download_verifications.pop(job['id'], None)
        future = self._download_futures.get(job['id'])
        if future is None or future.done():
            job['status'] = 'cancelled'
        self.store.save_download(job)

    def job_action(self, job_id, action):
        with self.lock:
            if self._download_stop.is_set():
                raise ValueError('Máy chủ đang dừng; hãy mở lại ứng dụng để quản lý tác vụ.')
            job = self._cache_job(self.jobs.get(job_id) or self.store.download_job(job_id))
            if action == "pause" and job["status"] == "running":
                job["status"] = "paused"
            elif action == "resume" and job["status"] == "paused":
                verification = self._download_verifications.get(job_id)
                job.update(status='verification_required' if verification else 'queued',
                           captcha=verification['captcha'] if verification else None)
            elif action == "cancel":
                self._cancel_download(job)
            elif action == "retry" and job["status"] in ("failed", "cancelled"):
                if job['completed'] < job['total']:
                    return self.download(job["book_id"], job["start"] + job["completed"],
                                         job["total"] - job["completed"])
                job.update(status='completed', cancelled=False)
            elif action == "delete" and job["status"] not in ("running", "queued", "paused", "verification_required"):
                self.store.delete_download(job_id)
                del self.jobs[job_id]
                self._download_verifications.pop(job_id, None)
                return dict(job)
            else:
                raise ValueError("Không thể thực hiện thao tác với trạng thái hiện tại.")
            self.store.save_download(job)
            self._schedule_downloads()
            return dict(job)

    def captcha(self, source, action, challenge_id, answer=None):
        try:
            return self.sources.captcha(source, action, challenge_id, answer)
        finally:
            # Verification can resolve or expire a shared session even when the HTTP action raises.
            self._download_wake.set()

    def capabilities(self):
        formats = documents.capabilities()
        formats['legacy'] = legacy_formats.capabilities()
        formats['supported_extensions'] = sorted(set(formats['supported_extensions']) | set(legacy_formats.SUPPORTED_EXTENSIONS))
        ocr = pdfocr.capabilities()
        formats['ocr'] = ocr
        formats['pdf'] = {"supported": ocr.get('supported', False), "details": ocr}
        if ocr.get('supported'):
            formats['supported_extensions'] = sorted(set(formats['supported_extensions']) | {'.pdf'})
        return {"name": "Kệ Truyện", "documents": formats,
                "sources": self.sources.sources(), "qt": self.qt.status(),
                "tts": {"browser": True, "offline": "Theo giọng localService của trình duyệt",
                        "online": "Theo giọng online của trình duyệt",
                        "audio_export": False, "voice_models": False},
                "ocr": ocr,
                "data_directory": str(self.store.data_dir),
                "limitations": ["Chưa nghiệm thu toàn bộ requirements. Giọng TTS phải có trên máy/trình duyệt.",
                                "PDF/OCR và định dạng phụ thuộc converter được báo theo engine thực có."]}

def image_type(body):
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"

class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not hasattr(socket, 'SO_EXCLUSIVEADDRUSE')

    def server_bind(self):
        # Windows SO_REUSEADDR lets stale servers share the port and receive requests.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def service_actions(self):
        try:
            self.app.cleanup_trash()
        except Exception:
            logging.exception('Cannot clean up expired trash; will retry.')

    def server_close(self):
        super().server_close()
        if hasattr(self, 'app'):
            self.app.close()

class Handler(BaseHTTPRequestHandler):
    server_version = "KeTruyen"
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):
        logging.info(format, *args)

    def _guard(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise PermissionError("Địa chỉ máy chủ không hợp lệ.")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed}:
            raise PermissionError("Ứng dụng chỉ nhận yêu cầu từ cửa sổ cục bộ của nó.")
        if self.command not in ("GET", "HEAD") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("Từ chối yêu cầu ngoài ứng dụng.")

    def _reply(self, status, value, content_type="application/json; charset=utf-8", extra=None):
        body = json.dumps(public_content(value), ensure_ascii=False, allow_nan=False).encode() if content_type.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' https: http: data:; media-src 'self' blob: data:; "
                         "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "private, max-age=60")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Không hỗ trợ kiểu truyền dữ liệu này.")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ValueError("Tệp vượt giới hạn tải lên 128 MiB.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Dữ liệu tải lên chưa đầy đủ.")
        return body

    def _json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Yêu cầu phải dùng application/json.")
        raw = self._body()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Dữ liệu JSON quá lớn.")
        def invalid(value):
            raise ValueError("Giá trị số không hợp lệ: " + value)
        result = json.loads(raw or b"{}", parse_constant=invalid)
        if not isinstance(result, dict):
            raise ValueError("Dữ liệu JSON phải là object.")
        return result

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._reply(403, {"error": "Không cho phép truy cập API từ website khác."})

    def _dispatch(self):
        try:
            self._guard()
            self._route()
        except SourceError as error:
            result = {'error': str(error), 'code': error.code}
            if error.http_status is not None:
                result['http_status'] = error.http_status
            if isinstance(error, SourceAccessError):
                result['access'] = error.access
            if isinstance(error, SourceCaptchaError):
                result['captcha'] = error.captcha
            status = 409 if error.code == 'captcha_required' else 403 if error.code in ('vip_locked', 'auth_required') else 503 if error.code in ('network_error', 'source_timeout') else 400
            self._reply(status, result)
        except PermissionError as error:
            self._reply(403, {"error": str(error)})
        except (KeyError, FileNotFoundError) as error:
            self._reply(404, {"error": str(error).strip("'")})
        except (ValueError, json.JSONDecodeError) as error:
            self._reply(400, {"error": str(error)})
        except (URLError, TimeoutError, ConnectionError) as error:
            self._reply(503, {"error": "Không kết nối được nguồn: " + str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Application request failed")
            self._reply(500, {"error": "Có lỗi khi xử lý. Dữ liệu gốc chưa bị thay đổi."})

    def _route(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        q = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        parts = [piece for piece in path.split("/") if piece]
        method = "GET" if self.command == "HEAD" else self.command
        store = self.app.store
        if path == '/api/instance' and method == 'GET':
            return self._reply(200, {'app': 'ke-truyen-local', 'data_directory': str(store.data_dir), 'pid': os.getpid()})
        if not path.startswith("/api/"):
            if method != "GET":
                raise KeyError("Không có đường dẫn này.")
            root = ROOT / "web"
            file = (root / (path.lstrip("/") or "index.html")).resolve()
            if not file.is_relative_to(root.resolve()):
                raise PermissionError("Không truy cập tệp ngoài giao diện ứng dụng.")
            if not file.is_file():
                raise FileNotFoundError("Không tìm thấy tệp giao diện.")
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            self._reply(200, file.read_bytes(), kind + ("; charset=utf-8" if kind.startswith("text/") or kind == "application/javascript" else ""))
            return
        if path == "/api/books" and method == "GET":
            return self._reply(200, store.list_books(query=q.get("q", ""), source=q.get("source", ""),
                               group=q.get("group", ""), sort=q.get("sort", "recent"), archived=q.get("archived") == "1",
                               trashed=q.get('trashed') == '1', kind=q.get('kind', 'all'),
                               reverse=q.get('reverse') == '1', offset=q.get('offset', 0), limit=q.get('limit', 60)))
        if path == "/api/groups" and method == "GET":
            return self._reply(200, store.groups())
        if path == "/api/settings":
            return self._reply(200, store.settings(self._json() if method == "POST" else None))
        if path == '/api/cache/images' and method == 'GET':
            return self._reply(200, store.image_cache_stats())
        if path == '/api/cache/images' and method == 'POST':
            return self._reply(200, store.set_image_cache_limit(self._json().get('max_bytes')))
        if path == '/api/cache/images/clear' and method == 'POST':
            self._json()
            return self._reply(200, store.clear_image_cache())
        if path == "/api/capabilities" and method == "GET":
            return self._reply(200, self.app.capabilities())
        if path == "/api/sources" and method == "GET":
            return self._reply(200, self.app.sources.sources())
        if path == '/api/sources/stv/captcha' and method == 'GET':
            return self._reply(200, self.app.captcha('stv', 'image', q.get('id')))
        if len(parts) == 5 and parts[:4] == ['api', 'sources', 'stv', 'captcha'] and method == 'POST':
            if parts[4] not in ('refresh', 'verify', 'cancel'):
                raise KeyError('Không có thao tác CAPTCHA này.')
            body = self._json()
            return self._reply(200, self.app.captcha('stv', parts[4], body.get('id'), body.get('answer')))
        if len(parts) == 4 and parts[:2] == ["api", "sources"]:
            source, action = parts[2:]
            if action == "search" and method == "GET":
                return self._reply(200, self.app.sources.search(source, q.get("q", ""), int(q.get("page", "1"))))
            if action == "detail" and method == "GET":
                return self._reply(200, self.app.sources.detail(source, q.get("url", "")))
            if action == "add" and method == "POST":
                return self._reply(200, self.app.add_source(source, self._json()["url"]))
        if path == "/api/import/backup" and method == "POST":
            source = self._json().get("path") or str(ROOT.parent)
            return self._reply(200, store.import_backup(source))
        if path == "/api/import/file" and method == "POST":
            return self._reply(200, self.app.import_file(unquote(self.headers.get("X-Filename", "book.txt")), self._body()))
        if len(parts) >= 3 and parts[:2] == ["api", "books"]:
            book_id = parts[2]
            if len(parts) == 3 and method == "GET":
                return self._reply(200, store.get_book(book_id))
            if len(parts) == 3 and method == 'DELETE':
                return self._reply(200, self.app.purge_book(book_id))
            if len(parts) == 4:
                action = parts[3]
                if action == 'shortcut' and method == 'POST':
                    self._json()
                    return self._reply(200, self.app.shortcut(book_id, self.server.server_address[1]))
                if action == 'trash' and method == 'POST':
                    self._json()
                    return self._reply(200, self.app.trash_book(book_id))
                if action == 'restore' and method == 'POST':
                    self._json()
                    return self._reply(200, store.restore_book(book_id))
                if action == 'cover' and method == 'GET':
                    body, kind = self.app.cover(book_id)
                    return self._reply(200, body, kind)
                if action == "file" and method == "GET":
                    book = store.get_book(book_id)
                    file = Path(book["path"]).resolve()
                    if not file.is_relative_to(self.app.uploads):
                        raise PermissionError("Tệp không thuộc thư viện nhập của ứng dụng.")
                    if file.suffix.lower() != '.pdf':
                        raise ValueError("Chức năng xem trang gốc hiện dành cho PDF.")
                    return self._reply(200, file.read_bytes(), "application/pdf", {"Content-Disposition": "inline; filename=document.pdf"})
                if action == "chapters" and method == "GET":
                    return self._reply(200, store.chapters(book_id))
                if action == "progress" and method == "POST":
                    body = self._json()
                    return self._reply(200, store.progress(book_id, body["index"], body["position"]))
                if action == "update" and method == "POST":
                    return self._reply(200, store.update_book(book_id, self._json()))
                if action == "search" and method == "GET":
                    return self._reply(200, store.search_content(book_id, q.get("q", ""), int(q.get('after', -1))))
                if action == "download" and method == "POST":
                    body = self._json()
                    return self._reply(200, self.app.download(book_id, body.get("start", 0), body.get("count", -1)))
            if len(parts) >= 5 and parts[3] == "chapters":
                index = int(parts[4])
                if len(parts) == 5 and method == "GET":
                    return self._reply(200, self.app.chapter(book_id, index))
                if len(parts) == 6 and method == "POST":
                    if parts[5] == 'original':
                        self._json()
                        book = store.get_book(book_id)
                        chapter = store.content(book_id, index)
                        return self._reply(200, self.app.sources.open_original(book['source'], chapter['url']))
                    if parts[5] == "edit":
                        body = self._json()
                        text = str(body.get("text", html_to_text(body.get("html", ""))))
                        safe = "<p>" + html.escape(text).replace("\n", "</p><p>") + "</p>"
                        return self._reply(200, store.edit_content(book_id, index,
                                           {"text": text, "html": safe} | ({"title": str(body["title"])} if "title" in body else {})))
                    if parts[5] == "restore":
                        self._json()
                        return self._reply(200, store.restore_content(book_id, index))
        if path == "/api/notes":
            if method == "GET":
                return self._reply(200, store.notes(q.get("book_id")))
            if method == "POST":
                return self._reply(200, store.save_note(self._json()))
        if len(parts) == 3 and parts[:2] == ["api", "notes"] and method == "DELETE":
            store.delete_note(parts[2])
            return self._reply(200, {"deleted": True})
        if path == "/api/jobs" and method == "GET":
            return self._reply(200, self.app.list_jobs(int(q.get('offset', 0))))
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and method == "POST":
            self._json()
            return self._reply(200, self.app.job_action(parts[2], parts[3]))
        if path == "/api/dictionaries":
            if method == "GET":
                if 'offset' in q or 'limit' in q:
                    return self._reply(200, self.app.qt.page(q.get('kind', 'name'), q.get('scope', '*'), q.get('q', ''),
                                                            int(q.get('offset', 0)), int(q.get('limit', 100))))
                return self._reply(200, self.app.qt.list(q.get("kind", "name"), q.get("scope", "*"), q.get("q", "")))
            if method == "POST":
                body = self._json()
                return self._reply(200, self.app.qt.save(body["kind"], body["word"], body["meaning"], body.get("scope", "*")))
            if method == "DELETE":
                return self._reply(200, self.app.qt.delete(q["kind"], q["word"], q.get("scope", "*")))
        if path == "/api/dictionaries/import" and method == "POST":
            body = self._json()
            return self._reply(200, self.app.qt.import_text(body["kind"], body["text"], body.get("mode", "merge"), body.get("scope", "*")))
        if path == "/api/dictionaries/export" and method == "GET":
            return self._reply(200, {"text": self.app.qt.export_text(q.get("kind", "name"), q.get("scope", "*"))})
        if path == "/api/dictionaries/rules/preview" and method == "POST":
            body = self._json()
            return self._reply(200, self.app.qt.preview_rule(body["pattern"], body["replacement"],
                                       body["sample"], body.get("book_id", "")))
        if path == "/api/qt" and method == "POST":
            body = self._json()
            return self._reply(200, {"text": self.app.qt.translate(body["text"], body.get("book_id", ""))})
        if path == "/api/image" and method == "GET":
            body, kind = self.app.image(q["url"], q.get("referer", ""))
            return self._reply(200, body, kind)
        raise KeyError("Không tìm thấy API.")

def make_server(data_dir=None, host="127.0.0.1", port=8765, source_service=None):
    if host != "127.0.0.1":
        raise ValueError("Ứng dụng chỉ lắng nghe trên 127.0.0.1.")
    server = HTTPServer((host, port), Handler)
    server.app = App(data_dir or ROOT / "user-data", source_service)
    return server
