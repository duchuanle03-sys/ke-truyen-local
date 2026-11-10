"""Bounded text imports for published RTF, HWPX and HWPML structures.

Format references (primary publishers):
https://learn.microsoft.com/en-us/previous-versions/office/developer/office2000/aa140284(v=office.10)
https://tech.hancom.com/hwpxformat/
https://tech.hancom.com/python-hwpx-parsing-1/
Hancom Hwp Document File Formats 3.0 / HWPML revision 1.2, 20141105.
"""

import codecs
from html import escape
from pathlib import Path
import re
import zipfile
import zlib

from documents import (CHAPTER, DocumentError, MAX_FILE_SIZE, MAX_MEMBER_SIZE,
                       UnsupportedFormatError, _Archive, _html_text,
                       _image_url, _nodes, _sections, _tag, _text_chapters, _xml)


SUPPORTED_EXTENSIONS = ['.hwpml', '.hwpx', '.rtf']
CHARSETS = {0: None, 1: None, 2: 'symbol', 77: 'mac_roman', 128: 'cp932', 129: 'cp949', 130: 'johab',
            134: 'gbk', 136: 'big5', 161: 'cp1253', 162: 'cp1254', 163: 'cp1258', 177: 'cp1255',
            178: 'cp1256', 186: 'cp1257', 204: 'cp1251', 222: 'cp874', 238: 'cp1250', 255: 'cp437'}
DESTINATIONS = set('fonttbl colortbl stylesheet info pict object objdata datastore filetbl fldinst generator listtable listoverridetable rsidtbl themedata colorschememapping latentstyles shppict nonshppict header headerl headerr headerf footer footerl footerr footerf annotation xmlnstbl xmlopen xmlclose xmlattrname xmlattrvalue'.split())
CONTROL_TEXT = {'par': '\n', 'line': '\n', 'page': '\n\n', 'sect': '\n\n', 'tab': '\t', 'cell': '\t',
                'row': '\n', 'emdash': '—', 'endash': '–', 'bullet': '•', 'lquote': '‘', 'rquote': '’',
                'ldblquote': '“', 'rdblquote': '”', 'enspace': '\u2002', 'emspace': '\u2003', 'qmspace': '\u2005'}
CONTROL_WORD = re.compile(br'\\([A-Za-z]+)(-?\d+)? ?')


def capabilities():
    return {'supported_extensions': SUPPORTED_EXTENSIONS,
            'rtf': {'supported': True, 'content': 'Văn bản Unicode/ANSI, nhóm, font charset, đoạn, bảng dạng chữ; không chạy đối tượng hoặc mã nhúng.'},
            'hwpx': {'supported': True, 'content': 'Thứ tự spine, metadata, đoạn và bảng, ảnh raster nhúng; script/OLE không chạy.'},
            'hwpml': {'supported': True, 'content': 'Metadata, section, đoạn, bảng XML; ảnh/đối tượng chưa chuyển đổi.'},
            'unsupported_extensions': ['.mobi', '.prc', '.azw', '.azw3', '.umd', '.chm', '.doc', '.dot', '.wps', '.hwp'],
            'limits': {'file_bytes': MAX_FILE_SIZE, 'text_bytes': MAX_MEMBER_SIZE, 'group_depth': 100},
            'layout': 'Nhập để đọc văn bản, không bảo toàn chính xác bố cục, phông hoặc dàn trang gốc.'}


def _codepage(number):
    encoding = 'cp' + str(number)
    try:
        return codecs.lookup(encoding).name
    except LookupError as error:
        raise DocumentError('RTF dùng codepage chưa được hỗ trợ: ' + str(number)) from error


