"""Import a user-downloaded vBook dictionary pack; no vocabulary is bundled.

Observed layout: big-endian counts, optional base/check int arrays, then
N UTF-8 meaning lines followed by N key lines. Rules use another grammar.
"""

import hashlib
from pathlib import Path
import sqlite3
import struct
import zipfile

from qt import QtError, _entry


SOURCE_URL = 'https://raw.githubusercontent.com/Darkrai9x/vbook-settings/main/translate2.zip'
KNOWN_SHA256 = '8132f987fd01db53d0f6cef199307d05a83da6458d05d02b940c2d4bd74e0c40'
KINDS = {'VP.dic': 'vietphrase', 'NE.dic': 'name', 'HV.dic': 'hanviet', 'Pronouns.dic': 'pronouns'}
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024


class PackError(QtError):
    pass


def _columns(filename, data):
    if filename not in KINDS:
        raise PackError('Loại từ điển không hỗ trợ; luật vBook chưa tương thích với parser luật hiện tại.')
    if len(data) > MAX_MEMBER:
        raise PackError('Từ điển vượt giới hạn 64 MiB.')
    position = 0

    def count(limit):
        nonlocal position
        if position + 4 > len(data):
            raise PackError('Từ điển bị thiếu header.')
        value = struct.unpack_from('>I', data, position)[0]
        position += 4
        if not 0 < value <= limit:
            raise PackError('Số mục hoặc kích thước trie không hợp lệ.')
        return value

    if filename != 'HV.dic':
        nodes = []
        for _ in range(2):
            size = count(4_000_000)
            nodes.append(size)
            position += size * 4
            if position > len(data):
                raise PackError('Từ điển bị thiếu mảng trie.')
        if nodes[0] != nodes[1]:
            raise PackError('Hai mảng trie có độ dài khác nhau.')
    size = count(1_000_000)
    try:
        text = data[position:].decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise PackError('Nội dung từ điển không phải UTF-8 hợp lệ.') from error
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    if len(lines) != size * 2:
        raise PackError(f'Từ điển hỏng: cần {size * 2} dòng nghĩa/từ, nhận {len(lines)}; không đoán ghép các cặp.')
    return size, lines


def decode_dictionary(filename, data):
    """Yield the pack's original key/meaning pairs, including alternatives."""
    size, lines = _columns(filename, data)
    for index in range(size):
        yield lines[size + index].removesuffix('\r'), lines[index].removesuffix('\r')


def install_pack(dictionary, archive_path):
    """Add missing global entries atomically; retain every existing user edit."""
    path = Path(archive_path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise PackError('Gói từ điển phải là tệp ZIP tối đa 32 MiB.')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {'source_url': SOURCE_URL if digest == KNOWN_SHA256 else None, 'sha256': digest,
              'files': {}, 'pending': []}
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise PackError('Không mở được ZIP từ điển.') from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or not {'VP.dic', 'NE.dic', 'HV.dic'}.issubset(names):
            raise PackError('ZIP thiếu từ điển chính hoặc chứa tên tệp trùng.')
        if sum(info.file_size for info in infos) > 80 * 1024 * 1024 or any(
                info.file_size > MAX_MEMBER or info.flag_bits & 1 for info in infos):
            raise PackError('ZIP từ điển quá lớn hoặc bị mã hóa.')
        connection = sqlite3.connect(dictionary.path, timeout=30)
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                for filename, kind in KINDS.items():
                    summary = {'kind': kind, 'declared': 0, 'inserted': 0, 'skipped_existing': 0,
                               'rejected': 0, 'warnings': []}
                    report['files'][filename] = summary
                    if filename not in names:
                        summary['warnings'].append('Gói chưa có từ điển này.')
                        report['pending'].append(kind)
                        continue
                    try:
                        data = archive.read(filename)
                        size, lines = _columns(filename, data)
                    except (PackError, zipfile.BadZipFile, RuntimeError) as error:
                        if filename != 'Pronouns.dic':
                            raise PackError(f'{filename}: {error}') from error
                        summary['warnings'].append(str(error))
                        report['pending'].append(kind)
                        continue
                    summary['declared'] = size
                    batch = []

                    def flush():
                        before = connection.total_changes
                        connection.executemany('INSERT OR IGNORE INTO entries(kind,word,meaning,scope) '
                                               'VALUES(:kind,:word,:meaning,:scope)', batch)
                        added = connection.total_changes - before
                        summary['inserted'] += added
                        summary['skipped_existing'] += len(batch) - added
                        batch.clear()

                    for index in range(size):
                        try:
                            batch.append(_entry(kind, lines[size + index].removesuffix('\r'),
                                                lines[index].removesuffix('\r'), '*'))
                        except QtError as error:
                            summary['rejected'] += 1
                            if len(summary['warnings']) < 10:
                                summary['warnings'].append(f'Mục {index + 1}: {error}')
                        if len(batch) == 1000:
                            flush()
                    flush()
                    del data, lines
                report['files']['Rule.dic'] = {'kind': 'rules', 'declared': 0, 'inserted': 0,
                    'skipped_existing': 0, 'rejected': 0,
                    'warnings': ['Giữ Rule.dic trong ZIP; cú pháp luật vBook chưa tương thích nên chưa nhập.']}
                report['pending'].append('rules')
        finally:
            connection.close()
    return report
