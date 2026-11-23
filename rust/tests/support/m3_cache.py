"""Chapter and image budgets, ownership, durable eviction and stale reader leases."""
import hashlib
import http.client
import os
import socket
from urllib.parse import urlencode

from m2_capture import PNG
from m3_support import Capture, db, finish, fixture, query, run, server, verify_db, wait_for


CHAPTERS = "/api/cache/chapters"
IMAGES = "/api/cache/images"


def chapter_stats(state):
    keys = ("max_chapters", "cache_count", "cache_bytes", "legacy_count", "legacy_bytes",
            "protected_count", "protected_bytes", "complete", "auto_vacuum", "page_size")
    return {key: state[key] for key in keys}


def chapter_cases(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "online", "name": "Online", "source": "full"},
                      [{"title": f"Chương {i}", "url": f"https://example.org/{i}"} for i in range(20)])
    store.upsert_book({"id": "local", "name": "Imported", "source": "file"}, [{"text": "Protected import"}])
    for i in range(19):
        store.save_content("online", i, {"text": f"Body needle {i}"}, offline=i == 17)
    store.save_content("online", 19, {"text": "Legacy needle"})
    with db(root) as connection:
        connection.execute("UPDATE chapter_cache SET kind='legacy' WHERE book_id='online' AND idx=19")
        connection.execute("UPDATE chapter_cache SET last_used=idx+1 WHERE book_id='online'")
    store.edit_content("online", 18, {"text": "Edited protected", "html": "<p>Edited protected</p>"})
    store.restore_content("online", 18)
    store.progress("online", 5, .25)
    store.save_note({"id": "eviction-note", "book_id": "online", "chapter_index": 5, "note": "Keep"})
    finish(store)
    inventory = "SELECT idx,downloaded FROM chapters WHERE book_id='online' ORDER BY idx"
    gate = "SELECT clear_until,fts_merge FROM chapter_cache_state"
    with server(binary, root, "before-crash") as (port, owner):
        api = Capture(port, events)
        start = api.request(CHAPTERS, project=chapter_stats)
        assert (start["cache_count"], start["legacy_count"], start["protected_count"]) == (17, 1, 3)
        for bad in (True, "8", 8.5, -1, 100001):
            api.post(CHAPTERS, {"max_chapters": bad}, 400)
        api.post(CHAPTERS + "/legacy", {"confirmed": 1}, 400)
        api.post(CHAPTERS + "/clear", {}, 403, headers={"Origin": "https://foreign.invalid"})
        api.post("/api/readers", {"id": "kept-tab", "book_id": "online", "index": 16, "sequence": 7})
        with db(root) as connection:
            connection.execute(
                "CREATE TRIGGER m3_stop_evict BEFORE UPDATE OF downloaded ON chapters "
                "WHEN OLD.book_id='online' AND OLD.idx=8 AND NEW.downloaded=0 "
                "BEGIN SELECT RAISE(ABORT,'synthetic eviction stop'); END"
            )
        api.post(CHAPTERS + "/clear", {}, project=lambda data: data["max_chapters"])
        wait_for(lambda: query(root, inventory), lambda rows: rows[7][1] == 0)
        assert query(root, inventory) == [(i, int(i >= 8)) for i in range(20)]
        assert query(root, gate)[0][0] > 0
        owner.kill()
        owner.wait(timeout=10)
    with db(root) as connection:
        connection.execute("DROP TRIGGER m3_stop_evict")
    with server(binary, root, "after-crash") as (port, _):
        api = Capture(port, events)
        active = api.post("/api/readers", {"id": "kept-tab", "book_id": None, "sequence": 6})
        assert active == {"active": True}
        wait_for(lambda: query(root, inventory), lambda rows: rows[15][1] == 0)
        assert query(root, inventory) == [(i, int(i >= 16)) for i in range(20)]
        assert query(root, gate)[0][0] > 0
        api.post("/api/readers", {"id": "kept-tab", "book_id": None, "sequence": 8})
        wait_for(lambda: query(root, gate), lambda rows: rows == [(0, 0)])
        assert query(root, inventory) == [(i, int(i >= 17)) for i in range(20)]
        assert query(root, "SELECT idx,title FROM chapters WHERE book_id='online' AND idx=5") == [(5, "Chương 5")]
        assert query(root, "SELECT chapter_index,position FROM books WHERE id='online'") == [(5, .25)]
        assert query(root, "SELECT note FROM notes WHERE id='eviction-note'") == [("Keep",)]
        search = api.request("/api/books/online/search?q=needle")
        assert [hit["index"] for hit in search["results"]] == [17, 18, 19]
        api.post(CHAPTERS, {"max_chapters": 0}, project=lambda data: data["max_chapters"])
        api.post(CHAPTERS + "/legacy", {"confirmed": True}, project=lambda data: data["max_chapters"])
        wait_for(lambda: query(root, inventory), lambda rows: rows[-1][1] == 0)
        wait_for(lambda: query(root, gate), lambda rows: rows == [(0, 0)])
        api.request(CHAPTERS, project=chapter_stats)
        assert query(root, "SELECT book_id,idx,kind,offline FROM chapter_cache ORDER BY book_id,idx") == [
            ("local", 0, "protected", 0), ("online", 17, "protected", 1), ("online", 18, "protected", 0)
        ]
    verify_db(root)


