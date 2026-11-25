"""Opt-in M7 gate: compare Python and the Rust production launcher on copies of a real library.

Usage: python -X utf8 -B rust/tests/support/m7_live_copy.py --binary EXE --source BACKUP_DIR
The source is copied twice into the OS temp directory; the original is only read.
"""
import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from m2_capture import m1, request
from m3_support import serve_reference  # noqa: F401  (imported by the reference command)

HERE = Path(__file__).resolve().parent
TIMES = ("added_at", "updated_at", "read_at", "trashed_at", "trash_expires_at", "expires", "created_at", "last_used")


def reference_command(root):
    entry = "import sys;sys.path.insert(0,sys.argv[1]);from m3_support import serve_reference;serve_reference(sys.argv[2])"
    return [sys.executable, "-B", "-X", "utf8", "-c", entry, str(HERE), str(root)]


def rust_command(binary, root):
    return [str(binary), "--data-dir", str(root), "--port", "0", "--no-browser"]


def normalize(value):
    if isinstance(value, dict):
        return {k: ("<t>" if k in TIMES and isinstance(v, (int, float)) and v else normalize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def capture(port):
    events = []

    def get(path):
        response = request(port, path)
        events.append((path, response["status"], normalize(response["json"])))
        return response["json"]

    for sort in ("recent", "name", "added", "progress", "chapters"):
        offset = 0
        while offset is not None:
            page = get(f"/api/books?sort={sort}&limit=200&offset={offset}")
            offset = page["next_offset"]
    get("/api/books?trashed=1&limit=200")
    get("/api/books?archived=1&limit=200")
    books = []
    offset = 0
    while offset is not None:
        page = request(port, f"/api/books?limit=200&offset={offset}")["json"]
        books += page["items"]
        offset = page["next_offset"]
    sampled = 0
    for book in books:
        get(f"/api/books/{book['id']}")
        toc = get(f"/api/books/{book['id']}/chapters")
        for chapter in [c for c in toc if c["downloaded"]][:5]:
            get(f"/api/books/{book['id']}/chapters/{chapter['index']}")
            sampled += 1
    for path in ("/api/notes", "/api/groups", "/api/settings", "/api/jobs", "/api/dictionaries?kind=name&limit=50&offset=0",
                 "/api/dictionaries?kind=vietphrase&limit=50&offset=0", "/api/sources"):
        get(path)
    return events, len(books), sampled


def stop(port, process):
    cookie = request(port, "/")["headers"]["set-cookie"].split(";", 1)[0]
    token = request(port, "/api/settings", headers={"Cookie": cookie})["headers"]["x-shutdown-token"]
    response = request(port, "/api/shutdown", "POST", {"pause_downloads": True}, {"Cookie": cookie, "X-Shutdown-Token": token})
    assert response["status"] == 200, response
    assert process.wait(timeout=60) == 0


def integrity(root):
    db = sqlite3.connect(f"file:{(root / 'library.sqlite3').as_posix()}?mode=ro", uri=True)
    db.create_function("shelf_fold", 1, str.casefold, deterministic=True)
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    assert (source / "library.sqlite3").is_file(), "source must be a library directory"
    assert source.parent.name == "backups" or "pre-m7" in source.name, "refusing: --source must be a backup, never the live library"
    output = m1.fresh_output().resolve()
    report = {"source": str(source)}
    copies = {}
    for label in ("python", "rust"):
        started = time.perf_counter()
        copies[label] = output / label
        shutil.copytree(source, copies[label])
        report[f"{label}_copy_seconds"] = round(time.perf_counter() - started, 1)
    results = {}
    for label, command in (("python", reference_command(copies["python"])), ("rust", rust_command(args.binary, copies["rust"]))):
        with m1.child(command, output / f"{label}.log") as (port, process):
            started = time.perf_counter()
            results[label] = capture(port)
            report[f"{label}_capture_seconds"] = round(time.perf_counter() - started, 1)
            report[f"{label}_memory"] = m1.memory(process.pid)
            if label == "rust":
                book = results[label][0][0][2]["items"][0]["id"]
                progress = request(port, f"/api/books/{book}/progress", "POST", {"index": 0, "position": 0.25})
                note = request(port, "/api/notes", "POST", {"book_id": book, "chapter_index": 0, "quote": "M7 kiểm tra",
                                                          "note": "ghi chú", "position": 0.25})
                word = request(port, "/api/dictionaries", "POST", {"kind": "name", "word": "测试", "meaning": "Kiểm Tra"})
                assert {progress["status"], note["status"], word["status"]} == {200}, (progress, note, word)
                report["rust_written_book"] = book
            stop(port, process)
    py_events, rs_events = results["python"][0], results["rust"][0]
    assert len(py_events) == len(rs_events), (len(py_events), len(rs_events))
    mismatches = [(p[0], p[1], r[1]) for p, r in zip(py_events, rs_events) if p != r]
    report.update({"books": results["rust"][1], "sampled_chapters": results["rust"][2], "requests": len(rs_events),
                   "mismatches": len(mismatches), "first_mismatches": mismatches[:10]})
    # Python must read what Rust wrote, unchanged, on the Rust copy.
    probe = ("import sys,json;sys.path.insert(0,sys.argv[1]);from library import Store;s=Store(sys.argv[2]);"
             "b=s.get_book(sys.argv[3]);n=[x for x in s.notes(sys.argv[3]) if x['quote']=='M7 kiểm tra'];"
             "print(json.dumps({'chapter_index':b['chapter_index'],'position':b['position'],'notes':len(n)}))")
    readback = json.loads(__import__("subprocess").run([sys.executable, "-B", "-X", "utf8", "-c", probe, str(HERE.parents[2]),
                                                        str(copies["rust"]), report["rust_written_book"]],
                                                       capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    report["python_reads_rust_writes"] = readback
    report["integrity"] = {label: integrity(path) for label, path in copies.items()}
    m1.write_json(output / "m7-live-copy.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "first_mismatches"}, ensure_ascii=False))
    for path, py_status, rs_status in mismatches[:10]:
        print("MISMATCH", path, py_status, rs_status)
    print("evidence:", output / "m7-live-copy.json")
    assert not mismatches and readback == {"chapter_index": 0, "position": 0.25, "notes": 1}
    assert report["integrity"] == {"python": "ok", "rust": "ok"}


if __name__ == "__main__":
    main()