def _rtf_text(data):
    if len(data) > MAX_MEMBER_SIZE or not re.match(br'^\s*\{\\rtf1\b', data):
        raise DocumentError('RTF thiếu chữ ký {\\rtf1 hoặc vượt giới hạn 64 MiB.')
    stack, state, pieces, pending, fonts = [], None, [], bytearray(), {}
    index, fallback, root_closed, default_font = 0, 0, False, 0
    def flush():
        if pending:
            encoding = fonts.get(state['font'] if state['font'] is not None else default_font) or state['encoding']
            if encoding == 'symbol':
                raise DocumentError('RTF dùng font Symbol chưa có ánh xạ Unicode.')
            try:
                pieces.append(bytes(pending).decode(encoding))
            except (UnicodeError, LookupError) as error:
                raise DocumentError('RTF có dữ liệu không hợp lệ cho encoding ' + encoding) from error
            pending.clear()
    def emit_bytes(value):
        nonlocal fallback
        skip = min(fallback, len(value))
        fallback -= skip
        if not state['skip'] and not state['hidden']:
            pending.extend(value[skip:])
    def emit(value):
        nonlocal fallback
        flush()
        if fallback:
            fallback -= 1
        elif not state['skip'] and not state['hidden']:
            pieces.append(value)
    while index < len(data):
        char = data[index]
        if state is None and char not in b' \r\n\t{':
            raise DocumentError('RTF có nội dung ngoài nhóm gốc.')
        if char == 123:
            if root_closed:
                raise DocumentError('RTF có nhiều nhóm gốc.')
            if len(stack) >= 100:
                raise DocumentError('RTF lồng nhóm quá sâu.')
            if state is not None:
                flush()
            stack.append(state)
            if state is None:
                state = {'skip': False, 'hidden': False, 'encoding': 'cp1252', 'uc': 1, 'font': None, 'fonttbl': False, 'upr_pending': 0, 'unicode_branch': False, 'parent_skip': False}
            else:
                parent = state
                state = parent.copy()
                state['parent_skip'] = parent['skip']
                state['unicode_branch'] = parent['upr_pending'] == 1
                if parent['upr_pending'] == 2:
                    state['skip'] = True
                if parent['upr_pending']:
                    parent['upr_pending'] -= 1
                state['upr_pending'] = 0
            fallback = 0
            index += 1
            continue
        if char == 125:
            if not stack:
                raise DocumentError('RTF đóng nhóm không có nhóm mở.')
            flush()
            state = stack.pop()
            root_closed = state is None
            fallback = 0
            index += 1
            continue
        if state is None or char in b'\r\n':
            index += 1
            continue
        if char != 92:
            end = index + 1
            while end < len(data) and data[end] not in b'\\{}\r\n':
                end += 1
            emit_bytes(data[index:end])
            index = end
            continue
        if index + 1 >= len(data):
            raise DocumentError('RTF kết thúc giữa escape.')
        symbol = data[index + 1]
        if symbol == 39:
            hexadecimal = data[index + 2:index + 4]
            if len(hexadecimal) != 2 or not re.fullmatch(br'[0-9a-fA-F]{2}', hexadecimal):
                raise DocumentError('RTF có hex escape không hợp lệ.')
            emit_bytes(bytes([int(hexadecimal, 16)]))
            index += 4
            continue
        if not (65 <= symbol <= 90 or 97 <= symbol <= 122):
            index += 2
            if symbol == 42:
                flush()
                state['skip'] = True
            elif symbol in (92, 123, 125):
                emit_bytes(bytes([symbol]))
            elif symbol == 126:
                emit('\u00a0')
            elif symbol == 95:
                emit('\u2011')
            elif symbol == 45:
                emit('\u00ad')
            elif symbol in (10, 13):
                emit('\n')
            continue
        match = CONTROL_WORD.match(data, index)
        if len(match.group(1)) > 32 or (match.group(2) and len(match.group(2)) > 11):
            raise DocumentError('RTF có control word hoặc tham số quá dài.')
        word = match.group(1).decode('ascii')
        number = int(match.group(2)) if match.group(2) else None
        index = match.end()
        if word == 'bin':
            flush()
            if number is None or number < 0 or number > len(data) - index:
                raise DocumentError('RTF có dữ liệu nhị phân bị cắt cụt.')
            index += number
        elif word in DESTINATIONS:
            flush()
            state['skip'] = True
            if word == 'fonttbl':
                state['fonttbl'] = True
        elif word == 'ud' and state['unicode_branch']:
            state['skip'] = state['parent_skip']
        elif word == 'upr':
            state['upr_pending'] = 2
        elif word == 'uc':
            if number is None or not 0 <= number <= 16:
                raise DocumentError('RTF có giá trị Unicode fallback không hợp lệ.')
            state['uc'] = number
        elif word == 'u':
            flush()
            if number is None or not -32768 <= number <= 65535 or number % 65536 in (65534, 65535):
                raise DocumentError('RTF có UTF-16 code unit không hợp lệ.')
            if not state['skip'] and not state['hidden']:
                pieces.append(chr(number % 65536))
            fallback = state['uc']
        elif word in ('ansi', 'mac', 'pc', 'pca', 'ansicpg'):
            flush()
            state['encoding'] = _codepage(number) if word == 'ansicpg' and number is not None else {'ansi': 'cp1252', 'mac': 'mac_roman', 'pc': 'cp437', 'pca': 'cp850'}.get(word, 'cp1252')
        elif word == 'deff' and number is not None:
            default_font = number
        elif word == 'f' and number is not None:
            flush()
            state['font'] = number
        elif word in ('fcharset', 'cpg') and state['fonttbl']:
            if number is None:
                raise DocumentError('RTF font thiếu charset/codepage.')
            if word == 'cpg':
                encoding = _codepage(number)
            else:
                if number not in CHARSETS:
                    raise DocumentError('RTF có font charset chưa hỗ trợ: ' + str(number))
                encoding = CHARSETS[number]
            fonts[state['font']] = encoding
        elif word == 'plain':
            flush()
            state['hidden'], state['font'] = False, None
        elif word == 'v':
            flush()
            state['hidden'] = number != 0
        elif word in CONTROL_TEXT:
            emit(CONTROL_TEXT[word])
    if stack or not root_closed:
        raise DocumentError('RTF thiếu dấu đóng nhóm.')
    try:
        text = ''.join(pieces).encode('utf-16-le', errors='surrogatepass').decode('utf-16-le')
    except UnicodeError as error:
        raise DocumentError('RTF có cặp UTF-16 surrogate không hợp lệ.') from error
    return text


