"""Independent process-kill checkpoint/replay across the two durable schedulers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from m2_capture import m1
from m3_support import finish, fixture, query, verify_db, wait_for


def python_driver(root, phase):
    from server import App

    class FixtureSource:
        def chapter(self, source, url):
            assert source == "full"
            index = int(url.rsplit("/", 1)[1])
            with (root / f"{phase}-fetches").open("a", encoding="ascii") as log:
                log.write(f"{index}\n")
            if phase == "crash" and index == 17:
                (root / "checkpoint-gate").write_text("17", encoding="ascii")
                while True:
                    time.sleep(.01)
            return {"html": f"<p>Saved chapter {index}</p>", "text": f"Saved chapter {index}", "images": []}

    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert (root / "m5-checkpoint-fixture").read_text(encoding="ascii") == "reader-m5-checkpoint-1"
    app = App(root, FixtureSource())
    try:
        if phase == "crash":
            job = app.download("book", start=3, count=30)
            (root / "checkpoint-job").write_text(job["id"], encoding="ascii")
            while True:
                time.sleep(1)
        wait_for(lambda: app.store.download_job(read_job(root)), lambda job: job["status"] == "completed")
    finally:
        assert app.close()


def read_job(root):
    return (root / "checkpoint-job").read_text(encoding="ascii").strip()


def calls(root, phase):
    return list(map(int, (root / f"{phase}-fetches").read_text(encoding="ascii").splitlines()))


def run_case(output, binary):
    backend = "rust" if binary else "python"
    root = output / backend
    store = fixture(root)
    store.upsert_book({"id": "book", "name": "Checkpoint book", "source": "full", "path": "https://example.invalid/"},
                      [{"title": str(i), "url": f"https://example.invalid/{i}"} for i in range(40)])
    store.progress("book", 1, .65)
    store.save_note({"id": "keep", "book_id": "book", "note": "preserved"})
    finish(store)
    (root / "m5-checkpoint-fixture").write_text("reader-m5-checkpoint-1", encoding="ascii")

    def worker(phase):
        argv = ([str(binary), "--ignored", "--nocapture", "--exact", "download_checkpoint_driver"] if binary
                else [sys.executable, "-B", "-X", "utf8", __file__, "--python-driver", str(root), "--phase", phase])
        env = os.environ | {"READER_M5_CHECKPOINT_FIXTURE": str(root), "READER_M5_CHECKPOINT_PHASE": phase}
        with (output / f"{backend}-{phase}.log").open("w", encoding="utf-8") as log:
            return subprocess.Popen(argv, stdout=log, stderr=log, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    cache = "SELECT idx,text,kind,offline FROM chapters JOIN chapter_cache USING(book_id,idx) ORDER BY idx"
    owner = worker("crash")
    try:
        wait_for(lambda: owner.poll() is not None or (root / "checkpoint-gate").exists())
        assert owner.poll() is None
        assert wait_for(lambda: (root / "checkpoint-job").is_file() and read_job(root))
        job = read_job(root)
        [(status, data)] = query(root, "SELECT status,data FROM download_jobs WHERE id=?", (job,))
        partial = json.loads(data)
        assert status == "running" and 0 <= 14 - partial["completed"] <= 9, partial
        assert (partial["start"], partial["end"], partial["total"]) == (3, 33, 30)
        assert query(root, cache) == [(i, f"Saved chapter {i}", "protected", 1) for i in range(3, 17)]
        assert calls(root, "crash") == list(range(3, 18))
    finally:
        owner.kill()
        owner.wait(timeout=5)
    owner = worker("recover")
    try:
        assert owner.wait(timeout=25) == 0
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
    [(status, cancelled, data)] = query(root, "SELECT status,cancelled,data FROM download_jobs WHERE id=?", (job,))
    final = json.loads(data)
    assert status == "completed" and cancelled == 0
    assert (final["start"], final["end"], final["total"], final["completed"]) == (3, 33, 30, 30)
    saved = query(root, cache)
    assert saved == [(i, f"Saved chapter {i}", "protected", 1) for i in range(3, 33)]
    recovered = calls(root, "recover")
    assert recovered == list(range(17, 33))
    assert query(root, "SELECT position FROM books WHERE id='book'") == [(.65,)]
    assert query(root, "SELECT note FROM notes WHERE id='keep'") == [("preserved",)]
    verify_db(root)
    return {"before_kill_checkpoint": partial["completed"], "state": final, "recovered_fetches": recovered,
            "cached": saved}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--driver", type=Path)
    mode.add_argument("--python-driver", type=Path)
    parser.add_argument("--phase", choices=("crash", "recover"))
    args = parser.parse_args()
    if args.python_driver:
        assert args.phase
        python_driver(args.python_driver.resolve(), args.phase)
    else:
        output = m1.fresh_output().resolve()
        print(f"M5 checkpoint evidence: {output}", flush=True)
        python = run_case(output, None)
        result = {"python": python}
        if args.driver:
            native = result["rust"] = run_case(output, args.driver.resolve())
            assert {key: value for key, value in python.items() if key != "before_kill_checkpoint"} == {
                key: value for key, value in native.items() if key != "before_kill_checkpoint"
            }
        m1.write_json(output / "checkpoint.json", result)
