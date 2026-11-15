"""Disposable M1 oracle, differential gate and same-machine benchmark (stdlib only).

Run without --binary to capture the oracle and deliberately fail the missing-Rust gate.
Never accepts an input database. --output must not exist and must be inside OS temp.
Rust startup protocol: one flushed stdout JSON line containing {"port": bound_port}.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import platform
import queue
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from library import Store
from server import make_server, public_content

MARKER = "m1-fixture.json"
SECURITY = ("x-content-type-options", "referrer-policy", "cross-origin-resource-policy",
            "content-security-policy", "cache-control", "content-type")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def source_hashes():
    paths = list(ROOT.glob("*.py")) + list((ROOT / "web").glob("*"))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def fresh_output(value=None):
    if value is None:
        return Path(tempfile.mkdtemp(prefix="reader-m1-"))
    path = Path(value).absolute()
    temp = Path(tempfile.gettempdir()).resolve()
    if not path.parent.resolve().is_relative_to(temp) or path.exists():
        raise ValueError("--output must be a nonexistent child of the OS temporary directory")
    path.mkdir(parents=True)
    return path


def fixture(root, profile="small", count=50000, sealed=True):
    """Only creates a new child in an already fresh, harness-owned temp root."""
    if not root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("fixture root must be temporary")
    root.mkdir(exist_ok=False)
    store = Store(root)
    with store.db() as db:
        if profile == "scale":
            db.executemany("INSERT INTO books(id,name,author,source,kind,groups_json,added_at,updated_at,read_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", ((f"{i:05}", f"TRUYỆN {i:05}", "Tác giả",
                           "hako" if i % 2 else "file", "text", json.dumps([f"group-{i % 100}"]),
                           i, i, i) for i in range(count)))
            for i in range(3):
                db.execute("INSERT INTO chapters(book_id,idx,text,html,downloaded) VALUES(?,0,?,?,1)",
                           (f"{i:05}", "Cached text.", "<p>Cached text.</p>"))
        elif profile in ("small", "sort-values", "numbers"):
            names = ("Ánh Trăng", "áNH trăng", "Truyện 100%_!", "Straße", "Last", "Identical",
                     "Identical", "İıΣσς ﬃ ẞ", "Archived", "Trashed")
            covers = ("https://cdn.example/a.jpg", "http://127.0.0.1/secret", "javascript:bad()",
                      "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,iVBORw0KGgp4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4")
            for i, name in enumerate(names):
                identity = "id /?#雪" if i == 7 else str(i)
                raw = {"warnings": [{"html": '<p onclick="bad()">Safe<script>bad()</script><img src="https://cdn.example/x.png"><img src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxyZWN0IHdpZHRoPSIxMCIgaGVpZ2h0PSIxMCIvPjwvc3ZnPg=="></p>',
                                     "cover": "https://cdn.example/nested.png"},
                                    {"html": '<img src="data:image/svg+xml;base64,PHN2Zz4="><img src="data:image/svg+xml;base64,@@@">'}],
                       "ocr": {"cover": "javascript:bad()"}}
                db.execute("INSERT INTO books(id,name,author,source,kind,groups_json,added_at,updated_at,read_at,"
                           "progress,chapter_count,cover,raw,archived,trashed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (identity, name, "TÁC GIẢ", "hako" if i < 3 else "file",
                            "comic" if i == 2 else "text", json.dumps(["one", "two"] if i == 0 else ["two"]),
                            10, 10, 10, .5, 20, covers[i % len(covers)], json.dumps(raw), i == 8,
                            4102444800 if i == 9 else 0))
                if profile == "sort-values":
                    db.execute("UPDATE books SET added_at=?,updated_at=?,read_at=?,progress=?,chapter_count=? WHERE id=?",
                               (i % 3, (i + 1) % 4, (i + 2) % 5, (i % 3) / 2, i % 4, identity))
                elif profile == "numbers":
                    value = (1e-7, 1e20, 1e15, .00001, 1.2345678901234567, 1000000000000000100.0,
                             -0.0, 2.2250738585072014e-308, 1.7976931348623157e308, 1e-6)[i]
                    db.execute("UPDATE books SET progress=?,position=?,read_at=?,raw=? WHERE id=?",
                               (value, value, value, json.dumps({"ocr": {"confidence": value}}), identity))
        elif profile == "trust-output":
            urls = ["http://example.internal/x", "http://example.lan/x", "http://example.home/x",
                    "http://example.test/x", "http://example.invalid/x", "http://example.org:80/x",
                    "http://example.org:443/x", "https://example.org:443/x", "https://example.org:80/x",
                    "http://[::ffff:127.0.0.1]/x", "http://[fec0::1]/x", "https://example.org/x",
                    "https://[example.com]/a", "https://[8.8.8.8]/a", "https://example.com:+443/a"]
            for i, url in enumerate(urls):
                markup = '<p title="a > b">One <b>two</b></p><img alt="a > b" src="' + url + '"><img alt="a < b" src="' + url + '"><p>&copy test &notit; &#x80;</p>'
                warnings = [{"html": markup, "cover": url}]
                if i == 0:
                    svgs = ('<s:svg xmlns:s="http://www.w3.org/2000/svg"><s:rect width="1" height="2"/></s:svg>',
                            '<svg xmlns="http://www.w3.org/2000/svg"><text>A&amp;B</text></svg>')
                    warnings.append({"html": ''.join('<img src="data:image/svg+xml;base64,' +
                                    base64.b64encode(svg.encode()).decode() + '">' for svg in svgs)})
                raw = json.dumps({"warnings": warnings})
                db.execute("INSERT INTO books(id,name,added_at,updated_at,raw) VALUES(?,?,1,1,?)",
                           (f"trust-{i}", f"Trust {i:02}", raw))
        elif profile in ("rawtext-entity", "visible-entity"):
            entity = "&#" + "9" * 4301 + ";"
            markup = ("<script>" + entity + "</script><style>" + entity + "</style>"
                      if profile == "rawtext-entity" else "<p>" + entity + "</p>")
            db.execute("INSERT INTO books(id,name,added_at,updated_at,raw) VALUES('entity','Entity',1,1,?)",
                       (json.dumps({"warnings": [{"html": markup}]}),))
        elif profile.startswith("malformed-"):
            raw = {"malformed-list": "[]", "malformed-null": "null", "malformed-json": "{",
                   "malformed-colon": '{"x" 1}', "malformed-array": '[1,]', "malformed-eof": '{\n"x":',
                   "malformed-zero": "0", "malformed-false": "false", "malformed-warning-list": '["warnings"]',
                   "malformed-string": '"hello"', "malformed-unicode": '{"x":"\\u12x4"}',
                   "malformed-literal": '{"x":tru}', "malformed-eof-true": '{"x":tru',
                   "malformed-eof-false": '{"x":fals', "malformed-eof-null": '{"x":nul'}[profile]
            db.execute("INSERT INTO books(id,name,added_at,updated_at,raw,groups_json) VALUES('raw','Raw',1,1,?,?)",
                       (raw, '{"a":true,"b":null}'))
        elif profile != "empty":
            raise ValueError(profile)
        if not sealed:
            # WAL stress targets snapshot visibility; HTML sanitizer coverage has its own captures.
            db.execute("UPDATE books SET raw='{}'")
        schema = [list(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        books = db.execute("SELECT count(*) FROM books").fetchone()[0]
        bodies = db.execute("SELECT count(*) FROM chapters WHERE text<>'' OR html<>''").fetchone()[0]
    store.close_inventory()
    store.checkpoint()
    if sealed:
        with contextlib.closing(sqlite3.connect(store.path)) as finalized:
            finalized.create_function("shelf_fold", 1, str.casefold, deterministic=True)
            assert finalized.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assert not (root / "library.sqlite3-wal").exists()
    marker = {"format": "reader-local-rust-m1", "version": 1, "database": "library.sqlite3",
              "synthetic": True, "sealed": sealed,
              "python": platform.python_version(), "unicode": unicodedata.unidata_version,
              "sqlite": sqlite3.sqlite_version, "books": books, "cached_bodies": bodies,
              "journal_mode": "delete" if sealed else "wal",
              "sha256": hashlib.sha256(store.path.read_bytes()).hexdigest(), "schema": schema}
    write_json(root / MARKER, marker)
    return store.path


@contextlib.contextmanager
def oracle(canonical, output, label):
    data = output / label
    data.mkdir()
    shutil.copy2(canonical, data / "library.sqlite3")
    server = make_server(data, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, server.app.store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(port, path, method="GET", headers=None, connection=None, timeout=10):
    own = connection is None
    conn = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        fields = {k.lower(): v for k, v in response.getheaders()}
        content = json.loads(body) if body and fields.get("content-type", "").startswith("application/json") else None
        return {"status": response.status, "headers": fields, "body_hex": body.hex(), "json": content}
    finally:
        if own:
            conn.close()


def cases():
    result = [("GET", "/api/books", {})]
    for sort in ("recent", "name", "added", "updated", "progress", "chapters", "unknown"):
        for reverse in ("0", "1"):
            for offset in (0, 2, 6, 999999):
                result.append(("GET", "/api/books?" + urlencode(dict(sort=sort, reverse=reverse, limit=2, offset=offset)), {}))
    queries = ["q=", "q=STRASSE", "q=%C3%81NH+TR%C4%82NG", "q=%FF", "q=100%25_%21", "q=%25%27+OR+1%3D1+--",
               "q=%EF%AC%83", "q=%C4%B0", "q=%CF%83", "q=++", "q=Last&q=Stra%C3%9Fe", "q=&q=Last",
               "source=hako", "group=one", "group=two", "kind=all", "kind=text", "kind=comic", "kind=file",
               "archived=1", "trashed=1", "source=hako&group=two&kind=text&sort=name&reverse=1",
               "limit=", "offset=", "limit=1_0", "limit=%D9%A1%D9%A0", "limit=%2B10", "limit=+10",
               "limit=invalid", "offset=invalid", "kind=invalid", "limit=-1&offset=-10", "limit=0", "limit=1.0",
               "limit=" + "9" * 200, "offset=" + "9" * 200, "limit=2&limit=3", "limit=&limit=2",
               "limit=" + "9" * 4301, "limit=%E2%80%8310%E2%80%83", "limit=%1C10", "limit=%00",
               "limit=%27%22%0A%09%01", "limit=" + "x" * 220, "offset=bad&limit=wrong&kind=invalid",
               "limit=wrong&kind=invalid", "q=%ED%A0%80", "q=+&q=STRASSE", "q=%00", "q=" + "x" * 800]
    queries += [urlencode({"limit": value}) for value in ("a'b", 'a"b', 'a\'"b', "a\nb", "a\u200bb", "a\u00a0b",
                "a😀b", "\u00a010\u00a0", "1١２", "\x1c10\x1f", "\u200b10")]
    queries += [urlencode({"q": value}) for value in ("\x1c\x1d\x1e\x1f", "\x1cSTRASSE\x1f", "\u00a0STRASSE\u00a0", "\u200bSTRASSE")]
    result += [("GET", "/api/books?" + q, {}) for q in queries]
    result += [("HEAD", path, {}) for path in ("/api/books", "/api/books?limit=2", "/api/books?kind=invalid", "/", "/app.js")]
    result += [(method, "/api/books", {}) for method in ("OPTIONS", "POST", "DELETE", "PUT", "PATCH")]
    result += [("GET", path, {}) for path in ("/", "/index.html", "/app.js", "/reader.js", "/captcha.js", "/style.css",
                 "/missing", "/api/books/", "/../server.py", "/%2e%2e/server.py", "/..%5cserver.py", "/C:%5cWindows%5cwin.ini")]
    result += [("GET", "/api/books", {"Host": "evil.example"}),
               ("GET", "/api/books", {"Origin": "https://evil.example"}),
               ("GET", "/api/books", {"Origin": "null"}),
               ("GET", "/api/books", {"Host": "LOCALHOST:{port}"}),
               ("GET", "/api/books", {"Host": "localhost:{port}", "Origin": "http://localhost:{port}"}),
               ("POST", "/api/books", {"Sec-Fetch-Site": "cross-site"}),
               ("PUT", "/api/books", {"Host": "evil.example"}),
               ("OPTIONS", "/api/books", {"Host": "evil.example"})]
    return result


def capture(port):
    return [{"method": method, "path": path, "request_headers": headers,
             "response": request(port, path, method, {k: v.replace("{port}", str(port)) for k, v in headers.items()})}
            for method, path, headers in cases()]


def compare(expected, actual):
    failures = []
    for before, after in zip(expected, actual, strict=True):
        a, b = before["response"], after["response"]
        label = before["method"] + " " + before["path"]
        if a["status"] != b["status"]:
            failures.append([label, "status", a["status"], b["status"]])
        if a["json"] != b["json"]:
            failures.append([label, "JSON", a["json"], b["json"]])
        if a["json"] is None and a["body_hex"] != b["body_hex"]:
            failures.append([label, "body bytes"])
        for field in SECURITY:
            if a["headers"].get(field) != b["headers"].get(field):
                failures.append([label, field, a["headers"].get(field), b["headers"].get(field)])
        if before["method"] == "HEAD":
            if b["body_hex"] or a["headers"].get("content-length") != b["headers"].get("content-length"):
                failures.append([label, "HEAD body/GET-equivalent length"])
        if "set-cookie" in a["headers"]:
            cookie = b["headers"].get("set-cookie", "")
            if not cookie.startswith("ke_truyen_session=") or cookie.split(";", 1)[-1] != a["headers"]["set-cookie"].split(";", 1)[-1]:
                failures.append([label, "cookie attributes"])
    return failures


def memory(pid):
    if os.name != "nt":
        return {"rss_bytes": None, "peak_rss_bytes": None, "note": "Windows counters unavailable"}
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                    ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                     "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"rss_bytes": counters.WorkingSetSize, "peak_rss_bytes": counters.PeakWorkingSetSize,
                "private_bytes": counters.PagefileUsage, "peak_private_bytes": counters.PeakPagefileUsage}
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def child(command, log):
    with log.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            try:
                line = lines.get(timeout=30)
            except queue.Empty:
                raise AssertionError("server did not print startup JSON within 30s")
            if not line:
                raise AssertionError(f"server exited before startup JSON; see {log}")
            hello = json.loads(line)
            assert isinstance(hello["port"], int) and 0 < hello["port"] < 65536, hello
            yield hello["port"], process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


def rust_command(binary, db):
    return [str(binary), "--fixture", str(db), "--web-root", str(ROOT / "web"), "--port", "0"]


def summary(samples):
    ordered = sorted(samples)
    return {"samples": samples, "n": len(samples), "p50_ms": statistics.median(samples),
            "p95_ms": ordered[math.ceil(.95 * len(samples)) - 1], "max_ms": max(samples)}


def timed_http(port, samples=30):
    request(port, "/api/books")
    values = []
    for _ in range(samples):
        start = time.perf_counter()
        result = request(port, "/api/books")
        values.append((time.perf_counter() - start) * 1000)
        assert result["status"] == 200 and len(result["json"]["items"]) == 60
        assert result["json"]["total"] == 50000
    return summary(values) | {"json_bytes": len(bytes.fromhex(result["body_hex"]))}


def python_query(store):
    statements, folds, values = [], [], []
    original = store.db
    owner_thread = threading.get_ident()
    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with original(*args, **kwargs) as db:
            if threading.get_ident() == owner_thread:
                db.set_trace_callback(statements.append)
                db.create_function("shelf_fold", 1, lambda text: folds.append(text) or text.casefold(), deterministic=True)
            yield db
    store.db = traced
    try:
        for _ in range(31):
            start = time.perf_counter()
            body = json.dumps(public_content(store.list_books()), ensure_ascii=False, allow_nan=False).encode()
            values.append((time.perf_counter() - start) * 1000)
    finally:
        store.db = original
    assert not folds, ("unexpected fold calls", folds[:3])
    chapter_sql = [s for s in statements if "FROM chapters" in s]
    assert not chapter_sql, ("unexpected chapter queries", chapter_sql[:3])
    plans = []
    with store.db() as db:
        for sql in dict.fromkeys(s for s in statements if s.startswith("SELECT * FROM books")):
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
            assert "books_shelf_recent" in str(plan) and "TEMP B-TREE" not in str(plan), plan
            plans.append(plan)
    return summary(values[1:]) | {"json_bytes": len(body), "fold_calls": len(folds), "query_plans": plans}


def benchmark(binary, output):
    db = fixture(output / "scale", "scale")
    before = hashes(db.parent)
    result = {"books": 50000, "cached_bodies": 3, "page_size": 60,
              "db_bytes": db.stat().st_size, "wal_bytes": 0,
              "limitations": ["Rust direct query timings require its dedicated release shelf_scale test",
                              "process-cold runs retain OS filesystem cache; full Python App has additional services",
                              "RSS is descriptive, not an equivalent-service startup/memory speedup claim"]}
    with oracle(db, output, "scale-oracle") as (port, store):
        expected_page = request(port, "/api/books")["json"]
        result["python_query_plus_public_json"] = python_query(store)
        result["python_http"] = timed_http(port)
        result["python_memory"] = memory(os.getpid()) | {"scope": "verifier process plus full Python App; includes fixture/capture allocations"}
    with child(rust_command(binary, db), output / "rust-scale.log") as (port, process):
        assert request(port, "/api/books")["json"] == expected_page, "scale shelf differs from reference"
        result["rust_http"] = timed_http(port)
        result["rust_memory"] = memory(process.pid)
    for engine in ("python", "rust"):
        cold, startup = [], []
        for i in range(3):
            if engine == "python":
                copy = output / f"cold-python-{i}"
                copy.mkdir()
                shutil.copy2(db, copy / "library.sqlite3")
                shutil.copy2(db.parent / MARKER, copy / MARKER)
                command = [sys.executable, str(Path(__file__).resolve()), "--oracle-server", str(copy)]
            else:
                command = rust_command(binary, db)
            start = time.perf_counter()
            with child(command, output / f"{engine}-cold-{i}.log") as (port, process):
                startup.append((time.perf_counter() - start) * 1000)
                start = time.perf_counter()
                response = request(port, "/api/books")
                cold.append((time.perf_counter() - start) * 1000)
                assert response["status"] == 200 and response["json"]["total"] == 50000
        result[engine + "_process_cold_first_http"] = {"samples_ms": cold, "median_ms": statistics.median(cold), "max_ms": max(cold)}
        result[engine + "_startup_descriptive_only"] = startup
    query_cold = []
    for i in range(3):
        copy = output / f"cold-python-query-{i}"
        copy.mkdir()
        shutil.copy2(db, copy / "library.sqlite3")
        shutil.copy2(db.parent / MARKER, copy / MARKER)
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--oracle-server", str(copy), "--oracle-query"],
                                   capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        sample = json.loads(completed.stdout)
        assert sample["json_bytes"] == result["python_query_plus_public_json"]["json_bytes"], sample
        query_cold.append(sample["elapsed_ms"])
    result["python_process_cold_query_public_json"] = {"samples_ms": query_cold,
            "median_ms": statistics.median(query_cold), "max_ms": max(query_cold)}
    assert before == hashes(db.parent), "sealed scale fixture changed"
    result["warm_latency_ratios"] = {metric: result["rust_http"][metric] / result["python_http"][metric]
                                    for metric in ("p50_ms", "p95_ms")}
    return result


def wal_concurrency(binary, output):
    db = fixture(output / "wal", "small", sealed=False)
    keeper = sqlite3.connect(db)
    keeper.create_function("shelf_fold", 1, str.casefold, deterministic=True)
    keeper.execute("PRAGMA journal_mode=WAL")
    stop, errors, writes = threading.Event(), [], []
    def writer():
        connection = sqlite3.connect(db, timeout=2)
        connection.create_function("shelf_fold", 1, str.casefold, deterministic=True)
        try:
            while not stop.is_set():
                for archived in (0, 1):
                    start = time.perf_counter()
                    with connection:
                        connection.execute("UPDATE books SET archived=? WHERE id='0'", (archived,))
                    writes.append((time.perf_counter() - start) * 1000)
                    stop.wait(.005)
        except Exception as error:
            errors.append(repr(error))
        finally:
            connection.close()
    thread = threading.Thread(target=writer, daemon=True)
    try:
        with child(rust_command(binary, db), output / "wal-rust.log") as (port, _):
            thread.start()
            totals = set()
            for _ in range(100):
                response = request(port, "/api/books?limit=200")
                assert response["status"] == 200, response
                page = response["json"]
                assert page["total"] == page["shelf_total"] == len(page["items"]), page
                totals.add(page["total"])
            assert totals == {7, 8}, ("reader failed to observe WAL commits", totals)
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(5)
        keeper.close()
    assert not errors and not thread.is_alive(), errors
    return {"observed_totals": sorted(totals), "writer_commits": len(writes),
            "writer_latency": summary(writes), "immutable": False}


def invalid_fixtures(binary, output):
    results = []
    for mode in ("missing", "unmarked", "malformed-marker", "unsupported-schema", "wrong-index"):
        root = output / ("invalid-" + mode)
        db = root / "library.sqlite3"
        if mode == "wrong-index":
            fixture(root, "empty")
            with contextlib.closing(sqlite3.connect(db)) as connection:
                connection.create_function("shelf_fold", 1, str.casefold, deterministic=True)
                connection.executescript("DROP INDEX books_shelf_recent; CREATE INDEX books_shelf_recent ON books(id)")
            marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
            marker["sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            write_json(root / MARKER, marker)
        else:
            root.mkdir()
        if mode not in ("missing", "wrong-index"):
            sqlite3.connect(db).close()
        if mode == "malformed-marker":
            (root / MARKER).write_text("{", encoding="utf-8")
        if mode == "unsupported-schema":
            write_json(root / MARKER, {"format": "reader-local-rust-m1", "version": 1,
                       "database": "library.sqlite3", "synthetic": True, "sealed": True})
        before = hashes(root)
        process = subprocess.Popen(rust_command(binary, db), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise AssertionError("invalid fixture started a server: " + mode)
        assert process.returncode != 0, (mode, stdout, stderr)
        assert before == hashes(root), "invalid fixture created/changed files: " + mode
        results.append({"mode": mode, "exit": process.returncode})
    return results


def stalled_body(port, pid):
    before = memory(pid)
    advertised = 16 * 1024 * 1024
    with socket.create_connection(("127.0.0.1", port), timeout=3) as stalled:
        stalled.settimeout(3)
        stalled.sendall((f"POST /api/books HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        f"Content-Length: {advertised}\r\nConnection: close\r\n\r\nx").encode())
        response = stalled.recv(4096)
        assert b"404" in response.split(b"\r\n", 1)[0], response
        during = memory(pid)
        if os.name == "nt":
            for key in ("rss_bytes", "private_bytes"):
                assert during[key] - before[key] < 8 * 1024 * 1024, ("oversized stalled-body allocation", key, before, during)
        assert request(port, "/api/books", timeout=3)["status"] == 200, "stalled body blocked another shelf reader"
    return {"advertised_bytes": advertised, "sent_body_bytes": 1, "before": before, "during": during}


def run(args):
    output = fresh_output(args.output)
    logging.basicConfig(filename=output / "python-oracle.log", level=logging.WARNING, force=True)
    print(str(output), flush=True)
    sources = source_hashes()
    report = {"environment": {"platform": platform.platform(), "python": sys.version,
                               "unicode": unicodedata.unidata_version, "sqlite": sqlite3.sqlite_version,
                               "cpu_count": os.cpu_count()}, "failures": [], "profiles": {}}
    try:
        # Identity folds are implied; the Rust test must enumerate every scalar, including identities.
        folds = {str(code): chr(code).casefold() for code in range(0x110000)
                 if not 0xD800 <= code <= 0xDFFF and chr(code).casefold() != chr(code)}
        write_json(output / "unicode-casefold.json", {"unicode": unicodedata.unidata_version,
                   "scalar_count": 0x110000 - 0x800, "nonidentity": folds})
        for profile in ("empty", "small", "sort-values", "numbers", "trust-output", "malformed-list", "malformed-null",
                        "malformed-json", "malformed-colon", "malformed-array", "malformed-eof",
                        "malformed-zero", "malformed-false", "malformed-warning-list", "malformed-string",
                        "malformed-unicode", "malformed-literal", "malformed-eof-true", "malformed-eof-false",
                        "malformed-eof-null", "rawtext-entity", "visible-entity"):
            db = fixture(output / profile, profile)
            before = hashes(db.parent)
            with oracle(db, output, profile + "-oracle") as (port, _):
                expected = capture(port)
            write_json(output / (profile + "-reference.json"), expected)
            report["profiles"][profile] = {"cases": len(expected), "sealed_hashes": before}
            if args.binary:
                with child(rust_command(Path(args.binary).resolve(), db), output / (profile + "-rust.log")) as (port, process):
                    actual = capture(port)
                    if expected[0]["response"]["status"] == 200:
                        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                        try:
                            assert request(port, "/api/books", connection=conn)["status"] == 200
                            assert request(port, "/api/books", "OPTIONS", connection=conn)["status"] == 403
                        finally:
                            conn.close()
                    if profile == "empty":
                        report["stalled_body"] = stalled_body(port, process.pid)
                write_json(output / (profile + "-rust.json"), actual)
                report["failures"] += compare(expected, actual)
            assert before == hashes(db.parent), "sealed canonical fixture changed"
            print(json.dumps({"profile": profile, "cases": len(expected), "sealed_unchanged": True}), flush=True)
        if not args.binary or not Path(args.binary).is_file():
            report["failures"].append(["missing Rust binary: implementation gate fails as intended"])
        else:
            binary = Path(args.binary).resolve()
            report["invalid_fixtures"] = invalid_fixtures(binary, output)
            report["wal_concurrency"] = wal_concurrency(binary, output)
            if args.benchmark:
                report["benchmark"] = benchmark(binary, output)
    except Exception as error:
        report["failures"].append([type(error).__name__, str(error)])
        raise
    finally:
        report["source_hashes"] = sources
        if source_hashes() != sources:
            report["failures"].append(["production Python/frontend hashes changed during verification"])
        write_json(output / "report.json", report)
    print(json.dumps({"report": str(output / "report.json"), "failures": len(report["failures"])}), flush=True)
    return bool(report["failures"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--binary")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--oracle-server", help=argparse.SUPPRESS)
    parser.add_argument("--oracle-query", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.oracle_server:
        path = Path(args.oracle_server).resolve()
        if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not path.name.startswith("cold-python-"):
            parser.error("oracle server only accepts harness-created temporary cold copies")
        marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
        if (not path.parent.name.startswith("reader-m1-") or marker.get("format") != "reader-local-rust-m1"
                or marker.get("synthetic") is not True or marker.get("sealed") is not True
                or marker.get("sha256") != hashlib.sha256((path / "library.sqlite3").read_bytes()).hexdigest()):
            parser.error("oracle server requires an intact harness-created synthetic copy")
        server = make_server(path, port=0)
        if args.oracle_query:
            try:
                start = time.perf_counter()
                body = json.dumps(public_content(server.app.store.list_books()), ensure_ascii=False, allow_nan=False).encode()
                elapsed = (time.perf_counter() - start) * 1000
                print(json.dumps({"elapsed_ms": elapsed, "json_bytes": len(body)}), flush=True)
            finally:
                server.server_close()
            return 0
        print(json.dumps({"port": server.server_port}), flush=True)
        server.serve_forever()
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
