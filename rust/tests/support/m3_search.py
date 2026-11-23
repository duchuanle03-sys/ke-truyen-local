"""Search contract, scope, bounded pages, cursor restart and transactional rebuild."""
from urllib.parse import urlencode

from m3_support import Capture, db, finish, fixture, query, run, server, verify_db, wait_for


def search(book, term, **params):
    return f"/api/books/{book}/search?" + urlencode({"q": term, **params})


def literals(root, binary, events):
    store = fixture(root)
    bodies = (
        "Một ngày CẦU MA bước vào phòng.", "Cá nhân đứng trước phố.",
        'Straße and STRASSE; a*b? "quote" OR NEAR(x) literal.', "林风走进房间。",
        "ab\x00c", "abc", "straße 🙂 e\u0301 é", "ß" * 300 + "needle at the end",
        "İ ı Σσς ﬃ ẞ", "100%_ persisted",
    )
    store.upsert_book({"id": "scope", "name": "Scope"}, [
        {"title": str(i), "text": body} for i, body in enumerate(bodies + ("Repeat needle",) * 205 + ("",))
    ])
    store.upsert_book({"id": "neighbor", "name": "Not in scope"}, [{"text": 'quote "needle" abc Straße CẦU MA'}])
    finish(store)
    with server(binary, root, "literal") as (port, _):
        api = Capture(port, events)
        for term, indices in (
            ("cầu ma", [0]), ("gày CẦ", [0]), ("ma", [0]), ("cau ma", []), ("nhân", [1]),
            ("STRASSE", [2, 6]), ("a*b?", [2]), ('"quote" OR NEAR(x)', [2]), ("风走进", [3]),
            ("林", [3]), ("abc", [5]), ("ab", [4, 5]), ("🙂", [6]), ("é", [6]), ("e\u0301", [6]),
            ("σσσ", [8]), ("FFI", [8]), ("İ", [8]), ("%_", [9]), ("unknown", []),
        ):
            result = api.request(search("scope", term))
            assert [hit["index"] for hit in result["results"]] == indices, (term, result)
            assert (result["searched"], result["total"], result["indexing"]) == (215, 216, False)
        for after, first, count, cursor in [(-1, 7, 100, 108), (108, 109, 100, 208), (208, 209, 6, None)]:
            page = api.request(search("scope", "needle", after=after))
            assert (page["results"][0]["index"], len(page["results"]), page["next_index"]) == (first, count, cursor)
            if after == -1:
                assert page["results"][0]["snippet"] == "ß" * 70 + "needle at the end"
        api.request("/api/books/scope/search?q=&q=STRASSE")
        api.request(search("scope", "abc"), method="HEAD", project=lambda _: None)
        for term, params in [("", {}), (" ", {}), ("\x00", {}), ("雪" * 501, {}), ("abc", {"after": -2}),
                             ("abc", {"after": 2 ** 63})]:
            api.request(search("scope", term, **params), 400)
        api.request(search("missing", "abc"), 404)
    verify_db(root)


def resume(root, binary, events):
    store = fixture(root)
    chapters = [{"text": "Early needle"}] + [{"title": "Metadata"}] * 598 + [{"text": "Late needle"}]
    store.upsert_book({"id": "scope", "name": "Scope"}, chapters)
    finish(store)
    cursor = "SELECT idx,complete FROM chapter_search_state"
    snapshot = "SELECT book_id,idx,text FROM chapter_search_docs ORDER BY idx"
    with server(binary, root, "before-crash") as (port, owner):
        with db(root) as connection:
            connection.execute("DELETE FROM chapter_search_docs")
            connection.execute("UPDATE chapter_search_state SET book_id='',idx=-1,complete=0")
            connection.execute(
                "CREATE TRIGGER m3_pause_search_cursor BEFORE UPDATE OF idx ON chapter_search_state "
                "WHEN NEW.idx>255 BEGIN SELECT RAISE(ABORT,'synthetic search cursor stop'); END"
            )
        assert wait_for(lambda: query(root, snapshot), lambda rows: len(rows) == 1) == [("scope", 0, "early needle")]
        assert query(root, cursor) == [(255, 0)]
        partial = Capture(port, events).request(search("scope", "needle"))
        assert (partial["searched"], partial["total"], partial["indexing"]) == (1, 600, True)
        owner.kill()
        owner.wait(timeout=10)
    assert query(root, cursor) == [(255, 0)]
    with db(root) as connection:
        connection.execute("DROP TRIGGER m3_pause_search_cursor")
        connection.execute("UPDATE chapters SET text='Revised needle' WHERE book_id='scope' AND idx=0")
    with server(binary, root, "restarted") as (port, _):
        wait_for(lambda: query(root, snapshot), lambda rows: rows == [("scope", 0, "revised needle"),
                                                                      ("scope", 599, "late needle")])
        assert query(root, cursor) == [(599, 1)]
        Capture(port, events).request(search("scope", "needle"))
    verify_db(root)


def stale(root, binary, events):
    store = fixture(root)
    store.upsert_book({"id": "scope", "name": "Scope"}, [{"text": "Old needle"}, {"text": "Other needle"}])
    finish(store)
    with server(binary, root, "rollback") as (port, _):
        log = root.parent / f"{root.name}-rollback.log"
        before = log.stat().st_size
        with db(root) as connection:
            connection.execute(
                "CREATE TRIGGER m3_fail_search_doc BEFORE UPDATE ON chapter_search_docs "
                "WHEN NEW.idx=1 BEGIN SELECT RAISE(ABORT,'synthetic index failure'); END"
            )
            connection.execute("UPDATE chapters SET text='Updated needle ' || idx WHERE book_id='scope'")
        wait_for(lambda: log.stat().st_size, lambda size: size > before)
        api = Capture(port, events)
        hidden = api.request(search("scope", "needle"))
        assert (hidden["results"], hidden["searched"], hidden["indexing"]) == ([], 0, True)
        pending = "SELECT id,idx FROM chapter_search_pending ORDER BY id"
        assert [idx for _, idx in query(root, pending)] == [0, 1]
        assert query(root, "SELECT text FROM chapter_search_docs ORDER BY idx") == [("old needle",), ("other needle",)]
        with db(root) as connection:
            connection.execute("UPDATE chapters SET text='Latest needle' WHERE book_id='scope' AND idx=0")
            connection.execute("DROP TRIGGER m3_fail_search_doc")
        wait_for(lambda: query(root, pending), lambda rows: not rows)
        indexed = api.request(search("scope", "needle"))
        assert [hit["snippet"] for hit in indexed["results"]] == ["Latest needle", "Updated needle 1"]
    verify_db(root)


if __name__ == "__main__":
    run("search", {"literals": literals, "resume": resume, "stale": stale})
