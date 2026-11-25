"""Durable library queue contracts, with no fetchable source in the offline case."""
import base64
import hashlib
import json
import re
from urllib.parse import urlencode

from m2_capture import PNG, request
from m3_support import Capture, db, finish, fixture, query, run, server, verify_db, wait_for
from m5_support import fixture_server, seed_transport


JOBS = "/api/jobs"


def job_result(job):
    assert re.fullmatch("[a-f0-9]{32}", job["id"]), job
    return {**job, "id": "<job>"}


def offline_jobs(root, binary, events):
    store = fixture(root)
    for book in ("paused", "trashed", "finished"):
        store.upsert_book({"id": book, "name": book, "source": "file"}, [{"title": str(i)} for i in range(3)])
    store.trash_book("trashed")
    finish(store)
    with db(root) as connection:
        rows = [
            (f"history-{i:02}", "finished", "failed" if i == 0 else "completed", 0,
             json.dumps({"name": "finished", "start": 0, "end": 3, "total": 3,
                         "completed": 1 if i == 0 else 3, "error": "synthetic failed" if i == 0 else ""}), i + 1, i + 1)
            for i in range(57)
        ]
        rows += [
            ("paused-job", "paused", "paused", 0,
             json.dumps({"name": "paused", "start": 1, "end": 3, "total": 2,
                         "completed": 1, "error": ""}), 100, 100),
            ("finished-cancelled", "finished", "cancelled", 1,
             json.dumps({"name": "finished", "start": 1, "end": 3, "total": 2,
                         "completed": 2, "error": ""}), 101, 101),
        ]
        connection.executemany("INSERT INTO download_jobs VALUES(?,?,?,?,?,?,?)", rows)
    with server(binary, root, "offline-jobs") as (port, _):
        api = Capture(port, events)
        first = api.request(JOBS)
        assert len(first["jobs"]) == 50 and first["next_offset"] == 50
        assert [job["id"] for job in first["jobs"][:2]] == ["finished-cancelled", "paused-job"]
        assert all(job["captcha"] is None for job in first["jobs"])
        last = api.request(JOBS + "?" + urlencode({"offset": first["next_offset"]}))
        assert len(last["jobs"]) == 9 and last["next_offset"] is None
        assert not {job["id"] for job in first["jobs"]} & {job["id"] for job in last["jobs"]}
        api.request(JOBS + "?offset=-1")
        api.request(JOBS + "?offset=1_0")
        api.request(JOBS + "?offset=no", 400)
        api.request(JOBS + "?offset=9223372036854775808", 500)
        api.post(JOBS + "/paused-job/delete", {}, 403, headers={"Origin": "https://foreign.invalid"})
        api.post(JOBS + "/paused-job/delete", {}, 400)
        api.post(JOBS + "/paused-job/pause", {}, 400)
        api.post(JOBS + "/unknown/delete", {}, 404)
        download = "/api/books/paused/download"
        for data in ({"start": -1}, {"start": 3}, {"count": 0}, {"count": -2}):
            api.post(download, data, 400)
        api.post(download, {"start": " ١ ", "count": "1_0"}, 400)
        api.post("/api/books/trashed/download", {}, 400)
        api.post("/api/books/unknown/download", {}, 404)
        cancelled = api.post(JOBS + "/paused-job/cancel", {})
        assert (cancelled["status"], cancelled["cancelled"], cancelled["completed"], cancelled["captcha"]) == (
            "cancelled", True, 1, None)
        done = api.post(JOBS + "/finished-cancelled/retry", {})
        assert (done["status"], done["cancelled"], done["completed"]) == ("completed", False, 2)
        api.post(JOBS + "/history-00/delete", {})
        api.post(JOBS + "/history-00/delete", {}, 404)
        assert query(root, "SELECT id,status,cancelled FROM download_jobs WHERE id IN ('paused-job','finished-cancelled') ORDER BY id") == [
            ("finished-cancelled", "completed", 0), ("paused-job", "cancelled", 1)
        ]
        assert query(root, "SELECT count(*) FROM download_jobs") == [(58,)]
        assert query(root, "SELECT count(*) FROM chapters WHERE downloaded=1") == [(0,)]
    verify_db(root)
    with server(binary, root, "offline-jobs-restart") as (port, _):
        restarted = Capture(port, events).request(JOBS)
        assert [job["status"] for job in restarted["jobs"][:2]] == ["completed", "cancelled"]


