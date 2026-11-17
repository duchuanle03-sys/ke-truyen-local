"""Explicit frozen-source M2 scale/streaming benchmark; stdlib, synthetic OS-temp only."""
import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import threading
import time

import m2_capture as h


def finish_fixture_search(root):
    store = h.Store(root)
    def body_snapshot():
        with store.db() as db:
            counts = tuple(db.execute(sql).fetchone()[0] for sql in (
                "SELECT count(*) FROM books", "SELECT count(*) FROM chapters",
                "SELECT count(*) FROM chapters WHERE downloaded=1"))
            assert counts == (500, 100000, 512), counts
            digest = hashlib.sha256()
            for row in db.execute("SELECT book_id,idx,text,html,images,original_text,original_html,raw,revision "
                                  "FROM chapters WHERE downloaded=1 ORDER BY book_id,idx"):
                digest.update(json.dumps(list(row), ensure_ascii=False).encode())
            return counts, digest.hexdigest()
    before = body_snapshot()
    for _ in range(513):
        if not store.index_search(limit=8):
            break
    else:
        raise AssertionError("synthetic FTS preparation did not finish within 513 bounded batches")
    assert body_snapshot() == before, "FTS preparation changed library counts or cached bodies"
    with store.db() as db:
        counts = {"fts_pending": db.execute("SELECT count(*) FROM chapter_search_pending").fetchone()[0],
                  "fts_documents": db.execute("SELECT count(*) FROM chapter_search_docs").fetchone()[0]}
    assert counts == {"fts_pending": 0, "fts_documents": 512}, counts
    store.close_inventory()
    store.checkpoint()
    marker = json.loads((root / h.m1.MARKER).read_text(encoding="utf-8"))
    marker["sha256"] = hashlib.sha256(store.path.read_bytes()).hexdigest()
    h.m1.write_json(root / h.m1.MARKER, marker)
    return counts


