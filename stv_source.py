"""Sáng Tác Việt over the independently verified .com HTTP sequence."""
import json
import random
import re
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from html import escape
from urllib.parse import parse_qs, urlencode, urlsplit

from htmltools import html_to_text, parse_html, safe_remote_url, safe_raster_data_url, sanitize_html
from source_errors import SourceError, SourceAccessError, SourceCaptchaError
from stv_http import StvHttp

ORIGIN = 'https://sangtacviet.com'
_MAX_HTML = 8 * 1024 * 1024
_MAX_WAITERS = 64
_PLACEHOLDER = re.compile(r'nhấp\s+vào\s+để\s+tải\s+chương|đang\s+tải\s+(?:nội\s+dung|chương)', re.I)


def _parts(url, chapter=False):
    parsed = urlsplit(url)
    match = re.fullmatch(r'/truyen/([a-zA-Z0-9_-]+)/1/([0-9]+)/(?:([0-9]+)/)?', parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'sangtacviet.com' or parsed.query or parsed.fragment
            or not match or bool(match[3]) != chapter):
        raise SourceError('URL truyện hoặc chương Sáng Tác Việt không hợp lệ.')
    return match.groups()


def _json(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        raise SourceError('Sáng Tác Việt trả dữ liệu JSON không hợp lệ.', code='parse_error') from error
    if not isinstance(value, dict):
        raise SourceError('Dữ liệu nguồn không phải object.', code='parse_error')
    return value


def _html(markup):
    if not isinstance(markup, str) or len(markup) > _MAX_HTML:
        raise SourceError('Nội dung nguồn không hợp lệ hoặc quá lớn.', code='parse_error')
    try:
        return parse_html(markup)
    except ValueError as error:
        raise SourceError('Không đọc được cấu trúc nội dung nguồn.', code='parse_error') from error


def _ciweimao_comments(root):
    for node in root.select('a.num-link[href]'):
        href = safe_remote_url(node.get('href'))
        if not href:
            continue
        link = urlsplit(href)
        if (link.scheme == 'https' and link.netloc == 'wap.ciweimao.com'
                and link.path == '/chapter/get_par_tsu_list' and not link.fragment):
            yield node, parse_qs(link.query, keep_blank_values=True).get('chapter_id', [])


class StvSource:
    def __init__(self, http=None, metadata_http=None):
        self.http = http if http is not None else StvHttp()
        self.metadata_http = metadata_http if metadata_http is not None else (http if http is not None else StvHttp())
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self._waiting = {False: deque(), True: deque()}
        self._reading = False
        self.closed = False
        self.pending = None
        self.tocs = {}

    def close(self):
        with self.condition:
            self.closed = True
            if self.pending:
                self.pending['event'].set()
                self.pending = None
            self.condition.notify_all()
        try: self.http.close()
        finally:
            if self.metadata_http is not self.http: self.metadata_http.close()

    @contextmanager
    def _reading_turn(self, background=False, cancelled=None):
        ticket = object()
        with self.condition:
            if self.closed:
                raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
            if sum(map(len, self._waiting.values())) >= _MAX_WAITERS:
                raise SourceError('Hàng đợi Sáng Tác Việt đang đầy; hãy thử lại sau.', code='source_busy')
            waiting = self._waiting[background]
            waiting.append(ticket)
            try:
                while True:
                    if self.closed:
                        raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
                    if cancelled is not None and cancelled():
                        raise SourceError('Đã dừng yêu cầu đang chờ lượt nguồn.', code='request_cancelled')
                    if not self._reading and waiting[0] is ticket and (not background or not self._waiting[False]):
                        self._reading = True
                        break
                    self.condition.wait(.1)
            finally:
                waiting.remove(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self._reading = False
                self.condition.notify_all()

    def _request(self, path, **options):
        with self.lock:
            if self.closed:
                raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
        channel = self.http if options.get('session') == 'reading' else self.metadata_http
        result = channel.request(path, **options)
        with self.lock:
            if self.closed:
                raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
        return result

    def _error(self, payload, url=None):
        code = str(payload.get('code', ''))
        if code == '21' and url is not None:
            with self.lock:
                if self.closed:
                    raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
                if self.pending is None:
                    self.pending = {'id': uuid.uuid4().hex, 'url': url, 'image': None,
                                    'generation': getattr(self.http, 'generation', 0), 'event': threading.Event(), 'verified': False}
                    self.pending['captcha'] = {'source': 'stv', 'id': self.pending['id']}
                raise SourceCaptchaError(self.pending['id'], self.pending)
        evidence = html_to_text(str(payload.get('err', '')))[:400]
        if re.search(r'\bvip\b', evidence, re.I) and re.search(r'chưa\s+(?:mua|mở\s+khóa)|không\s+có\s+quyền|(?:cần|phải|vui\s+lòng|yêu\s+cầu)\s+(?:mua|mở\s+khóa)', evidence, re.I):
            raise SourceAccessError('Chương VIP yêu cầu quyền đọc tại website gốc.', code='vip_locked', vip=True, evidence=evidence)
        if re.search(r'(?:vui\s+lòng|cần|hãy|phải)\s+đăng\s+nhập', evidence, re.I):
            raise SourceAccessError('Website gốc yêu cầu đăng nhập.', code='auth_required', evidence=evidence)
        raise SourceError('Sáng Tác Việt trả mã ' + (code or 'không rõ') + (': ' + evidence if evidence else '.'),
                          code='source_response_error')

    def _metadata(self, markup, url):
        root = _html(markup)
        def meta(name):
            node = root.first('meta[property="' + name + '"]')
            return node.get('content') if node else ''
        title = root.first('#book_name2')
        name = title.text() if title else meta('og:novel:book_name') or meta('og:title')
        if not name:
            raise SourceError('Không tìm thấy thông tin truyện Sáng Tác Việt.', code='parse_error')
        return {'name': name, 'author': meta('og:novel:author'), 'description': meta('og:description'),
                'cover': safe_remote_url(meta('og:image'), url), 'path': url, 'source': 'stv',
                'kind': 'text', 'format': 'online'}

    def search(self, query, page):
        fields = {'find': '', 'findinname': query, 'minc': 0, 'tag': '', 'p': page}
        path = '/io/searchtp/searchBooks?' + urlencode(fields)
        response = self._request(path, session='metadata')
        root = _html(response['text'])
        rows = root.select('a.booksearch')
        if not rows:
            if 'Không tìm thấy truyện?' in root.text() or not response['text'].strip():
                return {'items': [], 'next': None}
            raise SourceError('Chưa nhận diện được kết quả tìm kiếm nguồn.', code='parse_error')
        items, seen = [], set()
        for row in rows:
            name, author, tag = row.first('.searchbooktitle'), row.first('.searchbookauthor'), row.first('.searchtag')
            url = safe_remote_url(row.get('href'), ORIGIN)
            if not name or not name.text() or not url:
                raise SourceError('Kết quả thiếu tên hoặc liên kết truyện.', code='parse_error')
            parsed = urlsplit(url)
            if parsed.hostname not in ('sangtacviet.com', 'sangtacviet.vip'):
                raise SourceError('Liên kết tìm kiếm không thuộc Sáng Tác Việt.', code='parse_error')
            url = ORIGIN + parsed.path.rstrip('/') + '/'
            _parts(url)
            if url in seen:
                continue
            seen.add(url)
            image = row.first('img')
            cover = safe_remote_url(image.get('data-original') or image.get('data-src') or image.get('src'), ORIGIN) if image else ''
            items.append({'name': name.text(), 'author': author.text() if author else '', 'path': url,
                          'cover': cover, 'description': tag.text() if tag else ''})
        # shortcut: observed searchBooks page size is 48; update if the source changes pagination.
        return {'items': items, 'next': page + 1 if len(rows) >= 48 else None}

    def cover(self, url):
        _parts(url)
        book = self._metadata(self._request(url, session='metadata', navigation=True)['text'], url)
        if not book['cover']:
            raise SourceError('Trang truyện không cung cấp bìa hợp lệ.', code='parse_error')
        return {'cover': book['cover'], 'path': url}

    def _toc(self, host, book):
        url = ORIGIN + '/truyen/' + host + '/1/' + book + '/'
        path = '/index.php?' + urlencode({'ngmar': 'chapterlist', 'h': host, 'bookid': book, 'sajax': 'getchapterlist'})
        response = _json(self._request(path, session='metadata', referer=url)['text'])
        if str(response.get('code')) != '1':
            self._error(response)
        def entries(value):
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_HTML:
                raise SourceError('Mục lục nguồn trống hoặc không hợp lệ.', code='parse_error')
            result = []
            for row in value.strip().split('-//-'):
                fields = row.strip().split('-/-', 2)
                if len(fields) != 3 or not re.fullmatch(r'[0-9]+', fields[1]) or not fields[2].strip():
                    raise SourceError('Mục lục thiếu mã hoặc tên chương.', code='parse_error')
                result.append((fields[1], fields[2].strip()))
            return result
        translated = entries(response.get('data'))
        original = entries(response['oridata']) if response.get('oridata') else translated
        if [c[0] for c in translated] != [c[0] for c in original]:
            raise SourceError('Nguồn chưa trả đủ mục lục tiếng Việt.', code='parse_error')
        chapters, seen = [], set()
        for cid, title in translated:
            if cid not in seen:
                seen.add(cid)
                chapters.append({'index': len(chapters), 'title': title, 'url': url + cid + '/'})
        ids = frozenset(seen)
        with self.lock:
            self.tocs[(host, book)] = ids
        return chapters

    def detail(self, url):
        host, book, _ = _parts(url)
        result = self._metadata(self._request(url, session='metadata', navigation=True)['text'], url)
        result['chapters'] = self._toc(host, book)
        return result

    def _identity(self, payload, host, book, cid, response):
        requested = urlsplit(response.get('request_url') if isinstance(response.get('request_url'), str) else '')
        query = parse_qs(requested.query, keep_blank_values=True)
        request_matches = (response.get('request_method') == 'POST' and requested.scheme == 'https'
            and requested.netloc == 'sangtacviet.com' and requested.path == '/index.php' and not requested.fragment
            and all(query.get(key) == [value] for key, value in
                    (('bookid', book), ('h', host), ('c', cid), ('ngmar', 'readc'), ('sajax', 'readchapter'))))
        expected = {'qidian': 'https://m.qidian.com/book/' + book + '/' + cid + '.html',
                    'fanqie': 'https://fanqienovel.com/reader/' + cid}
        origin = payload.get('origin')
        # Cached providers can omit origin; the verified POST still binds the exact book and chapter.
        origin_matches = origin == expected[host] if host in expected else origin in ('', None)
        if host not in expected and isinstance(origin, str) and safe_remote_url(origin):
            parsed = urlsplit(origin)
            # Other providers must expose the exact numeric chapter as a whole path component.
            origin_matches = (not parsed.query and not parsed.fragment and
                parsed.path.rstrip('/').rsplit('/', 1)[-1] in (cid, cid + '.html', cid + '.htm'))
            if parsed.hostname in ('sangtacviet.com', 'sangtacviet.vip'):
                origin_matches = origin_matches and parsed.path == '/truyen/' + host + '/1/' + book + '/' + cid + '/'
            if host == 'ciweimao' and parsed.hostname not in ('ciweimao.com', 'www.ciweimao.com', 'wap.ciweimao.com', 'sangtacviet.com', 'sangtacviet.vip'):
                origin_matches = False
        if host == 'ciweimao':
            root = _html(payload.get('data'))
            nodes = root.select('a.num-link')
            if nodes:
                references = list(_ciweimao_comments(root))
                origin_matches = origin_matches and len(references) == len(nodes) and all(ids == [cid] for _, ids in references)
        if (not request_matches or str(payload.get('bookid')) != book or payload.get('bookhost') != host or not origin_matches
                or cid not in self.tocs[(host, book)]):
            raise SourceError('Chưa xác minh được đúng nguồn/truyện/chương từ phản hồi Sáng Tác Việt; không lưu nội dung không chắc chắn.', code='parse_error')

    def _clean(self, markup, url):
        root = _html(markup)
        if urlsplit(url).path.startswith('/truyen/ciweimao/'):
            for counter, _ in list(_ciweimao_comments(root)):
                counter.parent.children.remove(counter)
            # The source's Ciweimao preprocess removes attribute-free spans before rendering.
            for noise in list(root.select('span')):
                if not noise.attrs:
                    noise.parent.children.remove(noise)
        for token in root.select('i[t][h], i[t][v]'):
            token.tag = 'span'
        clean = sanitize_html(root.inner_html(), url)
        text = html_to_text(clean)
        if not text or _PLACEHOLDER.search(text) or '\ufffd' in text:
            raise SourceError('Nguồn trả chương trống, chưa tải xong hoặc lỗi mã hóa.', code='parse_error')
        if not re.search(r'<(?:p|div|br|h[1-6])\b', clean, re.I):
            clean = ''.join('<p>' + escape(line) + '</p>' for line in text.splitlines() if line.strip())
        return clean, text

    def chapter(self, url, *, background=False, cancelled=None):
        with self._reading_turn(background, cancelled):
            host, book, cid = _parts(url, chapter=True)
            with self.lock:
                if self.pending is not None:
                    raise SourceCaptchaError(self.pending['id'], self.pending)
            if (host, book) not in self.tocs or cid not in self.tocs[(host, book)]:
                self._toc(host, book)
            if cid not in self.tocs[(host, book)]:
                raise SourceError('Chương không có trong mục lục nguồn.', code='parse_error')
            book_url = ORIGIN + '/truyen/' + host + '/1/' + book + '/'
            document = self._request(url, session='reading', navigation=True, referer=book_url)['text']
            api = '/index.php?' + urlencode({'bookid': book, 'h': host, 'c': cid, 'ngmar': 'readc',
                                            'sajax': 'readchapter', 'sty': 1, 'exts': ''})
            if '/stv.readinit.js' in document:
                bootstrap = _json(self._request(api, session='reading', method='POST', seed=True, xhr=True, referer=url)['text'])
                if str(bootstrap.get('code')) not in ('7', '0'):
                    self._error(bootstrap, url)
            response = self._request(api, session='reading', method='POST', body='', referer=url)
            payload = _json(response['text'])
            if str(payload.get('code')) != '0':
                self._error(payload, url)
            self._identity(payload, host, book, cid, response)
            clean, text = self._clean(payload.get('data'), url)
            chinese = lambda value: len(re.findall(r'[\u3400-\u9fff]', value)) > len(re.findall(r'[A-Za-z\u00c0-\u024f\u1e00-\u1eff]', value))
            if chinese(text):
                translated = self._request('/index.php?ngmar=trans&langhint=chinese', session='metadata',
                    method='POST', body=urlencode({'ajax': 'trans', 'content': text}), referer=ORIGIN + '/trans/')['text']
                try:
                    envelope = json.loads(translated)
                except (ValueError, TypeError):
                    envelope = None
                if isinstance(envelope, (dict, list)):
                    # Only the chapter/read CAPTCHA workflow is verified; do not invent a Trans verification purpose.
                    confirmation = isinstance(envelope, dict) and str(envelope.get('code')) == '21'
                    message = 'Trans yêu cầu xác minh riêng; chưa có bản dịch hợp lệ.' if confirmation else 'Trans trả phản hồi lỗi thay vì nội dung dịch.'
                    raise SourceError(message, code='translation_verification_required' if confirmation else 'translation_error')
                clean, text = self._clean(translated, url)
                if chinese(text):
                    raise SourceError('Trans chưa trả nội dung tiếng Việt hợp lệ.', code='parse_error')
            return {'html': clean, 'text': text, 'images': [n.get('src') for n in parse_html(clean).select('img')],
                    'language': 'vi', 'translation': 'source'}

    def captcha(self, action, challenge_id, answer=None):
        with self._reading_turn():
            with self.lock:
                pending = self.pending
                if not pending or not isinstance(challenge_id, str):
                    raise SourceError('Yêu cầu CAPTCHA đã hết hiệu lực. Hãy mở lại chương.', code='captcha_expired')
                # A cancel can already be in flight when refresh rotates the image ID; old IDs cannot verify.
                if action == 'cancel' and challenge_id in (pending['id'], pending.get('previous_id')):
                    pending['event'].set()
                    self.pending = None
                    return {'cancelled': True}
                if challenge_id != pending['id']:
                    raise SourceError('Ảnh CAPTCHA đã thay đổi. Hãy dùng ảnh mới.', code='captcha_expired')
                if getattr(self.http, 'generation', 0) != pending['generation']:
                    pending['event'].set()
                    self.pending = None
                    raise SourceError('Phiên HTTP đã thay đổi; cần mở lại chương.', code='captcha_expired')
            if action in ('image', 'refresh'):
                if action == 'refresh' or pending['image'] is None:
                    response = self._request('/generate_captcha.php?random=' + str(random.random()),
                        session='reading', referer=pending['url'], image=True, require_session=True)
                    image = safe_raster_data_url('data:' + response['content_type'] + ';base64,' + response['body_base64'])
                    if not image:
                        raise SourceError('Website trả ảnh CAPTCHA không hợp lệ.', code='parse_error')
                    with self.lock:
                        if self.closed:
                            raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
                        if action == 'refresh':
                            pending['previous_id'] = pending['id']
                            pending['id'] = uuid.uuid4().hex
                            pending['captcha']['id'] = pending['id']
                        pending['image'] = image
                return {'id': pending['id'], 'image': pending['image']}
            if action == 'verify':
                if not pending['image'] or not isinstance(answer, str) or not 4 <= len(answer.strip()) <= 128 or re.search(r'[\x00-\x1f\x7f]', answer):
                    raise SourceError('Hãy tải ảnh và nhập mã CAPTCHA từ 4 ký tự.', code='captcha_failed')
                body = urlencode({'ajax': 'verifycaptcha', 'token': answer.strip(), 'purpose': 'read', 'provider': 'sangtacviet'})
                # Verification has side effects: never automatically replay the user's answer.
                response = self._request('/index.php?ngmar=verifyca', session='reading', method='POST', body=body,
                    referer=pending['url'], fresh_connection=True, require_session=True)
                if response['text'].strip() != 'success':
                    raise SourceError('Xác minh CAPTCHA thất bại. Kiểm tra mã hoặc làm mới ảnh rồi thử lại.', code='captcha_failed')
                with self.lock:
                    if self.closed:
                        raise SourceError('Nguồn Sáng Tác Việt đã đóng.', code='network_error')
                    pending['verified'] = True
                    pending['event'].set()
                    self.pending = None
                return {'verified': True}
            raise SourceError('Thao tác CAPTCHA không hợp lệ.')
