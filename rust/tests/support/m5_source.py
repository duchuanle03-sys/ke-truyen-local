"""Public source API contracts using offline, disposable fixture servers."""
import base64
from contextlib import closing
import hashlib
import json
import re
from urllib.parse import urlencode

from m2_capture import PNG, m1, request
from m3_support import Capture, finish, fixture, query, run, server
from m5_support import fixture_server, seed_transport
from qt import QtDictionary


SOURCES = "/api/sources"


def url(path, **fields):
    return path + "?" + urlencode(fields)


def rejected_sources(root, binary, events):
    finish(fixture(root))
    with server(binary, root, "offline-source-guards") as (port, _):
        api = Capture(port, events)
        expected = ["qq", "full", "hako", "stv"]
        source = api.request(SOURCES)
        assert [item["id"] for item in source] == expected
        api.request(SOURCES, method="HEAD")
        for domain in ("http://127.0.0.1/", "file:///not-a-source", "https://example.invalid/",
                       "https://truyenqqko.com@127.0.0.1/", "//127.0.0.1/"):
            result = api.request(url(SOURCES + "/qq/detail", url=domain), 400)
            assert result["code"] == "source_error"
        for path in ("https://sangtacviet.vip/not-book", "https://sangtacviet.com/truyen/qidian/1/17/?q=other",
                     "https://sangtacviet.com/truyen/qidian/2/17/", "https://sangtacviet.com/truyen/qidian/1/17/word/"):
            result = api.request(url(SOURCES + "/stv/detail", url=path), 400)
            assert result["code"] == "source_error"
        for name, q, page in (("qq", "", 1), ("full", "x", -1), ("hako", "x", 0),
                              ("full", "x", 10_001), ("hako", "雪" * 501, 1), ("unknown", "x", 1)):
            result = api.request(url(f"{SOURCES}/{name}/search", q=q, page=page), 400)
            assert result["code"] == "source_error"
        api.request(url(SOURCES + "/full/search", q="book", page="invalid"), 400)
        api.post(SOURCES + "/full/add", {"url": "/test/"}, 403, headers={"Origin": "https://foreign.invalid"})
        api.post(SOURCES + "/full/add", {"url": "https://example.invalid/book/"}, 400)
        api.request(url(SOURCES + "/stv/captcha", id="not-issued"), 400)
        api.post(SOURCES + "/stv/captcha/verify", {"id": "not-issued", "answer": "test"}, 403,
                 headers={"Origin": "https://foreign.invalid"})
        for name in ("verify", "refresh", "cancel"):
            api.post(f"{SOURCES}/stv/captcha/{name}", {"id": "not-issued", "answer": "test"}, 400)
        assert query(root, "SELECT id FROM books") == []


