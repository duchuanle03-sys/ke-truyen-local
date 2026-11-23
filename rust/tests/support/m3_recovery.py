"""Bounded image queue, crash after unlink, and idempotent durable recovery."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import subprocess
import threading

from m2_capture import request
from m3_support import Capture, db, finish, fixture, query, run, server, verify_db, wait_for


def bounded_cleanup(root, binary, events):
    store = fixture(root)
    finish(store)
    with db(root) as connection:
        connection.executemany("INSERT INTO image_cache(name,size,last_used) VALUES(?,64,?)",
                               ((f"{i:064x}", i) for i in range(40)))
    with server(binary, root, "before-crash") as (port, owner):
        with db(root) as connection:
            connection.execute("CREATE TRIGGER m3_stop_queue BEFORE INSERT ON asset_gc_candidates "
                               "WHEN NEW.evict=1 AND NEW.name=printf('%064x',32) "
                               "BEGIN SELECT RAISE(ABORT,'synthetic candidate boundary'); END")
        path = "/api/cache/images"
        Capture(port, events).post(path, {"max_bytes": 0}, project=lambda data: data["max_bytes"])
        names = "SELECT name FROM image_cache ORDER BY last_used"
        wait_for(lambda: query(root, names), lambda rows: len(rows) == 8)
        assert query(root, names) == [(f"{i:064x}",) for i in range(32, 40)]
        owner.kill()
        owner.wait(timeout=10)
    with db(root) as connection:
        connection.execute("DROP TRIGGER m3_stop_queue")
    with server(binary, root, "after-crash") as (port, _):
        wait_for(lambda: query(root, names), lambda rows: not rows)
        stats = Capture(port, events).request("/api/cache/images")
        assert (stats["total_bytes"], stats["entries"], stats["pending_bytes"]) == (0, 0, 0)
    verify_db(root)


def unlink_then_abort(root, binary, events):
    store = fixture(root)
    finish(store)
    path = root / "images" / ("a" * 64)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic cache image")
    store.touch_image(path, path.stat().st_size, legacy=False)
    candidates = "SELECT name,evict FROM asset_gc_candidates"
    with server(binary, root, "before-unlink") as (port, owner):
        with db(root) as connection:
            connection.execute("CREATE TRIGGER m3_stop_accounting BEFORE DELETE ON image_cache "
                               f"WHEN OLD.name='{path.name}' "
                               "BEGIN SELECT RAISE(ABORT,'synthetic account failure'); END")
        Capture(port, events).post("/api/cache/images", {"max_bytes": 0}, project=lambda data: data["max_bytes"])
        wait_for(lambda: not path.exists() and not list(path.parent.glob("*.gc-*")))
        assert query(root, candidates) == [(path.name, 1)]
        assert query(root, "SELECT total_bytes FROM image_cache_state") == [(len(b"synthetic cache image"),)]
        owner.kill()
        owner.wait(timeout=10)
    with db(root) as connection:
        connection.execute("DROP TRIGGER m3_stop_accounting")
    with server(binary, root, "after-unlink") as (port, _):
        wait_for(lambda: query(root, candidates), lambda rows: not rows)
        stats = Capture(port, events).request("/api/cache/images")
        assert (stats["total_bytes"], stats["entries"], stats["pending_bytes"]) == (0, 0, 0)
    verify_db(root)


def reserved_write(root, binary, events):
    store = fixture(root)
    urls = ["https://cdn.example/reserved-orphan.png", "https://cdn.example/reserved-offline.png"]
    paths = [root / "images" / hashlib.sha256(url.encode()).hexdigest() for url in urls]
    paths[0].parent.mkdir(exist_ok=True)
    store.upsert_book({"id": "offline", "name": "Offline", "source": "full"}, [{"images": [urls[1]]}])
    finish(store)
    # A crashed image writer reserved bytes and queued recovery before creating either file.
    for path in paths:
        store.touch_image(path, 64, legacy=False)
    store.pin_image(paths[1], "offline", 0)
    store.queue_assets(paths)
    assert all(not path.exists() for path in paths)
    with server(binary, root, "reserved-write") as (port, _):
        wait_for(lambda: query(root, "SELECT name FROM asset_gc_candidates"), lambda rows: not rows)
        assert query(root, "SELECT name,size,pin_count FROM image_cache") == [(paths[1].name, 0, 1)]
        stats = Capture(port, events).request("/api/cache/images")
        assert (stats["total_bytes"], stats["protected_bytes"], stats["entries"]) == (0, 0, 0)
    verify_db(root)


def junction_cleanup(root, binary, events):
    if os.name != "nt":
        return
    store = fixture(root)
    finish(store)
    target = root / "synthetic-outside-managed-assets"
    target.mkdir()
    external = target / ("e" * 64 + ".epub")
    external.write_bytes(b"synthetic original beyond a reparse point")
    junction = root / "files"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                   check=True, capture_output=True)
    assert os.path.isjunction(junction)
    good = root / "images" / ("b" * 64)
    good.parent.mkdir(exist_ok=True)
    good.write_bytes(b"synthetic unreferenced asset")
    try:
        store.queue_assets((junction / external.name, good))
        with server(binary, root, "real-junction") as (port, _):
            queue = "SELECT kind,name,retry_at>0 FROM asset_gc_candidates ORDER BY kind,name"
            expected = [("files", external.name, 1)]
            assert wait_for(lambda: query(root, queue), lambda rows: rows == expected) == expected
            assert not good.exists()
            assert os.path.isjunction(junction)
            assert list(target.iterdir()) == [external]
            assert external.read_bytes() == b"synthetic original beyond a reparse point"
            Capture(port, events).request("/api/books")
        verify_db(root)
    finally:
        junction.rmdir()
    assert external.is_file()


def concurrent_wal_cache(root, binary, events):
    store = fixture(root)
    text = "Synthetic needle " + "x" * 4096
    store.upsert_book({"id": "cached", "name": "Evictable", "source": "full"},
                      [{"title": str(i)} for i in range(24)])
    store.upsert_book({"id": "kept", "name": "Reader", "source": "file"}, [{"title": "Keep", "text": text}])
    for i in range(24):
        store.save_content("cached", i, {"text": text})
    finish(store)
    path = "/api/cache/chapters"
    first_eight = "SELECT downloaded FROM chapters WHERE book_id='cached' AND idx<8 ORDER BY idx"
    with server(binary, root, "concurrent-wal") as (port, _):
        api = Capture(port, events)
        finished = threading.Event()
        first_work = threading.Barrier(3)

        def foreground(path, result, method="GET", body=None):
            seen = 0
            first_work.wait(timeout=5)
            while not finished.is_set():
                response = request(port, path, method, body)
                assert response["status"] == 200, (method, path, response["status"], response["json"])
                assert result(response["json"]), response["json"]
                seen += 1
                finished.wait(.005)
            return seen

        def limit(value):
            stats = api.post(path, {"max_chapters": value}, project=lambda state: state["max_chapters"])
            assert stats["max_chapters"] == value
            assert stats["protected_count"] == 1 and stats["legacy_count"] == 0
            assert stats["reusable_bytes"] == stats["freelist_count"] * stats["page_size"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(foreground, "/api/books/kept/chapters/0", lambda data: data["text"] == text)
            writer = pool.submit(foreground, "/api/books/kept/progress", lambda data: data["position"] == .5,
                                 "POST", {"index": 0, "position": .5})
            try:
                first_work.wait(timeout=5)
                for _ in range(24):
                    for task in (reader, writer):
                        if task.done():
                            task.result()
                    limit(16)
                    wait_for(lambda: query(root, first_eight), lambda rows: rows == [(0,)] * 8)
                    limit(24)
                    with db(root) as connection:
                        connection.execute("UPDATE chapters SET text=?,downloaded=1 WHERE book_id='cached' AND idx<8", (text,))
                        connection.execute("UPDATE chapter_cache SET kind='cache',last_used=-idx-1 WHERE book_id='cached' AND idx<8")
                    wait_for(lambda: query(root, "SELECT id FROM chapter_search_pending LIMIT 1"), lambda rows: not rows)
            finally:
                finished.set()
            calls = [reader.result(timeout=10), writer.result(timeout=10)]
        assert min(calls) > 0
        events.append({"observation": "successful concurrent requests", "reader": calls[0], "progress": calls[1]})
        stats = "SELECT max_chapters,cache_count,protected_count FROM chapter_cache_state"
        assert query(root, stats) == [(24, 24, 1)]
        assert query(root, "SELECT position FROM books WHERE id='kept'") == [(.5,)]
        result = api.request("/api/books/cached/search?q=needle")
        assert len(result["results"]) == 24 and not result["indexing"]
    verify_db(root)


if __name__ == "__main__":
    run("recovery", {"bounded_cleanup": bounded_cleanup, "unlink_then_abort": unlink_then_abort,
                     "reserved_write": reserved_write, "junction_cleanup": junction_cleanup,
                     "concurrent_wal_cache": concurrent_wal_cache})
