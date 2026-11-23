"""Disposable HTTP and SQLite fixtures shared by the M3 differential tests."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import m2_capture as h
from library import Store


@contextmanager
def db(root):
    with closing(sqlite3.connect(root / "library.sqlite3", timeout=5)) as connection, connection:
        connection.create_function("shelf_fold", 1, str.casefold, deterministic=True)
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection


def query(root, sql, params=()):
    with db(root) as connection:
        return connection.execute(sql, params).fetchall()


def wait_for(action, ready=bool, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        result = action()
        if ready(result):
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"state failed to settle: {result!r}")
        time.sleep(.025)


def fixture(root):
    h.m1.fixture(root, "empty", sealed=False)
    return Store(root)


def finish(store, *, index_search=True):
    while not store.asset_index_ready():
        store.index_assets()
    while store.inventory_assets():
        pass
    while store.maintain_chapter_cache():
        pass
    if index_search:
        while store.index_search():
            pass
    store.close_inventory()
    store.checkpoint()
    marker_path = store.data_dir / h.m1.MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["sha256"] = hashlib.sha256(store.path.read_bytes()).hexdigest()
    h.m1.write_json(marker_path, marker)


def command(binary, root):
    if binary is not None:
        return h.command(binary, root)
    entry = (
        "import sys;sys.path.insert(0,sys.argv[1]);from m3_support import serve_reference;"
        "serve_reference(sys.argv[2])"
    )
    return [sys.executable, "-B", "-X", "utf8", "-c", entry, str(Path(__file__).parent.resolve()), str(root)]


def serve_reference(root):
    app = h.make_server(root, port=0, source_service=h.SourceService(fetcher=h.OfflineTransport()))
    app.app._image_fetcher.close()
    app.app._image_fetcher = h.OfflineTransport()
    try:
        print(json.dumps({"port": app.server_port}), flush=True)
        app.serve_forever()
    finally:
        app.server_close()


def stop(port, owner):
    cookie = h.request(port, "/")["headers"]["set-cookie"].split(";", 1)[0]
    token = h.request(port, "/api/settings", headers={"Cookie": cookie})["headers"]["x-shutdown-token"]
    response = h.request(port, "/api/shutdown", "POST", {"pause_downloads": True}, {"Cookie": cookie,
                                                                                 "X-Shutdown-Token": token})
    assert response["status"] == 200 and response["json"] == {"accepted": True}, response
    assert owner.wait(timeout=20) == 0


@contextmanager
def server(binary, root, label):
    with h.m1.child(command(binary, root), root.parent / f"{root.name}-{label}.log") as (port, owner):
        yield port, owner
        if owner.poll() is None:
            stop(port, owner)


class Capture:
    def __init__(self, port, events):
        self.port = port
        self.events = events

    def request(self, path, status=200, method="GET", body=None, headers=None, project=lambda value: value):
        response = h.request(self.port, path, method, body, headers)
        return self.record(response, path, status, method, project)

    def record(self, response, path, status, method, project):
        assert response["status"] == status, (method, path, status, response["status"], response["json"])
        fields = {key: response["headers"].get(key) for key in h.m1.SECURITY}
        value = response["json"]
        if method == "HEAD":
            assert not response["body_hex"]
        observed = project(value)
        if value is None and response["body_hex"]:
            body = bytes.fromhex(response["body_hex"])
            observed = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                        "content_type": response["headers"].get("content-type")}
        assert response.get("decode_error") is None, response
        self.events.append((method, path, status, fields, observed))
        return value

    def post(self, path, body, status=200, **kwargs):
        return self.request(path, status, "POST", body, **kwargs)


def verify_db(root):
    with db(root) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('integrity-check',1)")


def run(name, cases):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--case", choices=cases)
    args = parser.parse_args()
    binary = args.binary.resolve() if args.binary else None
    output = h.m1.fresh_output().resolve()
    report = {}
    print(f"{name} evidence: {output}", flush=True)
    for case, execute in cases.items():
        if args.case and args.case != case:
            continue
        reference = report[case] = {"python": [], "rust": []}
        try:
            execute(output / f"{case}-python", None, reference["python"])
            if binary:
                execute(output / f"{case}-rust", binary, reference["rust"])
                options = {"ensure_ascii": False, "sort_keys": True}
                python = [event for event in reference["python"] if not isinstance(event, dict)]
                rust = [event for event in reference["rust"] if not isinstance(event, dict)]
                for expected, actual in zip(python, rust, strict=True):
                    assert json.dumps(expected, **options) == json.dumps(actual, **options), (expected, actual)
        finally:
            h.m1.write_json(output / f"{name}.json", report)
