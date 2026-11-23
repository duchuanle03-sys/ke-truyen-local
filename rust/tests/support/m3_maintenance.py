"""Destructive route rollback, edit/restore, schema upgrade and explicit compaction."""
import http.client
import json
import os
import socket
import sqlite3
import struct
import subprocess
import time

from m3_support import Capture, db, finish, fixture, query, run, server, verify_db, wait_for
from m2_capture import lock_probe, request
from library import Store


BOOK = "/api/books/book"
CHAPTER = BOOK + "/chapters/0"
CACHE = "/api/cache/chapters"


def book_clock(value):
    if isinstance(value, dict):
        result = {key: book_clock(item) for key, item in value.items()}
        if value.get("trashed_at"):
            assert abs(time.time() - value["trashed_at"]) < 60, value
            assert value["trash_expires_at"] == value["trashed_at"] + 30 * 86400
            result.update(trashed_at="<clock>", trash_expires_at="<clock+30 days>")
        return result
    if isinstance(value, list):
        return [book_clock(item) for item in value]
    return value


def lifecycle(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "book", "name": "Book", "source": "file"}, [
        {"title": "Original", "text": "Original needle", "html": "<p>Original needle</p>"},
        {"title": "Unloaded"},
    ])
    store.upsert_book({"id": "comic", "name": "Comic", "kind": "comic", "source": "file"},
                      [{"title": "Panel", "images": ["https://cdn.example/panel.png"]}])
    store.progress("book", 0, .625)
    store.save_note({"id": "owned", "book_id": "book", "note": "Reading note"})
    store.save_note({"id": "orphan", "book_id": "missing", "note": "Orphan note"})
    finish(store)
    with server(binary, root, "lifecycle") as (port, _):
        api = Capture(port, events)
        api.request(BOOK, 400, "DELETE")
        api.post(BOOK + "/chapters/1/edit", {"text": "Unloaded"}, 400)
        api.post(BOOK + "/chapters/1/restore", {}, 400)
        api.post("/api/books/comic/chapters/0/edit", {"text": "Wrong kind"}, 400)
        api.post(CHAPTER + "/edit", {"title": "Edited", "text": "Edited <script>\n& \" '"})
        api.post(CHAPTER + "/edit", {"html": "<p>Second &amp; edit</p><p>Đoạn mới</p>"})
        assert query(root, "SELECT title,text,original_html,original_text FROM chapters WHERE book_id='book' AND idx=0") == [
            ("Edited", "Second & edit\nĐoạn mới", "<p>Original needle</p>", "Original needle")
        ]
        restored = api.post(CHAPTER + "/restore", {})
        assert (restored["title"], restored["text"]) == ("Edited", "Original needle")
        assert query(root, "SELECT kind FROM chapter_cache WHERE book_id='book' AND idx=0") == [("protected",)]
        api.post(BOOK + "/update", {"archived": True}, project=lambda val: {k: val[k] for k in ("id", "archived")})
        assert [row["id"] for row in api.request("/api/books?archived=1",
                                                project=lambda val: [b["id"] for b in val["items"]])["items"]] == ["book"]
        api.post(BOOK + "/trash", {}, 403, headers={"Origin": "https://foreign.invalid"})
        first = api.post(BOOK + "/trash", {}, project=book_clock_projection)
        again = api.post(BOOK + "/trash", {}, project=book_clock_projection)
        assert first["trashed_at"] == again["trashed_at"]
        api.request("/api/books?trashed=1", project=lambda val: [book_clock_projection(b) for b in val["items"]])
        restored = api.post(BOOK + "/restore", {}, project=lambda val: {k: val[k] for k in ("id", "archived", "trashed_at", "position")})
        assert (restored["position"], restored["archived"], restored["trashed_at"]) == (.625, False, 0)
        with db(root) as connection:
            connection.executemany("INSERT INTO download_jobs VALUES(?,?,'paused',0,'{}',1,1)",
                                   [("book-job", "book"), ("comic-job", "comic")])
        api.post(BOOK + "/trash", {}, project=book_clock_projection)
        jobs = "SELECT id,status,cancelled FROM download_jobs ORDER BY id"
        assert query(root, jobs) == [("book-job", "cancelled", 1), ("comic-job", "paused", 0)]
        with db(root) as connection:
            connection.execute("UPDATE download_jobs SET status='paused',cancelled=0 WHERE id='book-job'")
        api.request(BOOK, 400, "DELETE")
        with db(root) as connection:
            connection.execute("UPDATE download_jobs SET status='cancelled',cancelled=1 WHERE id='book-job'")
            connection.execute("CREATE TRIGGER m3_reject_purge BEFORE DELETE ON books WHEN OLD.id='book' "
                               "BEGIN SELECT RAISE(ABORT,'synthetic purge failure'); END")
        api.request(BOOK, 500, "DELETE")
        assert query(root, "SELECT id FROM notes ORDER BY id") == [("orphan",), ("owned",)]
        assert query(root, "SELECT count(*) FROM chapters WHERE book_id='book'") == [(2,)]
        with db(root) as connection:
            connection.execute("DROP TRIGGER m3_reject_purge")
        api.request(BOOK, 200, "DELETE")
        api.request(BOOK, 404)
        assert query(root, "SELECT id FROM notes ORDER BY id") == [("orphan",)]
        assert query(root, "SELECT count(*) FROM chapters WHERE book_id='book'") == [(0,)]
        assert query(root, "SELECT count(*) FROM chapter_search_docs WHERE book_id='book'") == [(0,)]
        assert query(root, "SELECT id FROM books ORDER BY id") == [("comic",)]
        assert query(root, jobs) == [("comic-job", "paused", 0)]
        with db(root) as connection:
            connection.execute("UPDATE books SET trashed_at=2 WHERE id='comic'")
        api.post("/api/books/comic/restore", {}, 400)
        assert query(root, jobs) == [("comic-job", "paused", 0)]
        assert query(root, "SELECT trashed_at FROM books WHERE id='comic'") == [(2.0,)]
    verify_db(root)


