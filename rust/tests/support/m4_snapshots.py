"""QT snapshot consistency through actual HTTP reads, failures and external commits."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import sqlite3
import threading

from m2_capture import request
from m3_support import Capture, finish, fixture, run, server
from m4_contract import DICTIONARY, TRANSLATE, meaning, route, translate


IMPORT = DICTIONARY + "/import"
CHAPTER = "甲乙|" * 3_000


@contextmanager
def dictionary_db(root):
    with closing(sqlite3.connect(root / "qt.sqlite3", timeout=5)) as db, db:
        yield db


def fixture_qt(root, binary, label):
    finish(fixture(root))
    return server(binary, root, label)


def sqlite_failures(root, binary, events):
    with fixture_qt(root, binary, "save-failures") as (port, _):
        api = Capture(port, events)
        meaning(api, "vp", "甲", "old")
        meaning(api, "vp", "乙", "pair")
        translate(api, "甲乙", "old pair", "book")
        with dictionary_db(root) as db:
            db.execute("CREATE TRIGGER m4_reject_replace BEFORE INSERT ON entries WHEN NEW.word='丙' "
                       "BEGIN SELECT RAISE(ABORT,'synthetic QT rollback'); END")
        api.post(IMPORT, {"kind": "vp", "mode": "replace", "text": "甲=replacement\n丙=fail"}, 500)
        translate(api, "甲乙", "old pair", "book")
        with dictionary_db(root) as db:
            assert db.execute("SELECT word,meaning FROM entries ORDER BY word").fetchall() == [("乙", "pair"), ("甲", "old")]
            assert db.execute("SELECT count(*) FROM backups").fetchone() == (0,)
            db.execute("DROP TRIGGER m4_reject_replace")
            db.execute("CREATE TRIGGER m4_fail_build AFTER UPDATE ON entries WHEN NEW.kind='vietphrase' AND NEW.word='甲' "
                       "BEGIN INSERT INTO entries VALUES('rules','{bad}','invalid','*'); END")
        translate(api, "甲乙", "old pair", "book")
        api.post(IMPORT, {"kind": "vp", "text": "甲=committed"}, 400)
        api.post(TRANSLATE, {"text": "甲乙", "book_id": "book"}, 400)
        with dictionary_db(root) as db:
            assert db.execute("SELECT meaning FROM entries WHERE kind='vietphrase' AND word='甲'").fetchone() == ("committed",)
            db.execute("DROP TRIGGER m4_fail_build")
            db.execute("DELETE FROM entries WHERE kind='rules'")
        translate(api, "甲乙", "committed pair", "book")
        with dictionary_db(root) as db:
            db.execute("UPDATE entries SET meaning='external' WHERE kind='vietphrase' AND word='甲'")
        translate(api, "甲乙", "external pair", "book")
    with dictionary_db(root) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def warm_while_writing(root, binary, events):
    with fixture_qt(root, binary, "blocked-writer") as (port, _):
        api = Capture(port, events)
        meaning(api, "vp", "甲", "before")
        translate(api, "甲", "before", "book")
        entered = threading.Event()

        def import_waiting():
            entered.set()
            return request(port, IMPORT, "POST", {"kind": "vp", "text": "甲=after"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            with dictionary_db(root) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                write = pool.submit(import_waiting)
                assert entered.wait(2)
                value = pool.submit(request, port, TRANSLATE, "POST", {"text": "甲", "book_id": "book"}).result(2)
                assert value["status"] == 200 and value["json"] == {"text": "before"}
                assert not write.done(), "write bypassed the locked dictionary"
            response = write.result(5)
        api.record(response, IMPORT, 200, "POST", lambda value: value)
        translate(api, "甲", "after", "book")


def overlapping_generations(root, binary, events):
    with fixture_qt(root, binary, "publish") as (port, _):
        api = Capture(port, events)
        api.post(IMPORT, {"kind": "vp", "text": "甲=one\n乙=pair"})
        translate(api, "甲乙", "one pair", "book")
        start = threading.Barrier(3)
        done = threading.Event()
        expected = {"one pair", "two pair", "rule pair", "private pair"}

        def reader():
            start.wait(5)
            count = 0
            while not done.is_set():
                response = request(port, TRANSLATE, "POST", {"text": CHAPTER, "book_id": "book"})
                assert response["status"] == 200, response["json"]
                chunks = response["json"]["text"].split("|")
                assert chunks[-1] == "" and len(chunks) == 3001
                rendered = set(chunks[:-1])
                assert len(rendered) == 1 and rendered <= expected, rendered
                count += 1
            return count

        with ThreadPoolExecutor(max_workers=2) as pool:
            readers = [pool.submit(reader) for _ in range(2)]
            try:
                start.wait(5)
                for _ in range(5):
                    api.post(IMPORT, {"kind": "vp", "mode": "replace", "text": "甲=two\n乙=pair"})
                    meaning(api, "name", "甲", "private", "book")
                    meaning(api, "rules", "甲乙", "rule pair", "book")
                    api.request(route(kind="rules", scope="book", word="甲乙"), method="DELETE")
                    api.request(route(kind="name", scope="book", word="甲"), method="DELETE")
                    with dictionary_db(root) as db:
                        db.execute("UPDATE entries SET meaning='one' WHERE scope='*' AND kind='vietphrase' AND word='甲'")
                    translate(api, "甲乙", "one pair", "book")
            finally:
                done.set()
            samples = [sample.result(10) for sample in readers]
        assert min(samples) > 0
        events.append({"observation": "whole-chapter reads during writes", "samples": samples})


if __name__ == "__main__":
    run("qt-snapshots", {"sqlite_failures": sqlite_failures, "warm_while_writing": warm_while_writing,
                         "overlapping_generations": overlapping_generations})