def _metadata(root, title_name, author_name):
    title, author = _nodes(root, title_name), _nodes(root, author_name)
    return {'name': ''.join(title[0].itertext()).strip() if title else '',
            'author': ', '.join(''.join(node.itertext()).strip() for node in author)}


def _render_hancom(node, warnings, image=None, hwpx=False):
    tag = _tag(node)
    lower = tag.casefold()
    if lower in {'ole', 'object', 'script', 'scripts', 'equation', 'unknownobject', 'button', 'radiobutton', 'checkbox', 'combobox', 'edit', 'listbox', 'scrollbar', 'hiddencomment'}:
        warnings.add('Đã bỏ qua script/đối tượng nhúng hoặc công thức không hỗ trợ; chỉ giữ văn bản hiển thị.')
        return ''
    if not hwpx and tag == 'PICTURE':
        warnings.add('Ảnh nhúng HWPML chưa được chuyển đổi; văn bản/bảng vẫn được giữ.')
        return ''
    if hwpx and tag == 'img':
        reference = node.get('binaryItemIDRef')
        if not reference or image is None:
            raise DocumentError('HWPX thiếu liên kết ảnh nhúng.')
        return '<img src="' + image(reference) + '" alt="">'
    if tag in ('t', 'CHAR'):
        inline = {'tab': '\t', 'TAB': '\t', 'lineBreak': '\n', 'LINEBREAK': '\n', 'NBSPACE': '\u00a0', 'FWSPACE': '\u2007', 'HYPEN': '\u00ad', 'nbSpace': '\u00a0', 'fwSpace': '\u2007'}
        text = escape(node.text or '')
        for child in node:
            text += (escape(inline[_tag(child)]) if _tag(child) in inline else _render_hancom(child, warnings, image, hwpx)) + escape(child.tail or '')
        return text.replace('\n', '<br>')
    inner = ''.join(_render_hancom(child, warnings, image, hwpx) for child in node)
    mapping = {'p': 'div', 'P': 'div', 'tbl': 'table', 'TABLE': 'table', 'tr': 'tr', 'ROW': 'tr', 'tc': 'td', 'CELL': 'td'}
    target = mapping.get(tag)
    return '<' + target + '>' + inner + '</' + target + '>' if target else inner


def _section_chapters(root, title, warnings, image=None, hwpx=False):
    events = []
    for node in root:
        html = _render_hancom(node, warnings, image, hwpx)
        text = _html_text(html)
        heading = text if len(text) <= 180 and '\n' not in text and CHAPTER.match(text) else None
        events.append((heading, html))
    return _sections(events, title)


