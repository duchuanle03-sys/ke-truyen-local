"""Small checks for the verifier itself; no production data or network."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest

from capture_reference import Store, compare, fixture, fresh_output, hashes, python_query, summary


class HarnessTests(unittest.TestCase):
    def test_output_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "keep").write_bytes(b"keep")
            before = hashes(path)
            with self.assertRaises(ValueError):
                fresh_output(path)
            self.assertEqual(before, hashes(path))

    def test_fixture_never_reuses_existing_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture"
            fixture(path, "empty")
            before = hashes(path)
            with self.assertRaises(FileExistsError):
                fixture(path, "small")
            self.assertEqual(before, hashes(path))

    def test_comparison_detects_json_headers_status_and_head_length(self):
        expected = [{"method": "HEAD", "path": "/api/books", "response": {
            "status": 200, "json": None, "body_hex": "", "headers": {
                "content-length": "100", "x-content-type-options": "nosniff"}}}]
        self.assertEqual(compare(expected, copy.deepcopy(expected)), [])
        actual = copy.deepcopy(expected)
        actual[0]["response"].update(status=400, json={"error": "bad"}, body_hex="61")
        actual[0]["response"]["headers"] = {"content-length": "1"}
        self.assertGreaterEqual(len(compare(expected, actual)), 4)

    def test_percentile_uses_nearest_rank_and_keeps_raw_samples(self):
        samples = list(range(1, 31))
        result = summary(samples)
        self.assertEqual((result["n"], result["p50_ms"], result["p95_ms"], result["max_ms"]), (30, 15.5, 29, 30))
        self.assertEqual(result["samples"], samples)

    def test_query_trace_excludes_background_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            original = store.list_books
            def maintenance():
                with store.db() as db:
                    db.execute("SELECT book_id,idx FROM chapters LIMIT 1").fetchall()
            def concurrent_list():
                worker = threading.Thread(target=maintenance)
                worker.start()
                worker.join()
                return original()
            store.list_books = concurrent_list
            try:
                result = python_query(store)
                self.assertEqual((result["n"], result["fold_calls"]), (30, 0))
                self.assertTrue(result["query_plans"])
            finally:
                store.close_inventory()
                store.checkpoint()

    def test_process_cold_query_worker_closes_and_rejects_unmarked_data(self):
        with tempfile.TemporaryDirectory(prefix="reader-m1-") as temporary:
            root = Path(temporary) / "cold-python-query-0"
            fixture(root, "empty")
            script = Path(__file__).with_name("capture_reference.py")
            command = [sys.executable, "-B", "-X", "utf8", str(script), "--oracle-server", str(root), "--oracle-query"]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertGreater(result["json_bytes"], 0)
            self.assertGreater(result["elapsed_ms"], 0)
            (root / "m1-fixture.json").unlink()
            before = hashes(root)
            rejected = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(before, hashes(root))


if __name__ == "__main__":
    unittest.main()
