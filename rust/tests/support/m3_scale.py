"""Frozen-release benchmark for M3 on matched, freshly generated OS-temp fixtures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import tempfile
import threading
import time

from m2_capture import m1, rust_source_hashes
from m3_support import db, finish, fixture, query, server, verify_db, wait_for


PROFILES = {"100k-512": (100_000, 512), "1m-512": (1_000_000, 512), "1m-10k": (1_000_000, 10_000)}
TEXT = "Benchmark needle " + "x" * (4096 - len("Benchmark needle "))
PURGES = 40


def corpus(root, profile):
    chapter_total, body_total = PROFILES[profile]
    store = fixture(root)
    each, extra = divmod(chapter_total - 50_000, 499)
    counts = [50_000] + [each + int(i < extra) for i in range(499)]
    ids = [f"b{i:03}" for i in range(500)]
    large_book_bodies = min(512, body_total // 4)
    online_bodies = body_total - large_book_bodies
    with db(root) as connection:
        connection.executemany(
            "INSERT INTO books(id,name,source,chapter_count,added_at,updated_at,trashed_at) VALUES(?,?,?,?,?,?,?)",
            ((book, "Truyện " + book, "file" if i == 0 else "full", counts[i], 1, 1,
              time.time() if i >= len(ids) - PURGES else 0) for i, book in enumerate(ids)),
        )
        for index, (book, count) in enumerate(zip(ids, counts, strict=True)):
            body_count = large_book_bodies if index == 0 else min(count, online_bodies)
            if index:
                online_bodies -= body_count
            connection.executemany(
                "INSERT INTO chapters(book_id,idx,title,url,text,downloaded) VALUES(?,?,?,?,?,?)",
                ((book, c, f"Chương {c}", f"https://example.org/{book}/{c}",
                  TEXT if c < body_count else "", int(c < body_count)) for c in range(count)),
            )
        assert online_bodies == 0
        connection.execute("UPDATE chapter_cache SET kind='cache',last_used=id WHERE book_id!='b000'")
        connection.executemany("INSERT INTO image_cache(name,size,last_used) VALUES(?,0,?)",
                               ((f"{i:064x}", i) for i in range(100_000)))
        # Modern triggers recorded all relevant owners. Prepare a steady-state fixture before timing.
        connection.execute("UPDATE asset_index_state SET complete=1")
        connection.execute("UPDATE chapter_search_state SET complete=1")
        connection.execute("UPDATE chapter_cache_state SET complete=1,max_chapters=100000")
    finish(store)
    rows = query(root, "SELECT count(*),sum(downloaded),sum(length(CAST(text AS BLOB))) FROM chapters")
    assert rows == [(chapter_total, body_total, body_total * 4096)]
    assert query(root, "SELECT count(*) FROM chapter_search_docs") == [(body_total,)]
    assert query(root, "SELECT count(*) FROM image_cache") == [(100_000,)]
    assert not list((root / "images").glob("*"))
    return {"books": len(ids), "chapters": chapter_total, "cached_bodies": body_total, "text_bytes_per_body": 4096,
            "total_body_payload_bytes": body_total * 4096, "image_metadata": 100_000, "image_files": 0,
            "chapter_distribution": {"large_book": 50_000, "other_books": 499, "base_chapters": each, "extra": extra},
            "high_fanout_cached_bodies": large_book_bodies,
            "purge_chapter_counts": dict(zip(ids[-PURGES:], counts[-PURGES:], strict=True)),
            "database_bytes": store.path.stat().st_size}


def http(port, path, method="GET", body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        value = json.loads(raw)
        assert response.status == 200, (method, path, response.status, value)
        return value, len(raw)
    finally:
        connection.close()


def clock(action):
    started = time.perf_counter()
    value = action()
    return (time.perf_counter() - started) * 1000, value


def io(pid):
    if os.name != "nt":
        return {}

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops",
                                                        "read_bytes", "write_bytes", "other_bytes")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessIoCounters.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters))
    owner = kernel.OpenProcess(0x1000, False, pid)
    if not owner:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = Counters()
        if not kernel.GetProcessIoCounters(owner, ctypes.byref(counters)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {name: getattr(counters, name) for name, _ in counters._fields_}
    finally:
        kernel.CloseHandle(owner)


def sample_http(port, samples):
    routes = {"shelf": "/api/books", "toc_50000_metadata": "/api/books/b000/chapters",
              "cached_chapter": "/api/books/b000/chapters/0", "scoped_search": "/api/books/b001/search?q=needle"}
    measured = {}
    for name, path in routes.items():
        value, length = http(port, path)
        if name == "toc_50000_metadata":
            assert len(value) == 50_000
            assert not any({"text", "html", "original_text", "original_html"} & row.keys() for row in value)
        if name == "cached_chapter":
            assert value["text"] == TEXT
        if name == "scoped_search":
            assert len(value["results"]) == 100 and not value["indexing"]
        times = [clock(lambda: http(port, path))[0] for _ in range(samples)]
        measured[name] = {**m1.summary(times), "response_bytes": length}
    return measured


def maintenance(port, root, dataset, samples):
    chapter_cache = "/api/cache/chapters"
    image_cache = "/api/cache/images"
    eviction, cleanup, purge, reader, writer = [], [], [], [], []
    stop = threading.Event()

    def foreground(path, mode, data, times):
        while not stop.is_set():
            elapsed, _ = clock(lambda: http(port, path, mode, data))
            times.append(elapsed)
            stop.wait(.015)

    remaining = dataset["cached_bodies"] - dataset["high_fanout_cached_bodies"]
    deleted = []
    bodies = "SELECT downloaded FROM chapters WHERE book_id='b001' AND idx<8 ORDER BY idx"
    images = "SELECT count(*) FROM image_cache WHERE id<=32"

    def evict():
        http(port, chapter_cache, "POST", {"max_chapters": remaining - 8})
        wait_for(lambda: query(root, bodies), lambda rows: rows == [(0,)] * 8)

    def clean():
        http(port, image_cache, "POST", {"max_bytes": 0})
        wait_for(lambda: query(root, images), lambda rows: rows == [(0,)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = pool.submit(foreground, "/api/books/b000/chapters/0", "GET", None, reader)
        writers = pool.submit(foreground, "/api/books/b000/progress", "POST", {"index": 0, "position": .5}, writer)
        try:
            for book, chapter_count in list(dataset["purge_chapter_counts"].items())[:samples]:
                assert query(root, bodies) == [(1,)] * 8
                eviction.append(clock(evict)[0])
                # Rearm outside timing. Negative age keeps the same eight bodies at the head of LRU.
                http(port, chapter_cache, "POST", {"max_chapters": remaining})
                with db(root) as connection:
                    connection.execute("UPDATE chapters SET text=?,downloaded=1 WHERE book_id='b001' AND idx<8", (TEXT,))
                    connection.execute("UPDATE chapter_cache SET kind='cache',last_used=-idx-1 WHERE book_id='b001' AND idx<8")
                    connection.executemany(
                        "INSERT INTO image_cache(id,name,size,last_used) VALUES(?,?,64,?) "
                        "ON CONFLICT(id) DO UPDATE SET size=excluded.size",
                        ((i + 1, f"{i:064x}", i) for i in range(32)),
                    )
                cleanup.append(clock(clean)[0])
                http(port, image_cache, "POST", {"max_bytes": 1073741824})
                purge.append(clock(lambda: http(port, "/api/books/" + book, "DELETE"))[0])
                assert query(root, "SELECT count(*) FROM chapters WHERE book_id=?", (book,)) == [(0,)]
                deleted.append(chapter_count)
                wait_for(lambda: query(root, "SELECT id FROM chapter_search_pending LIMIT 1"), lambda rows: not rows)
        finally:
            stop.set()
        readers.result(timeout=20)
        writers.result(timeout=20)
    assert query(root, "SELECT count(*) FROM image_cache") == [(100_000 - 32,)]
    assert query(root, "SELECT count(*) FROM chapters WHERE downloaded=1") == [(dataset["cached_bodies"],)]
    return {"chapter_eviction_8_bodies": {**m1.summary(eviction), "affected_per_sample": 8},
            "image_cleanup_32_absent": {**m1.summary(cleanup), "affected_per_sample": 32},
            "purge": {**m1.summary(purge), "cascaded_chapters_per_sample": deleted},
            "concurrent_cached_reader": m1.summary(reader), "concurrent_progress_writer": m1.summary(writer)}


def high_fanout_purge(port, root, dataset):
    owner = "b000"
    chapters = "SELECT count(*),coalesce(sum(downloaded),0) FROM chapters WHERE book_id=?"
    assert query(root, chapters, (owner,)) == [(50_000, dataset["high_fanout_cached_bodies"])]
    unaffected = query(root, chapters, ("b001",))
    with db(root) as connection:
        connection.executemany("INSERT INTO notes(id,book_id,created_at,note) VALUES(?,?,1,'Synthetic note')",
                               [("purge-owned", owner), ("purge-unrelated", "b001")])
    http(port, f"/api/books/{owner}/trash", "POST", {})
    stopped = threading.Event()
    ready = threading.Barrier(3)

    def foreground(path, method, body):
        requests = []
        ready.wait(timeout=10)
        while not stopped.is_set():
            start = time.perf_counter_ns()
            http(port, path, method, body)
            requests.append((start, time.perf_counter_ns()))
            stopped.wait(.015)
        return requests

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(foreground, "/api/books/b001/chapters/0", "GET", None)
        writer = pool.submit(foreground, "/api/books/b001/progress", "POST", {"index": 0, "position": .75})
        try:
            ready.wait(timeout=10)
            start = time.perf_counter_ns()
            answer, _ = http(port, f"/api/books/{owner}", "DELETE")
            end = time.perf_counter_ns()
        finally:
            stopped.set()
        overlaps = [[(b - a) / 1_000_000 for a, b in future.result(timeout=20) if a < end and b > start]
                    for future in (reader, writer)]
    assert answer == {"id": owner, "deleted": True}
    assert all(overlaps), ("no reader/writer overlapped the DELETE", overlaps)
    assert query(root, chapters, (owner,)) == [(0, 0)] and query(root, chapters, ("b001",)) == unaffected
    assert query(root, "SELECT id FROM books WHERE id IN ('b000','b001')") == [("b001",)]
    assert query(root, "SELECT id FROM notes ORDER BY id") == [("purge-unrelated",)]
    assert query(root, "SELECT 1 FROM chapter_search_docs WHERE book_id=? LIMIT 1", (owner,)) == []
    assert query(root, "SELECT 1 FROM chapter_cache WHERE book_id=? LIMIT 1", (owner,)) == []
    assert query(root, "SELECT position FROM books WHERE id='b001'") == [(.75,)]
    return {"n": 1, "elapsed_ms": (end - start) / 1_000_000, "cascaded_chapters": 50_000,
            "cascaded_cached_bodies": dataset["high_fanout_cached_bodies"],
            "overlapping_reader_samples_ms": overlaps[0], "overlapping_writer_samples_ms": overlaps[1]}


def measure(output, label, binary, canonical, dataset, samples):
    root = output / label
    shutil.copytree(canonical, root)
    startup = []
    for i in range(3):
        started = time.perf_counter()
        with server(binary, root, f"startup-{i}") as (port, _):
            startup.append((time.perf_counter() - started) * 1000)
            assert http(port, "/api/books")[0]["total"] == 500 - PURGES
    with server(binary, root, "workload") as (port, owner):
        before = m1.memory(owner.pid)
        initial_io = io(owner.pid)
        measured = sample_http(port, samples)
        measured.update(maintenance(port, root, dataset, samples))
        measured["purge_50k_high_fanout"] = high_fanout_purge(port, root, dataset)
        after = m1.memory(owner.pid)
        final_io = io(owner.pid)
        file_sizes = {path.name: path.stat().st_size for path in root.glob("library.sqlite3*")}
    verify_db(root)
    return {"startup": m1.summary(startup), **measured, "memory_before": before, "memory_after": after,
            "file_bytes_before_shutdown": file_sizes,
            "process_io_delta": {key: final_io[key] - value for key, value in initial_io.items()}}


def run(args):
    binary = args.binary.resolve()
    output = m1.fresh_output().resolve()
    print(f"Scale evidence: {output}", flush=True)
    rust = rust_source_hashes()
    reference = m1.source_hashes()
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    harness_paths = [Path(__file__).with_name(name) for name in
                     ("m3_scale.py", "m3_support.py", "m2_capture.py", "capture_reference.py")]
    harness = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in harness_paths}
    for profile in args.profile or PROFILES:
        with tempfile.TemporaryDirectory(prefix=profile + "-", dir=output) as directory:
            work = Path(directory)
            canonical = work / "canonical"
            dataset = corpus(canonical, profile)
            before = m1.hashes(canonical)
            result = {"profile": profile, "dataset": dataset, "binary_sha256": binary_hash,
                      "sources": rust, "reference_sources": reference, "harness_sha256": harness,
                      "environment": {"platform": platform.platform(), "cpu": os.cpu_count(),
                                      "python": platform.python_version(), "sqlite_python": sqlite3.sqlite_version},
                      "limits": ["No file imports in M3", "Process I/O includes OS and network I/O",
                                 "Startup retains the operating system's disk cache",
                                 "Maintenance timing includes the worker scheduling and SQL observation interval",
                                 "Counts describe observed rows affected. Internal Rust SQL/decode counters are unavailable."]}
            destination = output / f"{profile}.json"
            for label, executable in (("python", None), ("rust", binary)):
                result[label] = measure(work, label, executable, canonical, dataset, args.samples)
                m1.write_json(destination, result)
            assert m1.hashes(canonical) == before
            assert hashlib.sha256(binary.read_bytes()).hexdigest() == binary_hash
            assert rust_source_hashes() == rust and m1.source_hashes() == reference
            assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in harness_paths} == harness
            print(destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    parser.add_argument("--samples", default=30, type=int)
    args = parser.parse_args()
    assert 3 <= args.samples <= PURGES
    run(args)
