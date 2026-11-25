"""M6 import latency, reader tail latency and memory on a large synthetic library (opt-in benchmark)."""
import argparse
import json
from pathlib import Path
import platform
import statistics
import threading
import time

from m2_capture import m1, request
from m3_support import db, finish, fixture, query, server, verify_db
from m6_documents import raw_request

BODY = "Đoạn văn được lưu sẵn cho trình đọc. " * 110
TXT_CHAPTERS = 6_000
BACKUP_BOOKS, BACKUP_CHAPTERS = 200, 500


def prepare(root):
    store = fixture(root)
    with db(root) as conn:
        conn.executemany("INSERT INTO books(id,name,source,chapter_count,added_at,updated_at) VALUES(?,?,'file',?,1,1)",
                         ((f"book-{i:05}", f"Truyện {i:05}", 512 if i == 0 else 10) for i in range(10_000)))
        conn.executemany("INSERT INTO chapters(book_id,idx,title,text,html,downloaded) VALUES(?,?,?,?,?,?)",
                         ((f"book-{b:05}", idx, str(idx), BODY if b == 0 else "", f"<p>{BODY}</p>" if b == 0 else "", int(b == 0))
                          for b in range(10_000) for idx in range(512 if b == 0 else 10)))
        conn.execute("DELETE FROM asset_pending")
        conn.execute("UPDATE asset_index_state SET complete=1")
        conn.execute("UPDATE chapter_search_state SET complete=1")
        conn.execute("UPDATE chapter_cache_state SET complete=1")
    finish(store, index_search=False)
    counts = query(root, "SELECT count(*),sum(downloaded) FROM chapters")[0]
    assert counts == (100_502, 512), counts
    return {"books": 10_000, "chapter_rows": counts[0], "cached_bodies": counts[1],
            "database_bytes": store.path.stat().st_size}


def backup_folder(folder):
    for b in range(BACKUP_BOOKS):
        path = folder / "books" / f"b{b:03}"
        path.mkdir(parents=True)
        (path / "book.json").write_text(json.dumps({"book_id": f"backup-{b:03}", "name": f"Sao lưu {b}", "source": "local",
                                                    "path": f"/sdcard/{b}.epub"}), encoding="utf-8")
        (path / "chapters.json").write_text(json.dumps([{"name": f"Chương {i}", "path": f"{b}/{i}"}
                                                        for i in range(BACKUP_CHAPTERS)]), encoding="utf-8")
    return {"books": BACKUP_BOOKS, "chapters": BACKUP_BOOKS * BACKUP_CHAPTERS}


def post_json(port, path, value):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
    try:
        conn.request("POST", path, json.dumps(value).encode(), {"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read()
        return {"status": response.status, "json": json.loads(body) if body else None}
    finally:
        conn.close()


def summary(samples):
    ordered = sorted(samples)
    return {"n": len(samples), "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[max(0, int(len(ordered) * .95) - 1)], 3), "max_ms": round(ordered[-1], 3)}


def timed_with_reader(port, action):
    samples, errors, stop = [], [], threading.Event()

    def reader():
        index = 0
        while not stop.is_set():
            start = time.perf_counter()
            try:
                result = request(port, f"/api/books/book-00000/chapters/{index % 512}")
                if result["status"] != 200:
                    errors.append(result["status"])
            except OSError as error:
                errors.append(type(error).__name__)
            samples.append((time.perf_counter() - start) * 1000)
            index += 7
            time.sleep(.02)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(.2)
    start = time.perf_counter()
    result = action()
    elapsed = (time.perf_counter() - start) * 1000
    stop.set()
    thread.join(timeout=30)
    return result, round(elapsed, 1), summary(samples), errors


def measure(root, binary, output):
    dataset = prepare(root)
    text = "".join(f"Chương {i} Tên\n" + "Nội dung dài. " * 400 + "\n" for i in range(TXT_CHAPTERS)).encode()
    folder = output / f"{root.name}-backup"
    backup = backup_folder(folder)
    report = {"dataset": dataset, "txt_bytes": len(text), "txt_chapters": TXT_CHAPTERS, "backup": backup}
    with server(binary, root, "scale") as (port, owner):
        report["startup_memory"] = m1.memory(owner.pid)
        result, elapsed, reads, errors = timed_with_reader(
            port, lambda: raw_request(port, "lon.txt", text, {"X-Filename": "lon.txt"}))
        assert result["status"] == 200, result
        report["txt_import"] = {"ms": elapsed, "reader": reads, "reader_errors": errors, "memory": m1.memory(owner.pid)}
        result, elapsed, reads, errors = timed_with_reader(
            port, lambda: post_json(port, "/api/import/backup", {"path": str(folder)}))
        assert result["status"] == 200, result
        report["backup_import"] = {"ms": elapsed, "reader": reads, "reader_errors": errors, "memory": m1.memory(owner.pid),
                                   "report": result["json"]}
    report["final_rows"] = query(root, "SELECT count(*) FROM books")[0][0], query(root, "SELECT count(*) FROM chapters")[0][0]
    verify_db(root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--engines", default="python,rust")
    args = parser.parse_args()
    output = m1.fresh_output().resolve()
    report = {"machine": {"platform": platform.platform(), "python": platform.python_version()}}
    for label, binary in (("python", None), ("rust", args.binary.resolve())):
        if label not in args.engines.split(","):
            continue
        report[label] = measure(output / label, binary, output)
        print(label, json.dumps({k: v for k, v in report[label].items() if k != "dataset"}, ensure_ascii=False), flush=True)
    m1.write_json(output / "m6-scale.json", report)
    print("evidence:", output / "m6-scale.json")


if __name__ == "__main__":
    main()