def offline_download(root, binary, events):
    store = fixture(root)
    book = "https://truyenfull.live/synthetic/"
    cdn = "https://cdn.example.org/m5-panel.png"
    url = "/api/books/synthetic"
    chapters = [{"title": f"Part {i}", "url": f"{book}chuong-{i + 1}/"} for i in range(4)]
    pages = {entry["url"]: f'<div class="chapter-c"><p>Synthetic body {i}.</p><img src="{cdn}"></div>'
             for i, entry in enumerate(chapters[:3])}
    store.upsert_book({"id": "synthetic", "name": "Synthetic download", "source": "full", "path": book}, chapters)
    store.progress("synthetic", 0, .625)
    store.save_note({"id": "keep-note", "book_id": "synthetic", "note": "keep"})
    finish(store)
    seed_transport(root, pages, {cdn: PNG})
    job_state = "SELECT status,cancelled,data FROM download_jobs WHERE id=?"
    image_path = "/api/image?" + urlencode({"url": cdn, "referer": book})
    image_name = hashlib.sha256(cdn.encode()).hexdigest()
    with fixture_server(binary, root, "download-start") as (port, _):
        api = Capture(port, events)
        original = api.request(url + "/chapters/0")
        assert original["downloaded"] and original["text"] == "Synthetic body 0."
        assert query(root, "SELECT kind,offline FROM chapter_cache WHERE book_id='synthetic'") == [("cache", 0)]
        created = api.post(url + "/download", {"start": 0, "count": 3}, project=job_result)
        assert (created["start"], created["end"], created["total"], created["status"]) == (0, 3, 3, "queued")
        settled = wait_for(lambda: query(root, job_state, (created["id"],)),
                           lambda rows: bool(rows) and rows[0][0] in ("completed", "failed"), timeout=20)
        status, cancelled, payload = settled[0]
        row = json.loads(payload)
        assert (status, cancelled, row["completed"]) == ("completed", 0, 3), settled
        assert set(row) == {"name", "start", "end", "total", "completed", "error"}
        listed = api.request(JOBS, project=lambda value: {**value, "jobs": [job_result(job) for job in value["jobs"]]})
        assert [job["id"] for job in listed["jobs"]] == [created["id"]]
        assert query(root, "SELECT idx,downloaded FROM chapters WHERE book_id='synthetic' ORDER BY idx") == [
            (0, 1), (1, 1), (2, 1), (3, 0)]
        assert query(root, "SELECT idx,kind,offline FROM chapter_cache ORDER BY idx") == [
            (0, "protected", 1), (1, "protected", 1), (2, "protected", 1)]
        assert query(root, "SELECT idx,name FROM image_pins ORDER BY idx") == [(i, image_name) for i in range(3)]
        assert (root / "images" / image_name).read_bytes() == PNG
        api.post("/api/cache/chapters/clear", {}, project=lambda state: state["max_chapters"])
        api.post("/api/cache/images/clear", {}, project=lambda state: state["max_bytes"])
        api.request(image_path)
    seed_transport(root, {})
    with fixture_server(binary, root, "download-offline-restart") as (port, _):
        api = Capture(port, events)
        for i in range(3):
            assert api.request(url + f"/chapters/{i}")["text"] == f"Synthetic body {i}."
        api.request(image_path)
        assert api.request(url, project=lambda value: value["position"])["position"] == .625
        assert api.request("/api/notes?book_id=synthetic", project=lambda notes: [note["id"] for note in notes])[0]["id"] == "keep-note"
        assert query(root, job_state, (created["id"],)) == settled
    verify_db(root)


def ordinary_image_budget(root, binary, events):
    store = fixture(root)
    cdn = "https://cdn.example.org/m5-ordinary.png"
    book = "https://truyenfull.live/m5-ordinary/"
    store.upsert_book({"id": "image-reader", "name": "Image reader", "source": "full", "path": book},
                      [{"title": "Referenced image", "images": [cdn]}])
    finish(store)
    seed_transport(root, {}, {cdn: PNG})
    path = "/api/image?" + urlencode({"url": cdn, "referer": book})
    name = hashlib.sha256(cdn.encode()).hexdigest()
    cached = root / "images" / name
    cache = "SELECT size,legacy,pin_count FROM image_cache WHERE name=?"
    etag = '"' + hashlib.sha256(PNG).hexdigest() + '"'
    with fixture_server(binary, root, "ordinary-image-budget") as (port, _):
        api = Capture(port, events)
        api.post("/api/cache/images", {"max_bytes": 0}, project=lambda state: state["max_bytes"])
        miss = request(port, path)
        api.record(miss, path, 200, "GET", lambda value: value)
        assert miss["headers"].get("etag") == etag
        head = request(port, path, "HEAD")
        api.record(head, path, 200, "HEAD", lambda value: value)
        assert (head["headers"].get("etag"), head["headers"].get("content-length")) == (etag, str(len(PNG)))
        conditional = request(port, path, headers={"If-None-Match": etag})
        api.record(conditional, path, 304, "GET", lambda value: value)
        assert not conditional["body_hex"] and conditional["headers"].get("etag") == etag
        assert not cached.exists()
        assert query(root, cache, (name,)) == []
        zero = api.request("/api/cache/images")
        assert (zero["cache_bytes"], zero["protected_bytes"], zero["entries"]) == (0, 0, 0)
        api.post("/api/cache/images", {"max_bytes": len(PNG)}, project=lambda state: state["max_bytes"])
        miss = request(port, path)
        api.record(miss, path, 200, "GET", lambda value: value)
        assert miss["headers"].get("etag") == etag
        assert cached.read_bytes() == PNG
        assert query(root, cache, (name,)) == [(len(PNG), 0, 0)]
        positive = api.request("/api/cache/images")
        assert (positive["cache_bytes"], positive["protected_bytes"], positive["entries"]) == (len(PNG), 0, 1)
    verify_db(root)