def recorded_sources(root, binary, events):
    store = fixture(root)
    source_dir = m1.ROOT / "tests" / "fixtures"
    recorded = {path.stem.removeprefix("source_"): json.loads(path.read_text(encoding="utf-8"))
                for path in source_dir.glob("source_*.json")}
    pages = {saved["url"]: saved["html"] for saved in recorded.values() if "html" in saved}
    pages.update({page["url"]: page["body"] for page in recorded["full_toc"]["pages"]})
    names = {"qq": ("xu so", 62), "full": ("cau ma", 1470), "hako": ("mua xuan", 6)}
    mirrors = {"qq": ("truyenqqko.com", "truyenqqgo.com"), "full": ("truyenfull.live", "truyenfull.vision"),
               "hako": ("docln.sbs", "ln.hako.vn")}
    for source in names:
        chapter = recorded[source + "_chapter"]["url"]
        store.upsert_book({"id": f"{source}-sample", "name": "Synthetic reader", "source": source,
                           "path": recorded[source + "_detail"]["url"]},
                          [{"title": "Fixture chapter", "url": chapter}])
    finish(store)
    seed_transport(root, pages)

    def durable_book(book):
        return {key: value for key, value in book.items() if key not in ("added_at", "updated_at", "read_at")}

    def detail_contract(value):
        chapters = value["chapters"]
        encoded = json.dumps(chapters, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        return {**value, "chapters": {"count": len(chapters), "sha256": hashlib.sha256(encoded).hexdigest()}}

    with fixture_server(binary, root, "recorded-adapters") as (port, _):
        api = Capture(port, events)
        for source, (search, count) in names.items():
            source_path = SOURCES + "/" + source
            result = api.request(url(source_path + "/search", q=search))
            assert result["items"] and all(item["path"].startswith("https://") for item in result["items"])
            detail = recorded[source + "_detail"]["url"].replace(*mirrors[source]) + (".html" if source == "qq" else "")
            title = api.request(url(source_path + "/detail", url=detail), project=detail_contract)
            assert len(title["chapters"]) == count
            assert [item["index"] for item in title["chapters"]] == list(range(count))
            added = api.post(source_path + "/add", {"url": detail}, project=durable_book)
            assert added["chapter_count"] == count and added["source"] == source
            if source == "full":
                book = "/api/books/" + added["id"]
                api.post(book + "/progress", {"index": 12, "position": .625}, project=durable_book)
                api.post(book + "/trash", {}, project=lambda value: value["id"])
                restored = api.post(source_path + "/add", {"url": detail}, project=durable_book)
                assert restored["id"] == added["id"]
                assert (restored["chapter_index"], restored["position"], restored["trashed_at"]) == (12, .625, 0)
            chapter = f"/api/books/{source}-sample/chapters/0"
            fetched = api.request(chapter)
            assert fetched["downloaded"]
            if source == "qq":
                assert len(fetched["images"]) == 2 and all(path.startswith("/api/image?") for path in fetched["images"])
            else:
                assert len(fetched["text"]) > 10
            assert api.request(chapter) == fetched
            assert query(root, "SELECT downloaded,revision FROM chapters WHERE book_id=? AND idx=0", (f"{source}-sample",)) == [(1, 1)]
        volume = recorded["hako_volume"]["url"]
        assert len(api.request(url(SOURCES + "/hako/detail", url=volume), project=detail_contract)["chapters"]) == 6


def scripted_stv(root, binary, events):
    store = fixture(root)
    origin = "https://sangtacviet.com"
    book = origin + "/truyen/qidian/1/123/"
    path = "/api/books/stv-sample"
    toc = [{"title": f"Chương {i}", "url": book + f"{901 + i}/"} for i in range(4)]
    toc[0]["text"] = "Đã lưu trước."
    store.upsert_book({"id": "stv-sample", "name": "Thử nguồn", "source": "stv", "path": book}, toc)
    store.progress("stv-sample", 0, .62)
    finish(store)
    chinese = "天山江林中文"
    with closing(QtDictionary(root)) as qt:
        qt.save("name", chinese, "Lựa chọn QT cục bộ")
    source_data = "-//-".join(f"1-/-{901 + i}-/-Chương {i}" for i in range(4))
    toc_url = origin + "/index.php?ngmar=chapterlist&h=qidian&bookid=123&sajax=getchapterlist"

    def read_url(cid):
        return origin + f"/index.php?bookid=123&h=qidian&c={cid}&ngmar=readc&sajax=readchapter&sty=1&exts="

    def response(address, value, **options):
        return {"url": address, "text": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value, **options}

    def chapter_payload(cid, text):
        return {"code": "0", "bookid": "123", "bookhost": "qidian",
                "origin": f"https://m.qidian.com/book/123/{cid}.html", "data": f"<p>{text}</p>"}

    read = {"method": "POST", "request_body": ""}
    captcha_url = origin + "/generate_captcha.php"
    pngs = [PNG[:24], PNG[:24] + b"refresh"]
    good_answer = "synthetic-manual"
    replies = [
        response(toc_url, {"code": 1, "data": source_data, "oridata": source_data}, repeat=True),
        response(book + "902/", '<script src="/stv.readinit.js"></script>',
                 headers=[["Set-Cookie", "_ac=synthetic-seed; Path=/"]]),
        response(book + "902/", "<div>initialized</div>"),
        response(read_url(902), {"code": "7"}, method="POST", request_body="synthetic-seed"),
        response(read_url(902), {"code": "21"}, **read),
        response(read_url(902), chapter_payload(902, chinese), **read),
        *[{"url": captcha_url, "body_base64": base64.b64encode(image).decode(), "headers": [["Content-Type", "image/png"]]}
          for image in pngs],
        response(origin + "/index.php?ngmar=verifyca", "success", method="POST",
                 request_body=urlencode({"ajax": "verifycaptcha", "token": good_answer, "purpose": "read", "provider": "sangtacviet"})),
        response(origin + "/index.php?ngmar=trans&langhint=chinese", "<p>Văn bản dịch từ nguồn.</p>", method="POST",
                 request_body=urlencode({"ajax": "trans", "content": chinese})),
        response(book + "903/", "<div>initialized</div>"),
        response(read_url(903), {"code": "5", "err": "Chương VIP. Bạn chưa mua chương này."}, **read),
        response(book + "904/", "<div>initialized</div>"),
        response(read_url(904), chapter_payload(905, "Wrong chapter"), **read),
    ]
    seed_transport(root, {}, stv=replies)
    captcha = SOURCES + "/stv/captcha"

    def replace_id(body):
        assert re.fullmatch("[a-f0-9]{32}", body["id"]), body
        return {**body, "id": "<challenge>"}

    with fixture_server(binary, root, "scripted-stv") as (port, _):
        api = Capture(port, events)
        challenge = path + "/chapters/1"
        project = lambda value: {**value, "captcha": replace_id(value["captcha"])}
        issued = api.request(challenge, 409, project=project)
        assert issued["code"] == "captcha_required" and "access" not in issued
        original = issued["captcha"]["id"]
        assert api.request(challenge, 409, project=project)["captcha"]["id"] == original
        assert query(root, "SELECT downloaded FROM chapters WHERE book_id='stv-sample' AND idx=1") == [(0,)]
        api.post(captcha + "/verify", {"id": original, "answer": good_answer}, 400)
        image = api.record(request(port, url(captcha, id=original)), captcha + "?id=<challenge>", 200, "GET", replace_id)
        assert image["image"] == "data:image/png;base64," + base64.b64encode(pngs[0]).decode()
        refreshed = api.post(captcha + "/refresh", {"id": original}, project=replace_id)
        assert refreshed["id"] != original
        assert refreshed["image"] == "data:image/png;base64," + base64.b64encode(pngs[1]).decode()
        api.post(captcha + "/verify", {"id": original, "answer": good_answer}, 400)
        assert api.post(captcha + "/verify", {"id": refreshed["id"], "answer": " " + good_answer + " "}) == {"verified": True}
        translated = api.request(challenge)
        assert (translated["text"], translated["language"], translated["translation"]) == ("Văn bản dịch từ nguồn.", "vi", "source")
        assert api.request(challenge) == translated
        vip = api.request(path + "/chapters/2", 403)
        assert vip["code"] == "vip_locked" and vip["access"]["vip"]
        wrong = api.request(path + "/chapters/3", 400)
        assert wrong["code"] == "parse_error" and "access" not in wrong
        api.post(path + "/progress", {"index": 2, "position": .5}, 400)
        assert api.request(path, project=lambda value: value["position"])["position"] == .62
        assert query(root, "SELECT idx,downloaded FROM chapters WHERE book_id='stv-sample' ORDER BY idx") == [
            (0, 1), (1, 1), (2, 0), (3, 0)]
        assert query(root, "SELECT count(*) FROM download_jobs") == [(0,)]
        stored = " ".join(raw for raw, in query(root, "SELECT raw FROM chapters WHERE book_id='stv-sample'"))
        assert original not in stored and refreshed["id"] not in stored and good_answer not in stored


if __name__ == "__main__":
    run("m5-source", {"rejected_sources": rejected_sources, "recorded_sources": recorded_sources,
                      "scripted_stv": scripted_stv})
