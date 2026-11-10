"""Private local library. Imports never write to the source backup."""
import contextlib
from collections import Counter
import hashlib
import json
import logging
import math
import re
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from htmltools import parse_html, safe_remote_url

MAX_JSON = 32 * 1024 * 1024
MAX_BACKUP = 512 * 1024 * 1024
TRASH_RETENTION = 30 * 24 * 60 * 60

def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)

def _source(value):
    text = str(value or "").casefold()
    return next((key for key in ("qq", "full", "hako") if key in text), text)

def _groups(value):
    if value is None or value == -1 or value == "-1":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v) != "-1"]
    return [str(value)]

def _chapter_key(url):
    return urlsplit(url or "").path.rstrip("/").removesuffix(".html")

class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "library.sqlite3"
        self._asset_lock = threading.RLock()
        # shortcut: in-flight leases cover one server; share leases before supporting multiple servers per library.
        self._active_assets = Counter()
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'text',
                    format TEXT NOT NULL DEFAULT 'online', description TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0, chapter_index INTEGER NOT NULL DEFAULT 0,
                    position REAL NOT NULL DEFAULT 0, groups_json TEXT NOT NULL DEFAULT '[]',
                    archived INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL,
                    trashed_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL, read_at REAL NOT NULL DEFAULT 0,
                    chapter_count INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                    html TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
                    images TEXT NOT NULL DEFAULT '[]', downloaded INTEGER NOT NULL DEFAULT 0,
                    original_html TEXT, original_text TEXT, raw TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (book_id, idx)
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, chapter_index INTEGER NOT NULL DEFAULT 0,
                    quote TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
                    position REAL NOT NULL DEFAULT 0, type TEXT NOT NULL DEFAULT 'note',
                    created_at REAL NOT NULL, raw TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS groups_list (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy (path TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chapter_book ON chapters(book_id);
                CREATE INDEX IF NOT EXISTS note_book ON notes(book_id);
            """)
            if 'trashed_at' not in {row[1] for row in db.execute('PRAGMA table_info(books)')}:
                db.execute('ALTER TABLE books ADD COLUMN trashed_at REAL NOT NULL DEFAULT 0')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _book(self, row):
        if row is None:
            raise KeyError("Không tìm thấy truyện.")
        item = dict(row)
        item["groups"] = json.loads(item.pop("groups_json"))
        item["archived"] = bool(item["archived"])
        item['trash_expires_at'] = item['trashed_at'] + TRASH_RETENTION if item['trashed_at'] else None
        metadata = json.loads(item.pop("raw", '{}'))
        for key in ('warnings', 'ocr'):
            if key in metadata:
                item[key] = metadata[key]
        return item

    def list_books(self, query="", source="", group="", sort="recent", archived=False, trashed=False):
        with self.db() as db:
            rows = (db.execute('SELECT * FROM books WHERE trashed_at>0').fetchall() if trashed else
                    db.execute('SELECT * FROM books WHERE trashed_at=0 AND archived=?', (int(archived),)).fetchall())
        result = [self._book(row) for row in rows]
        term = query.casefold().strip()
        if term:
            result = [b for b in result if term in (b["name"] + " " + b["author"]).casefold()]
        if source:
            result = [b for b in result if b["source"] == source]
        if group:
            result = [b for b in result if str(group) in b["groups"]]
        keys = {"recent": "read_at", "added": "added_at", "updated": "updated_at",
                "progress": "progress", "chapters": "chapter_count", "name": "name"}
        key = keys.get(sort, "read_at")
        return sorted(result, key=lambda b: ((b[key] or "").casefold() if key == "name" else b[key], b["name"]),
                      reverse=key != "name")

    def get_book(self, book_id):
        with self.db() as db:
            return self._book(db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())

    def upsert_book(self, book, chapters=None):
        with self.db() as db:
            return self._put_book(db, book, chapters)

    def _put_book(self, db, book, chapters=None):
        source = _source(book.get("source", book.get("book_source", "")))
        path = str(book.get("path", book.get("url", "")))
        book_id = str(book.get("id") or book.get("book_id") or
                      hashlib.sha256((source + ":" + path).encode()).hexdigest()[:32])
        # A source refresh must reuse imported shelf IDs rather than split reading history.
        existing = db.execute("SELECT id FROM books WHERE source=? AND path=?", (source, path)).fetchone() if path else None
        if not existing and source in ("qq", "full", "hako", "stv") and path:
            existing = next((row for row in db.execute("SELECT id,path FROM books WHERE source=?", (source,))
                             if _chapter_key(row["path"]) == _chapter_key(path)), None)
        if existing and "id" not in book and "book_id" not in book:
            book_id = existing["id"]
        if chapters is not None and source in ('qq', 'full', 'hako', 'stv'):
            old_urls = [_chapter_key(row['url']) for row in db.execute(
                'SELECT url FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
            new_urls = [_chapter_key(chapter.get('url', chapter.get('path', ''))) for chapter in chapters]
            if old_urls and (len(new_urls) < len(old_urls) or old_urls != new_urls[:len(old_urls)]):
                raise ValueError('Mục lục nguồn đã đổi thứ tự hoặc thiếu chương. Thư viện, ghi chú và tiến độ cũ được giữ nguyên; cần đối chiếu chương trước khi cập nhật.')
        kind = book.get("kind") or ("comic" if book.get("book_type") == 2 else "text")
        now = time.time()
        count = len(chapters) if chapters is not None else int(book.get("chapter_count", 0) or 0)
        position = float(book.get("position", book.get("last_position", 0)) or 0)
        progress = float(book.get("progress", book.get("percent", 0)) or 0)
        if not math.isfinite(position) or position < 0:
            position = 0
        if not math.isfinite(progress):
            progress = 0
        db.execute("""
            INSERT INTO books(id,name,author,source,path,cover,kind,format,description,
                progress,chapter_index,position,groups_json,added_at,updated_at,read_at,chapter_count,raw)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,author=excluded.author,
                source=excluded.source,path=excluded.path,cover=excluded.cover,kind=excluded.kind,
                format=excluded.format,description=excluded.description,
                chapter_count=excluded.chapter_count,updated_at=excluded.updated_at,
                read_at=CASE WHEN books.read_at=0 THEN excluded.read_at ELSE books.read_at END,raw=excluded.raw
        """, (book_id, str(book.get("name") or "Chưa đặt tên"), str(book.get("author") or ""), source, path,
              str(book.get("cover") or ""), kind, str(book.get("format") or "online"),
              str(book.get("description") or ""), min(1, max(0, progress)),
              max(0, int(book.get("chapter_index", book.get("last_index", 0)) or 0)),
              min(1, position), _json(_groups(book.get("groups", book.get("book_folder")))),
              now, now, float(book.get('read_at', 0) or 0), count, _json(book)))
        if chapters is not None:
            for index, chapter in enumerate(chapters):
                title = str(chapter.get("title", chapter.get("name", "")) or "Chương " + str(index + 1))
                url = str(chapter.get("url", chapter.get("path", "")) or "")
                old = db.execute("SELECT url,raw FROM chapters WHERE book_id=? AND idx=?", (book_id, index)).fetchone()
                same_url = old and _chapter_key(old['url']) == _chapter_key(url)
                metadata = json.loads(old['raw']) if same_url else {}
                metadata.update({key: value for key, value in chapter.items()
                                 if value is not None or key not in ('language', 'translation', 'is_vip', 'access')})
                if chapter.get('is_vip') is False:
                    metadata.pop('access', None)
                if old and not same_url:
                    db.execute("UPDATE chapters SET html='',text='',images='[]',downloaded=0,"
                               "original_html=NULL,original_text=NULL WHERE book_id=? AND idx=?", (book_id, index))
                has_body = bool(chapter.get("html") or chapter.get("text") or chapter.get("images"))
                db.execute("""
                    INSERT INTO chapters(book_id,idx,title,url,html,text,images,downloaded,raw)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET
                    title=excluded.title,url=excluded.url,raw=excluded.raw
                """, (book_id, index, title, url, chapter.get("html", ""), chapter.get("text", ""),
                      _json(chapter.get("images", [])), int(has_body), _json(metadata)))
        return self._book(db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())

    def chapters(self, book_id):
        self.get_book(book_id)
        with self.db() as db:
            result = []
            for row in db.execute('SELECT idx,title,url,downloaded,raw FROM chapters WHERE book_id=? ORDER BY idx', (book_id,)):
                item = dict(row)
                metadata = json.loads(item.pop('raw'))
                if type(metadata.get('is_vip')) is bool:
                    item['is_vip'] = metadata['is_vip']
                item.update(index=item['idx'], downloaded=bool(item['downloaded']))
                result.append(item)
            return result

    def content(self, book_id, index):
        with self.db() as db:
            row = db.execute("SELECT * FROM chapters WHERE book_id=? AND idx=?", (book_id, int(index))).fetchone()
        if row is None:
            raise KeyError("Không tìm thấy chương.")
        item = dict(row)
        item["index"] = item.pop("idx")
        item["images"] = json.loads(item["images"])
        item["downloaded"] = bool(item["downloaded"])
        metadata = json.loads(item.pop('raw'))
        if type(metadata.get('is_vip')) is bool:
            item['is_vip'] = metadata['is_vip']
        if metadata.get('translation') == 'source' and metadata.get('language') == 'vi':
            item.update(translation='source', language='vi')
        return item

    def save_content(self, book_id, index, content):
        self.content(book_id, index)
        with self.db() as db:
            metadata = json.loads(db.execute('SELECT raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()['raw'])
            if content.get('translation') == 'source' and content.get('language') == 'vi':
                metadata.update(translation='source', language='vi')
            metadata.pop('access', None)
            db.execute("UPDATE chapters SET html=?,text=?,images=?,downloaded=1,raw=? WHERE book_id=? AND idx=?",
                       (content.get("html", ""), content.get("text", ""), _json(content.get("images", [])),
                        _json(metadata), book_id, int(index)))
        return self.content(book_id, index)

    def chapter_access(self, book_id, index, access):
        if (access.get('state') != 'vip_locked' or access.get('vip') is not True
                or not isinstance(access.get('evidence'), str) or not access['evidence'].strip()):
            return
        self.content(book_id, index)
        with self.db() as db:
            metadata = json.loads(db.execute('SELECT raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()['raw'])
            metadata.update(is_vip=True, access=access)
            db.execute('UPDATE chapters SET raw=? WHERE book_id=? AND idx=?', (_json(metadata), book_id, index))

    def edit_content(self, book_id, index, content):
        old = self.content(book_id, index)
        if self.get_book(book_id)["kind"] != "text":
            raise ValueError("Chương ảnh không dùng trình sửa văn bản.")
        with self.db() as db:
            db.execute("UPDATE chapters SET original_html=COALESCE(original_html,html),"
                       "original_text=COALESCE(original_text,text),html=?,text=?,title=? "
                       "WHERE book_id=? AND idx=?", (content["html"], content["text"],
                       content.get("title", old["title"]), book_id, int(index)))
        return self.content(book_id, index)

    def restore_content(self, book_id, index):
        self.content(book_id, index)
        with self.db() as db:
            db.execute("UPDATE chapters SET html=COALESCE(original_html,html),"
                       "text=COALESCE(original_text,text),original_html=NULL,original_text=NULL "
                       "WHERE book_id=? AND idx=?", (book_id, int(index)))
        return self.content(book_id, index)

    def progress(self, book_id, index, position):
        index, position = int(index), float(position)
        book = self.get_book(book_id)
        if not math.isfinite(position) or not 0 <= position <= 1 or not 0 <= index < book["chapter_count"]:
            raise ValueError("Vị trí đọc không hợp lệ.")
        with self.db() as db:
            chapter = db.execute('SELECT downloaded,raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()
            access = json.loads(chapter['raw']).get('access') if chapter else None
            if chapter and not chapter['downloaded'] and isinstance(access, dict) and access.get('state') == 'vip_locked':
                raise ValueError('Chương VIP chưa đọc được; vị trí đọc trước đó được giữ nguyên.')
            db.execute("UPDATE books SET chapter_index=?,position=?,progress=?,read_at=? WHERE id=?",
                       (index, position, (index + position) / book["chapter_count"], time.time(), book_id))
        return self.get_book(book_id)

    def update_book(self, book_id, changes):
        self.get_book(book_id)
        allowed = {k: changes[k] for k in ("name", "author", "archived", "cover") if k in changes}
        if "groups" in changes:
            allowed["groups_json"] = _json(_groups(changes["groups"]))
        if allowed:
            with self.db() as db:
                db.execute("UPDATE books SET " + ",".join(k + "=?" for k in allowed) + " WHERE id=?",
                           (*allowed.values(), book_id))
        return self.get_book(book_id)

    def groups(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT id,name FROM groups_list ORDER BY name")]

    def trash_book(self, book_id):
        with self.db() as db:
            changed = db.execute('UPDATE books SET trashed_at=CASE WHEN trashed_at=0 THEN ? ELSE trashed_at END WHERE id=?',
                                 (time.time(), book_id)).rowcount
            if not changed:
                raise KeyError('Không tìm thấy truyện.')
        return self.get_book(book_id)

    def restore_book(self, book_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT trashed_at FROM books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise KeyError('Không tìm thấy truyện.')
            if row['trashed_at'] and row['trashed_at'] + TRASH_RETENTION <= time.time():
                raise ValueError('Truyện đã hết thời hạn 30 ngày trong thùng rác, không thể khôi phục.')
            db.execute('UPDATE books SET trashed_at=0,archived=0 WHERE id=?', (book_id,))
        return self.get_book(book_id)

    @staticmethod
    def _purge(db, book_id):
        db.execute('DELETE FROM notes WHERE book_id=?', (book_id,))
        db.execute('DELETE FROM books WHERE id=?', (book_id,))

    def purge_book(self, book_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT trashed_at FROM books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise KeyError('Không tìm thấy truyện.')
            if not row['trashed_at']:
                raise ValueError('Chỉ xóa vĩnh viễn truyện đã nằm trong thùng rác.')
            self._purge(db, book_id)
        self.cleanup_files()

    def purge_expired(self, now=None, skip_ids=()):
        cutoff = (time.time() if now is None else now) - TRASH_RETENTION
        skipped = set(skip_ids)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            ids = [row['id'] for row in db.execute('SELECT id FROM books WHERE trashed_at>0 AND trashed_at<=?', (cutoff,))
                   if row['id'] not in skipped]
            for book_id in ids:
                self._purge(db, book_id)
        if ids:
            self.cleanup_files()
        return ids

    @contextlib.contextmanager
    def use_asset(self, path):
        path = Path(path).resolve()
        with self._asset_lock:
            self._active_assets[path] += 1
        try:
            yield
        finally:
            with self._asset_lock:
                self._active_assets[path] -= 1
                if not self._active_assets[path]:
                    del self._active_assets[path]

    def cleanup_files(self):
        # Purge commits first; a separate write lock keeps references stable during collection.
        try:
            with self._asset_lock, self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                files, images = set(), set()

                def image_reference(value, base):
                    if not isinstance(value, str):
                        raise ValueError('Invalid image reference')
                    if not value or value.startswith('data:'):
                        return
                    parsed = urlsplit(value)
                    urls = (parse_qs(parsed.query).get('url', []) if parsed.path == '/api/image' and
                            (not parsed.netloc or parsed.hostname in ('127.0.0.1', 'localhost', '::1')) else [value])
                    for url in urls:
                        checked = safe_remote_url(url, base)
                        if checked:
                            images.add(hashlib.sha256(checked.encode()).hexdigest())

                for row in db.execute('SELECT path,cover FROM books'):
                    files.add(row['path'].replace('\\', '/').rsplit('/', 1)[-1].lower())
                    image_reference(row['cover'], row['path'])
                for row in db.execute('SELECT c.html,c.original_html,c.images,b.path FROM chapters c JOIN books b ON b.id=c.book_id'):
                    values = json.loads(row['images'])
                    if not isinstance(values, list):
                        raise ValueError('Invalid chapter images')
                    for value in values:
                        image_reference(value, row['path'])
                    for markup in (row['html'], row['original_html']):
                        if markup:
                            for node in parse_html(markup).select('img'):
                                image_reference(node.get('data-original') or node.get('data-src') or node.get('src'), row['path'])

                for name, pattern, referenced in (
                        ('files', r'[0-9a-f]{64}\.[a-z0-9]+', files),
                        ('images', r'([0-9a-f]{64})(\.[0-9a-f]{32}\.tmp)?', images)):
                    directory = self.data_dir / name
                    try:
                        info = directory.lstat()
                        if (not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                                or directory.resolve() != directory):
                            continue
                        paths = list(directory.iterdir())
                    except FileNotFoundError:
                        continue
                    except (OSError, RuntimeError):
                        logging.getLogger(__name__).warning('Cannot inspect asset directory %s', directory, exc_info=True)
                        continue
                    for path in paths:
                        match = re.fullmatch(pattern, path.name)
                        if not match:
                            continue
                        asset = directory / (match[1] if name == 'images' else path.name)
                        if asset in self._active_assets or (path.name in referenced):
                            continue
                        try:
                            info = path.lstat()
                            if (stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
                                    and path.resolve().parent == directory):
                                path.unlink()
                        except FileNotFoundError:
                            pass
                        except (OSError, RuntimeError):
                            logging.getLogger(__name__).warning('Cannot remove unused asset %s', path, exc_info=True)
        except (sqlite3.Error, ValueError, TypeError, RecursionError):
            logging.getLogger(__name__).warning('Skipping file cleanup: cannot verify library references', exc_info=True)

    def settings(self, values=None):
        with self.db() as db:
            if values is not None:
                for key, value in values.items():
                    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (str(key), _json(value)))
            return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}

    def notes(self, book_id=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM notes" + (" WHERE book_id=?" if book_id else "") +
                              " ORDER BY created_at DESC", (book_id,) if book_id else ()).fetchall()
        return [dict(r) for r in rows]

    def save_note(self, note):
        book_id = str(note.get("book_id") or "")
        if not book_id:
            raise ValueError("Thiếu truyện của ghi chú.")
        note_id = str(note.get("id") or uuid.uuid4().hex)
        position = float(note.get("position", 0) or 0)
        if not math.isfinite(position):
            raise ValueError("Vị trí ghi chú không hợp lệ.")
        with self.db() as db:
            db.execute("""
                INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET quote=excluded.quote,note=excluded.note,
                    color=excluded.color,position=excluded.position
            """, (note_id, book_id, int(note.get("chapter_index", 0)), str(note.get("quote", "")),
                  str(note.get("note", "")), str(note.get("color", "")), position,
                  str(note.get("type", "note")), time.time(), _json(note)))
        return next(r for r in self.notes() if r["id"] == note_id)

    def delete_note(self, note_id):
        with self.db() as db:
            db.execute("DELETE FROM notes WHERE id=?", (note_id,))

    def search_content(self, book_id, query):
        if not query.strip():
            raise ValueError("Nhập cụm từ cần tìm.")
        book = self.get_book(book_id)
        term = query.casefold()
        results = []
        searched = 0
        with self.db() as db:
            for row in db.execute("SELECT idx,title,text,downloaded FROM chapters WHERE book_id=? ORDER BY idx",
                                  (book_id,)):
                if not row["downloaded"]:
                    continue
                searched += 1
                at = row["text"].casefold().find(term)
                if at >= 0:
                    results.append({"index": row["idx"], "title": row["title"],
                                    "snippet": row["text"][max(0, at - 70):at + len(query) + 120]})
        return {"results": results, "searched": searched, "total": book["chapter_count"]}

    def import_backup(self, source):
        source = Path(source).expanduser().resolve()
        archive = None
        if source.is_file():
            if not zipfile.is_zipfile(source):
                raise ValueError("Bản sao lưu phải là thư mục hoặc ZIP.")
            archive = zipfile.ZipFile(source)
            infos = archive.infolist()
            if len(infos) > 200000 or sum(i.file_size for i in infos) > MAX_BACKUP:
                archive.close()
                raise ValueError("Bản sao lưu vượt giới hạn xử lý an toàn.")
            for info in infos:
                p = PurePosixPath(info.filename.replace("\\", "/"))
                if p.is_absolute() or ".." in p.parts or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                    archive.close()
                    raise ValueError("Đường dẫn trong ZIP không an toàn.")
            names = {i.filename: i for i in infos}
            entries = sorted(n for n in names if re_book_path(n))
            if not entries:
                archive.close()
                raise ValueError("Không tìm thấy books/*/book.json trong bản sao lưu.")
            roots = {PurePosixPath(name).parents[2] for name in entries}
            if len(roots) != 1:
                archive.close()
                raise ValueError("ZIP chứa nhiều bản sao lưu. Hãy chọn riêng một bản.")
            root = roots.pop().as_posix()
            prefix = '' if root == '.' else root + '/'
            def read(relative, default=None):
                name = prefix + relative
                if name not in names:
                    return default
                if names[name].file_size > MAX_JSON:
                    raise ValueError("Tệp JSON quá lớn: " + relative)
                return json.loads(archive.read(name).decode("utf-8-sig"))
            book_paths = [n[len(prefix):] for n in entries]
        elif source.is_dir():
            if not (source / "books").is_dir():
                raise ValueError("Chọn thư mục chứa books, data và prefs.")
            def read(relative, default=None):
                path = (source / relative).resolve()
                if not path.is_relative_to(source):
                    raise ValueError("Liên kết trong backup đi ra ngoài thư mục.")
                if not path.is_file():
                    return default
                if path.stat().st_size > MAX_JSON:
                    raise ValueError("Tệp JSON quá lớn: " + relative)
                return json.loads(path.read_text(encoding="utf-8-sig"))
            book_paths = sorted(str(p.relative_to(source)).replace("\\", "/")
                                for p in (source / "books").glob("*/book.json"))
        else:
            raise ValueError("Không tìm thấy đường dẫn bản sao lưu.")
        report = {"books": 0, "chapters": 0, "annotations": 0, "orphan_annotations": 0, "warnings": []}
        try:
            with self.db() as db:
                for relative in book_paths:
                    book = read(relative)
                    if not isinstance(book, dict) or not book.get("book_id"):
                        raise ValueError("Dữ liệu truyện không hợp lệ: " + relative)
                    folder = relative.rsplit("/", 1)[0]
                    chapters = read(folder + "/chapters.json", [])
                    if not isinstance(chapters, list):
                        raise ValueError("Mục lục không hợp lệ: " + folder)
                    novel = read(folder + "/novel.json", {})
                    self._put_book(db, dict(book, description=novel.get("description", ""),
                                           read_at=float(book.get('timestamp', 0) or 0) / 1000), chapters)
                    for leaf in ("book.json", "chapters.json", "novel.json", "names.json", "pages.json"):
                        value = read(folder + "/" + leaf)
                        if value is not None:
                            db.execute("INSERT OR IGNORE INTO legacy VALUES(?,?)", (folder + "/" + leaf, _json(value)))
                    report["books"] += 1
                    report["chapters"] += len(chapters)
                    if book.get("chapter_count") and book["chapter_count"] != len(chapters):
                        report["warnings"].append("Số chương khai báo khác mục lục: " + str(book.get("name", book["book_id"])))
                prefs = read("prefs/setting.json", {})
                groups = prefs.get("shelf.group.list", "{}")
                if isinstance(groups, str):
                    groups = json.loads(groups)
                for key, name in groups.items():
                    db.execute("INSERT OR IGNORE INTO groups_list VALUES(?,?)", (str(key), str(name)))
                for leaf in ("notes.json", "bookmarks.json"):
                    for item in read("data/" + leaf, []):
                        book_id = str(item.get("book_id", item.get("name", "")))
                        note_id = "legacy-" + leaf + "-" + str(item.get("id", item.get("a", uuid.uuid4().hex)))
                        db.execute("INSERT OR IGNORE INTO notes VALUES(?,?,?,?,?,?,?,?,?,?)", (
                            note_id, book_id, int(item.get("chap_index", 0)), str(item.get("content", "")),
                            str(item.get("title", "")), "", float(item.get("position", 0) or 0),
                            "bookmark" if leaf == "bookmarks.json" else "note",
                            float(item.get("timestamp", 0) or 0) / 1000, _json(item)))
                        report["annotations"] += 1
                        if not db.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                            report["orphan_annotations"] += 1
                for folder, leaves in {"prefs": ["setting.json", "book.json", "novel.json", "comic.json", "tts.json"],
                                       "data": ["notes.json", "bookmarks.json", "names.json", "downloads.json",
                                                "repositories.json", "chinese_names.json", "chinese_vietphrases.json",
                                                "trashes.json"]}.items():
                    for leaf in leaves:
                        value = read(folder + "/" + leaf)
                        if value is not None:
                            db.execute("INSERT OR IGNORE INTO legacy VALUES(?,?)", (folder + "/" + leaf, _json(value)))
                report["groups"] = len(groups)
        finally:
            if archive:
                archive.close()
        return report

def re_book_path(name):
    parts = PurePosixPath(name).parts
    return len(parts) >= 3 and parts[-3] == "books" and parts[-1] == "book.json"
