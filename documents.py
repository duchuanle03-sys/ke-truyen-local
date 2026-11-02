"""Read local documents without extracting archives or modifying originals.

HTML is rebuilt from a small inert tag set; archive images are embedded as data
URLs so imported books keep working after the selected file moves. Resource
limits apply before any archive member is read. PDF/OCR/legacy engines are
reported as unavailable until an actual integration is implemented and tested.
"""

import base64
from contextvars import ContextVar
from html import escape as _escape_html
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile


class DocumentError(ValueError):
    pass


class UnsupportedFormatError(DocumentError):
    pass


MAX_FILE_SIZE = 128 * 1024 * 1024
MAX_MEMBER_SIZE = 64 * 1024 * 1024
MAX_ARCHIVE_SIZE = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_RATIO = 200
MAX_OUTPUT_SIZE = 128 * 1024 * 1024
_OUTPUT_BUDGET = ContextVar('document_output_budget', default=None)
TEXT_EXTENSIONS = {'.txt', '.html', '.htm', '.xhtml', '.md', '.markdown', '.fb2', '.fodt'}
OFFICE_EXTENSIONS = {'.docx', '.docm', '.dotx', '.dotm', '.odt', '.ott'}
SUPPORTED_EXTENSIONS = sorted(TEXT_EXTENSIONS | OFFICE_EXTENSIONS | {'.epub', '.zip', '.cbz', '.fbz'})
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
CHAPTER = re.compile(r'^(?:chương\s+\S|chapter\s+\S|part\s+\S|quyển\s+\S|phần\s+\S|第[^\s]{1,24}[章节卷回部])', re.IGNORECASE)


class _OutputBudget:
    def __init__(self):
        self.used = 0


def _reserve_output(size):
    # Count intermediates and persisted fields; archive size alone cannot bound repeated embedded references.
    budget = _OUTPUT_BUDGET.get()
    total = size + (budget.used if budget is not None else 0)
    if total > MAX_OUTPUT_SIZE:
        raise DocumentError('Tài liệu vượt giới hạn nội dung được tạo (HTML, văn bản và ảnh nhúng).')
    if budget is not None:
        budget.used = total


def escape(value, quote=True):
    size = len(value) + value.count('&') * 4 + (value.count('<') + value.count('>')) * 3
    if quote:
        size += (value.count('"') + value.count("'")) * 5
    _reserve_output(size)
    return _escape_html(value, quote=quote)


def capabilities():
    return {
        'supported_extensions': SUPPORTED_EXTENSIONS,
        'pdf': {'supported': False, 'reason': 'Chưa tích hợp engine đọc/render PDF.', 'detected_engine': shutil.which('pdftotext')},
        'ocr': {'supported': False, 'reason': 'Chưa tích hợp OCR PDF scan hoặc dữ liệu ngôn ngữ.', 'detected_engine': shutil.which('tesseract')},
        'legacy': {'supported': False, 'reason': 'MOBI/PRC/AZW/AZW3/UMD/CHM/DOC/DOT/WPS/RTF/HWP/HWPX/HWPML cần bộ chuyển đổi đã kiểm thử.', 'detected_engine': shutil.which('ebook-convert') or shutil.which('soffice')},
        'limits': {'file_bytes': MAX_FILE_SIZE, 'member_bytes': MAX_MEMBER_SIZE, 'archive_uncompressed_bytes': MAX_ARCHIVE_SIZE, 'archive_members': MAX_MEMBERS, 'compression_ratio': MAX_RATIO, 'generated_character_budget': MAX_OUTPUT_SIZE},
        'layout': 'Giữ văn bản, tiêu đề và ảnh raster; không bảo toàn toàn bộ bố cục, CSS hoặc macro.',
    }