def measurements(port, pid, root, samples):
    result = {"pid": pid, "memory_before": h.m1.memory(pid)}
    workloads = {
        "shelf": ("/api/books", None),
        "toc_50000_metadata": ("/api/books/0/chapters", None),
        "cached_chapter": ("/api/books/0/chapters/0", None),
        "progress": ("/api/books/0/progress", {"index": 0, "position": .5}),
        "lease": ("/api/readers", {"id": "scale", "book_id": "0", "index": 0, "sequence": 4}),
    }
    for name, (path, value) in workloads.items():
        times = []
        before_workload = h.m1.memory(pid)
        first_start = time.perf_counter()
        response = h.request(port, path, "POST" if value is not None else "GET", value)
        first_ms = (time.perf_counter() - first_start) * 1000
        assert response["status"] == 200, response
        if name == "toc_50000_metadata":
            assert len(response["json"]) == 50000
            assert all(not ({"text", "html", "original_text", "original_html"} & set(r)) for r in response["json"])
        for _ in range(samples):
            start = time.perf_counter()
            response = h.request(port, path, "POST" if value is not None else "GET", value)
            times.append((time.perf_counter() - start) * 1000)
            assert response["status"] == 200
        result[name] = {**h.m1.summary(times), "first_ms": first_ms,
                       "response_bytes": len(bytes.fromhex(response["body_hex"])),
                       "memory_before": before_workload, "memory_after": h.m1.memory(pid)}
    stopped = threading.Event()
    writer_times, writer_errors = [], []

    def writer():
        try:
            with contextlib.closing(sqlite3.connect(root / "library.sqlite3", timeout=10)) as db:
                db.create_function("shelf_fold", 1, str.casefold, deterministic=True)
                index = 0
                while not stopped.is_set():
                    start = time.perf_counter()
                    db.execute("INSERT INTO settings VALUES('writer',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(index),))
                    db.commit()
                    writer_times.append((time.perf_counter() - start) * 1000)
                    index += 1
                    stopped.wait(.005)
        except Exception as error:
            writer_errors.append(repr(error))
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(samples):
            assert h.request(port, "/api/books/0/progress", "POST", {"index": 0, "position": .6})["status"] == 200
            assert h.request(port, "/api/books")["status"] == 200
    finally:
        stopped.set()
        thread.join(15)
    assert not thread.is_alive() and not writer_errors and writer_times, writer_errors
    result["concurrent_writer"] = h.m1.summary(writer_times)
    result["memory_after"] = h.m1.memory(pid)
    result["bytes"] = {p.name: p.stat().st_size for p in root.glob("library.sqlite3*")}
    return result


def stream(port, pid, root):
    before = h.m1.memory(pid)
    peaks = []
    stopped = threading.Event()
    def sample():
        while not stopped.wait(.002):
            peaks.append(h.m1.memory(pid))
    thread = threading.Thread(target=sample)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    digest, size = hashlib.sha256(), 0
    start = time.perf_counter()
    try:
        conn.request("GET", h.IMAGE_PATH)
        response = conn.getresponse()
        assert response.status == 200
        while body := response.read(65536):
            digest.update(body)
            size += len(body)
            time.sleep(.001)
    finally:
        conn.close()
        stopped.set()
        thread.join(5)
    file = root / "images" / hashlib.sha256(h.IMAGE_URL.encode()).hexdigest()
    with file.open("rb") as handle:
        expected = hashlib.file_digest(handle, "sha256").hexdigest()
    assert size == file.stat().st_size and digest.hexdigest() == expected
    return {"bytes": size, "elapsed_ms": (time.perf_counter() - start) * 1000,
            "memory_before": before, "memory_samples": peaks, "memory_after": h.m1.memory(pid)}


def run(args):
    binary = Path(args.binary).resolve()
    output = h.m1.fresh_output(args.output)
    report = {"output": str(output), "environment": {"platform": platform.platform(),
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "cpus": os.cpu_count()}, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "samples": args.samples, "scope": "equivalent HTTP work in separate child processes; Python includes full App services, Rust M2 subset"}
    report["source_sha256"] = h.rust_source_hashes()
    report["reference_sha256"] = h.m1.source_hashes()
    report["harness_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__), Path(h.__file__), Path(h.m1.__file__))}
    canonical = output / "canonical"
    h.fixture(canonical, scale=True)
    search_counts = finish_fixture_search(canonical)
    before = h.m1.hashes(canonical)
    with contextlib.closing(sqlite3.connect(canonical / "library.sqlite3")) as db:
        report["dataset"] = {"books": db.execute("SELECT count(*) FROM books").fetchone()[0],
            **search_counts,
            "chapter_metadata": db.execute("SELECT count(*) FROM chapters").fetchone()[0],
            "cached_bodies": db.execute("SELECT count(*) FROM chapters WHERE downloaded=1").fetchone()[0],
            "chapter_distribution": "one book 50000, 498 books 100 each, one book 200",
            "body_payload_bytes": db.execute("SELECT sum(length(CAST(text AS BLOB))+length(CAST(html AS BLOB))) FROM chapters").fetchone()[0],
            "cached_files": 2, "cached_images": 1, "original_files": 1,
            "original_file_bytes": len(b"synthetic original"), "image_bytes": len(h.PNG)}
    def server(language, root, label):
        if language == "rust":
            command = h.command(binary, root)
        else:
            script = ("import sys,json,time;sys.path.insert(0,sys.argv[1]);import m2_capture as h;"
                      "context=h.oracle(h.Path(sys.argv[2]));port,store=context.__enter__();"
                      "print(json.dumps({'port':port}),flush=True);time.sleep(3600)")
            command = [sys.executable, "-B", "-X", "utf8", "-c", script,
                       str(Path(__file__).parent.resolve()), str(root)]
        return h.m1.child(command, output / (label + ".log"))
    for language in ("python", "rust"):
        root = output / language
        shutil.copytree(canonical, root)
        start = time.perf_counter()
        context = server(language, root, language)
        with context as (port, owner):
            startup = (time.perf_counter() - start) * 1000
            pid = owner.pid
            report[language] = measurements(port, pid, root, args.samples)
            startup_samples = [startup]
            if language == "rust":
                h.shutdown(port, owner, root)
        for index in range(2):
            startup_root = output / f"{language}-startup-{index}"
            shutil.copytree(canonical, startup_root)
            start = time.perf_counter()
            with server(language, startup_root, f"{language}-startup-{index}") as (port, owner):
                startup_samples.append((time.perf_counter() - start) * 1000)
                assert h.request(port, "/api/books")["status"] == 200
                if language == "rust":
                    h.shutdown(port, owner, startup_root)
        report[language]["startup"] = h.m1.summary(startup_samples)
        h.m1.write_json(output / "scale.json", report)
    # Streaming uses one separate large cached file and avoids a response-sized client buffer.
    root = output / "stream"
    h.fixture(root, image_bytes=h.PNG[:8] + b"x" * (64 * 1024 * 1024 - 8))
    with h.m1.child(h.command(binary, root), output / "stream.log") as (port, owner):
        report["stream_64MiB"] = stream(port, owner.pid, root)
        # Disconnect an in-flight response, then prove admission and orderly drain still work.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", h.IMAGE_PATH)
        response = conn.getresponse()
        assert response.status == 200
        response.read(1024)
        conn.close()
        assert h.request(port, "/api/books")["status"] == 200
        stalled = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            stalled.request("GET", h.IMAGE_PATH)
            response = stalled.getresponse()
            assert response.status == 200
            response.read(1024)
            # Leave this large response unread while shutdown aborts and drains its ownership.
            time.sleep(.1)
            h.shutdown(port, owner, root)
        finally:
            stalled.close()
    assert h.m1.hashes(canonical) == before
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == report["binary_sha256"]
    assert h.rust_source_hashes() == report["source_sha256"]
    assert h.m1.source_hashes() == report["reference_sha256"]
    h.m1.write_json(output / "scale.json", report)
    print(json.dumps({"report": str(output / "scale.json"), "dataset": report["dataset"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output")
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    assert args.samples >= 3
    run(args)
