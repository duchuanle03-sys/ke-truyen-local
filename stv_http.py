"""One private, persistent Node HTTP process; nothing is saved to a browser profile."""
import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path

from source_errors import SourceError


class StvHttp:
    def __init__(self):
        self.process = None
        self.generation = 0
        self.lock = threading.RLock()
        self.request_lock = threading.Lock()
        self.closed = False

    def _start(self):
        node = shutil.which('node')
        if not node:
            raise SourceError('Sáng Tác Việt cần Node.js 20 trở lên để tải HTTP. QQ, Full và Hako vẫn dùng bình thường.', code='transport_unavailable')
        try:
            self.process = subprocess.Popen([node, str(Path(__file__).with_name('stv_http_worker.mjs'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as error:
            raise SourceError('Không khởi động được HTTP Sáng Tác Việt.', code='transport_unavailable') from error
        self.generation += 1
        self.output = queue.Queue()
        def collect(process, output):
            try:
                for line in process.stdout:
                    output.put(line)
            finally:
                output.put(None)
        threading.Thread(target=collect, args=(self.process, self.output), daemon=True).start()

    def request(self, path, require_session=False, **options):
        with self.request_lock:
            with self.lock:
                if self.closed:
                    raise SourceError('Kênh HTTP Sáng Tác Việt đã đóng.', code='network_error')
                if self.process is None or self.process.poll() is not None:
                    if require_session:
                        raise SourceError('Phiên HTTP đã kết thúc. Hãy hủy xác minh và mở lại chương để nhận yêu cầu mới.', code='captcha_expired')
                    self._stop_locked()
                    self._start()
                process, output = self.process, self.output
                try:
                    process.stdin.write(json.dumps({'path': path, **options}, ensure_ascii=True) + '\n')
                    process.stdin.flush()
                except (OSError, ValueError) as error:
                    self._stop_locked()
                    raise SourceError('Mất kết nối với HTTP Sáng Tác Việt; hãy mở lại chương.', code='network_error') from error
            try:
                line = output.get(timeout=50)
                if line is None:
                    raise SourceError('HTTP Sáng Tác Việt đã dừng. Kiểm tra Node.js 20 trở lên rồi thử lại.', code='network_error')
                result = json.loads(line)
            except queue.Empty as error:
                with self.lock:
                    if self.process is process:
                        self._stop_locked()
                raise SourceError('HTTP Sáng Tác Việt hết thời gian chờ. Không tự gửi lại đáp án xác minh.', code='source_timeout') from error
            except (OSError, ValueError) as error:
                with self.lock:
                    if self.process is process:
                        self._stop_locked()
                raise SourceError('Mất kết nối với HTTP Sáng Tác Việt; hãy mở lại chương.', code='network_error') from error
            if not isinstance(result, dict):
                raise SourceError('Phản hồi HTTP nguồn không hợp lệ.', code='parse_error')
            if result.get('error'):
                raise SourceError(result['error'], code=result.get('code', 'network_error'), http_status=result.get('http_status'))
            return result

    def close(self):
        with self.lock:
            self.closed = True
            self._stop_locked()

    def _stop_locked(self):
        process, self.process = self.process, None
        if process is not None:
            self.output.put(None)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