def decode_text(data):
    """Return (text, encoding); fallback encodings remain visible to callers."""
    if len(data) > MAX_MEMBER_SIZE:
        raise DocumentError('Văn bản vượt giới hạn 64 MiB.')
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        encodings = ['utf-32']
    elif data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    elif data.startswith(b'\xef\xbb\xbf'):
        encodings = ['utf-8-sig']
    elif b'\x00' in data:
        raise DocumentError('Tệp chứa dữ liệu nhị phân hoặc UTF-16 thiếu BOM; hãy lưu lại với encoding rõ ràng.')
    else:
        declaration = re.search(br'(?:encoding\s*=\s*[\"\']|charset\s*=\s*[\"\']?)([a-zA-Z0-9_-]+)', data[:1024], re.IGNORECASE)
        explicit = declaration.group(1).decode('ascii') if declaration else None
        # shortcut: legacy encodings are ambiguous, add user encoding selection before promising exact decoding of every legacy text file.
        encodings = ([explicit] if explicit else []) + ['utf-8', 'gb18030', 'cp1258', 'cp1252']
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except (UnicodeError, LookupError):
            continue
        controls = sum(ord(c) < 32 and c not in '\t\n\r\f' for c in text)
        if controls:
            continue
        return text.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff'), encoding
    raise DocumentError('Không giải mã được tệp văn bản; encoding lỗi hoặc nội dung nhị phân.')


def _xml(data):
    text, _ = decode_text(data)
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.IGNORECASE):
        raise DocumentError('Không cho phép DTD/entity trong tài liệu XML.')
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as error:
        raise DocumentError('Tài liệu XML không hợp lệ: ' + str(error)) from error
    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 100:
            raise DocumentError('XML lồng quá sâu.')
        pending.extend((child, depth + 1) for child in node)
    return root


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _nodes(root, name):
    return [node for node in root.iter() if _tag(node) == name]


def _node_text(node):
    return ''.join(node.itertext()).strip()


def _natural(value):
    return [int(part) if part.isdecimal() else part.casefold() for part in re.split(r'(\d+)', str(value))]


