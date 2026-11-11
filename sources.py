"""Independent adapters for public QQ, Truyện Full and Hako pages."""
import base64
import binascii
import http.client
import json
import re
import socket
import threading
import webbrowser
import urllib.error
import urllib.request
from html import escape
from http.cookiejar import Cookie, CookieJar
from urllib.parse import quote, urlencode, urljoin, urlsplit

from htmltools import html_to_text, is_public_ip, parse_html, safe_remote_url, sanitize_html
from source_errors import SourceError


_SOURCES = {
    'qq': ('Truyện QQ', 'https://truyenqqko.com', {
        'truyenqqko.com', 'truyenqqgo.com', 'truyenqqviet.com', 'truyenqqto.com', 'truyenqqno.com',
        'truyenqq.com', 'truyenqqvn.com', 'truyenqqpro.com'}),
    'full': ('Truyện Full', 'https://truyenfull.live', {
        'truyenfull.live', 'truyenfull.vision', 'truyenfull.io', 'truyenfull.vn', 'truyenfull.com'}),
    'hako': ('Hako', 'https://docln.sbs', {'docln.sbs', 'docln.net', 'ln.hako.vn'}),
    'stv': ('Sáng Tác Việt', 'https://sangtacviet.com', {'sangtacviet.com', 'sangtacviet.vip'}),
}
_MAX_RESPONSE = 8 * 1024 * 1024


def _public_addresses(host, port):
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError('Không phân giải được tên miền: ' + str(exc), code='network_error') from exc
    if not answers or any(not is_public_ip(answer[4][0]) for answer in answers):
        raise SourceError('URL trỏ tới địa chỉ mạng không công khai')
    return answers


def validate_public_url(url, resolve=True):
    checked = safe_remote_url(url)
    if not checked:
        raise SourceError('Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng')
    parsed = urlsplit(checked)
    if resolve:
        _public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    return checked


def _public_socket(address, timeout=20, source_address=None, **kwargs):
    # Connect to the checked sockaddr itself so a second DNS lookup cannot rebind it.
    error = None
    for family, socktype, proto, _, sockaddr in _public_addresses(*address):
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            error = exc
            connection.close()
    raise error or SourceError('Không kết nối được tới nguồn')


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self._create_connection = _public_socket
        super().connect()


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self._create_connection = _public_socket
        super().connect()


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe = validate_public_url(newurl)
        if urlsplit(req.full_url).scheme == 'https' and urlsplit(safe).scheme != 'https':
            raise SourceError('Nguồn chuyển hướng HTTPS sang HTTP')
        return super().redirect_request(req, fp, code, msg, headers, safe)


class _PublicFetcher:
    def __init__(self):
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(),
            _HTTPSHandler(), _PublicRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))

    def _read(self, url, headers=None, max_bytes=_MAX_RESPONSE):
        url = validate_public_url(url)
        if type(max_bytes) is not int or max_bytes < 1:
            raise SourceError('Giới hạn tải không hợp lệ')
        request_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalReader/1.0',
            'Accept': 'text/html,application/json;q=0.9,image/*;q=0.8', 'Accept-Encoding': 'identity'}
        if headers:
            if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) or re.search(r'[\r\n]', k + v) for k, v in headers.items()):
                raise SourceError('HTTP headers không hợp lệ')
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                validate_public_url(response.url)
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise SourceError('Nội dung nguồn vượt giới hạn tải')
                return raw, response.headers.get_content_charset() or 'utf-8'
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SourceError('Nguồn trả HTTP ' + str(exc.code), code='http_error', http_status=exc.code) from exc
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise SourceError('Không tải được nguồn: ' + str(exc), code='network_error') from exc

    def __call__(self, url):
        for attempt in range(2):
            raw, encoding = self._read(url)
            try:
                text = raw.decode(encoding, errors='replace')
            except LookupError as exc:
                raise SourceError('Nguồn trả bảng mã không hợp lệ') from exc
            cookie = re.search(r'document\.cookie\s*=\s*([\"\'])([^\r\n]*?)\1', text)
            if attempt == 0 and len(text) < 5000 and urlsplit(url).hostname == 'truyenqqko.com' and cookie:
                pair = cookie[2].split(';', 1)[0]
                name, equal, value = pair.partition('=')
                if equal and re.fullmatch(r'[A-Za-z0-9_-]+', name) and re.fullmatch(r'[A-Za-z0-9._~%+/-]*', value):
                    self.cookies.set_cookie(Cookie(0, name, value, None, False, 'truyenqqko.com',
                        False, False, '/', True, url.startswith('https:'), None, True, None, None, {}))
                    continue
            return text
        raise SourceError('Nguồn yêu cầu kiểm tra trình duyệt; chưa tải được nội dung')


