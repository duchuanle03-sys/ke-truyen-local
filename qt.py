"""Local, deterministic QT dictionaries; no network or bundled vocabulary.

Precedence: private book scope, global scope; within each scope Names,
VietPhrase, pronouns, Han Viet; longest match inside a tier. Rules are tried
first, private before global, then longest match. This is our documented
grammar, not a claim of compatibility with other QT rule engines.
"""

from collections import OrderedDict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading


class QtError(ValueError):
    pass


KINDS = ('name', 'vietphrase', 'pronouns', 'hanviet', 'rules')
ALIASES = {'names': 'name', 'vp': 'vietphrase', 'pronoun': 'pronouns', 'rule': 'rules', 'han-viet': 'hanviet'}
CJK = re.compile('[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]')
FIELDS = re.compile(r'\{(n[1-8]?|word[1-8]?)\}')
HEADER = '# qt-dictionary-v1 '


def _kind(value):
    value = ALIASES.get(value, value)
    if value not in KINDS:
        raise QtError('Loại từ điển không hợp lệ: ' + str(value))
    return value


def _scope(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise QtError('Phạm vi từ điển phải là * hoặc mã truyện hợp lệ.')
    return value


def _compile_rule(pattern, replacement):
    if len(pattern) > 256 or len(replacement) > 2048:
        raise QtError('Luật quá dài (pattern tối đa 256, bản dịch tối đa 2048 ký tự).')
    fields, pieces, end = [], [], 0
    for match in FIELDS.finditer(pattern):
        field = match.group(1)
        if field in fields:
            raise QtError('Placeholder trong pattern không được lặp: ' + field)
        if fields:
            separator = pattern[end:match.start()]
            previous = r'[\u3400-\u9fff\uf900-\ufaff]' if fields[-1].startswith('word') else r'[0-9.,]'
            if not any(re.fullmatch(previous, char) is None for char in separator):
                raise QtError('Giữa các placeholder cần dấu phân cách mà placeholder trước không thể chứa (word: ngoài chữ Hán; n: ngoài 0-9, dấu chấm/phẩy).')
        pieces.append(re.escape(pattern[end:match.start()]))
        number = r'[0-9]{1,12}(?:[.,][0-9]{1,6})?'
        word = r'[\u3400-\u9fff\uf900-\ufaff]{1,32}'
        pieces.append('(?P<' + field + '>' + (number if field.startswith('n') else word) + ')')
        fields.append(field)
        end = match.end()
    pieces.append(re.escape(pattern[end:]))
    remaining = FIELDS.sub('', pattern)
    if '{' in remaining or '}' in remaining:
        raise QtError('Placeholder pattern chỉ hỗ trợ {n}, {n1}…{n8}, {word}, {word1}…{word8}.')
    if not pattern:
        raise QtError('Pattern không được rỗng.')
    replacement_fields = re.findall(r'\{(lookup:)?([a-zA-Z0-9]+)\}', replacement)
    for lookup, field in replacement_fields:
        if field not in fields:
            raise QtError('Bản dịch tham chiếu placeholder chưa khai báo: ' + field)
        if lookup and not field.startswith('word'):
            raise QtError('lookup chỉ dùng với placeholder word.')
    stripped = re.sub(r'\{(?:lookup:)?[a-zA-Z0-9]+\}', '', replacement)
    if '{' in stripped or '}' in stripped:
        raise QtError('Placeholder bản dịch không hợp lệ.')
    return re.compile(''.join(pieces))


def _entry(kind, word, meaning, scope):
    kind, scope = _kind(kind), _scope(scope)
    if not isinstance(word, str) or not isinstance(meaning, str):
        raise QtError('Từ và nghĩa phải là văn bản.')
    word, meaning = word.strip(), meaning.strip()
    if not word or not meaning:
        raise QtError('Từ và nghĩa không được rỗng.')
    if len(word) > 256 or len(meaning) > 8192:
        raise QtError('Từ hoặc nghĩa quá dài.')
    if '=' in word or word.startswith('#') or any(ord(c) < 32 for c in word + meaning):
        raise QtError('Từ không chứa =, ký tự điều khiển hoặc bắt đầu bằng #; nghĩa phải nằm trên một dòng.')
    if kind == 'rules':
        _compile_rule(word, meaning)
    return {'kind': kind, 'word': word, 'meaning': meaning, 'scope': scope}


def _longest(trie, text, start):
    node, best, index = trie, None, start
    while index < len(text) and text[index] in node:
        node = node[text[index]]
        index += 1
        if None in node:
            best = (index, node[None])
    return best


def _join(tokens):
    result = []
    previous_translated = False
    for part, translated in tokens:
        if not part:
            continue
        if result and (translated or previous_translated) and result[-1][-1].isalnum() and part[0].isalnum():
            result.append(' ')
        result.append(part)
        previous_translated = translated
    return ''.join(result)


@dataclass(frozen=True)
class _ScopeSnapshot:
    tries: dict
    rules: dict
    installed: bool
    generation: int


def _copy_path(root, word, meaning):
    parents, node = [], root
    for char in word:
        parents.append((node, char))
        node = node.get(char, {})
    copied = dict(node)
    if meaning is None: copied.pop(None, None)
    else: copied[None] = meaning.split('¦', 1)[0].strip()
    for parent, char in reversed(parents):
        updated = dict(parent)
        if copied: updated[char] = copied
        else: updated.pop(char, None)
        copied = updated
    return copied


class QtDictionary:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'qt.sqlite3'
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        # Owners serialize all shared-connection use; warm snapshots never wait for this mutex.
        self._db_guard = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.create_function('casefold', 1, str.casefold, deterministic=True)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS entries (
                kind TEXT NOT NULL, word TEXT NOT NULL, meaning TEXT NOT NULL,
                scope TEXT NOT NULL, PRIMARY KEY(kind, scope, word));
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL,
                kind TEXT NOT NULL, scope TEXT NOT NULL, entries TEXT NOT NULL);
        ''')
        self._db.commit()
        self._cache = OrderedDict()
        self._retired = []
        self._scope_generations = {}
        self._external_version = self._db.execute('PRAGMA data_version').fetchone()[0]

    def close(self):
        with self._write_lock:
            with self._lock:
                if self._closed: return
                self._closed = True
                self._retired.append(self._cache)
                self._cache = OrderedDict()
            with self._db_guard: self._db.close()
        self._reap()

    @contextmanager
    def _reader(self):
        with self._lock:
            if self._closed: raise QtError('Từ điển QT đã đóng.')
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.create_function('casefold', 1, str.casefold, deterministic=True)
            db.execute('BEGIN')
            yield db

    def _check_external_changes(self):
        if self._closed: raise QtError('Từ điển QT đã đóng.')
        if not self._db_guard.acquire(blocking=False): return
        try: current = self._db.execute('PRAGMA data_version').fetchone()[0]
        finally: self._db_guard.release()
        if current != self._external_version:
            self._retired.append(self._cache)
            self._cache = OrderedDict()
            for scope in self._scope_generations: self._scope_generations[scope] += 1
            self._external_version = current

    def _reap(self):
        with self._lock: retired, self._retired = self._retired, []
        retired.clear()

    def _publish(self, scope, snapshot, expected):
        with self._lock:
            self._check_external_changes()
            if (self._scope_generations.get(scope, 0), self._external_version) != expected: return False
            self._retired.append(self._cache.get(scope))
            self._cache[scope] = snapshot
            self._cache.move_to_end(scope)
            while len(self._cache) > 9:
                oldest = next(key for key in self._cache if key != '*')
                self._retired.append(self._cache.pop(oldest))
            return True

    def _build_rules(self, db, scope):
        rules = {}
        for item in db.execute("SELECT word,meaning FROM entries WHERE kind='rules' AND scope=?", (scope,)):
            first = item['word'][0] if not item['word'].startswith('{') else ''
            rules.setdefault(first, []).append((_compile_rule(item['word'], item['meaning']), item['meaning']))
        return {first: tuple(items) for first, items in rules.items()}

    def _build_scope(self, scope, generation):
        tries = {}
        with self._reader() as db:
            rules = self._build_rules(db, scope)
            for kind in KINDS[:-1]:
                trie = {}
                for item in db.execute('SELECT word,meaning FROM entries WHERE kind=? AND scope=?', (kind, scope)):
                    node = trie
                    for char in item['word']: node = node.setdefault(char, {})
                    node[None] = item['meaning'].split('¦', 1)[0].strip()
                tries[kind] = trie
        return _ScopeSnapshot(tries, rules, bool(rules or any(tries.values())), generation)

    def _changed(self, scope, kind=None, word=None, meaning=None):
        with self._lock:
            self._check_external_changes()
            generation = self._scope_generations.get(scope, 0) + 1
            self._scope_generations[scope] = generation
            expected = generation, self._external_version
            old = self._cache.get(scope)
        if old is None and kind is not None: return
        try:
            if kind and kind != 'rules':
                tries = dict(old.tries)
                tries[kind] = _copy_path(tries[kind], word, meaning)
                snapshot = _ScopeSnapshot(tries, old.rules, bool(old.rules or any(tries.values())), generation)
            elif kind == 'rules':
                with self._reader() as db: rules = self._build_rules(db, scope)
                snapshot = _ScopeSnapshot(old.tries, rules, bool(rules or any(old.tries.values())), generation)
            else:
                snapshot = self._build_scope(scope, generation)
            self._publish(scope, snapshot, expected)
        except BaseException:
            with self._lock:
                self._retired.append(self._cache.pop(scope, None))
            raise

    def status(self):
        with self._reader() as db:
            counts = [dict(row) for row in db.execute('SELECT kind,scope,count(*) AS count FROM entries GROUP BY kind,scope')]
            return {'installed': bool(counts), 'counts': counts, 'backups': db.execute('SELECT count(*) FROM backups').fetchone()[0],
                    'precedence': 'book > global; name > vietphrase > pronouns > hanviet; longest within each tier; rules first',
                    'rule_grammar': '{n}, {n1}..{n8}: Arabic number; {word}, {word1}..{word8}: 1..32 Han characters; between fields require a delimiter outside the preceding field alphabet; replacement {lookup:word} looks up dictionary',
                    'bundled_data': False, 'traditional_conversion': False}

    def list(self, kind='name', scope='*', query=''):
        kind, scope = _kind(kind), _scope(scope)
        if not isinstance(query, str) or len(query) > 8192:
            raise QtError('Chuỗi tìm kiếm không hợp lệ.')
        with self._reader() as db:
            rows = db.execute('SELECT kind,word,meaning,scope FROM entries WHERE kind=? AND scope=? ORDER BY word', (kind, scope))
            needle = query.casefold()
            return [dict(row) for row in rows if not needle or needle in row['word'].casefold() or needle in row['meaning'].casefold()]

    def save(self, kind, word, meaning, scope='*'):
        item = _entry(kind, word, meaning, scope)
        try:
            with self._write_lock:
                with self._lock: self._check_external_changes()
                with self._db_guard, self._db:
                    self._db.execute('INSERT INTO entries(kind,word,meaning,scope) VALUES(:kind,:word,:meaning,:scope) ON CONFLICT(kind,scope,word) DO UPDATE SET meaning=excluded.meaning', item)
                self._changed(item['scope'], item['kind'], item['word'], item['meaning'])
        finally: self._reap()
        return item

    def page(self, kind='name', scope='*', query='', offset=0, limit=100):
        kind, scope = _kind(kind), _scope(scope)
        if not isinstance(query, str) or len(query) > 8192:
            raise QtError('Chuỗi tìm kiếm không hợp lệ.')
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise QtError('Trang từ điển cần offset không âm, limit từ 1 đến 500.')
        where, params = 'kind=? AND scope=?', [kind, scope]
        if query:
            where += ' AND (instr(casefold(word),?)>0 OR instr(casefold(meaning),?)>0)'
            params += [query.casefold(), query.casefold()]
        with self._reader() as db:
            total = db.execute('SELECT count(*) FROM entries WHERE ' + where, params).fetchone()[0]
            items = [dict(row) for row in db.execute(
                'SELECT kind,word,meaning,scope FROM entries WHERE ' + where + ' ORDER BY word LIMIT ? OFFSET ?',
                params + [limit, offset])]
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit}

    def delete(self, kind, word, scope='*'):
        kind, scope = _kind(kind), _scope(scope)
        try:
            with self._write_lock:
                with self._lock: self._check_external_changes()
                with self._db_guard, self._db:
                    count = self._db.execute('DELETE FROM entries WHERE kind=? AND scope=? AND word=?', (kind, scope, word)).rowcount
                self._changed(scope, kind, word)
            return {'deleted': count}
        finally: self._reap()

    def _compiled(self, book_id):
        book_id = book_id or '*'
        scopes = (book_id, '*') if book_id != '*' else ('*',)
        try:
            for _ in range(4):
                with self._lock:
                    self._check_external_changes()
                    if all(scope in self._cache for scope in scopes):
                        snapshots = tuple(self._cache[scope] for scope in scopes)
                        for scope in scopes: self._cache.move_to_end(scope)
                        return (tuple(s.tries[kind] for s in snapshots for kind in KINDS[:-1] if s.tries[kind]),
                                tuple(s.rules for s in snapshots), any(s.installed for s in snapshots))
                with self._write_lock:
                    for scope in scopes:
                        with self._lock:
                            self._check_external_changes()
                            if scope in self._cache: continue
                            generation = self._scope_generations.setdefault(scope, 0)
                            expected = generation, self._external_version
                        snapshot = self._build_scope(scope, generation)
                        if not self._publish(scope, snapshot, expected): break
            raise QtError('Từ điển đang được thay đổi liên tục; hãy thử dịch lại.')
        finally: self._reap()

    @staticmethod
    def _lookup(text, tries):
        for trie in tries:
            match = _longest(trie, text, 0)
            if match and match[0] == len(text):
                return match[1]
        return text

    @classmethod
    def _replacement(cls, match, template, tries):
        def replace(field):
            value = match.group(field.group(2))
            return cls._lookup(value, tries) if field.group(1) else value
        return re.sub(r'\{(lookup:)?([a-zA-Z0-9]+)\}', replace, template)

    def translate(self, text, book_id=''):
        if not isinstance(text, str) or len(text) > 2_000_000:
            raise QtError('Nội dung QT phải là văn bản, tối đa 2 triệu ký tự.')
        if not isinstance(book_id, str) or len(book_id) > 512:
            raise QtError('Mã truyện không hợp lệ.')
        tries, rules, installed = self._compiled(book_id)
        if not installed and CJK.search(text):
            raise QtError('Chưa có dữ liệu QT cho truyện này. Hãy nhập Names/VietPhrase/Hán Việt trước.')
        tokens, index = [], 0
        while index < len(text):
            chosen = None
            for scope_rules in rules:
                for regex, replacement in scope_rules.get(text[index], ()) + scope_rules.get('', ()):
                    match = regex.match(text, index)
                    if match and (chosen is None or match.end() > chosen[0]):
                        chosen = (match.end(), self._replacement(match, replacement, tries))
                if chosen:
                    break
            if chosen is None:
                for trie in tries:
                    chosen = _longest(trie, text, index)
                    if chosen:
                        break
            if chosen:
                tokens.append((chosen[1], True))
                index = chosen[0]
            else:
                tokens.append((text[index], False))
                index += 1
        return _join(tokens)

    def validate_rule(self, pattern, replacement):
        try:
            _entry('rules', pattern, replacement, '*')
            return {'valid': True, 'errors': [], 'warnings': ['Chỉ hỗ trợ template giới hạn; không phải regex hoặc cú pháp luật của ứng dụng khác.']}
        except (QtError, TypeError) as error:
            return {'valid': False, 'errors': [str(error)], 'warnings': []}

    def preview_rule(self, pattern, replacement, sample, book_id=''):
        result = self.validate_rule(pattern, replacement)
        if not isinstance(sample, str) or len(sample) > 100_000:
            raise QtError('Mẫu thử luật tối đa 100.000 ký tự.')
        result.update(output=sample, matches=[])
        if result['valid']:
            tries = self._compiled(book_id)[0]
            regex = _compile_rule(pattern, replacement)
            result['matches'] = [{'start': m.start(), 'end': m.end(), 'source': m.group(), 'translation': self._replacement(m, replacement, tries)} for m in regex.finditer(sample)]
            result['output'] = regex.sub(lambda m: self._replacement(m, replacement, tries), sample)
        return result

    def import_text(self, kind, text, mode='merge', scope='*'):
        kind, scope = _kind(kind), _scope(scope)
        if mode not in ('merge', 'missing', 'replace'):
            raise QtError('Chế độ nhập phải là merge, missing hoặc replace.')
        if not isinstance(text, str) or len(text) > 32_000_000:
            raise QtError('Tệp từ điển phải là văn bản, tối đa 32 triệu ký tự.')
        text = text.lstrip('\ufeff')
        lines = text.splitlines()
        if lines and lines[0].startswith(HEADER):
            try:
                metadata = json.loads(lines[0][len(HEADER):])
                if _kind(metadata['kind']) != kind:
                    raise QtError('Loại từ điển trong tệp khác loại đã chọn.')
                header_scope = _scope(metadata['scope'])
                if scope != '*' and scope != header_scope:
                    raise QtError('Phạm vi trong tệp khác phạm vi đã chọn.')
                scope = header_scope
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise QtError('Header từ điển không hợp lệ.') from error
        report = {'kind': kind, 'scope': scope, 'mode': mode, 'inserted': 0, 'updated': 0, 'skipped': 0, 'errors': [], 'applied': False, 'backup_id': None}
        items = {}
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            separator = '=' if '=' in line else '\t'
            try:
                if separator not in line:
                    raise QtError('Thiếu dấu = hoặc tab ngăn từ và nghĩa.')
                word, meaning = line.split(separator, 1)
                item = _entry(kind, word, meaning, scope)
                if item['word'] in items:
                    report['skipped'] += 1
                items[item['word']] = item
            except QtError as error:
                report['errors'].append({'line': number, 'error': str(error)})
        if mode == 'replace' and (report['errors'] or not items):
            if not items and not report['errors']:
                report['errors'].append({'line': 0, 'error': 'Không thay thế bằng tệp rỗng; dùng thao tác xóa rõ ràng.'})
            return report
        try:
            with self._write_lock:
                with self._lock: self._check_external_changes()
                with self._db_guard, self._db:
                    self._db.execute('BEGIN IMMEDIATE')
                    old = [dict(row) for row in self._db.execute('SELECT kind,word,meaning,scope FROM entries WHERE kind=? AND scope=? ORDER BY word', (kind, scope))]
                    existing = {item['word'] for item in old}
                    if mode == 'replace':
                        cursor = self._db.execute('INSERT INTO backups(created,kind,scope,entries) VALUES(?,?,?,?)', (datetime.now(timezone.utc).isoformat(), kind, scope, json.dumps(old, ensure_ascii=False)))
                        report['backup_id'] = cursor.lastrowid
                        self._db.execute('DELETE FROM entries WHERE kind=? AND scope=?', (kind, scope))
                        existing = set()
                    for word, item in items.items():
                        if word in existing and mode == 'missing':
                            report['skipped'] += 1
                            continue
                        report['updated' if word in existing else 'inserted'] += 1
                        self._db.execute('INSERT INTO entries(kind,word,meaning,scope) VALUES(:kind,:word,:meaning,:scope) ON CONFLICT(kind,scope,word) DO UPDATE SET meaning=excluded.meaning', item)
                self._changed(scope)
                report['applied'] = True
        finally: self._reap()
        return report

    def export_text(self, kind, scope='*'):
        kind, scope = _kind(kind), _scope(scope)
        header = HEADER + json.dumps({'kind': kind, 'scope': scope}, ensure_ascii=False)
        return '\n'.join([header] + [item['word'] + '=' + item['meaning'] for item in self.list(kind, scope)]) + '\n'

    def restore_backup(self, backup_id):
        try:
            with self._write_lock:
                with self._lock: self._check_external_changes()
                with self._db_guard, self._db:
                    self._db.execute('BEGIN IMMEDIATE')
                    row = self._db.execute('SELECT * FROM backups WHERE id=?', (backup_id,)).fetchone()
                    if row is None: raise QtError('Không tìm thấy bản sao từ điển.')
                    items = [_entry(**item) for item in json.loads(row['entries'])]
                    current = [dict(item) for item in self._db.execute('SELECT kind,word,meaning,scope FROM entries WHERE kind=? AND scope=?', (row['kind'], row['scope']))]
                    self._db.execute('INSERT INTO backups(created,kind,scope,entries) VALUES(?,?,?,?)', (datetime.now(timezone.utc).isoformat(), row['kind'], row['scope'], json.dumps(current, ensure_ascii=False)))
                    self._db.execute('DELETE FROM entries WHERE kind=? AND scope=?', (row['kind'], row['scope']))
                    self._db.executemany('INSERT INTO entries(kind,word,meaning,scope) VALUES(:kind,:word,:meaning,:scope)', items)
                self._changed(row['scope'])
            return {'restored': len(items), 'kind': row['kind'], 'scope': row['scope']}
        finally: self._reap()