def byte_bound(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "online", "name": "Online", "source": "full"}, [{"title": str(i)} for i in range(3)])
    for i in range(3):
        store.save_content("online", i, {"text": "x" * 150000})
    finish(store)
    rows = "SELECT idx,downloaded FROM chapters ORDER BY idx"
    with server(binary, root, "byte-bound") as (port, _):
        api = Capture(port, events)
        with db(root) as connection:
            connection.execute("UPDATE chapter_cache SET last_used=idx")
            connection.execute("CREATE TRIGGER m3_stop_big_eviction BEFORE UPDATE OF downloaded ON chapters "
                               "WHEN NEW.idx=1 AND NEW.downloaded=0 "
                               "BEGIN SELECT RAISE(ABORT,'synthetic byte boundary'); END")
        api.post(CHAPTERS, {"max_chapters": 0}, project=lambda data: data["max_chapters"])
        wait_for(lambda: query(root, rows), lambda values: values[0][1] == 0)
        assert query(root, rows) == [(0, 0), (1, 1), (2, 1)]
        with db(root) as connection:
            connection.execute("DROP TRIGGER m3_stop_big_eviction")
        wait_for(lambda: query(root, rows), lambda values: sum(row[1] for row in values) == 0)
    verify_db(root)


def images(root, binary, events):
    store = fixture(root)
    image_dir = root / "images"
    file_dir = root / "files"
    image_dir.mkdir(exist_ok=True)
    file_dir.mkdir(exist_ok=True)
    urls = [f"https://cdn.example/{kind}.png" for kind in ("shared", "old", "recent", "ambiguous")]
    paths = [image_dir / hashlib.sha256(url.encode()).hexdigest() for url in urls]
    document = file_dir / (hashlib.sha256(b"shared original").hexdigest() + ".epub")
    document.write_bytes(b"shared original")
    personal = file_dir / "keep-personal.epub"
    personal.write_bytes(b"personal original")
    for book in ("first", "second"):
        store.upsert_book({"id": book, "name": book, "source": "file", "path": str(document)},
                          [{"title": "Picture", "images": [urls[0]]}])
    store.upsert_book({"id": "online", "name": "Online", "source": "full"}, [{"images": urls[1:]}])
    for i, path in enumerate(paths):
        path.write_bytes(PNG)
        store.touch_image(path, len(PNG), legacy=i == 3)
    for book in ("first", "second"):
        store.pin_image(paths[0], book, 0)
    finish(store)
    count = "SELECT count(*) FROM asset_gc_candidates"
    entries = "SELECT total_bytes,protected_bytes,legacy_bytes,entries,protected_entries,pending_bytes FROM image_cache_state"
    with server(binary, root, "images") as (port, _):
        api = Capture(port, events)
        for bad in (True, "8", 8.5, -1, 65 * 1024 ** 3):
            api.post(IMAGES, {"max_bytes": bad}, 400)
        start = api.request(IMAGES)
        assert (start["total_bytes"], start["protected_bytes"], start["legacy_bytes"]) == (len(PNG) * 4, len(PNG) * 2, len(PNG))
        with db(root) as connection:
            connection.execute("UPDATE image_cache SET last_used=0 WHERE name=?", (paths[1].name,))
        api.post(IMAGES, {"max_bytes": len(PNG)}, project=lambda data: data["max_bytes"])
        wait_for(lambda: paths[1].exists(), lambda value: not value)
        assert paths[2].read_bytes() == PNG
        api.post("/api/books/first/trash", {}, project=lambda data: data["id"])
        api.post("/api/books/second/trash", {}, project=lambda data: data["id"])
        assert document.is_file() and paths[0].is_file()
        api.post(IMAGES, {"max_bytes": 0}, project=lambda data: data["max_bytes"])
        api.post(IMAGES + "/clear", {}, project=lambda data: data["max_bytes"])
        wait_for(lambda: query(root, entries), lambda rows: rows == [(2 * len(PNG), 2 * len(PNG), len(PNG), 2, 2, 0)])
        api.request("/api/image?" + urlencode({"url": urls[0]}), project=lambda data: data)
        api.request("/api/books/first", method="DELETE")
        wait_for(lambda: query(root, count), lambda values: values == [(0,)])
        assert document.is_file() and paths[0].is_file()
        assert query(root, "SELECT book_id FROM image_pins ORDER BY book_id") == [("second",)]
        api.request("/api/books/second", method="DELETE")
        wait_for(lambda: (document.exists(), paths[0].exists(), query(root, count)), lambda x: x == (False, False, [(0,)]))
        assert paths[3].read_bytes() == PNG and personal.read_bytes() == b"personal original"
        api.request(IMAGES, project=lambda data: {key: value for key, value in data.items() if key != "clearing"})
    verify_db(root)