def safe_fetch_bytes(url, headers=None, max_bytes=20 * 1024 * 1024):
    return _PublicFetcher()._read(url, headers, max_bytes)[0]


def _value(root, selector, attribute=None):
    node = root.first(selector)
    return (node.get(attribute) if attribute else node.text()) if node else ''


def _hako_author(root):
    for item in root.select('.info-item'):
        if 'tác giả' in _value(item, '.info-name').casefold():
            return _value(item, '.info-value')
    return ''


def _cover(root, base_url):
    image = root.first('img')
    if image:
        return safe_remote_url(image.get('data-original') or image.get('data-src') or image.get('src'), base_url)
    image = root.first('[data-bg], [data-image], [style]')
    if image:
        background = image.get('data-bg') or image.get('data-image')
        match = re.search(r'url\([\"\']?(.*?)[\"\']?\)', image.get('style'))
        return safe_remote_url(background or (match[1] if match else ''), base_url)
    return ''


def _next_page(root, page):
    for container in root.select('.pagination, .page_redirect'):
        links = container.select('a')
        for link in links:
            if 'next' in link.get('rel').split() and 'disabled' not in (link.get('class') + ' ' + link.parent.get('class')).split():
                return page + 1
        for node in container.walk():
            if 'active' not in node.get('class').split():
                continue
            parent = node.parent
            if parent and parent.tag == 'a':
                parent = parent.parent
            siblings = [c for c in parent.children if hasattr(c, 'tag')] if parent else []
            active = node if node in siblings else node.parent
            if active in siblings:
                for following in siblings[siblings.index(active) + 1:]:
                    a = following if following.tag == 'a' else following.first('a')
                    if a and 'disabled' not in following.get('class').split() and a.get('href') and not a.get('href').startswith('javascript:'):
                        return page + 1
                    break
    return None


def _decode_hako(root):
    protected = root.first('#chapter-c-protected')
    if not protected:
        return root.inner_html()
    scheme = protected.get('data-s', 'none')
    if scheme not in ('none', 'xor_shuffle', 'base64_reverse'):
        raise SourceError('Hako: unsupported chapter encoding', code='parse_error')
    try:
        chunks = json.loads(protected.get('data-c'))
        key = protected.get('data-k').encode('ascii')
        if not isinstance(chunks, list) or not chunks or len(chunks) > 20000 or (scheme == 'xor_shuffle' and not key):
            raise ValueError('invalid encoded chunks')
        if any(not isinstance(chunk, str) or not re.match(r'^\d{4}', chunk) for chunk in chunks):
            raise ValueError('invalid chunk sequence')
        decoded = []
        for sequence, chunk in enumerate(sorted(chunks, key=lambda c: int(c[:4]))):
            if int(chunk[:4]) != sequence:
                raise ValueError('missing or duplicate encoded chunk')
            data = base64.b64decode(chunk[4:][::-1] if scheme == 'base64_reverse' else chunk[4:], validate=True)
            if scheme == 'xor_shuffle':
                data = bytes(value ^ key[index % len(key)] for index, value in enumerate(data))
            decoded.append(data)
        return b''.join(decoded).decode('utf-8')
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise SourceError('Hako: không đọc được chương mã hóa: ' + str(exc), code='parse_error') from exc