def book_clock_projection(book):
    return book_clock({key: book[key] for key in ("id", "trashed_at", "trash_expires_at")})


def schema(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "book", "name": "Book", "source": "file"}, [{"text": "Legacy needle"}])
    store.upsert_book({"id": "expired", "name": "Expired", "source": "file"}, [{"text": "Expired text"}])
    store.progress("book", 0, .75)
    store.save_note({"id": "orphan", "book_id": "missing", "note": "Preserve"})
    finish(store)
    with db(root) as connection:
        connection.execute("UPDATE books SET added_at=1,updated_at=1,read_at=2 WHERE id='book'")
        connection.execute("UPDATE books SET trashed_at=2 WHERE id='expired'")
        triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'search_%'").fetchall()
        for trigger, in triggers:
            connection.execute(f'DROP TRIGGER "{trigger}"')
        for table in ("chapter_search_fts", "chapter_search_docs", "chapter_search_pending", "chapter_search_state"):
            connection.execute(f'DROP TABLE "{table}"')
    with server(binary, root, "upgrade") as (port, _):
        api = Capture(port, events)
        expired = "/api/books/expired"
        started = time.monotonic()
        initial = request(port, expired)
        assert initial["status"] in ((404,) if binary is None else (200, 404)), initial
        if initial["status"] == 200:
            assert initial["json"]["trashed_at"] == 2.0
        wait_for(lambda: query(root, "SELECT id FROM books WHERE id='expired'"), lambda rows: not rows)
        events.append({"observation": "approved expiry startup timing", "first_status": initial["status"],
                       "elapsed_to_absence_ms": round((time.monotonic() - started) * 1000, 3)})
        api.request(expired, 404)
        assert api.request(BOOK)["position"] == .75
        assert wait_for(lambda: query(root, "SELECT text FROM chapter_search_docs")) == [("legacy needle",)]
        api.request(BOOK + "/search?q=needle")
    verify_db(root)
    # Starting the unchanged Store after native writes also exercises retained schema and expression indexes.
    reopened = Store(root)
    assert reopened.get_book("book")["position"] == .75
    assert reopened.notes()[0]["id"] == "orphan"
    assert [hit["index"] for hit in reopened.search_content("book", "needle")["results"]] == [0]


