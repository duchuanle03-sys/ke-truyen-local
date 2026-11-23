"""Differential QT wire contracts on disposable dictionary and library fixtures."""
from contextlib import closing
import hashlib
import json
import sqlite3
import struct
import subprocess
from urllib.parse import urlencode
import zipfile

from m2_capture import m1, request
from m3_support import Capture, command, finish, fixture, run, server, stop
from qt import QtDictionary
from qt_pack import PackError, install_pack


DICTIONARY = "/api/dictionaries"
TRANSLATE = "/api/qt"
PREVIEW = DICTIONARY + "/rules/preview"


def route(path=DICTIONARY, **query):
    return path + "?" + urlencode(query)


def meaning(api, kind, word, value, scope="*"):
    return api.post(DICTIONARY, {"kind": kind, "word": word, "meaning": value, "scope": scope})


def translate(api, text, expected, scope=""):
    result = api.post(TRANSLATE, {"text": text, "book_id": scope})
    assert result == {"text": expected}, (result, expected)


def priority(root, binary, events):
    finish(fixture(root))
    with server(binary, root, "priority") as (port, _):
        api = Capture(port, events)
        initial = api.request("/api/capabilities", project=lambda value: value["qt"])["qt"]
        assert initial["installed"] is False and initial["counts"] == []
        for character in ("中", "𠀀", "\U000323af"):
            api.post(TRANSLATE, {"text": character}, 400)
        translate(api, "Tiếng Việt\n𝟙 🙂 \U000323b0", "Tiếng Việt\n𝟙 🙂 \U000323b0")
        for entry in (
            ("names", "刘备", "Lưu Bị"),
            ("vp", "刘备去了", "wrong"),
            ("vp", "去", "đi"),
            ("vp", "去了", "đã đi¦đi rồi"),
            ("pronoun", "他", "anh ấy"),
            ("han-viet", "山", "sơn"),
            ("hanviet", "川", "xuyên"),
            ("hanviet", "刘", "Riêng", "story"),
            ("pronouns", "备", "Bị", "story"),
            ("name", "𠀀", "ngoài BMP"),
            ("name", "甲", "A"),
            ("vp", "字", "¦giữ phương án"),
        ):
            meaning(api, *entry)
        translate(api, "刘备去了山川。", "Lưu Bị đã đi sơn xuyên。")
        translate(api, "刘备去了", "Riêng Bị đã đi", "story")
        translate(api, "刘备去了", "Lưu Bị đã đi", "unrelated")
        translate(api, "他𠀀。", "anh ấy ngoài BMP。")
        translate(api, "0甲𝟙|xͅ甲|甲²|x字甲", "0 A 𝟙|xͅA|A ²|x A")
        api.request(route(kind="hanviet", word="刘", scope="story"), method="DELETE")
        translate(api, "刘备", "Lưu Bị", "story")
        status = api.request("/api/capabilities", project=lambda value: value["qt"])["qt"]
        assert status["installed"] is True and status["backups"] == 0
        with closing(sqlite3.connect(root / "qt.sqlite3")) as db:
            assert db.execute("SELECT meaning FROM entries WHERE kind='vietphrase' AND word='去了'").fetchone() == ("đã đi¦đi rồi",)


