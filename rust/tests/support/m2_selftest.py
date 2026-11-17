"""Small self-checks for the independent M2 harness, without a Rust binary."""
import json
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import m2_capture as h
import m2_depth
import m2_browser


class HarnessTests(unittest.TestCase):
    def test_debugger_metadata_waits_for_readable_complete_valid_lines(self):
        values = [PermissionError(), "", "wrong\n/devtools/browser/x\n", "70000\n/devtools/browser/x\n",
                  "12345\nwrong\n", "12345\n/devtools/browser/x\n"]
        with patch.object(Path, "read_text", side_effect=values):
            for _ in range(5):
                self.assertIsNone(m2_browser.debugger_endpoint(Path("synthetic-profile")))
            self.assertEqual(m2_browser.debugger_endpoint(Path("synthetic-profile")), "http://127.0.0.1:12345")

    def test_depth_digest_preserves_types_without_recursive_walk(self):
        integer, boolean = 0, False
        for _ in range(1100):
            integer, boolean = [integer], [boolean]
        self.assertNotEqual(m2_depth.json_digest(integer), m2_depth.json_digest(boolean))
        self.assertEqual(m2_depth.json_digest({"a": 1, "b": 2}), m2_depth.json_digest({"b": 2, "a": 1}))

    def test_unreadable_response_is_retained_as_failure_evidence(self):
        response = MagicMock(status=200)
        body = b'{"x":' + b'1' * 4301 + b'}'
        response.read.return_value = body
        response.getheaders.return_value = [("Content-Type", "application/json")]
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(h.http.client, "HTTPConnection", return_value=connection):
            result = h.request(1, "/api/settings", "POST", raw=b"{}")
        self.assertIsNone(result["json"])
        self.assertIn("4301", result["decode_error"])
        self.assertEqual(result["body_hex"], body.hex())

    def test_refuses_non_temporary_fixture(self):
        with self.assertRaises(ValueError):
            h.fixture(h.m1.ROOT / "must-never-exist-m2")

    def test_differential_detects_payload_status_and_headers(self):
        response = {"status": 200, "headers": {"content-type": "application/json"},
                    "json": {"a": 1}, "body_hex": ""}
        expected = [{"case": ["GET", "/api/settings", None, {}], "response": response}]
        self.assertEqual(h.compare(expected, expected), [])
        actual = json.loads(json.dumps(expected))
        actual[0]["response"].update(status=400, json={"a": True}, headers={})
        failures = h.compare(expected, actual)
        self.assertTrue(any(f.get("field") == "status" for f in failures))
        self.assertTrue(any(f.get("field") == "json" for f in failures))
        self.assertTrue(any(f.get("header") == "content-type" for f in failures))

    def test_python_oracle_and_canonical_separation(self):
        with tempfile.TemporaryDirectory(prefix="reader-m2-selftest-") as temporary:
            output = Path(temporary)
            canonical = output / "canonical"
            h.fixture(canonical)
            before = h.m1.hashes(canonical)
            runtime = output / "oracle"
            h.shutil.copytree(canonical, runtime)
            with h.oracle(runtime) as (port, _):
                with self.assertLogs(level="ERROR"):
                    results = h.capture(port, runtime)
                self.assertEqual(len(results), len(h.corpus()))
                self.assertEqual(h.compare(results, results), [])
                h.conditional(port, runtime)
                self.assertTrue(all(r["response"]["status"] == 200 for r in h.encoded_json(port)))
                boundaries = h.parser_boundaries(port)
                for item in boundaries:
                    label = item["case"][2]["raw_case"]
                    self.assertEqual(item["response"]["status"], 400 if "4301" in label or "bom-only" in label else 200, label)
                settings = h.request(port, "/api/settings")["json"]
                self.assertEqual(settings["stored-reserved"], {"$serde_json::private::Number": "123"})
                self.assertEqual(settings["nested"], {"$serde_json::private::Number": "123"})
                self.assertNotIn("overflow-digits", settings)
                self.assertNotIn("negative-overflow-digits", settings)
                state = h.state(runtime)
                lease = next(r for r in state["chapter_readers"] if r["id"] == "durable")
                self.assertEqual((lease["book_id"], lease["sequence"]), ("0", 9))
                self.assertTrue(any(r["id"] == "orphan" for r in state["notes"]))
                notes = {r["id"]: r for r in state["notes"]}
                self.assertEqual(notes["minimum"]["chapter_index"], -(2 ** 63))
                self.assertEqual(notes["maximum"]["chapter_index"], 2 ** 63 - 1)
                self.assertFalse(set(notes) & {"overflow", "underflow", "beyond-u64", "string-overflow", "string-underflow"})
                self.assertTrue(any(r["book_id"] == "3" and r["kind"] == "files" for r in state["asset_refs"]))
                self.assertTrue(any(r["book_id"] == "3" and r["blocked"] == 1 for r in state["asset_pending"]))
                self.assertEqual(state["books"][2]["progress"], 0)
            self.assertEqual(before, h.m1.hashes(canonical))
            with contextlib.closing(sqlite3.connect(canonical / "library.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM chapter_readers").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
