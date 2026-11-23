"""Independent mutable synthetic M2 HTTP/state/process gate; never accepts user data.

Without --binary, capture Python and deliberately fail the missing implementation gate.
All generated data and reports stay in a fresh OS temporary directory.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import capture_reference as m1
from library import Store
from lifecycle import ServerLifecycle
from server import make_server
from sources import SourceService, SourceError

IMAGE_URL = "https://cdn.example.org/m2-page.png"
IMAGE_PATH = "/api/image?" + urlencode({"url": IMAGE_URL})
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII=") + b"\0" * 65536


def disabled_fetch(*args, **kwargs):
    raise SourceError("Synthetic offline transport disabled", code="network_error")


class OfflineTransport:
    __call__ = staticmethod(disabled_fetch)
    _read = staticmethod(disabled_fetch)

    def close(self):
        pass


def rust_source_hashes():
    paths = list((m1.ROOT / "rust" / "src").glob("*.rs"))
    paths += [m1.ROOT / "rust" / name for name in ("Cargo.toml", "Cargo.lock")]
    return {str(path.relative_to(m1.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def fixture(root, scale=False, image_bytes=None):
    root = root.resolve()
    dbpath = m1.fixture(root, "empty", sealed=False)
    store = Store(root)
    books, chapters, cached = (500, 200, 512) if scale else (4, 4, 8)
    chapter_counts = [50000] + [100] * 498 + [200] if scale else [chapters] * books
    def downloaded(book, index):
        return book == 0 and index < cached if scale else book * chapters + index < cached
    with store.db() as db:
        db.executemany("INSERT INTO books(id,name,source,path,cover,kind,chapter_count,added_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", ((str(i), f"Truyện tiếng Việt {i}", "file",
                       "https://cdn.example.org/book", IMAGE_URL, "comic" if i == 1 else "text", chapter_counts[i],
                       1, 1) for i in range(books)))
        db.executemany("INSERT INTO chapters(book_id,idx,title,url,text,html,images,downloaded,raw) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", ((str(b), c, f"Chương {c}", "https://example.org/chapter",
                       "Nội dung tiếng Việt. " * (2048 if scale else 128) if downloaded(b, c) else "",
                       '<p onclick="bad()">Tiếng Việt<script>bad()</script></p>' if downloaded(b, c) else "",
                       json.dumps([IMAGE_URL]) if b == 1 else "[]", int(downloaded(b, c)),
                       json.dumps({"is_vip": True, "access": {"state": "vip_locked"}}) if b == 2 and c == 1 else "{}")
                       for b in range(books) for c in range(chapter_counts[b])))
        db.execute("INSERT INTO settings VALUES('appearance',?)", (json.dumps({"theme": "sepia", "size": 22}),))
        reserved = {"$serde_json::private::Number": "123"}
        db.execute("INSERT INTO settings VALUES('stored-reserved',?)", (json.dumps(reserved),))
        db.execute("UPDATE books SET raw=? WHERE id='2'", (json.dumps({"warnings": [
            {"payload": reserved}, {"payload": {**reserved, "safe": True}}]}),))
        db.execute("INSERT INTO notes(id,book_id,created_at,note) VALUES('orphan','missing',1,'preserve')")
    (root / "images").mkdir(exist_ok=True)
    (root / "files").mkdir(exist_ok=True)
    original = root / "files" / (hashlib.sha256(b"synthetic original").hexdigest() + ".txt")
    original.write_bytes(b"synthetic original")
    with store.db() as db:
        db.execute("UPDATE books SET path=? WHERE id='3'", (str(original),))
    image = root / "images" / hashlib.sha256(IMAGE_URL.encode()).hexdigest()
    image.write_bytes(PNG if image_bytes is None else image_bytes)
    store.touch_image(image, image.stat().st_size)
    # Complete one-time synthetic indexing before any timed/runtime work.
    while not store.asset_index_ready():
        store.index_assets(limit=4096)
    store.close_inventory()
    store.checkpoint()
    marker = json.loads((root / m1.MARKER).read_text(encoding="utf-8"))
    marker.update(books=books, cached_bodies=cached,
                  sha256=hashlib.sha256(dbpath.read_bytes()).hexdigest())
    m1.write_json(root / m1.MARKER, marker)
    return dbpath


@contextlib.contextmanager
def oracle(root):
    server = make_server(root, port=0, source_service=SourceService(fetcher=OfflineTransport()))
    server.app._image_fetcher.close()
    server.app._image_fetcher = OfflineTransport()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, server.app.store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(port, path, method="GET", value=None, headers=None, raw=None):
    if value is None and raw is None:
        return m1.request(port, path, method, headers)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, raw if raw is not None else json.dumps(value, ensure_ascii=False).encode(),
                     {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        body = response.read()
        fields = {k.lower(): v for k, v in response.getheaders()}
        result = {"status": response.status, "headers": fields, "body_hex": body.hex(), "json": None}
        if body and fields.get("content-type", "").startswith("application/json"):
            try:
                result["json"] = json.loads(body)
            except ValueError as error:
                result["decode_error"] = str(error)
        return result
    finally:
        conn.close()


def corpus():
    result = [("GET", p, None, {}) for p in ("/", "/api/settings", "/api/groups", "/api/sources",
              "/api/books", "/api/books/0", "/api/books/0/chapters", "/api/books/0/chapters/0",
              "/api/books/1/chapters/0", "/api/books/2/chapters/1", "/api/books/absent",
              "/api/books/0/chapters/99", IMAGE_PATH, "/api/books/0/cover", "/api/notes")]
    result += [("HEAD", p, None, {}) for p in ("/api/settings", "/api/books/0/chapters",
                "/api/books/0/chapters/0", IMAGE_PATH, "/api/books/0/cover")]
    result += [("POST", "/api/settings", {"appearance": {"theme": "dark", "size": 25}, "null": None,
                "array": [True, 1, "Việt"]}, {})]
    result += [("POST", "/api/books/0/progress", v, {}) for v in
               ({"index": 1, "position": .5}, {"index": "0", "position": "0.25"},
                {"index": 0, "position": "0_0.5"}, {"index": 0, "position": "٠.٥"},
                {"index": None, "position": 0}, {"index": 0, "position": None},
                {"index": -1, "position": .5}, {"index": 0, "position": 2}, {"index": 0})]
    result += [("POST", "/api/books/2/progress", {"index": 1, "position": .5}, {})]
    result += [("POST", "/api/readers", v, {}) for v in
               ({"id": "reader", "book_id": "0", "index": 0, "sequence": 3},
                {"id": "reader", "book_id": None, "sequence": 2},
                {"id": "reader", "book_id": "1", "index": 0, "sequence": 4},
                {"id": "reader", "book_id": None, "sequence": 5},
                {"id": "", "book_id": None}, {"id": "x", "book_id": "0", "index": True},
                {"id": "x", "book_id": "0", "index": 0, "sequence": True},
                {"id": "x", "book_id": "missing", "index": 0},
                {"id": "x", "book_id": "2", "index": 1},
                {"id": "雪" * 128, "book_id": None, "sequence": 0},
                {"id": "雪" * 129, "book_id": None},
                {"id": "too-large", "book_id": None, "sequence": 2 ** 53 + 1},
                {"id": None, "book_id": None},
                {"id": "durable", "book_id": "0", "index": 0, "sequence": 9})]
    result += [("GET", "/api/books/0/chapters/0", None, {"X-Reader-ID": "header", "X-Reader-Seq": "7"})]
    result += [("POST", "/api/notes", v, {}) for v in
               ({"id": "note", "book_id": "0", "chapter_index": 1, "position": .4,
                 "quote": "trích dẫn", "note": "ghi chú", "color": "yellow"},
                {"id": "note", "book_id": "1", "chapter_index": 3, "note": "updated"},
                {"id": "orphan-new", "book_id": "missing", "note": "orphan allowed"}, {})]
    result += [("POST", "/api/notes", v, {}) for v in (
        {"id": "overflow", "book_id": "0", "chapter_index": 9223372036854775808},
        {"id": "underflow", "book_id": "0", "chapter_index": -9223372036854775809},
        {"id": "beyond-u64", "book_id": "0", "chapter_index": 18446744073709551616},
        {"id": "string-overflow", "book_id": "0", "chapter_index": "9223372036854775808"},
        {"id": "string-underflow", "book_id": "0", "chapter_index": "-9223372036854775809"},
        {"id": "minimum", "book_id": "0", "chapter_index": "-9223372036854775808"},
        {"id": "maximum", "book_id": "0", "chapter_index": "9223372036854775807"},
        {"id": "containers", "book_id": "0", "quote": {"a": True, "b": None},
         "note": [True, None, ["雪"], {"x": "y"}], "color": [False, 1]})]
    result += [("GET", "/api/notes?book_id=0", None, {}), ("DELETE", "/api/notes/note", None, {}),
               ("DELETE", "/api/notes/absent", None, {}), ("GET", "/api/notes", None, {})]
    result += [("POST", "/api/books/0/update", {"name": "Đổi tên", "author": "Tác giả",
                "groups": ["one", -1, "two", "one"], "archived": True, "ignored": 1}, {}),
               ("GET", "/api/groups", None, {}), ("GET", "/api/books/0", None, {})]
    result += [("POST", "/api/books/0/update", v, {}) for v in (
        {"name": 9223372036854775808}, {"name": -9223372036854775809},
        {"name": 18446744073709551616}, {"groups": -1.0},
        {"groups": [{"k": True}, ["雪", None], True, None]})]
    result += [("POST", "/api/books/0/update", {"cover": cover}, {}) for cover in (
        IMAGE_PATH, "HTTPS://CDN.EXAMPLE.ORG:443/m2-page.png", "/m2-page.png", "//cdn.example.org/m2-page.png")]
    result += [("GET", "/api/image?" + urlencode(q), None, {}) for q in (
        {"url": "http://localhost/uncached.png"},
        {"url": "https://93.184.216.34/uncached.png", "referer": "http://localhost/book"},
        {"url": IMAGE_URL, "referer": "http://localhost/book"})]
    result += [("POST", "/api/books/3/update", {"cover": "http://[broken"}, {})]
    return result


def normalized(value):
    if isinstance(value, dict):
        for key in ("read_at", "created_at"):
            if isinstance(value.get(key), (float, int)) and value[key] > 1000000000:
                assert abs(time.time() - value[key]) < 60, (key, value[key])
        return {k: ("<clock>" if k in ("read_at", "created_at") and isinstance(v, (float, int)) and v > 1000000000
                    else normalized(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def capture(port, root=None):
    result = []
    for method, path, value, headers in corpus():
        response = request(port, path, method, value, headers)
        item = {"case": [method, path, value, headers], "response": response}
        if root is not None and isinstance(value, dict) and "cover" in value:
            with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
                book = path.split("/")[3]
                item["assets"] = {table: [list(row) for row in db.execute(sql, (book,))] for table, sql in (
                    ("refs", "SELECT book_id,idx,kind,name FROM asset_refs WHERE book_id=? ORDER BY idx,kind,name"),
                    ("pins", "SELECT book_id,idx,name FROM image_pins WHERE book_id=? ORDER BY idx,name"),
                    ("pending", "SELECT book_id,idx,blocked FROM asset_pending WHERE book_id=? ORDER BY idx"))}
        result.append(item)
    return result


def compare(expected, actual):
    failures = []
    for e, a in zip(expected, actual, strict=True):
        er, ar = e["response"], a["response"]
        if e.get("assets") != a.get("assets"):
            failures.append({"case": e["case"], "field": "assets", "expected": e.get("assets"), "actual": a.get("assets")})
        fields = ["status"]
        if er["json"] is not None:
            fields += ["json"]
        elif e["case"][0] != "HEAD":
            fields += ["body_hex"]
        for field in fields:
            if json.dumps(normalized(er[field]), sort_keys=True, ensure_ascii=False) != json.dumps(normalized(ar[field]), sort_keys=True, ensure_ascii=False):
                failures.append({"case": e["case"], "field": field, "expected": er[field], "actual": ar[field]})
        for header in m1.SECURITY:
            if er["headers"].get(header) != ar["headers"].get(header):
                failures.append({"case": e["case"], "header": header,
                                 "expected": er["headers"].get(header), "actual": ar["headers"].get(header)})
        if e["case"][0] == "HEAD" and ar["body_hex"]:
            failures.append({"case": e["case"], "error": "HEAD returned body"})
    return failures


def state(root):
    with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
        db.create_function("shelf_fold", 1, str.casefold, deterministic=True)
        db.row_factory = sqlite3.Row
        tables = {}
        for table, order in (("books", "id"), ("chapters", "book_id,idx"), ("notes", "id"),
                             ("settings", "key"), ("chapter_readers", "id"), ("chapter_cache", "id"),
                             ("image_cache", "name"), ("groups_list", "id"),
                             ("book_groups", "book_id,group_id"),
                             ("asset_refs", "book_id,idx,kind,name"), ("image_pins", "book_id,idx,name"),
                             ("asset_pending", "id"), ("asset_gc_candidates", "id"),
                             ("chapter_cache_state", "id"), ("image_cache_state", "id")):
            rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for row in rows:
                if table == "asset_gc_candidates" and row.get("retry_at", 0) > 1000000000:
                    row["retry_at"] = "<clock>"
                for field in ("expires", "last_used"):
                    if field in row and row[field] > 1000000000:
                        assert row[field] <= time.time() + 301, row
                        if field == "expires":
                            assert row[field] >= time.time() + 290, row
                        row[field] = "<clock>"
            tables[table] = normalized(rows)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        return tables


def conditional(port, root):
    full = request(port, IMAGE_PATH)
    assert full["status"] == 200 and bytes.fromhex(full["body_hex"]) == PNG
    fields = full["headers"]
    assert int(fields["content-length"]) == len(PNG)
    assert fields["etag"].startswith('W/"') and fields["last-modified"]
    image = root.resolve() / "images" / hashlib.sha256(IMAGE_URL.encode()).hexdigest()
    with image.open("rb") as handle:
        info = os.fstat(handle.fileno())
    identity = f'{image}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'.encode()
    assert fields["etag"] == 'W/"' + hashlib.sha256(identity).hexdigest() + '"', fields
    head = request(port, IMAGE_PATH, "HEAD")
    assert head["status"] == 200 and not head["body_hex"]
    for field in ("etag", "last-modified", "content-length", "content-type"):
        assert head["headers"][field] == fields[field]
    for method in ("GET", "HEAD"):
        for header, value in (("If-None-Match", fields["etag"]), ("If-None-Match", "*"),
                              ("If-Modified-Since", fields["last-modified"])):
            response = request(port, IMAGE_PATH, method, headers={header: value})
            assert response["status"] == 304 and not response["body_hex"], response
    assert request(port, IMAGE_PATH, headers={"If-None-Match": '"wrong"',
                   "If-Modified-Since": fields["last-modified"]})["status"] == 200


def automatic_note(port):
    before = time.time()
    response = request(port, "/api/notes", "POST", {"book_id": "0", "note": "generated id"})
    assert response["status"] == 200, response
    note = response["json"]
    assert re.fullmatch("[0-9a-f]{32}", note["id"]), note
    assert before <= note["created_at"] <= time.time() + 1, note
    assert request(port, "/api/notes/" + note["id"], "DELETE")["json"] == {"deleted": True}


def encoded_json(port):
    result = []
    for encoding in ("utf-8-sig", "utf-16", "utf-32", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        raw = json.dumps({"bom": "Tiếng Việt"}, ensure_ascii=False).encode(encoding)
        response = request(port, "/api/settings", "POST", raw=raw)
        result.append({"case": ["POST", "/api/settings", {"encoding": encoding}, {}], "response": response})
        if encoding.endswith("-be"):
            raw = (b"\xfe\xff" if encoding == "utf-16-be" else b"\0\0\xfe\xff") + raw
            result.append({"case": ["POST", "/api/settings", {"encoding": encoding + "-bom"}, {}],
                           "response": request(port, "/api/settings", "POST", raw=raw)})
    return result


def parser_boundaries(port):
    cases = [
        ("nested-reserved", b'{"nested":{"$serde_json::private::Number":"123"}}'),
        ("nested-reserved-multi", b'{"nested-multi":{"$serde_json::private::Number":"123","safe":true}}'),
        ("top-reserved", b'{"$serde_json::private::Number":"123"}'),
        ("top-reserved-multi", b'{"$serde_json::private::Number":"123","safe":true}'),
        ("integer-4300", b'{"digits":' + b'1' * 4300 + b'}'),
        ("negative-integer-4300", b'{"negative-digits":-' + b'1' * 4300 + b'}'),
        ("integer-4301", b'{"overflow-digits":' + b'1' * 4301 + b'}'),
        ("negative-integer-4301", b'{"negative-overflow-digits":-' + b'1' * 4301 + b'}'),
        ("empty-raw-body", b''),
        ("utf8-bom-only", b'\xef\xbb\xbf'),
        ("utf16le-bom-only", b'\xff\xfe'),
        ("utf16be-bom-only", b'\xfe\xff'),
        ("utf32le-bom-only", b'\xff\xfe\0\0'),
        ("utf32be-bom-only", b'\0\0\xfe\xff'),
    ]
    return [{"case": ["POST", "/api/settings", {"raw_case": label}, {}],
             "response": request(port, "/api/settings", "POST", raw=body)} for label, body in cases]


def offline_miss(port, root):
    before = m1.hashes(root / "images")
    response = request(port, "/api/image?" + urlencode({"url": "https://93.184.216.34/uncached.png"}))
    assert response["status"] == 503 and response["json"]["code"] == "network_error", response
    assert m1.hashes(root / "images") == before, "offline miss created or modified cached assets"
    return {"status": response["status"], "code": response["json"]["code"]}


def reader_capacity(port):
    results = []
    for index in range(130):
        response = request(port, "/api/readers", "POST", {"id": f"capacity-{index}", "book_id": None})
        results.append({"status": response["status"], "json": response["json"]})
    assert any(r["status"] == 200 for r in results) and any(r["status"] == 400 for r in results)
    return results


def cover_fixture(canonical, root):
    shutil.copytree(canonical, root)
    store = Store(root)
    with store.db() as db:
        db.execute("UPDATE books SET cover='https://93.184.216.34/uncached.png' WHERE id IN ('0','2')")
        db.execute("UPDATE books SET path='synthetic-original.txt' WHERE id='0'")
        db.execute("UPDATE books SET source='hako',path='https://docln.net/truyen/123' WHERE id='2'")
    store.close_inventory()
    store.checkpoint()


def cover_misses(port, root):
    before = m1.hashes(root / "images")
    local = request(port, "/api/books/0/cover")
    online = request(port, "/api/books/2/cover")
    assert local["status"] == online["status"] == 400, (local, online)
    prefix = "Không tải được bìa cũ hoặc bìa trên trang nguồn: "
    assert online["json"]["error"].startswith(prefix), online
    assert "code" not in online["json"], online
    assert m1.hashes(root / "images") == before
    # The transport stub's diagnostic text cannot be part of the native offline contract.
    return {"local": {"status": local["status"], "json": local["json"]},
            "online": {"status": online["status"], "error_prefix": prefix}}


def command(binary, root, port=0):
    return m1.rust_command(binary, root / "library.sqlite3")[:-1] + [str(port), "--writable"]


def rejected(command_line, root, unchanged=True, coordination=False):
    # Windows denies reads of the byte-zero coordination lock held by another process.
    def protected_hashes():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file() and p.name != ".server.lock"
                and not (coordination and p.name in ("library.sqlite3-wal", "library.sqlite3-shm"))}
    before = protected_hashes()
    lock_size = (root / ".server.lock").stat().st_size if (root / ".server.lock").exists() else None
    result = subprocess.run(command_line, capture_output=True, timeout=15)
    assert result.returncode != 0, result.stdout
    if unchanged:
        after = protected_hashes()
        assert after == before, (str(root), "rejected startup changed fixture", {
            name: [before.get(name), after.get(name)] for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)})
        if lock_size is None and not coordination:
            assert not (root / ".server.lock").exists(), "rejected startup created lock"
        elif lock_size is not None:
            assert (root / ".server.lock").stat().st_size == lock_size
    return {"exit": result.returncode, "stderr": result.stderr.decode("utf-8", errors="replace")}


def lock_probe(root):
    handle = ServerLifecycle._acquire_library_lock(root)
    handle.close()


def startup_gates(binary, canonical, output):
    results = {}
    for kind in ("sealed", "not-synthetic", "wrong-unicode", "malformed-marker", "missing-marker", "missing-db", "wrong-schema"):
        root = output / kind
        shutil.copytree(canonical, root)
        if kind == "missing-marker":
            (root / m1.MARKER).unlink()
        elif kind == "missing-db":
            (root / "library.sqlite3").unlink()
        elif kind == "malformed-marker":
            (root / m1.MARKER).write_text("{", encoding="utf-8")
        elif kind in ("sealed", "not-synthetic", "wrong-unicode"):
            marker = json.loads((root / m1.MARKER).read_text(encoding="utf-8"))
            key, value = {"sealed": ("sealed", True), "not-synthetic": ("synthetic", False),
                          "wrong-unicode": ("unicode", "0.0.0")}[kind]
            marker[key] = value
            m1.write_json(root / m1.MARKER, marker)
        else:
            with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
                db.execute("DROP INDEX chapter_book")
                db.commit()
        results[kind] = rejected(command(binary, root), root, coordination=kind == "wrong-schema")
        if kind == "wrong-schema":
            lock_probe(root)
    root = output / "bind"
    shutil.copytree(canonical, root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        results["bind"] = rejected(command(binary, root, listener.getsockname()[1]), root)
    root = output / "python-lock"
    shutil.copytree(canonical, root)
    script = ("import sys,json,time;from lifecycle import ServerLifecycle;"
              "h=ServerLifecycle._acquire_library_lock(sys.argv[1]);"
              "print(json.dumps({'port':1}),flush=True);time.sleep(120)")
    with m1.child([sys.executable, "-B", "-X", "utf8", "-c", script, str(root)],
                  output / "python-lock.log"):
        results["python-lock"] = rejected(command(binary, root), root)
    lock_probe(root)
    return results


def shutdown(port, process, root, seed_jobs=False):
    assert request(port, "/api/shutdown", "POST", {})["status"] == 403
    home = request(port, "/")
    cookie = home["headers"]["set-cookie"].split(";", 1)[0]
    settings = request(port, "/api/settings", headers={"Cookie": cookie})
    token = settings["headers"]["x-shutdown-token"]
    headers = {"Cookie": cookie, "X-Shutdown-Token": token}
    assert request(port, "/api/shutdown", "POST", {}, {**headers, "Origin": "https://evil.example"})["status"] == 403
    assert request(port, "/api/shutdown", "POST", {}, {**headers, "X-Shutdown-Token": "wrong"})["status"] == 403
    if seed_jobs:
        with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
            db.executemany("INSERT INTO download_jobs VALUES(?,?,?,?,?,?,?)",
                           [("unloaded-queued", "0", "queued", 0, "{}", 1, 1),
                            ("unloaded-cancelled", "1", "verification_required", 1, "{}", 2, 2)])
            db.commit()
        refused = request(port, "/api/shutdown", "POST", {}, headers)
        assert refused["status"] == 409 and refused["json"]["code"] == "active_downloads" and refused["json"]["active_downloads"] == 2, refused
        assert request(port, "/api/books")["status"] == 200
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
    stalled.sendall((f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                     "Content-Length: 100000\r\nContent-Type: application/json\r\n\r\n{").encode())
    try:
        response = request(port, "/api/shutdown", "POST", {"pause_downloads": True}, headers)
        assert response["status"] == 200 and response["json"] == {"accepted": True}, response
        process.wait(timeout=20)
        assert process.returncode == 0, process.returncode
    finally:
        stalled.close()
    lock_probe(root)
    if seed_jobs:
        with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
            assert dict(db.execute("SELECT id,status FROM download_jobs")) == {
                "unloaded-queued": "paused", "unloaded-cancelled": "cancelled"}
    try:
        request(port, "/api/books")
    except (OSError, http.client.HTTPException):
        pass
    else:
        raise AssertionError("listener admitted request after shutdown")


def blocked_writer(port, root):
    cookie = request(port, "/")["headers"]["set-cookie"].split(";", 1)[0]
    token = request(port, "/api/settings", headers={"Cookie": cookie})["headers"]["x-shutdown-token"]
    results = []
    def write():
        try:
            results.append(request(port, "/api/books/0/progress", "POST", {"index": 0, "position": .625}))
        except Exception as error:
            results.append({"error": repr(error)})
    with contextlib.closing(sqlite3.connect(root / "library.sqlite3", timeout=1)) as db:
        db.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=write)
        thread.start()
        try:
            time.sleep(.1)
            assert thread.is_alive(), "write did not wait for the external SQLite writer"
            assert request(port, "/api/books")["status"] == 200
            try:
                lock_probe(root)
            except OSError:
                pass
            else:
                raise AssertionError("owner lock released while SQLite work remains")
            refused = request(port, "/api/shutdown", "POST", {"pause_downloads": True},
                              {"Cookie": cookie, "X-Shutdown-Token": token})
            assert refused["status"] == 409 and refused["json"]["code"] == "busy_operation", refused
            assert request(port, "/api/books")["status"] == 200
        finally:
            db.rollback()
            thread.join(10)
    assert not thread.is_alive() and results and results[0].get("status") == 200, results
    assert request(port, "/api/books/0")["json"]["position"] == .625
    return {"read_admitted_while_writer_waits": True, "busy_shutdown_retryable": True,
            "waiting_write_committed_after_release": True}


def durable_eviction(binary, canonical, output):
    root = output / "lease-eviction"
    shutil.copytree(canonical, root)
    store = Store(root)
    with store.db() as db:
        db.execute("UPDATE books SET source='hako' WHERE id='0'")
        db.execute("UPDATE chapter_cache SET kind='cache',offline=0 WHERE book_id='0'")
    store.close_inventory()
    store.checkpoint()
    with m1.child(command(binary, root), output / "lease-kill.log") as (port, process):
        assert request(port, "/api/readers", "POST", {"id": "eviction", "book_id": "0", "index": 0, "sequence": 12})["json"] == {"active": True}
        process.kill()
        process.wait(timeout=10)
    def python_evict(expected_removed):
        store = Store(root)
        # Give the reference exclusive control of eviction while testing Rust's persisted lease.
        store.set_chapter_cache_limit(0)
        with store.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM chapter_cache WHERE book_id='0' AND idx IN (0,1) ORDER BY idx")]
        assert all(r["kind"] == "cache" and r["offline"] == 0 for r in rows), rows
        assert store.evict_chapters(rows) == expected_removed
        with store.db() as db:
            states = dict(db.execute("SELECT idx,downloaded FROM chapters WHERE book_id='0' AND idx IN (0,1)"))
        store.set_chapter_cache_limit(100)
        store.close_inventory()
        store.checkpoint()
        return states
    assert python_evict(1) == {0: 1, 1: 0}, "unleased control must evict while durable lease protects"
    with m1.child(command(binary, root), output / "lease-restart.log") as (port, process):
        assert request(port, "/api/readers", "POST", {"id": "eviction", "book_id": None, "sequence": 11})["json"] == {"active": True}
        shutdown(port, process, root)
    assert python_evict(0) == {0: 1, 1: 0}, "stale sequence must preserve effective lease"
    with m1.child(command(binary, root), output / "lease-release.log") as (port, process):
        assert request(port, "/api/readers", "POST", {"id": "eviction", "book_id": None, "sequence": 13})["json"] == {"active": False}
        shutdown(port, process, root)
    assert python_evict(1) == {0: 0, 1: 0}, "new sequence release must permit Python eviction"
    return {"unleased_control_evicted": True, "killed_lease_protected": True,
            "stale_release_protected": True, "new_release_evictable": True}


def stalled_stream(binary, canonical, output):
    root = output / "stalled-stream"
    shutil.copytree(canonical, root)
    image = root / "images" / hashlib.sha256(IMAGE_URL.encode()).hexdigest()
    with image.open("wb") as handle:
        for _ in range(256):
            handle.write(PNG[:65536])
    with m1.child(command(binary, root), output / "stalled-stream.log") as (port, process):
        def open_stream():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", IMAGE_PATH)
            response = connection.getresponse()
            assert response.status == 200 and int(response.getheader("Content-Length")) == 16 * 1024 * 1024
            assert response.read(1024).startswith(PNG[:8])
            return connection
        disconnected = open_stream()
        disconnected.close()
        assert request(port, "/api/books")["status"] == 200
        stalled = open_stream()
        try:
            time.sleep(.1)
            assert request(port, "/api/books")["status"] == 200
            try:
                lock_probe(root)
            except OSError:
                pass
            else:
                raise AssertionError("owner lock released during streaming")
            shutdown(port, process, root)
        finally:
            stalled.close()
    return {"cached_file_bytes": image.stat().st_size, "disconnect_admission": True,
            "unread_stream_and_upload_drained": True, "owner_lock_released_after_drain": True}


def run(args):
    output = m1.fresh_output(args.output)
    before_sources = m1.source_hashes()
    canonical = output / "canonical"
    fixture(canonical)
    sealed_hashes = m1.hashes(canonical)
    python_root = output / "python"
    shutil.copytree(canonical, python_root)
    managed_before = {kind: m1.hashes(canonical / kind) for kind in ("images", "files")}
    report = {"output": str(output), "cases": len(corpus()), "failures": []}
    report["rust_source_sha256"] = rust_source_hashes()
    try:
        with oracle(python_root) as (port, _):
            report["expected"] = capture(port, python_root)
            report["expected_encoded_json"] = encoded_json(port)
            report["expected_parser_boundaries"] = parser_boundaries(port)
            automatic_note(port)
            report["python_offline_miss"] = offline_miss(port, python_root)
            report["python_reader_capacity"] = reader_capacity(port)
            conditional(port, python_root)
            instance = request(port, "/api/instance")["json"]
            assert instance == {"app": "ke-truyen-local", "data_directory": str(python_root.resolve()), "pid": os.getpid()}
            expected_state = state(python_root)
        assert {kind: m1.hashes(python_root / kind) for kind in managed_before} == managed_before
        cover_python = output / "cover-python"
        cover_fixture(canonical, cover_python)
        with oracle(cover_python) as (port, _):
            report["python_cover_misses"] = cover_misses(port, cover_python)
        if not args.binary:
            report["failures"].append("missing Rust --writable implementation gate")
        else:
            binary = Path(args.binary).resolve()
            report["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
            report["startup"] = startup_gates(binary, canonical, output)
            rust_root = output / "rust"
            shutil.copytree(canonical, rust_root)
            with m1.child(command(binary, rust_root), output / "rust.log") as (port, process):
                actual = capture(port, rust_root)
                report["actual_encoded_json"] = encoded_json(port)
                report["actual_parser_boundaries"] = parser_boundaries(port)
                report["failures"] += compare(report["expected_parser_boundaries"], report["actual_parser_boundaries"])
                report["failures"] += compare(report["expected_encoded_json"], report["actual_encoded_json"])
                automatic_note(port)
                assert offline_miss(port, rust_root) == report["python_offline_miss"]
                assert reader_capacity(port) == report["python_reader_capacity"]
                report["actual"] = actual
                report["failures"] += compare(report["expected"], actual)
                conditional(port, rust_root)
                instance = request(port, "/api/instance")["json"]
                assert instance == {"app": "ke-truyen-local", "data_directory": str(rust_root.resolve()), "pid": process.pid}
                try:
                    lock_probe(rust_root)
                except OSError:
                    pass
                else:
                    raise AssertionError("Python acquired lock while Rust serves")
                actual_state = state(rust_root)
                for table in expected_state:
                    if expected_state[table] != actual_state[table]:
                        report["failures"].append({"table": table, "expected": expected_state[table], "actual": actual_state[table]})
                report["second_rust_owner"] = rejected(command(binary, rust_root), rust_root, coordination=True)
                report["blocked_writer"] = blocked_writer(port, rust_root)
                shutdown(port, process, rust_root, seed_jobs=True)
            assert {kind: m1.hashes(rust_root / kind) for kind in managed_before} == managed_before
            report["managed_files_unchanged"] = True
            # Reopen only the disposable runtime copy with unchanged Python.
            store = Store(rust_root)
            assert store.settings()["appearance"] == {"theme": "dark", "size": 25}
            assert store.get_book("0")["name"] == "Đổi tên"
            assert any(n["id"] == "orphan" for n in store.notes())
            store.close_inventory()
            store.checkpoint()
            # Separate kill/restart from orderly cleanup; persisted sequence must win.
            with m1.child(command(binary, rust_root), output / "kill.log") as (port, process):
                assert request(port, "/api/readers", "POST", {"id": "durable", "book_id": "0", "index": 0, "sequence": 12})["json"] == {"active": True}
                process.kill()
                process.wait(timeout=10)
            with m1.child(command(binary, rust_root), output / "restart.log") as (port, process):
                assert request(port, "/api/readers", "POST", {"id": "durable", "book_id": None, "sequence": 11})["json"] == {"active": True}
                shutdown(port, process, rust_root)
            report["durable_eviction"] = durable_eviction(binary, canonical, output)
            report["stalled_stream"] = stalled_stream(binary, canonical, output)
            cover_rust = output / "cover-rust"
            cover_fixture(canonical, cover_rust)
            with m1.child(command(binary, cover_rust), output / "cover-rust.log") as (port, process):
                assert cover_misses(port, cover_rust) == report["python_cover_misses"]
                shutdown(port, process, cover_rust)
            assert hashlib.sha256(binary.read_bytes()).hexdigest() == report["binary_sha256"]
    except Exception as error:
        import traceback
        report["failures"].append({"exception": repr(error), "traceback": traceback.format_exc()})
    finally:
        report["canonical_unchanged"] = m1.hashes(canonical) == sealed_hashes
        report["reference_sources_unchanged"] = m1.source_hashes() == before_sources
        report["rust_sources_unchanged"] = rust_source_hashes() == report["rust_source_sha256"]
        for safeguard in ("canonical_unchanged", "reference_sources_unchanged", "rust_sources_unchanged"):
            if not report[safeguard]:
                report["failures"].append({"safeguard": safeguard, "error": "changed during verification"})
        m1.write_json(output / "report.json", report)
        print(json.dumps({"report": str(output / "report.json"), "cases": report["cases"], "failures": len(report["failures"])}))
    return bool(report["failures"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary")
    parser.add_argument("--output")
    sys.exit(run(parser.parse_args()))