def restart_trashed_jobs(root, binary, events):
    store = fixture(root)
    for book in ("old", "fresh", "live"):
        store.upsert_book({"id": book, "name": book, "source": "file"}, [{"text": book}])
    finish(store)
    jobs = "SELECT id,status,cancelled FROM download_jobs ORDER BY id"
    paused = [(f"{book}-job", "paused", 0) for book in ("fresh", "live", "old")]
    with server(binary, root, "before-job-crash") as (_, owner):
        with db(root) as connection:
            connection.execute("UPDATE books SET trashed_at=2 WHERE id='old'")
            connection.execute("UPDATE books SET trashed_at=? WHERE id='fresh'", (time.time(),))
            connection.executemany("INSERT INTO download_jobs VALUES(?,?,'paused',0,'{}',1,1)",
                                   [(f"{book}-job", book) for book in ("old", "fresh", "live")])
        assert query(root, jobs) == paused
        owner.kill()
        owner.wait(timeout=10)
    try:
        persisted = query(root, jobs)
    except sqlite3.OperationalError as error:
        if os.name != "nt" or error.sqlite_errorcode & 0xff != sqlite3.SQLITE_IOERR:
            raise
        events.append({"observation": "external SQLite observer retried once after forced termination",
                       "error": error.sqlite_errorname, "detail": str(error)})
        time.sleep(.025)
        persisted = query(root, jobs)
    assert persisted == paused
    with server(binary, root, "recovered-job-crash") as (port, _):
        api = Capture(port, events)
        expected = [("fresh-job", "cancelled", 1), ("live-job", "paused", 0)]
        wait_for(lambda: query(root, jobs), lambda rows: rows == expected)
        api.request("/api/books/old", 404)
        assert api.request("/api/books/fresh", project=book_clock_projection)["trashed_at"] > 0
        with db(root) as connection:
            connection.execute("UPDATE books SET trashed_at=2 WHERE id='live'")
        api.post("/api/books/live/restore", {}, 400)
        api.request("/api/books/live", 400, "DELETE")
        assert query(root, jobs) == expected
        assert query(root, "SELECT id FROM books ORDER BY id") == [("fresh",), ("live",)]
    verify_db(root)


def compact(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "book", "name": "Book", "source": "file"}, [{"text": "Persistent needle"}])
    store.upsert_book({"id": "throwaway", "name": "Throwaway", "source": "file"}, [{"text": "x" * 512000}])
    finish(store)
    store.trash_book("throwaway")
    store.purge_book("throwaway")
    store.checkpoint()
    with server(binary, root, "compact") as (port, _):
        api = Capture(port, events)
        api.post(CACHE + "/compact", {"confirmed": False}, 400)
        api.post("/api/readers", {"id": "tab", "book_id": "book", "index": 0, "sequence": 1})
        api.post(CACHE + "/compact", {"confirmed": True}, 400)
        api.post("/api/readers", {"id": "tab", "book_id": None, "sequence": 2})
        with db(root) as connection:
            connection.execute("INSERT INTO download_jobs VALUES('blocked','book','verification_required',0,'{}',1,1)")
        path = CACHE + "/compact"
        message = {"error": "Hãy tạm dừng các tác vụ tải trước khi thu gọn."}
        refusal = wait_for(lambda: request(port, path, "POST", {"confirmed": True}),
                           lambda answer: answer["status"] == 400 and answer["json"] == message)
        api.record(refusal, path, 400, "POST", lambda data: data)
        with db(root) as connection:
            connection.execute("UPDATE download_jobs SET status='paused' WHERE id='blocked'")
        before = api.request(CACHE, project=lambda data: {key: data[key] for key in ("auto_vacuum", "page_size", "max_chapters")})
        assert before["freelist_count"] > 0
        # A maintenance tick may legitimately win admission before the compact request.
        response = wait_for(lambda: request(port, path, "POST", {"confirmed": True}),
                            lambda response: response["status"] == 200)
        compacted = api.record(response, path, 200, "POST",
                               lambda data: {key: data[key] for key in ("auto_vacuum", "page_size", "max_chapters")})
        assert compacted["auto_vacuum"] == before["auto_vacuum"]
        assert compacted["freelist_count"] == 0
        assert compacted["database_bytes"] < before["database_bytes"]
        assert compacted["wal_bytes"] == 0
        api.request(BOOK + "/search?q=needle")
    verify_db(root)