class SourceService:
    def __init__(self, fetcher=None):
        self.fetcher = fetcher if fetcher is not None else _PublicFetcher()
        self._stv_source = None
        self._stv_lock = threading.Lock()

    def _stv(self):
        from stv_source import StvSource
        with self._stv_lock:
            if self._stv_source is None:
                self._stv_source = StvSource()
            return self._stv_source

    def close(self):
        if self._stv_source is not None:
            self._stv_source.close()

    def captcha(self, source, action, challenge_id, answer=None):
        if source != 'stv':
            raise SourceError('Nguồn này không cung cấp xác minh CAPTCHA trong ứng dụng.')
        return self._stv().captcha(action, challenge_id, answer)

    def sources(self):
        return [{'id': key, 'name': value[0]} for key, value in _SOURCES.items()]

    def _source(self, source):
        if not isinstance(source, str) or source not in _SOURCES:
            raise SourceError('Nguồn chưa được hỗ trợ: ' + str(source))
        return _SOURCES[source]

    def _url(self, source, url, base=None):
        _, origin, aliases = self._source(source)
        checked = validate_public_url(urljoin(base or origin + '/', url), resolve=False)
        parsed = urlsplit(checked)
        if parsed.hostname.removeprefix('www.') not in aliases:
            raise SourceError('URL không thuộc nguồn đã chọn')
        path = parsed.path
        if source == 'stv' and (parsed.query or parsed.fragment or not re.fullmatch(r'/truyen/[a-zA-Z0-9_-]+/1/[0-9]+(?:/[0-9]+)?/?', path)):
            raise SourceError('URL Sáng Tác Việt phải là trang truyện hoặc chương gốc hợp lệ')
        if source == 'stv':
            path = path.rstrip('/') + '/'
        if source == 'qq' and path.endswith('.html'):
            path = path[:-5]
        return origin + path + ('?' + parsed.query if parsed.query else '')

    def open_original(self, source, url):
        url = self._url(source, url)
        return {'opened': bool(webbrowser.open(url)), 'url': url}

    def _load(self, url):
        try:
            text = self.fetcher(url)
            if not isinstance(text, str) or len(text) > _MAX_RESPONSE:
                raise SourceError('Nguồn trả dữ liệu không hợp lệ hoặc quá lớn')
            if not text.strip() or (len(text) < 5000 and re.search(r'document\.cookie\s*=|<title>\s*(?:Just a moment|Attention Required)', text, re.I)):
                raise SourceError('Nguồn trả trang trống hoặc yêu cầu kiểm tra trình duyệt')
            return text
        except SourceError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SourceError('Nguồn trả HTTP ' + str(exc.code), code='http_error', http_status=exc.code) from exc
        except Exception as exc:
            raise SourceError('Không tải được nguồn: ' + str(exc), code='network_error') from exc

    def search(self, source, query, page=1):
        _, origin, _ = self._source(source)
        if type(page) is not int or not 1 <= page <= 10000 or not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SourceError('Từ khóa hoặc số trang không hợp lệ')
        if source == 'stv':
            return self._stv().search(query.strip(), page)
        if source == 'qq':
            url = origin + '/tim-kiem/trang-' + str(page) + '?q=' + quote(query.strip())
            selector, title = '#main_homepage .list_grid li', '.book_name a'
        elif source == 'full':
            url = origin + '/tim-kiem/?' + urlencode({'tukhoa': query.strip(), 'page': page})
            selector, title = '.list-truyen div[itemscope]', '.truyen-title a'
        else:
            url = origin + '/tim-kiem?' + urlencode({'keywords': query.strip()}) + ('&page=' + str(page) if page > 1 else '')
            selector, title = '.thumb-item-flow', '.series-title a'
        root = parse_html(self._load(url))
        items = []
        for row in root.select(selector):
            link = row.first(title)
            if not link or not link.text() or not link.get('href'):
                continue
            items.append({'name': link.text(), 'author': _value(row, '.author'),
                'path': self._url(source, link.get('href'), url), 'cover': _cover(row, url),
                'description': _value(row, '.last_chapter a, .chapter-title, .author')})
        return {'items': items, 'next': _next_page(root, page)}

    def cover(self, source, url):
        url = self._url(source, url) if source in _SOURCES else validate_public_url(url, resolve=False)
        if source == 'stv':
            return self._stv().cover(url)
        root = parse_html(self._load(url))
        selector = {'qq': '.book_avatar', 'full': 'div.book', 'hako': '.series-cover'}.get(source)
        container = root.first(selector) if selector else None
        cover = _cover(container, url) if container else ''
        if not cover:
            metadata = root.first('meta[property="og:image"], meta[name="og:image"]')
            cover = safe_remote_url(metadata.get('content'), url) if metadata else ''
        if not cover:
            raise SourceError('Trang truyện không cung cấp URL bìa công khai hợp lệ.')
        return {'cover': cover, 'path': url}

    def detail(self, source, url):
        url = self._url(source, url)
        if source == 'stv':
            return self._stv().detail(url)
        root = parse_html(self._load(url))
        if source == 'qq':
            title, author, cover, description = 'h1[itemprop=name]', '.book_info .author a', '.book_avatar', '.story-detail-info'
        elif source == 'full':
            title, author, cover, description = 'h3.title', 'a[itemprop=author]', 'div.book', '.desc-text'
        else:
            title, author, cover, description = '.series-name', '', '.series-cover', '.summary-content'
        name = _value(root, title)
        if not name:
            raise SourceError('Không tìm thấy thông tin truyện; nguồn có thể đổi giao diện hoặc yêu cầu kiểm tra trình duyệt')
        book = {'name': name, 'author': _value(root, author) if source != 'hako' else _hako_author(root),
            'path': url, 'cover': _cover(root.first(cover) or root, url),
            'description': _value(root, description), 'source': source,
            'kind': 'comic' if source == 'qq' else 'text', 'format': 'online', 'chapters': []}
        seen = set()
        def append(links, volume=''):
            for link in links:
                if not link.get('href') or not link.text():
                    continue
                chapter_url = self._url(source, link.get('href'), url)
                if chapter_url in seen:
                    continue
                seen.add(chapter_url)
                chapter = {'index': len(book['chapters']), 'title': link.text(), 'url': chapter_url}
                if volume:
                    chapter['volume'] = volume
                book['chapters'].append(chapter)
        if source == 'qq':
            append(reversed(root.select('.works-chapter-list a')))
        elif source == 'full':
            pages_raw = _value(root, '#total-page', 'value')
            try:
                pages = int(pages_raw) if pages_raw else 1
                if not 1 <= pages <= 10000:
                    raise ValueError('invalid page count')
            except ValueError as exc:
                raise SourceError('Nguồn trả số trang mục lục không hợp lệ') from exc
            story_id, ascii_name = _value(root, '#truyen-id', 'value'), _value(root, '#truyen-ascii', 'value')
            if story_id and ascii_name:
                for page in range(1, pages + 1):
                    endpoint = _SOURCES[source][1] + '/ajax.php?' + urlencode({'type': 'list_chapter',
                        'tid': story_id, 'tascii': ascii_name, 'page': page, 'totalp': pages})
                    try:
                        markup = json.loads(self._load(endpoint))['chap_list']
                        links = parse_html(markup).select('.list-chapter li a')
                    except (ValueError, KeyError, TypeError) as exc:
                        raise SourceError('Không đọc được trang mục lục ' + str(page) + ': ' + str(exc)) from exc
                    if not links:
                        raise SourceError('Trang mục lục ' + str(page) + ' trống; chưa tải đủ chương')
                    previous_count = len(book['chapters'])
                    append(links)
                    if len(book['chapters']) == previous_count:
                        raise SourceError('Trang mục lục ' + str(page) + ' lặp lại; chưa tải đủ chương')
            elif pages > 1:
                raise SourceError('Thiếu thông tin phân trang mục lục; chưa tải đủ chương')
            else:
                append(root.select('.list-chapter li a'))
        else:
            volumes = root.select('section.volume-list')
            for volume in volumes:
                links = volume.select('.list-chapters a')
                volume_page = volume.first('.volume-cover a')
                if volume_page and (not links or not book['author']):
                    volume_url = self._url(source, volume_page.get('href'), url)
                    volume_root = parse_html(self._load(volume_url))
                    book['author'] = book['author'] or _hako_author(volume_root)
                    links = links or volume_root.select('.list-chapters a')
                if not links:
                    raise SourceError('Tập truyện thiếu mục lục; chưa tải đủ chương')
                append(links, _value(volume, '.sect-title'))
            if not volumes:
                append(root.select('.list-chapters a'), _value(root, '.volume-name'))
        if not book['chapters']:
            raise SourceError('Không tìm thấy mục lục truyện')
        return book

    def chapter(self, source, url, *, background=False, cancelled=None):
        url = self._url(source, url)
        if source == 'stv':
            return self._stv().chapter(url, background=background, cancelled=cancelled)
        raw = self._load(url)
        root = parse_html(raw)
        if source == 'qq':
            images = []
            for image in root.select('.chapter_content .page-chapter img'):
                src = safe_remote_url(image.get('data-original') or image.get('src'), url)
                if not src:
                    raise SourceError('Ảnh chương có URL không hợp lệ')
                images.append(src)
            if not images:
                raise SourceError('Không tìm thấy ảnh chương; nguồn có thể yêu cầu kiểm tra trình duyệt')
            markup = ''.join('<img src="' + escape(src, quote=True) + '" alt="Trang ' + str(index + 1) + '">' for index, src in enumerate(images))
            return {'html': sanitize_html(markup, url), 'text': '', 'images': images}
        content = root.first('.chapter-c' if source == 'full' else '#chapter-content')
        if not content:
            raise SourceError('Không tìm thấy nội dung chương; nguồn có thể yêu cầu đăng nhập hoặc kiểm tra trình duyệt', code='parse_error')
        markup = _decode_hako(content) if source == 'hako' else content.inner_html()
        guest = bool(source == 'hako' and re.search(r'\bisLoggedIn\s*=\s*false\b', raw))
        clean = sanitize_html(markup, url, allow_images=not guest)
        text = html_to_text(clean)
        images = [node.get('src') for node in parse_html(clean).select('img')]
        if not text and not images:
            raise SourceError('Nội dung chương trống; chưa tải được nội dung', code='parse_error')
        return {'html': clean, 'text': text, 'images': images}