def same_job_captcha(root, binary, events):
    store = fixture(root)
    origin = "https://sangtacviet.com"
    book = origin + "/truyen/qidian/1/321/"
    challenge_url = book + "902/"
    chapter_route = "/api/books/challenge-book/chapters/1"
    captcha = "/api/sources/stv/captcha"
    sample = PNG[:24]
    answer = "synthetic-background"
    store.upsert_book({"id": "challenge-book", "name": "Background challenge", "source": "stv", "path": book},
                      [{"title": "Cached", "text": "Already cached.", "url": book + "901/"},
                       {"title": "Challenge", "url": challenge_url}])
    store.progress("challenge-book", 0, .42)
    finish(store)
    read_url = origin + "/index.php?bookid=321&h=qidian&c=902&ngmar=readc&sajax=readchapter&sty=1&exts="
    chapter = {"code": "0", "bookid": "321", "bookhost": "qidian", "origin": "https://m.qidian.com/book/321/902.html",
               "data": "<p>Văn bản đã xác minh.</p>"}
    seed_transport(root, {}, stv=[
        {"url": origin + "/index.php?ngmar=chapterlist&h=qidian&bookid=321&sajax=getchapterlist",
         "text": json.dumps({"code": 1, "data": "1-/-901-/-Cached-//-1-/-902-/-Challenge"}), "repeat": True},
        {"url": challenge_url, "text": "<div>initialized</div>", "repeat": True},
        {"url": read_url, "method": "POST", "request_body": "", "text": '{"code":"21"}'},
        {"url": read_url, "method": "POST", "request_body": "", "text": json.dumps(chapter, ensure_ascii=False)},
        {"url": origin + "/generate_captcha.php", "headers": [["Content-Type", "image/png"]],
         "body_base64": base64.b64encode(sample).decode()},
        {"url": origin + "/index.php?ngmar=verifyca", "method": "POST", "text": "success",
         "request_body": urlencode({"ajax": "verifycaptcha", "token": answer, "purpose": "read", "provider": "sangtacviet"})},
    ])
    state = "SELECT status,cancelled,data FROM download_jobs WHERE id=?"

    def normalize(value):
        job, = value["jobs"]
        captured = job_result(job)
        if captured["captcha"] is not None:
            assert re.fullmatch("[a-f0-9]{32}", captured["captcha"]["id"])
            captured["captcha"] = {**captured["captcha"], "id": "<challenge>"}
        return {**value, "jobs": [captured]}

    with fixture_server(binary, root, "same-job-captcha") as (port, _):
        api = Capture(port, events)
        queued = api.post("/api/books/challenge-book/download", {"start": 0, "count": 2}, project=job_result)
        identity = queued["id"]
        pending = wait_for(lambda: query(root, state, (identity,)),
                           lambda rows: bool(rows) and rows[0][0] == "verification_required")
        raw = pending[0][2]
        assert json.loads(raw)["completed"] == 1
        listing = api.request(JOBS, project=normalize)
        waiting, = listing["jobs"]
        assert waiting["id"] == identity and waiting["completed"] == 1 and waiting["status"] == "verification_required"
        generated = waiting["captcha"]["id"]
        assert generated not in raw and "captcha" not in json.loads(raw) and answer not in raw
        assert query(root, "SELECT downloaded FROM chapters WHERE book_id='challenge-book' ORDER BY idx") == [(1,), (0,)]
        fetched = request(port, captcha + "?" + urlencode({"id": generated}))
        image = api.record(fetched, captcha + "?id=<challenge>", 200, "GET",
                           lambda value: {**value, "id": "<challenge>"})
        assert image == {"id": generated, "image": "data:image/png;base64," + base64.b64encode(sample).decode()}
        assert api.post(captcha + "/verify", {"id": generated, "answer": " " + answer + " "}) == {"verified": True}
        finished = wait_for(lambda: query(root, state, (identity,)),
                            lambda rows: bool(rows) and rows[0][0] == "completed")
        assert finished[0][:2] == ("completed", 0)
        assert json.loads(finished[0][2])["completed"] == 2
        after = api.request(JOBS, project=normalize)["jobs"]
        assert [job["id"] for job in after] == [identity] and after[0]["captcha"] is None
        api.post(captcha + "/verify", {"id": generated, "answer": answer}, 400)
        actual = api.request(chapter_route)
        assert (actual["text"], actual["language"], actual["translation"]) == ("Văn bản đã xác minh.", "vi", "source")
        assert query(root, "SELECT position FROM books WHERE id='challenge-book'") == [(.42,)]
        assert query(root, "SELECT idx,kind,offline FROM chapter_cache ORDER BY idx") == [(0, "protected", 1), (1, "protected", 1)]
        assert query(root, "SELECT count(*) FROM download_jobs") == [(1,)]
    verify_db(root)


if __name__ == "__main__":
    run("m5-download", {"offline_jobs": offline_jobs, "offline_download": offline_download,
                        "ordinary_image_budget": ordinary_image_budget, "same_job_captcha": same_job_captcha})