def grammar(root, binary, events):
    finish(fixture(root))
    with server(binary, root, "grammar") as (port, _):
        api = Capture(port, events)
        meaning(api, "vp", "苹果", "táo")
        meaning(api, "rule", "3个苹果和|", "global longer")
        meaning(api, "rules", "{n}个{word}", "{n} quả {lookup:word}", "story")
        meaning(api, "rule", "第{n}", "literal {n}")
        meaning(api, "rule", "{word}3", "universal")
        translate(api, "第3", "literal 3")
        translate(api, "3个苹果和|", "global longer")
        translate(api, "3个苹果和|", "3 quả 苹果和|", "story")
        translate(api, "3个苹果。", "3 quả táo。", "story")
        sample = "𠀀🙂3.5个苹果。—1,25个梨。\n１２个苹果。"
        preview = api.post(PREVIEW, {"pattern": "{n}个{word}", "replacement": "{n} quả {lookup:word}",
                                     "sample": sample})
        assert preview["matches"] == [
            {"start": 2, "end": 8, "source": "3.5个苹果", "translation": "3.5 quả táo"},
            {"start": 10, "end": 16, "source": "1,25个梨", "translation": "1,25 quả 梨"},
        ]
        for pattern, replacement, sample in (
            ("{n1}年{n2}月", "tháng {n2}, năm {n1}", "2026年11月"),
            (" {n}个{word} ", " {n}={lookup:word} ", " 10个苹果 "),
            ("{word}", "{lookup:word}", "𠀀🙂苹果𠀀"),
            ("{n}", "N{n}", "1234567890123.1234567 ＋９"),
            ("{word}", "{word}", "甲" * 33),
        ):
            api.post(PREVIEW, {"pattern": pattern, "replacement": replacement, "sample": sample})
        malformed = (
            ("{word1}甲{word2}", "{word1}"), ("{n1},{n2}", "{n1}"), ("{n1}{n2}", "{n2}"),
            ("{n}:{n}", "{n}"), ("{n9}", "?"), ("{word0}", "?"), ("{word}", "{lookup:n}"),
            ("{n}", "{lookup:n}"), ("{word}", "{{word}}"), ("甲", "rule" + "x" * 2048),
        )
        for pattern, replacement in malformed:
            invalid = api.post(PREVIEW, {"pattern": pattern, "replacement": replacement, "sample": "甲123"})
            assert not invalid["valid"] and not invalid["matches"]
            api.post(DICTIONARY, {"kind": "rule", "word": pattern, "meaning": replacement}, 400)
        meaning(api, "vp", "梨", "lê riêng", "1.0e+20")
        typed = {"pattern": "{word}", "replacement": "{lookup:word}", "sample": "梨"}
        assert api.post(PREVIEW, {**typed, "book_id": 1e20})["output"] == "lê riêng"
        api.post(PREVIEW, {**typed, "book_id": 2**63}, 500)
        meaning(api, "vp", "梨", "lê vô hạn", "Inf")
        raw = ('{"book_id":1e400,' + json.dumps(typed, ensure_ascii=False)[1:]).encode("utf-8")
        response = request(port, PREVIEW, "POST", raw=raw)
        infinite = api.record(response, PREVIEW, 200, "POST", lambda value: value)
        assert infinite["output"] == "lê vô hạn"
        invalid = api.post(PREVIEW, {**typed, "pattern": "{x}", "book_id": ["nonempty"]})
        assert not invalid["valid"] and invalid["output"] == "梨" and invalid["matches"] == []
        api.post(PREVIEW, {"pattern": "甲", "replacement": "A", "sample": "x" * 100001}, 400)
        api.post(TRANSLATE, {"text": "x" * 2_000_001}, 400)


def storage(root, binary, events):
    finish(fixture(root))
    scope = "私 ①"
    with server(binary, root, "storage") as (port, _):
        api = Capture(port, events)
        for word, value in (("一", "Một"), ("二", "Straße"), ("三", "%_"), ("四", "İ i\u0307 Σς")):
            meaning(api, "vp", word, value)
        for params in (
            {"limit": "٢"}, {"offset": 2, "limit": "1_0"}, {"offset": 99, "limit": 1},
            {"q": "STRASSE", "limit": 1}, {"q": "%_", "limit": 500}, {"q": "i\u0307 σσ", "limit": 100},
        ):
            api.request(route(kind="vp", **params))
        api.request(route(kind="vp", q="straße"))
        for params in ({"limit": 0}, {"offset": -1}, {"limit": 501}, {"scope": "\t"}, {"kind": "other"}):
            api.request(route(**params), 400)
        api.request(route(offset=2**63, limit=1), 500)
        api.request(route(offset=2**63, limit=501), 400)
        api.request(route(offset=-(2**63)-1, limit=1), 400)
        api.post(DICTIONARY, {"kind": "vp", "scope": "", "word": "x", "meaning": "value"}, 400)
        api.post(DICTIONARY, {"kind": "vp", "word": "x=x", "meaning": "value"}, 400)
        api.post(DICTIONARY, {"kind": "vp", "word": "x", "meaning": "a\nline"}, 400)
        import_path = DICTIONARY + "/import"
        lines = "\ufeff\ufeff# synthetic test\r\n一=Trước¦khác\u2028二\tTrước\u2029bad line\n一=Sau=giữ¦phụ"
        report = api.post(import_path, {"kind": "vp", "text": lines, "scope": scope})
        assert (report["inserted"], report["skipped"], report["errors"][0]["line"]) == (2, 1, 4)
        text = api.request(route(DICTIONARY + "/export", kind="vp", scope=scope))["text"]
        assert text == '# qt-dictionary-v1 {"kind": "vietphrase", "scope": "私 ①"}\n一=Sau=giữ¦phụ\n二=Trước\n'
        assert api.post(import_path, {"kind": "vp", "text": text, "mode": "missing"})["skipped"] == 2
        api.post(import_path, {"kind": "vp", "text": text, "scope": "other"}, 400)
        api.post(import_path, {"kind": "name", "text": text}, 400)
        api.post(import_path, {"kind": "vp", "text": '# qt-dictionary-v1 {"kind": 3}', "mode": "merge"}, 400)
        empty = api.post(import_path, {"kind": "vp", "text": "# only comment", "mode": "replace", "scope": scope})
        invalid = api.post(import_path, {"kind": "vp", "text": "三=ba\ninvalid", "mode": "replace", "scope": scope})
        assert not empty["applied"] and not invalid["applied"]
        translated = api.post(import_path, {"kind": "vp", "text": "三=ba", "mode": "replace", "scope": scope})
        assert translated["backup_id"] == 1 and translated["applied"]
        api.request(route(DICTIONARY + "/export", kind="vp", scope=scope))
        translate(api, "一三", "Một ba", scope)
        result = api.post(TRANSLATE, {"text": "a" * 2_000_000},
                          project=lambda value: hashlib.sha256(value["text"].encode()).hexdigest())
        assert len(result["text"]) == 2_000_000
    with closing(sqlite3.connect(root / "qt.sqlite3")) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        kind, saved_scope, entries = db.execute("SELECT kind,scope,entries FROM backups WHERE id=1").fetchone()
        assert (kind, saved_scope) == ("vietphrase", scope)
        assert [(item["word"], item["meaning"]) for item in json.loads(entries)] == [("一", "Sau=giữ¦phụ"), ("二", "Trước")]
    reference = QtDictionary(root)
    try:
        assert reference.restore_backup(1) == {"restored": 2, "kind": "vietphrase", "scope": scope}
        assert reference.export_text("vp", scope) == text
    finally:
        reference.close()