def staged(root, binary, events):
    store = fixture(root)
    finish(store)
    path = root / "files" / (hashlib.sha256(b"interrupted cleanup").hexdigest() + ".epub")
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"interrupted cleanup")
    store.queue_assets((path,))
    candidate = "SELECT id,retry_at FROM asset_gc_candidates WHERE name=?"
    [(identity, _)] = query(root, candidate, (path.name,))
    staged_path = path.with_name(f"{path.name}.gc-{identity}")
    # Durable candidate exists but its file was moved before the previous process exited.
    path.rename(staged_path)
    if os.name == "nt":
        # Windows' CRT handle uses a sharing mode that denies deletion.
        with staged_path.open("rb") as lock, server(binary, root, "locked") as (_, owner):
            wait_for(lambda: query(root, candidate, (path.name,)), lambda rows: rows and rows[0][1] > 0)
            assert lock.read() == b"interrupted cleanup"
            owner.kill()
            owner.wait(timeout=10)
        assert staged_path.is_file()
        with db(root) as connection:
            connection.execute("UPDATE asset_gc_candidates SET retry_at=0 WHERE name=?", (path.name,))
    with server(binary, root, "staged-recovery") as (port, _):
        wait_for(lambda: query(root, candidate, (path.name,)), lambda values: not values)
        assert not path.exists() and not staged_path.exists()
        Capture(port, events).request("/api/books")
    verify_db(root)