def shutdown_reset(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "book", "name": "Book", "source": "file"}, [{"text": "Keep this chapter"}])
    finish(store)
    pause = {"pause_downloads": True}
    jobs = "SELECT status,cancelled FROM download_jobs WHERE id='held-job'"
    with server(binary, root, "shutdown-held-write") as (port, owner):
        cookie = request(port, "/")["headers"]["set-cookie"].split(";", 1)[0]
        token = request(port, "/api/settings", headers={"Cookie": cookie})["headers"]["x-shutdown-token"]
        headers = {"Cookie": cookie, "X-Shutdown-Token": token}
        body = json.dumps(pause).encode()
        raw = (f"POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n"
               f"X-Shutdown-Token: {token}\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
        with db(root) as connection:
            connection.execute("INSERT INTO download_jobs VALUES('held-job','book','verification_required',0,'{}',1,1)")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as first:
            with db(root) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                first.sendall(raw)
                if binary is None:
                    # Python closes admission while saving; its reference contract uses a normal response flush.
                    wait_for(lambda: request(port, "/api/books"), lambda answer: answer["status"] == 503, timeout=.5)
                else:
                    # Give the first request a chance to reach its blocked write, then prove it owns shutdown.
                    first.settimeout(.05)
                    try:
                        premature = first.recv(1)
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError(f"first shutdown completed under SQLite write lock: {premature!r}")
                    refused = request(port, "/api/shutdown", "POST", pause, headers)
                    assert refused["status"] == 409 and refused["json"] == {
                        "code": "busy_operation", "error": "Đang lưu trạng thái tắt máy chủ.",
                    }, refused
                    first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
                    first.close()
            if binary is None:
                first.settimeout(5)
                with http.client.HTTPResponse(first) as ack:
                    ack.begin()
                    assert ack.status == 200 and json.load(ack) == {"accepted": True}
        expected = [("paused", 0)]
        assert wait_for(lambda: query(root, jobs), lambda rows: rows == expected, timeout=5) == expected
        events.append({"observation": "pause committed after releasing SQLite writer",
                       "client_reset": binary is not None, "state": expected})
        try:
            assert owner.wait(timeout=8) == 0
        except subprocess.TimeoutExpired as error:
            try:
                lock_probe(root)
            except OSError:
                events.append({"observation": "process and library lock remain live after pause commit"})
            raise AssertionError("shutdown did not exit after the pause committed") from error
    lock_probe(root)
    verify_db(root)
    with server(binary, root, "shutdown-reopened") as (port, _):
        assert query(root, jobs) == [("paused", 0)]
        Capture(port, events).request(CHAPTER)


if __name__ == "__main__":
    run("maintenance", {"lifecycle": lifecycle, "schema": schema, "restart_trashed_jobs": restart_trashed_jobs,
                        "compact": compact, "shutdown_reset": shutdown_reset})
