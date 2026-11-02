"""Small HTML reader and allowlist sanitizer for untrusted document/source HTML."""
from html import escape
from html.parser import HTMLParser
import base64
import ipaddress
import re
from urllib.parse import urljoin, urlsplit, urlunsplit


VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
DROP = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'form', 'button', 'input', 'select', 'textarea'}
BLOCK = {'p', 'div', 'section', 'article', 'header', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'li', 'ul', 'ol', 'tr'}
ALLOWED = BLOCK | {'br', 'hr', 'span', 'em', 'strong', 'b', 'i', 'u', 's', 'del', 'sub', 'sup', 'ruby', 'rt', 'rp', 'code', 'img', 'table', 'thead', 'tbody', 'td', 'th'}


def is_public_ip(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved and not getattr(address, 'is_site_local', False)


def safe_remote_url(url, base_url=''):
    """Check URL syntax/public IP literals without doing DNS or fetching content."""
    if not isinstance(url, str) or not url or re.search(r'[\x00-\x20\x7f\\]', url):
        return ''
    try:
        parsed = urlsplit(urljoin(base_url, url))
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in ('http', 'https') or not host or parsed.username is not None or parsed.password is not None:
            return ''
        if parsed.port not in (None, 80 if parsed.scheme == 'http' else 443):
            return ''
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.test', '.invalid')):
            return ''
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Numeric legacy IPv4 forms are interpreted inconsistently by URL clients.
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '.' not in host or re.fullmatch(r'\d+|0x[0-9a-f]+', host.rsplit('.', 1)[-1]):
                return ''
        else:
            if not is_public_ip(address):
                return ''
        netloc = '[' + host + ']' if ':' in host else host
        path = parsed.path or '/'
        return urlunsplit((parsed.scheme, netloc, path, parsed.query, ''))
    except (ValueError, UnicodeError):
        return ''


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    def get(self, name, default=''):
        return self.attrs.get(name, default) or default

    def text(self):
        return re.sub(r'\s+', ' ', ''.join(_text_parts(self))).strip()

    def select(self, selector):
        found = []
        for node in self.walk():
            if any(_matches_chain(node, group.split()) for group in selector.split(',')):
                found.append(node)
        return found

    def first(self, selector):
        return next(iter(self.select(selector)), None)

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def inner_html(self):
        return ''.join(_serialize(c) for c in self.children)


def _matches(node, selector):
    tag = re.match(r'^[\w-]+', selector)
    if tag and node.tag != tag[0]:
        return False
    for kind, value in re.findall(r'([.#])([\w-]+)', re.sub(r'\[[^]]*\]', '', selector)):
        if kind == '#' and node.get('id') != value:
            return False
        if kind == '.' and value not in node.get('class').split():
            return False
    for name, quote, value in re.findall(r'\[([\w-]+)(?:=([\"\']?)([^\]\"\']*)\2)?\]', selector):
        if name not in node.attrs or value and node.get(name) != value:
            return False
    return True


def _matches_chain(node, parts):
    if not parts or not _matches(node, parts[-1]):
        return False
    for part in reversed(parts[:-1]):
        node = node.parent
        while node is not None and not _matches(node, part):
            node = node.parent
        if node is None:
            return False
    return True


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if len(self.stack) > 256 or self.count > 100000:
            raise ValueError('HTML exceeds parsing limits')
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(markup):
    parser = _TreeParser()
    parser.feed(markup)
    parser.close()
    return parser.root


def _serialize(node):
    if isinstance(node, str):
        return escape(node)
    attrs = ''.join(' ' + escape(k) + '="' + escape(v or '', quote=True) + '"' for k, v in node.attrs.items())
    return '<' + node.tag + attrs + '>' + ('' if node.tag in VOID else node.inner_html() + '</' + node.tag + '>')


def _hidden(node):
    return (node.tag in DROP or 'hidden' in node.attrs or node.get('aria-hidden') == 'true'
            or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|em|rem|pt)?(?:\s*[;!]|$))', node.get('style'), re.I)
            or bool({'ads-responsive', 'adsbygoogle'} & set(node.get('class').split())))


def _text_parts(node):
    for child in node.children:
        if isinstance(child, str):
            yield child
        elif not _hidden(child):
            if child.tag in BLOCK | {'br', 'hr'}:
                yield '\n'
            yield from _text_parts(child)
            if child.tag in BLOCK:
                yield '\n'


def html_to_text(markup):
    value = ''.join(_text_parts(parse_html(markup)))
    return '\n'.join(line for line in (re.sub(r'[^\S\n]+', ' ', s).strip() for s in value.splitlines()) if line)


def safe_raster_data_url(url):
    if not isinstance(url, str) or len(url) > 28 * 1024 * 1024:
        return ''
    match = re.fullmatch(r'data:image/(png|jpeg|gif|webp|bmp);base64,([A-Za-z0-9+/=]+)', url)
    if not match:
        return ''
    try:
        data = base64.b64decode(match[2], validate=True)
    except ValueError:
        return ''
    kind = match[1]
    valid = ((kind == 'png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or
             (kind == 'jpeg' and data.startswith(b'\xff\xd8\xff')) or
             (kind == 'gif' and data.startswith((b'GIF87a', b'GIF89a'))) or
             (kind == 'webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP') or
             (kind == 'bmp' and data.startswith(b'BM')))
    return url if valid else ''


def sanitize_html(markup, base_url='', allow_images=True, allow_data_images=False):
    def clean(node):
        if isinstance(node, str):
            return escape(node)
        if _hidden(node):
            return ''
        body = ''.join(clean(c) for c in node.children)
        if node.tag == 'img':
            raw_url = node.get('data-original') or node.get('data-src') or node.get('src')
            url = safe_remote_url(raw_url, base_url) or (safe_raster_data_url(raw_url) if allow_data_images else '')
            return '<img src="' + escape(url, quote=True) + '" alt="' + escape(node.get('alt'), quote=True) + '" loading="lazy">' if url and allow_images else ''
        if node.tag not in ALLOWED:
            return body
        return '<' + node.tag + '>' + ('' if node.tag in VOID else body + '</' + node.tag + '>')
    return ''.join(clean(c) for c in parse_html(markup).children)