def _hwpx(path):
    with _Archive(path) as archive:
        if archive.read('mimetype').strip() != b'application/hwp+zip':
            raise DocumentError('Tệp không có mimetype HWPX hợp lệ.')
        package = _xml(archive.read('Contents/content.hpf'))
        if _tag(package) != 'package':
            raise DocumentError('HWPX thiếu cấu trúc package OPF.')
        metadata = _metadata(package, 'title', 'creator')
        manifest = {}
        for item in _nodes(package, 'item'):
            item_id, href = item.get('id'), item.get('href')
            if not item_id or not href or item_id in manifest:
                raise DocumentError('HWPX có manifest ID/href không hợp lệ hoặc trùng.')
            manifest[item_id] = archive.resolve('', href)
        spine = _nodes(package, 'itemref')
        if not spine:
            raise DocumentError('HWPX thiếu spine/thứ tự đọc.')
        warnings, chapters = set(), []
        if any(name.startswith('Scripts/') for name in archive.members):
            warnings.add('Đã bỏ qua script trong tài liệu; không thực thi mã nhúng.')
        def image(reference):
            member = manifest.get(reference)
            if member is None:
                raise DocumentError('HWPX ảnh tham chiếu ID manifest không tồn tại.')
            return _image_url(archive.read(member))
        seen = set()
        section_count = 0
        for reference in spine:
            member = manifest.get(reference.get('idref'))
            if member is None or member not in archive.members:
                raise DocumentError('HWPX spine tham chiếu mục không tồn tại.')
            if member in seen:
                raise DocumentError('HWPX spine chứa mục lặp.')
            seen.add(member)
            root = _xml(archive.read(member))
            if _tag(root) in ('head', 'HWPApplicationSetting'):
                continue
            if _tag(root) != 'sec':
                raise DocumentError('HWPX spine chứa cấu trúc chưa hỗ trợ: ' + _tag(root))
            section_count += 1
            chapters.extend(_section_chapters(root, f'Phần {section_count}', warnings, image, hwpx=True))
        metadata['warnings'] = sorted(warnings)
        return metadata, chapters


def _hwpml(data):
    root = _xml(data)
    if _tag(root) != 'HWPML':
        raise DocumentError('Tệp không có cấu trúc HWPML hợp lệ.')
    heads = [child for child in root if _tag(child) == 'HEAD']
    summaries = _nodes(heads[0], 'DOCSUMMARY') if heads else []
    metadata = _metadata(summaries[0], 'TITLE', 'AUTHOR') if summaries else {}
    warnings, chapters = set(), []
    bodies = [child for child in root if _tag(child) == 'BODY']
    sections = [child for body in bodies for child in body if _tag(child) == 'SECTION']
    for index, section in enumerate(sections, 1):
        chapters.extend(_section_chapters(section, f'Phần {index}', warnings))
    metadata['warnings'] = sorted(warnings)
    return metadata, chapters


def import_legacy(path):
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError('Định dạng ' + extension + ' cần bộ chuyển đổi thực đã kiểm thử; không có parser trong bản này.')
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise DocumentError('Không tìm thấy tệp hoặc tệp vượt giới hạn 128 MiB.')
    try:
        if extension == '.rtf':
            data = path.read_bytes()
            text = _rtf_text(data)
            metadata = {'warnings': ['RTF giữ văn bản/đoạn; bỏ định dạng, ảnh, header/footer và đối tượng nhúng.']}
            chapters = _text_chapters(text, path.stem)
        elif extension == '.hwpx':
            metadata, chapters = _hwpx(path)
        else:
            metadata, chapters = _hwpml(path.read_bytes())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as error:
        raise DocumentError('Không đọc được tài liệu: ' + str(error)) from error
    if not chapters:
        raise DocumentError('Tài liệu không có văn bản hoặc ảnh đọc được.')
    for index, chapter in enumerate(chapters):
        chapter['index'] = index
    book = {'name': path.stem, 'author': '', 'kind': 'text', 'format': extension[1:], 'description': '', 'source': 'local', 'path': str(path.resolve()), 'chapter_count': len(chapters)}
    book.update({key: value for key, value in metadata.items() if value or key == 'warnings'})
    return {'book': book, 'chapters': chapters}