def column_bytes(entries, trie=True):
    prefix = struct.pack(">I2iI2i", 2, 1, 0, 2, 0, 1) if trie else b""
    values = [meaning for _, meaning in entries]
    keys = [word for word, _ in entries]
    return prefix + struct.pack(">I", len(entries)) + "\n".join(values + keys).encode("utf-8")


def pack(root, binary, events):
    finish(fixture(root))
    with closing(QtDictionary(root)) as qt:
        qt.save("name", "刘备", "Tên do tôi chọn")
    good = root / "synthetic-qt.zip"
    bad = root / "corrupt-qt.zip"
    mismatch = root / "local-name-qt.zip"
    for archive in (good, bad, mismatch):
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            members = {
                "VP.dic": column_bytes([("苹果", "táo¦quả táo"), ("坏", "bad\x07")] if archive == good else [("新", "would leak")]),
                "NE.dic": b"bad" if archive == bad else column_bytes([("刘备", "overwrite")]),
                "HV.dic": column_bytes([("山", "sơn")], trie=False),
                "Pronouns.dic": struct.pack(">I2iI2iI", 2, 1, 0, 2, 0, 1, 2) + b"broken",
                "Rule.dic": b"incompatible but retained",
            }
            for name, contents in members.items():
                info = zipfile.ZipInfo(name)
                output.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(mismatch) as archive:
        offset = archive.getinfo("NE.dic").header_offset + 30
    data = mismatch.read_bytes()
    assert data[offset:offset + 6] == b"NE.dic"
    mismatch.write_bytes(data[:offset] + b"NX.dic" + data[offset + 6:])
    report_path = root / "qt-install-report.json"

    def check(port, report, count):
        events.append(("pack report", report))
        assert report["source_url"] is None
        assert report["pending"] == ["pronouns", "rules"]
        assert report["files"]["VP.dic"]["inserted"] == count
        assert report["files"]["VP.dic"]["rejected"] == 1
        assert report["files"]["NE.dic"]["skipped_existing"] == 1
        api = Capture(port, events)
        translate(api, "刘备苹果山", "Tên do tôi chọn táo sơn")
        api.request(route(kind="vietphrase"))

    for count in (1, 0):
        if binary is None:
            with closing(QtDictionary(root)) as qt:
                report = install_pack(qt, good)
            m1.write_json(report_path, report)
            with server(None, root, f"installed-{count}") as (port, _):
                check(port, report, count)
        else:
            with m1.child(command(binary, root) + ["--import-qt", str(good)],
                          root.parent / f"{root.name}-installed-{count}.log") as (port, owner):
                check(port, json.loads(report_path.read_text(encoding="utf-8")), count)
                stop(port, owner)
    before = report_path.read_bytes()
    for archive in (bad, mismatch):
        if binary is None:
            with closing(QtDictionary(root)) as qt:
                try:
                    install_pack(qt, archive)
                except PackError as error:
                    failure = str(error)
                else:
                    raise AssertionError("corrupt main dictionary committed")
        else:
            outcome = subprocess.run(command(binary, root) + ["--import-qt", str(archive)], check=False,
                                     capture_output=True, text=True, encoding="utf-8", timeout=15)
            assert outcome.returncode and not outcome.stdout.strip(), outcome
            failure = outcome.stderr.strip()
        if archive == bad:
            assert "NE.dic: Từ điển bị thiếu header." in failure, failure
        elif binary is not None:
            assert "NE.dic" in failure and "Local file header filename mismatch" in failure, failure
        else:
            assert "NE.dic" in failure and "NX.dic" in failure, failure
        events.append({"observation": "pack rejected without mutation", "archive": archive.name, "diagnostic": failure})
        assert report_path.read_bytes() == before
        with closing(QtDictionary(root)) as qt:
            assert qt.list("vp", query="新") == []
            assert qt.translate("刘备苹果山") == "Tên do tôi chọn táo sơn"


if __name__ == "__main__":
    run("qt-contract", {"priority": priority, "grammar": grammar, "storage": storage, "pack": pack})
