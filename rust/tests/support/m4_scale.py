"""Compare the Python and Rust QT engines on one generated million-entry corpus."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gc
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from qt import QtDictionary


PREFIX = "QT_SCALE "
SCOPE = "m4-book"
SENTINEL = "__m4_sentinel__"
COUNT = 1_014_374
FIXTURE_ID = 0x51544231


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def timed(action):
    started = time.perf_counter()
    result = action()
    return (time.perf_counter() - started) * 1000, result


def emit(phase, **values):
    print(PREFIX + json.dumps({"phase": phase, "values": values}), flush=True)
    assert sys.stdin.readline().strip() == "go"


def checked_input(root):
    assert root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    data = json.loads((root / "m4-synthetic.json").read_text(encoding="utf-8"))
    assert data["format"] == "reader-m4-qt-synthetic"
    with closing(sqlite3.connect((root / "qt.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA application_id").fetchone() == (FIXTURE_ID,)
        assert db.execute("SELECT count(*) FROM entries").fetchone() == (COUNT,)
    return data


def python_child(root):
    data = checked_input(root)
    ms, qt = timed(lambda: QtDictionary(root))
    with closing(qt):
        emit("opened", samples=[ms])
        ms, cold = timed(lambda: qt._compiled(SCOPE))
        emit("cold", samples=[ms])
        emit("warm", samples=[timed(lambda: qt._compiled(SCOPE))[0] for _ in range(100)])
        with patch.object(qt, "_compiled", new=lambda _: cold):
            for label, chapter in data["chapters"].items():
                expected = qt.translate(chapter, SCOPE)
                samples = []
                for _ in range(data["samples"]):
                    ms, value = timed(lambda: qt.translate(chapter, SCOPE))
                    assert value == expected
                    samples.append(ms)
                emit(label, samples=samples, output_sha256=digest(expected), characters=len(chapter))
        del cold
        qt.save("name", "私", "riêng", SCOPE)
        for scope in ("*", SCOPE):
            for action in ("add", "update", "delete"):
                samples = []
                for index in range(9):
                    word = f"__m4_edit_{index}__"
                    if action != "add":
                        qt.save("name", word, "before", scope)
                    if action == "delete":
                        ms, value = timed(lambda: qt.delete("name", word, scope))
                        assert value == {"deleted": 1}
                    else:
                        ms, _ = timed(lambda: qt.save("name", word, "after", scope))
                    samples.append(ms)
                emit(f"{scope}:{action}", samples=samples)
        qt.save("name", SENTINEL, "before")
        old = qt._compiled(SCOPE)
        probe = data["probe"]
        before = qt.translate(probe, SCOPE)
        after = before.replace("before", "after")
        ready, done = threading.Barrier(2), threading.Event()

        def reader():
            values = []
            ready.wait(timeout=5)
            while not done.is_set():
                ms, text = timed(lambda: qt.translate(probe, SCOPE))
                assert text in (before, after)
                values.append(ms)
                done.wait(.002)
            return values

        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(reader)
            ready.wait(timeout=5)
            writing = pool.submit(timed, lambda: qt.import_text("name", data["bulk"], "merge"))
            try:
                with closing(sqlite3.connect(qt.path)) as observer:
                    marker = "SELECT meaning FROM entries WHERE kind='name' AND scope='*' AND word=?"
                    deadline = time.monotonic() + 30
                    while observer.execute(marker, (SENTINEL,)).fetchone() != ("after",):
                        assert not writing.done() and time.monotonic() < deadline
                        time.sleep(.002)
                contender, _ = timed(lambda: qt.save("name", "__m4_second_writer__", "done"))
                import_ms, bulk = writing.result(timeout=60)
            finally:
                done.set()
            readers = reading.result(timeout=5)
        assert readers
        assert qt.translate(probe, SCOPE) == after
        with patch.object(qt, "_compiled", new=lambda _: old):
            assert qt.translate(probe, SCOPE) == before
        emit("publish", samples=[import_ms], readers_ms=readers, second_writer_ms=contender,
             old_sha256=digest(before), new_sha256=digest(after), report=bulk)
        del old
        gc.collect()
        emit("reclaimed")
        with closing(sqlite3.connect(qt.path)) as db:
            count, = db.execute("SELECT count(*) FROM entries WHERE kind='name' AND scope='*'").fetchone()
        ms, replacement = timed(lambda: qt.import_text("name", "__m4_replacement__=temporary", "replace"))
        emit("replace", samples=[ms], report=replacement)
        ms, restored = timed(lambda: qt.restore_backup(replacement["backup_id"]))
        assert restored["restored"] == count and qt.translate(SENTINEL, SCOPE) == "after"
        emit("restore", samples=[ms], report=restored)
        with closing(sqlite3.connect(qt.path)) as external, external:
            external.execute("UPDATE entries SET meaning='external' WHERE kind='name' AND scope='*' AND word=?", (SENTINEL,))
        ms, value = timed(lambda: qt.translate(SENTINEL, SCOPE))
        assert value == "external"
        emit("external_commit", samples=[ms], output_sha256=digest(value))


def receive(argv, environment, output, label):
    from m2_capture import m1

    phases = {}
    with (output / f"{label}.log").open("w", encoding="utf-8") as issues:
        with subprocess.Popen(argv, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=issues, text=True, encoding="utf-8") as owner:
            messages = queue.Queue()

            def read():
                for line in owner.stdout:
                    messages.put(line)
                messages.put(None)

            threading.Thread(target=read, daemon=True).start()
            try:
                while (line := messages.get(timeout=180)) is not None:
                    if PREFIX not in line:
                        continue
                    event = json.loads(line.split(PREFIX, 1)[1])
                    phase, values = event["phase"], event["values"]
                    assert phase not in phases
                    if "samples" in values:
                        values.update(m1.summary(values["samples"]))
                    if "readers_ms" in values:
                        values["concurrent_readers"] = m1.summary(values["readers_ms"])
                    values["process_memory"] = m1.memory(owner.pid)
                    phases[phase] = values
                    owner.stdin.write("go\n")
                    owner.stdin.flush()
                    if phase in {"cold", "publish", "restore"}:
                        print(f"{label}: {phase} complete", flush=True)
                assert owner.wait(timeout=10) == 0, (label, owner.returncode, issues.name)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
    return phases


def prepare(root, samples):
    from m2_capture import m1
    from m3_support import finish, fixture

    sys.path.insert(0, str(ROOT / "tests"))
    from qt_index_benchmark import generate, workloads

    finish(fixture(root))
    profile_path = m1.ROOT / "tests" / "fixtures" / "qt_index_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    db = root / "qt.sqlite3"
    generate(profile, db)
    chapters = workloads(db)
    assert sorted(set(map(len, chapters.values()))) == [10_000, 100_000]
    bulk = "\n".join([SENTINEL + "=after"] + [f"__m4_bulk_{index:04}__=thử" for index in range(2000)])
    m1.write_json(root / "m4-synthetic.json", {"format": "reader-m4-qt-synthetic", "samples": samples,
                                             "chapters": chapters, "bulk": bulk, "probe": (SENTINEL + "|") * 1000})
    return {"entries": COUNT, "db_bytes": db.stat().st_size,
            "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            "chapter_characters": {key: len(value) for key, value in chapters.items()}}


def persisted(root):
    path = root / "qt.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        return {"tiers": db.execute("SELECT kind,scope,count(*) FROM entries GROUP BY kind,scope").fetchall(),
                "backups": db.execute("SELECT kind,scope FROM backups ORDER BY id").fetchall(),
                "sentinel": db.execute("SELECT meaning FROM entries WHERE word=?", (SENTINEL,)).fetchone()}


def run(binary, samples):
    from m2_capture import m1, rust_source_hashes

    output = m1.fresh_output().resolve()
    print(f"QT scale evidence: {output}", flush=True)
    harness = [Path(__file__), m1.ROOT / "rust/tests/qt_scale.rs", Path(__file__).with_name("m3_support.py"),
               m1.ROOT / "tests/qt_index_benchmark.py"]
    harnesses = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in harness}
    native_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    rust, reference = rust_source_hashes(), m1.source_hashes()
    with tempfile.TemporaryDirectory(prefix="m4-qt-", dir=output) as directory:
        directory = Path(directory)
        canonical = directory / "canonical"
        dataset = prepare(canonical, samples)
        hashes = m1.hashes(canonical)
        summary = {"dataset": dataset, "canonical_sha256": hashes, "rust_source_sha256": rust,
                   "python_source_sha256": reference, "test_executable_sha256": native_hash,
                   "harness_sha256": harnesses, "platform": platform.platform(), "cpu_count": os.cpu_count(),
                   "python": platform.python_version(), "sqlite_python": sqlite3.sqlite_version,
                   "limits": ["direct QT engines; no HTTP latency or interpreter startup",
                              "warm filesystem", "cold construction/bulk/publish/restore each n=1",
                              "working set and private commit measured at phase boundaries, OS peak across all phases",
                              "second writer time includes contention and publishing its successful edit",
                              "allocator counts unavailable; reclaimed process memory does not measure live allocation directly"]}
        destination = output / "qt-scale.json"
        for label in ("python", "rust"):
            root = directory / label
            shutil.copytree(canonical, root)
            assert m1.hashes(root) == hashes
            environment = os.environ | {"READER_M4_SCALE_FIXTURE": str(root)}
            argv = ([sys.executable, "-B", "-X", "utf8", __file__, "--python-child", str(root)] if label == "python"
                    else [str(binary), "--ignored", "--nocapture", "--exact", "qt_scale_driver"])
            summary[label] = receive(argv, environment, output, label)
            summary[label]["persistence"] = persisted(root)
            m1.write_json(destination, summary)
        python, native = summary["python"], summary["rust"]
        assert python.keys() == native.keys()
        for phase in python:
            for field in ("output_sha256", "characters", "old_sha256", "new_sha256", "report"):
                assert python[phase].get(field) == native[phase].get(field), (phase, field)
        assert python["persistence"] == native["persistence"]
        assert m1.hashes(canonical) == hashes
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == native_hash
        assert rust_source_hashes() == rust and m1.source_hashes() == reference
        assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in harness} == harnesses
    print(destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--native-test", type=Path)
    mode.add_argument("--python-child", type=Path)
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    if args.python_child:
        python_child(args.python_child.resolve())
    else:
        assert 3 <= args.samples <= 30
        run(args.native_test.resolve(), args.samples)
