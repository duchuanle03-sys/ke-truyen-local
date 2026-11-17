"""Scoped synthetic depth differential and reference-boundary probe; no timing claims."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

import m2_capture as h


def nested(depth):
    return b"[" * depth + b"0" + b"]" * depth


def json_digest(value):
    """Iterative typed digest keeps the verifier out of the tested recursion limit."""
    digest = hashlib.sha256()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            keys = sorted(item)
            digest.update(f"object:{len(keys)};".encode())
            for key in reversed(keys):
                pending.extend((item[key], key))
        elif isinstance(item, list):
            digest.update(f"array:{len(item)};".encode())
            pending.extend(reversed(item))
        else:
            digest.update((type(item).__name__ + ":" + json.dumps(item, ensure_ascii=False) + ";").encode())
    return digest.hexdigest()


def summary(response):
    return {"status": response["status"], "json_digest": json_digest(response["json"]),
            "error": response["json"] if response["status"] >= 400 else None,
            "decode_error": response.get("decode_error"),
            "headers": {key: response["headers"].get(key) for key in h.m1.SECURITY}}


def setting_row(root, key):
    with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return None if row is None else {"bytes": len(row[0].encode()), "sha256": hashlib.sha256(row[0].encode()).hexdigest()}


def remove_setting(root, key):
    with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
        db.commit()


def endpoint_probe(port, root, depth, pad_to=None):
    key = "depth-probe"
    raw = b'{"depth-probe":' + nested(depth) + b'}'
    if pad_to is not None:
        assert len(raw) <= pad_to
        raw += b" " * (pad_to - len(raw))
    response = h.request(port, "/api/settings", "POST", raw=raw)
    row = setting_row(root, key)
    read = h.request(port, "/api/settings")
    result = {"arrays": depth, "input_body_bytes": len(raw), "post": summary(response), "committed": row,
              "read_after": summary(read)}
    # Each probe gets a clean baseline even when the reference commits before a reply failure.
    remove_setting(root, key)
    assert h.request(port, "/api/settings")["status"] == 200
    return result


def reference_boundaries(port, root):
    results = {}
    def get(depth):
        if depth not in results:
            results[depth] = endpoint_probe(port, root, depth)
        return results[depth]
    for depth in (127, 128, 256, 512, 900, 950, 970, 980, 990, 999, 1000, 1024, 1100):
        get(depth)
    low, high = 512, 1100
    assert get(low)["post"]["status"] == 200 and get(high)["post"]["status"] != 200
    while high - low > 1:
        middle = (low + high) // 2
        if get(middle)["post"]["status"] == 200:
            low = middle
        else:
            high = middle
    for depth in range(low - 2, high + 3):
        get(depth)
    return {"last_accepted_arrays": low, "first_error_arrays": high,
            "probes": [results[depth] for depth in sorted(results)]}


def prepare_stored(canonical, root, depth):
    shutil.copytree(canonical, root)
    raw = nested(depth).decode()
    with contextlib.closing(sqlite3.connect(root / "library.sqlite3")) as db:
        db.create_function("shelf_fold", 1, str.casefold, deterministic=True)
        db.execute("INSERT INTO settings VALUES('stored-depth',?)", (raw,))
        db.execute("UPDATE books SET raw=? WHERE id='0'", ('{"warnings":[{"payload":' + raw + '}]}' ,))
        db.commit()


def supported(port, root, depth):
    result = {"stored_settings": summary(h.request(port, "/api/settings")),
              "stored_metadata": summary(h.request(port, "/api/books/0"))}
    result["post"] = summary(h.request(port, "/api/settings", "POST", raw=b'{"posted-depth":' + nested(depth) + b'}'))
    result["row"] = setting_row(root, "posted-depth")
    result["read_after"] = summary(h.request(port, "/api/settings"))
    return result


def run(args):
    output = h.m1.fresh_output(args.output)
    canonical = output / "canonical"
    h.fixture(canonical)
    canonical_hashes = h.m1.hashes(canonical)
    source_hashes = h.rust_source_hashes()
    report = {"output": str(output), "supported_depths": [127, 128, 256, 512], "failures": [],
              "approved_max_containers": 984,
              "approved_deviation": "Overbound JSON is rejected before writes; Python commit-then-500 is intentionally excluded.",
              "source_sha256": source_hashes, "reference_sha256": h.m1.source_hashes(),
              "binary_sha256": hashlib.sha256(Path(args.binary).read_bytes()).hexdigest() if args.binary else None}
    reference = output / "reference-boundary"
    shutil.copytree(canonical, reference)
    try:
        with h.oracle(reference) as (port, _):
            report["reference_boundary"] = reference_boundaries(port, reference)
        for depth in report["supported_depths"]:
            python_root = output / f"python-{depth}"
            prepare_stored(canonical, python_root, depth)
            with h.oracle(python_root) as (port, _):
                expected = supported(port, python_root, depth)
            assert all(expected[key]["status"] == 200 for key in ("stored_settings", "stored_metadata", "post", "read_after")), expected
            report[f"python_{depth}"] = expected
            if not args.binary:
                continue
            rust_root = output / f"rust-{depth}"
            prepare_stored(canonical, rust_root, depth)
            binary = Path(args.binary).resolve()
            with h.m1.child(h.command(binary, rust_root), output / f"rust-{depth}.log") as (port, process):
                actual = supported(port, rust_root, depth)
                report[f"rust_{depth}"] = actual
                if actual != expected:
                    report["failures"].append({"depth": depth, "expected": expected, "actual": actual})
                h.shutdown(port, process, rust_root)
            with h.m1.child(h.command(binary, rust_root), output / f"restart-{depth}.log") as (port, process):
                restarted = summary(h.request(port, "/api/settings"))
                if restarted != expected["read_after"]:
                    report["failures"].append({"restart_depth": depth, "expected": expected["read_after"], "actual": restarted})
                h.shutdown(port, process, rust_root)
            store = h.Store(rust_root)
            value = store.settings()["posted-depth"]
            for _ in range(depth):
                assert isinstance(value, list) and len(value) == 1
                value = value[0]
            assert type(value) is int and value == 0
            store.close_inventory()
            store.checkpoint()
        if args.binary:
            boundary_root = output / "rust-boundary"
            shutil.copytree(canonical, boundary_root)
            boundary = report["reference_boundary"]
            depths = (983, 984, 985, 1100)
            expected = {row["arrays"]: row for row in boundary["probes"]}
            with h.m1.child(h.command(Path(args.binary).resolve(), boundary_root), output / "rust-boundary.log") as (port, process):
                report["rust_boundary"] = []
                baseline = summary(h.request(port, "/api/settings"))
                baseline_state = h.state(boundary_root)
                for depth in depths:
                    actual = endpoint_probe(port, boundary_root, depth)
                    report["rust_boundary"].append(actual)
                    if depth == 983 and actual != expected[depth]:
                        report["failures"].append({"boundary_depth": depth, "expected": expected[depth], "actual": actual})
                    if depth > 983:
                        assert actual["post"]["status"] == 400 and actual["post"]["error"] == {"error": "Dữ liệu JSON lồng quá sâu."}, actual
                        assert actual["committed"] is None and actual["read_after"] == baseline, actual
                    assert h.state(boundary_root) == baseline_state, "depth request changed unrelated logical state"
                padded = endpoint_probe(port, boundary_root, 984, pad_to=2 * 1024 * 1024)
                assert padded["post"]["status"] == 400 and padded["post"]["error"] == {"error": "Dữ liệu JSON lồng quá sâu."}, padded
                assert padded["committed"] is None and padded["read_after"] == baseline
                assert h.state(boundary_root) == baseline_state
                report["padded_2MiB_overbound"] = padded
                h.shutdown(port, process, boundary_root)
            with h.m1.child(h.command(Path(args.binary).resolve(), boundary_root), output / "boundary-restart.log") as (port, process):
                assert summary(h.request(port, "/api/settings")) == baseline
                assert h.state(boundary_root) == baseline_state
                report["overbound_restart_healthy"] = True
                h.shutdown(port, process, boundary_root)
        assert h.m1.hashes(canonical) == canonical_hashes
        if args.binary:
            assert h.rust_source_hashes() == source_hashes
            assert hashlib.sha256(Path(args.binary).read_bytes()).hexdigest() == report["binary_sha256"]
    except Exception as error:
        import traceback
        report["failures"].append({"exception": repr(error), "traceback": traceback.format_exc()})
    finally:
        h.m1.write_json(output / "depth.json", report)
        print(json.dumps({"report": str(output / "depth.json"), "failures": len(report["failures"]),
                          "boundary": {k: v for k, v in report.get("reference_boundary", {}).items() if k != "probes"}}))
    return bool(report["failures"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary")
    parser.add_argument("--output")
    sys.exit(run(parser.parse_args()))