def reader_asset_lease(root, binary, events):
    store = fixture(root)
    url = "https://cdn.example/live-stream.png"
    path = root / "images" / hashlib.sha256(url.encode()).hexdigest()
    path.parent.mkdir(exist_ok=True)
    store.upsert_book({"id": "online", "name": "Online", "source": "full"}, [{"images": [url]}])
    finish(store)
    target_size = 16 * 1024 * 1024
    with path.open("wb") as image:
        image.write(PNG)
        image.truncate(target_size)
    store.touch_image(path, target_size, legacy=False)
    queued = "SELECT evict,retry_at>0 FROM asset_gc_candidates WHERE name=?"
    with server(binary, root, "live-stream-lease") as (port, _):
        api = Capture(port, events)
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            stream.connect()
            stream.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stream.request("GET", "/api/image?" + urlencode({"url": url}))
            response = stream.getresponse()
            assert response.status == 200 and int(response.getheader("Content-Length")) == target_size
            assert response.read(8) == PNG[:8]
            api.post(IMAGES + "/clear", {}, project=lambda data: data["max_bytes"])
            wait_for(lambda: query(root, queued, (path.name,)), lambda rows: rows == [(2, 1)])
            assert path.stat().st_size == target_size
        finally:
            stream.close()
        wait_for(lambda: query(root, queued, (path.name,)), lambda rows: not rows)
        assert not path.exists()
        stats = api.request(IMAGES)
        assert (stats["total_bytes"], stats["pending_bytes"], stats["entries"]) == (0, 0, 0)
    verify_db(root)


def same_scheme_image_reference(root, binary, events):
    store = fixture(root)
    original = "https://cdn.example.org/books/original.html"
    url = "https://cdn.example.org/books/panel.png"
    name = hashlib.sha256(url.encode()).hexdigest()
    path = root / "images" / name
    path.parent.mkdir(exist_ok=True)
    store.upsert_book({"id": "offline", "name": "Offline", "source": "file", "path": original},
                      [{"html": '<p>Panel</p><img src="panel.png">'}])
    finish(store)
    path.write_bytes(PNG)
    store.touch_image(path, len(PNG), legacy=False)
    refs = "SELECT book_id,idx,name FROM asset_refs WHERE kind='images' ORDER BY book_id,idx,name"
    pins = "SELECT book_id,idx,name FROM image_pins ORDER BY book_id,idx,name"
    expected = [("offline", 0, name)]
    assert query(root, refs) == query(root, pins) == expected
    orphan = path.with_name(hashlib.sha256(b"unrelated GC marker").hexdigest())
    orphan.write_bytes(b"synthetic unowned asset")
    settled = ("SELECT (SELECT count(*) FROM asset_pending WHERE book_id='offline'), "
               "(SELECT count(*) FROM asset_gc_candidates WHERE name IN (?,?))")
    with server(binary, root, "same-scheme-reindex") as (port, _):
        with db(root) as connection:
            connection.execute("UPDATE chapters SET html=? WHERE book_id='offline' AND idx=0",
                               ('<p>Panel</p><img src="https:panel.png">',))
            connection.execute("INSERT INTO asset_gc_candidates(kind,name) VALUES('images',?)", (orphan.name,))
        wait_for(lambda: query(root, settled, (name, orphan.name)), lambda rows: rows == [(0, 0)])
        owners, offline_pins, exists = query(root, refs), query(root, pins), path.is_file()
        events.append({"observation": "post-index and GC ownership", "refs": owners, "pins": offline_pins, "file_exists": exists})
        assert not orphan.exists()
        assert (owners, offline_pins, exists) == (expected, expected, True), (owners, offline_pins, exists)
        assert path.read_bytes() == PNG
        Capture(port, events).request("/api/image?" + urlencode({"url": url}))
    verify_db(root)


if __name__ == "__main__":
    run("cache", {"chapters": chapter_cases, "bytes": byte_bound, "images": images, "staged": staged,
                  "reader_asset_lease": reader_asset_lease, "same_scheme_image_reference": same_scheme_image_reference})
