"""Matched direct scheduler and cached-reader workload on a disposable library."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from m2_capture import m1
from m3_support import db, finish, fixture, query, verify_db, wait_for


BODY = "Synthetic cached chapter. " + "x" * (4096 - len("Synthetic cached chapter. "))
FORMAT = "reader-m5-scale-1"
READY = "M5_SCALE "


def prepare(root):
    store = fixture(root)
    books = {"m5-reader": ("file", 512), "m5-active-stv": ("stv", 1), "m5-full": ("full", 256),
             "m5-metadata": ("file", 89_231)}
    books.update({f"wait-stv-{i:05}": ("stv", 1) for i in range(10_000)})
    payload = {"name": "Synthetic job", "start": 0, "end": 1, "total": 1, "completed": 0, "error": ""}
    waiting = json.dumps(payload)
    history = json.dumps({**payload, "completed": 1})
    with db(root) as conn:
        conn.executemany("INSERT INTO books(id,name,source,chapter_count,added_at,updated_at) VALUES(?,?,?,?,1,1)",
                         ((book, book, source, count) for book, (source, count) in books.items()))
        conn.executemany("INSERT INTO chapters(book_id,idx,title,text,downloaded) VALUES(?,?,?,?,?)",
                         ((book, idx, str(idx), BODY if book == "m5-reader" else "", int(book == "m5-reader"))
                          for book, (_, count) in books.items() for idx in range(count)))
        conn.execute("INSERT INTO download_jobs VALUES('held-stv','m5-active-stv','queued',0,?,1,1)", (waiting,))
        conn.executemany("INSERT INTO download_jobs VALUES(?,?,'queued',0,?,?,?)",
                         ((f"queue-{i:05}", f"wait-stv-{i:05}", waiting, i + 2, i + 2) for i in range(10_000)))
        conn.executemany("INSERT INTO download_jobs VALUES(?,'m5-reader','completed',0,?,?,?)",
                         ((f"history-{i:05}", history, i + 10002, i + 10002) for i in range(50_000)))
        # This synthetic corpus has no covers, original paths, body HTML, or image references.
        conn.execute("DELETE FROM asset_pending")
        conn.execute("UPDATE asset_index_state SET complete=1")
        conn.execute("UPDATE chapter_search_state SET complete=1")
        conn.execute("UPDATE chapter_cache_state SET complete=1")
    finish(store)
    (root / "m5-scale-fixture").write_text(FORMAT, encoding="ascii")
    assert query(root, "SELECT count(*),sum(downloaded) FROM chapters") == [(100_000, 512)]
    return {"books": len(books), "chapter_rows": 100_000, "cached_bodies": 512, "bytes_per_body": 4096,
            "database_bytes": store.path.stat().st_size, "blocked_stv_jobs": 10_000,
            "completed_history": 50_000, "full_job_chapters": 256, "reader_samples": 30}


def python_driver(root):
    from server import App
    from source_errors import SourceError

    assert root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert (root / "m5-scale-fixture").read_text(encoding="ascii") == FORMAT
    gated, observed = threading.Event(), queue.Queue()
    started = None

    class DisabledSource:
        def chapter(self, *args, **kwargs):
            raise AssertionError("No upstream source may be used by the scheduler benchmark")

    class TestApp(App):
        def _chapter_content(self, book, index, background=False, cancelled=None):
            if not background:
                return super()._chapter_content(book, index, background, cancelled)
            if book["id"] == "m5-active-stv":
                gated.set()
                while not cancelled():
                    time.sleep(.005)
                raise SourceError("Synthetic hold cancelled", code="request_cancelled")
            assert book["id"] == "m5-full"
            if index == 0:
                observed.put((time.perf_counter() - started) * 1000)
            time.sleep(.004)
            return {"html": "", "images": []}

    app = TestApp(root, DisabledSource())
    app._asset_stop.set()
    app._asset_worker.join(5)
    assert not app._asset_worker.is_alive()
    try:
        assert gated.wait(20)
        with closing(sqlite3.connect(root / "library.sqlite3", timeout=5)) as observer:
            def counts():
                return dict(observer.execute("SELECT status,count(*) FROM download_jobs GROUP BY status"))

            def full():
                return observer.execute("SELECT status FROM download_jobs WHERE book_id='m5-full'").fetchone()[0]

            assert counts() == {"queued": 10_000, "running": 1, "completed": 50_000}
            started = time.perf_counter()
            job = app.download("m5-full", 0, 256)
            submit_ms = (time.perf_counter() - started) * 1000
            wait_ms = observed.get(timeout=20)
            times, overlaps, digest = [], 0, None
            for _ in range(30):
                active = full() == "running"
                read_started = time.perf_counter()
                result = app.chapter("m5-reader", 0)
                times.append((time.perf_counter() - read_started) * 1000)
                assert result["downloaded"]
                current = hashlib.sha256(result["text"].encode()).hexdigest()
                assert digest is None or current == digest
                digest = current
                overlaps += int(active and full() == "running")
                time.sleep(.02)
            assert overlaps > 0
            wait_for(full, lambda status: status == "completed", timeout=30)
            assert counts() == {"queued": 10_000, "running": 1, "completed": 50_001}
        saved = app.store.download_job(job["id"])
        assert saved["completed"] == 256
        print(READY + json.dumps({"queue_wait_ms": wait_ms, "submit_ms": submit_ms, "readers_ms": times,
              "reads_entirely_during_full_job": overlaps, "output_sha256": digest, "completed": saved["completed"],
              "remaining_stv_queued": 10_000}), flush=True)
        assert sys.stdin.readline().strip() == "go"
        app.begin_shutdown(True)
    finally:
        assert app.close()


def measure(root, driver, output, label):
    argv = ([sys.executable, "-B", "-X", "utf8", __file__, "--python-driver", str(root)] if label == "python"
            else [str(driver), "--ignored", "--nocapture", "--exact", "download_scale_driver"])
    environment = os.environ | {"READER_M5_SCALE_FIXTURE": str(root)}
    with (output / (label + ".log")).open("w", encoding="utf-8") as stderr:
        with subprocess.Popen(argv, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                              text=True, encoding="utf-8") as child:
            messages = queue.Queue()

            def read():
                for line in child.stdout:
                    messages.put(line)
                messages.put(None)

            threading.Thread(target=read, daemon=True).start()
            report = None
            try:
                while (line := messages.get(timeout=90)) is not None:
                    if READY not in line:
                        continue
                    assert report is None
                    report = json.loads(line.split(READY, 1)[1])
                    report["process_memory"] = m1.memory(child.pid)
                    child.stdin.write("go\n")
                    child.stdin.flush()
                assert child.wait(timeout=30) == 0 and report is not None, (label, child.returncode, stderr.name)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
    expected_hash = hashlib.sha256(BODY.encode()).hexdigest()
    assert report["output_sha256"] == expected_hash
    assert len(report["readers_ms"]) == 30 and 1 <= report["reads_entirely_during_full_job"] <= 30
    assert report["completed"] == 256 and report["remaining_stv_queued"] == 10_000
    report["reader"] = m1.summary(report["readers_ms"])
    report["persisted"] = sorted(query(root, "SELECT status,count(*) FROM download_jobs GROUP BY status"))
    assert report["persisted"] == [("completed", 50_001), ("paused", 10_001)]
    assert query(root, "SELECT count(*),sum(downloaded) FROM chapters") == [(100_000, 512)]
    verify_db(root)
    return report


def run(driver):
    output = m1.fresh_output().resolve()
    print(f"M5 scheduler scale evidence: {output}", flush=True)
    def digests(paths):
        return {path.relative_to(m1.ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    rust_paths = [*(m1.ROOT / "rust/src").rglob("*.rs"), m1.ROOT / "rust/Cargo.toml", m1.ROOT / "rust/Cargo.lock"]
    test_paths = [Path(__file__).resolve(), m1.ROOT / "rust/tests/download_recovery.rs", *(
        Path(__file__).resolve().with_name(name) for name in ("m3_support.py", "m2_capture.py", "capture_reference.py"))]
    hashes, harness = digests(rust_paths), digests(test_paths)
    reference = m1.source_hashes()
    driver_hash = hashlib.sha256(driver.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="m5-scale-", dir=output) as work:
        work = Path(work)
        canonical = work / "canonical"
        dataset = prepare(canonical)
        canonical_hashes = m1.hashes(canonical)
        report = {"dataset": dataset, "source_sha256": hashes, "reference_sha256": reference,
                  "driver_sha256": driver_hash, "fixture_sha256": canonical_hashes,
                  "harness_sha256": harness,
                  "platform": platform.platform(), "cpu_count": os.cpu_count(),
                  "python_version": platform.python_version(), "sqlite_python": sqlite3.sqlite_version,
                  "scope": "direct scheduler and cached-reader entry points; one held STV lane, 256 synthetic 4 ms chapter callbacks; HTTP, source parsing, body writes, image I/O, and background maintenance excluded"}
        for label in ("python", "rust"):
            root = work / label
            shutil.copytree(canonical, root)
            assert m1.hashes(root) == canonical_hashes
            report[label] = measure(root, driver, output, label)
            m1.write_json(output / "scheduler.json", report)
        assert report["python"]["persisted"] == report["rust"]["persisted"]
        assert m1.hashes(canonical) == canonical_hashes
        assert hashlib.sha256(driver.read_bytes()).hexdigest() == driver_hash
        assert digests(rust_paths) == hashes and digests(test_paths) == harness and m1.source_hashes() == reference
    print(output / "scheduler.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--driver", type=Path)
    mode.add_argument("--python-driver", type=Path)
    args = parser.parse_args()
    if args.python_driver:
        python_driver(args.python_driver.resolve())
    else:
        run(args.driver.resolve())