def _image_url(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime = 'image/webp'
    elif data.startswith(b'BM'):
        mime = 'image/bmp'
    else:
        raise DocumentError('Ảnh không phải PNG/JPEG/GIF/WebP/BMP hợp lệ.')
    _reserve_output(len('data:' + mime + ';base64,') + 4 * ((len(data) + 2) // 3))
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


class _Archive:
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path)
        try:
            infos = self.archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise DocumentError('Archive chứa quá nhiều mục (tối đa 10.000).')
            self.members, total = {}, 0
            for info in infos:
                name = info.filename.replace('\\', '/')
                parts = name.rstrip('/').split('/')
                if name.startswith('/') or not name or any(part in ('', '.', '..') or ':' in part or any(ord(c) < 32 for c in part) for part in parts):
                    raise DocumentError('Đường dẫn archive không an toàn: ' + name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise DocumentError('Không cho phép symlink trong archive.')
                if info.flag_bits & 1:
                    raise DocumentError('Archive có mật khẩu chưa được hỗ trợ.')
                if info.is_dir():
                    continue
                if name in self.members:
                    raise DocumentError('Archive có tên mục trùng: ' + name)
                total += info.file_size
                if info.file_size > MAX_MEMBER_SIZE or total > MAX_ARCHIVE_SIZE:
                    raise DocumentError('Archive vượt giới hạn kích thước giải nén.')
                if info.file_size > 1_048_576 and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
                    raise DocumentError('Tỷ lệ nén archive vượt giới hạn an toàn.')
                self.members[name] = info
        except Exception:
            self.archive.close()
            raise

    def __enter__(self):
        self._budget_token = _OUTPUT_BUDGET.set(_OutputBudget()) if _OUTPUT_BUDGET.get() is None else None
        return self

    def __exit__(self, *_):
        try:
            self.archive.close()
        finally:
            if self._budget_token is not None:
                _OUTPUT_BUDGET.reset(self._budget_token)

    def read(self, name):
        if name not in self.members:
            raise DocumentError('Archive thiếu mục bắt buộc: ' + name)
        with self.archive.open(self.members[name]) as handle:
            data = handle.read(MAX_MEMBER_SIZE + 1)
        if len(data) > MAX_MEMBER_SIZE:
            raise DocumentError('Mục archive vượt giới hạn kích thước.')
        return data

    @staticmethod
    def resolve(base, reference):
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            raise DocumentError('Tài liệu archive không được đọc tài nguyên bên ngoài.')
        path = unquote(url.path)
        if '\\' in path or path.startswith('/') or ':' in path or '\x00' in path:
            raise DocumentError('Tham chiếu archive không an toàn.')
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
        if resolved == '..' or resolved.startswith('../'):
            raise DocumentError('Tham chiếu thoát khỏi archive.')
        return resolved

    def image(self, base, reference):
        name = self.resolve(base, reference)
        if PurePosixPath(name).suffix.lower() not in IMAGE_EXTENSIONS:
            raise DocumentError('Chỉ hỗ trợ ảnh raster trong tài liệu.')
        return _image_url(self.read(name))


BLOCKS = {'p', 'div', 'section', 'article', 'main', 'br', 'hr', 'li', 'blockquote', 'pre', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
ALLOWED = BLOCKS | {'b', 'strong', 'em', 'i', 'u', 's', 'ul', 'ol', 'code', 'table', 'td', 'th', 'thead', 'tbody', 'sup', 'sub', 'span', 'a', 'img'}
SKIP = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'form'}
WRAPPERS = {'root', 'html', 'body', 'div', 'section', 'article', 'main'}


class _Node:
    def __init__(self, tag, attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []


class _HTML(HTMLParser):
    def __init__(self, source, image=None):
        super().__init__(convert_charrefs=True)
        self.root = _Node('root')
        self.stack = [self.root]
        self.skipped = []
        self.image = image
        self.title = ''
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 100:
            raise DocumentError('HTML lồng quá sâu.')
        if self.skipped:
            if tag in SKIP:
                self.skipped.append(tag)
            return
        if tag in SKIP:
            self.skipped.append(tag)
            return
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('img', 'br', 'hr', 'meta', 'link', 'input', 'source', 'wbr', 'base', 'area', 'param', 'col'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                node = self.stack[index]
                if tag == 'title':
                    self.title = _html_text(self.render_children(node))
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.skipped:
            self.stack[-1].children.append(data)

    def render_children(self, node):
        return ''.join(self.render(child) for child in node.children)

    def render(self, node):
        if isinstance(node, str):
            return escape(node)
        if node.tag in ('title', 'head', 'meta', 'link'):
            return ''
        if node.tag == 'img':
            if not self.image or not node.attrs.get('src'):
                return ''
            reference = node.attrs['src']
            if urlsplit(reference).scheme or urlsplit(reference).netloc:
                return ''
            url = self.image(reference)
            return '<img src="' + escape(url, quote=True) + '" alt="' + escape(node.attrs.get('alt', ''), quote=True) + '">'
        inner = self.render_children(node)
        if node.tag not in ALLOWED:
            return inner
        _reserve_output(len(node.tag) + 2 + (0 if node.tag in ('br', 'hr') else len(node.tag) + 3))
        return '<' + node.tag + '>' + inner + ('' if node.tag in ('br', 'hr') else '</' + node.tag + '>')

    def chapters(self, default_title):
        events = []
        def walk(node):
            if isinstance(node, _Node) and node.tag in WRAPPERS:
                for child in node.children:
                    walk(child)
            elif isinstance(node, _Node) and node.tag in ('h1', 'h2'):
                rendered = self.render(node)
                events.append((_html_text(rendered), rendered))
            else:
                events.append((None, self.render(node)))
        walk(self.root)
        return _sections(events, default_title)


class _Plain(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, _):
        if tag in BLOCKS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)


def _html_text(source):
    text = ''.join(_Plain(source).parts)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(line.strip() for line in text.splitlines())).strip()


def _chapter(title, html):
    _reserve_output(len(html))
    text = _html_text(html)
    images = re.findall(r'<img src="(data:image/[^\"]+)"', html)
    _reserve_output(len(text) + sum(map(len, images)) + len(title))
    return {'index': 0, 'title': title.strip(), 'html': html.strip(), 'text': text, 'images': images, 'downloaded': True}


def _sections(events, default_title):
    chapters, title, parts = [], default_title, []
    def flush():
        html = ''.join(parts)
        if _html_text(html) or '<img ' in html:
            chapters.append(_chapter(title, html))
    for heading, html in events:
        if heading:
            flush()
            title, parts = heading, []
        parts.append(html)
    flush()
    return chapters


def _text_chapters(text, title, markdown=False):
    events = []
    fenced = False
    for line in text.splitlines():
        if markdown and line.lstrip().startswith('```'):
            fenced = not fenced
            events.append((None, '<pre><code>' if fenced else '</code></pre>'))
            continue
        heading = re.match(r'^#{1,2}\s+(.+?)\s*#*$', line) if markdown and not fenced else None
        if heading:
            name = heading.group(1)
            events.append((name, '<h2>' + escape(name) + '</h2>'))
        elif not fenced and len(line.strip()) <= 180 and CHAPTER.match(line.strip()):
            events.append((line.strip(), '<h2>' + escape(line.strip()) + '</h2>'))
        elif fenced:
            events.append((None, escape(line) + '\n'))
        elif not line.strip():
            events.append((None, '\n'))
        else:
            rendered = escape(line)
            if markdown:
                rendered = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', rendered)
                rendered = re.sub(r'`([^`]+)`', r'<code>\1</code>', rendered)
                rendered = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', rendered)
                rendered = re.sub(r'^#{3,6}\s+(.+)$', r'<strong>\1</strong>', rendered)
            events.append((None, '<p>' + rendered + '</p>'))
    if fenced:
        events.append((None, '</code></pre>'))
    return _sections(events, title)


def _fb2(data, default_title):
    root = _xml(data)
    if _tag(root) != 'FictionBook':
        raise DocumentError('Tệp không có cấu trúc FictionBook FB2.')
    titles = _nodes(root, 'book-title')
    name = _node_text(titles[0]) if titles else default_title
    author = ', '.join(' '.join(_node_text(child) for child in node if _tag(child) in ('first-name', 'middle-name', 'last-name', 'nickname')) for node in _nodes(root, 'author'))
    binaries = {}
    for binary in _nodes(root, 'binary'):
        try:
            binaries[binary.get('id')] = base64.b64decode(''.join(binary.itertext()).strip().encode('ascii'), validate=False)
        except (ValueError, UnicodeError) as error:
            raise DocumentError('Ảnh nhúng FB2 không hợp lệ.') from error
    def render(node):
        tag = _tag(node)
        if tag == 'image':
            reference = next((value for key, value in node.attrib.items() if key.rsplit('}', 1)[-1] == 'href'), '')
            if not reference.startswith('#') or reference[1:] not in binaries:
                raise DocumentError('FB2 thiếu ảnh nhúng được tham chiếu.')
            return '<img src="' + _image_url(binaries[reference[1:]]) + '" alt="">'
        inner = escape(node.text or '') + ''.join(render(child) + escape(child.tail or '') for child in node)
        mapping = {'p': 'p', 'v': 'p', 'subtitle': 'h3', 'strong': 'strong', 'emphasis': 'em', 'strikethrough': 's', 'code': 'code', 'epigraph': 'blockquote', 'cite': 'blockquote', 'title': 'h2', 'empty-line': 'br'}
        target = mapping.get(tag)
        return '<' + target + '>' + inner + ('' if target == 'br' else '</' + target + '>') if target else inner
    chapters = []
    def section(node, fallback):
        title_node = next((child for child in node if _tag(child) == 'title'), None)
        title = _node_text(title_node) if title_node is not None else fallback
        contents = []
        for child in node:
            if _tag(child) == 'section':
                if contents:
                    chapters.append(_chapter(title, ''.join(contents)))
                    contents = []
                section(child, title)
            else:
                contents.append(render(child))
        if contents:
            chapter = _chapter(title, ''.join(contents))
            if chapter['text'] or chapter['images']:
                chapters.append(chapter)
    for body in [child for child in root if _tag(child) == 'body']:
        section(body, name)
    return {'name': name, 'author': author}, chapters


def _epub(archive, title):
    container = _xml(archive.read('META-INF/container.xml'))
    roots = _nodes(container, 'rootfile')
    if not roots or not roots[0].get('full-path'):
        raise DocumentError('EPUB thiếu đường dẫn package.')
    opf_path = archive.resolve('', roots[0].get('full-path'))
    package = _xml(archive.read(opf_path))
    if _tag(package) != 'package':
        raise DocumentError('EPUB package không hợp lệ.')
    title_nodes, creators, descriptions = _nodes(package, 'title'), _nodes(package, 'creator'), _nodes(package, 'description')
    book = {'name': _node_text(title_nodes[0]) if title_nodes else title, 'author': ', '.join(_node_text(node) for node in creators), 'description': _node_text(descriptions[0]) if descriptions else ''}
    manifest = {node.get('id'): node for node in _nodes(package, 'item')}
    spine = _nodes(package, 'itemref')
    if not spine:
        raise DocumentError('EPUB không có spine/mục lục đọc.')
    chapters, seen = [], set()
    for ref in spine:
        item = manifest.get(ref.get('idref'))
        if item is None or not item.get('href'):
            raise DocumentError('EPUB spine tham chiếu mục manifest không tồn tại.')
        if item.get('media-type') not in ('application/xhtml+xml', 'text/html', None):
            raise DocumentError('EPUB spine chứa định dạng chưa hỗ trợ: ' + str(item.get('media-type')))
        member = archive.resolve(opf_path, item.get('href'))
        if member in seen:
            raise DocumentError('EPUB spine lặp lại cùng tài liệu; từ chối khuếch đại nội dung.')
        seen.add(member)
        text, _ = decode_text(archive.read(member))
        parser = _HTML(text, lambda reference, member=member: archive.image(member, reference))
        chapters.extend(parser.chapters(parser.title or PurePosixPath(member).stem))
    return book, chapters


def _office(archive, extension, title, flat_data=None):
    is_word = extension in {'.docx', '.docm', '.dotx', '.dotm'}
    member = 'word/document.xml' if is_word else 'content.xml'
    root = _xml(flat_data if flat_data is not None else archive.read(member))
    expected = 'document' if is_word else ('document', 'document-content')
    if (_tag(root) != expected if isinstance(expected, str) else _tag(root) not in expected):
        raise DocumentError('Cấu trúc tài liệu Office không hợp lệ.')
    book = {'name': title, 'author': ''}
    if archive:
        meta_path = 'docProps/core.xml' if is_word else 'meta.xml'
        if meta_path in archive.members:
            metadata = _xml(archive.read(meta_path))
            titles, creators = _nodes(metadata, 'title'), _nodes(metadata, 'creator')
            if titles and _node_text(titles[0]):
                book['name'] = _node_text(titles[0])
            book['author'] = ', '.join(_node_text(node) for node in creators)
    relations = {}
    if is_word and 'word/_rels/document.xml.rels' in archive.members:
        for relation in _nodes(_xml(archive.read('word/_rels/document.xml.rels')), 'Relationship'):
            if relation.get('TargetMode') != 'External':
                relations[relation.get('Id')] = relation.get('Target', '')
    def text_content(node):
        if is_word:
            return ''.join((child.text or '') if _tag(child) == 't' else '\n' if _tag(child) in ('br', 'cr') else '\t' if _tag(child) == 'tab' else '' for child in node.iter())
        result = node.text or ''
        for child in node:
            tag = _tag(child)
            result += ('\n' if tag == 'line-break' else '\t' if tag == 'tab' else ' ' * min(int(next((v for k, v in child.attrib.items() if k.rsplit('}', 1)[-1] == 'c'), '1')), 1000) if tag == 's' else text_content(child)) + (child.tail or '')
        return result
    events = []
    for node in root.iter():
        tag = _tag(node)
        if tag not in (('p',) if is_word else ('h', 'p')):
            continue
        text = text_content(node)
        heading = tag == 'h'
        if is_word:
            styles = _nodes(node, 'pStyle')
            style = next(iter(styles[0].attrib.values()), '') if styles else ''
            heading = bool(re.match(r'heading\s*[12]$', style, re.IGNORECASE))
        html = '<h2>' + escape(text) + '</h2>' if heading else '<p>' + escape(text).replace('\n', '<br>') + '</p>'
        for image in node.iter():
            if is_word and _tag(image) == 'blip':
                relation_id = next((value for key, value in image.attrib.items() if key.rsplit('}', 1)[-1] == 'embed'), '')
                if relation_id not in relations:
                    raise DocumentError('DOCX thiếu liên kết ảnh nhúng.')
                html += '<img src="' + archive.image(member, relations[relation_id]) + '" alt="">'
            elif not is_word and _tag(image) == 'image':
                href = next((value for key, value in image.attrib.items() if key.rsplit('}', 1)[-1] == 'href'), '')
                if href and archive:
                    html += '<img src="' + archive.image(member, href) + '" alt="">'
        events.append((text if heading else None, html))
    return book, _sections(events, book['name'])


def _parse_bytes(data, extension, title, image=None):
    if extension == '.fb2':
        return _fb2(data, title)
    if extension == '.fodt':
        return _office(None, extension, title, flat_data=data)
    text, encoding = decode_text(data)
    if extension in ('.html', '.htm', '.xhtml'):
        parser = _HTML(text, image)
        return {'name': parser.title or title, 'encoding': encoding}, parser.chapters(parser.title or title)
    return {'name': title, 'encoding': encoding}, _text_chapters(text, title, extension in ('.md', '.markdown'))


def _parse_archive(path, extension, title):
    with _Archive(path) as archive:
        if extension == '.epub':
            return _epub(archive, title)
        if extension in OFFICE_EXTENSIONS:
            return _office(archive, extension, title)
        chapters, image_members, text_members, ignored = [], [], [], []
        for name in sorted(archive.members, key=_natural):
            suffix = PurePosixPath(name).suffix.lower()
            if name.startswith('__MACOSX/') or PurePosixPath(name).name.startswith('.'):
                continue
            if suffix in IMAGE_EXTENSIONS:
                image_members.append(name)
            elif suffix in TEXT_EXTENSIONS and extension != '.cbz':
                text_members.append(name)
            else:
                ignored.append(name)
        if extension == '.fbz' and (len(text_members) != 1 or PurePosixPath(text_members[0]).suffix.lower() != '.fb2'):
            raise DocumentError('FBZ phải chứa đúng một tài liệu FB2.')
        book = {'name': title}
        for member in text_members:
            metadata, member_chapters = _parse_bytes(archive.read(member), PurePosixPath(member).suffix.lower(), PurePosixPath(member).stem, lambda reference, member=member: archive.image(member, reference))
            if len(text_members) == 1:
                book.update(metadata)
            chapters.extend(member_chapters)
        if image_members and not text_members:
            groups = {}
            for member in image_members:
                groups.setdefault(str(PurePosixPath(member).parent), []).append(member)
            for folder, members in groups.items():
                html = ''.join('<img src="' + _image_url(archive.read(member)) + '" alt="' + escape(PurePosixPath(member).name, quote=True) + '">' for member in members)
                chapters.append(_chapter(title if folder == '.' else folder, html))
            book['kind'] = 'comic'
        if ignored:
            book['warnings'] = ['Đã bỏ qua các mục không phải nội dung đọc: ' + ', '.join(ignored[:20])]
        return book, chapters


def import_document(path):
    path = Path(path)
    if not path.is_file():
        raise DocumentError('Không tìm thấy tệp tài liệu.')
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        label = 'PDF/OCR' if extension == '.pdf' else extension.upper().lstrip('.') or 'không có phần mở rộng'
        raise UnsupportedFormatError('Chưa hỗ trợ ' + label + ': cần engine chuyển đổi thực đã tích hợp và kiểm thử. Xem capabilities().')
    if path.stat().st_size > MAX_FILE_SIZE:
        raise DocumentError('Tệp vượt giới hạn 128 MiB.')
    budget_token = _OUTPUT_BUDGET.set(_OutputBudget())
    try:
        if extension in TEXT_EXTENSIONS:
            metadata, chapters = _parse_bytes(path.read_bytes(), extension, path.stem)
        else:
            metadata, chapters = _parse_archive(path, extension, path.stem)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, OSError, UnicodeError, ET.ParseError) as error:
        raise DocumentError('Không đọc được tài liệu: ' + str(error)) from error
    finally:
        _OUTPUT_BUDGET.reset(budget_token)
    if not chapters:
        raise DocumentError('Tài liệu không có văn bản hoặc ảnh đọc được.')
    for index, chapter in enumerate(chapters):
        chapter['index'] = index
    book = {'name': path.stem, 'author': '', 'kind': 'text', 'format': extension[1:], 'description': '', 'source': 'local', 'path': str(path.resolve()), 'chapter_count': len(chapters)}
    book.update(metadata)
    return {'book': book, 'chapters': chapters}
