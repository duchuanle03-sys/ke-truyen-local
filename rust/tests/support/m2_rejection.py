"""Synthetic diagnostic for rejected small request bodies; never retries a failed request."""
import argparse
import http.client
import json
from pathlib import Path
import shutil
import socket
import sys
import time

import m2_capture as h


def rejected_request(port, mode):
    body = b"" if mode in ("zero", "stalled", "advertised-65537") else b"{}"
    if mode == "limit-65536":
        body = b"x" * 65536
    length = 2 if mode == "stalled" else 65537 if mode == "advertised-65537" else len(body)
    headers = (f"POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode()
    result = {"mode": mode, "advertised_bytes": length, "sent_body_bytes": len(body)}
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            if mode in ("buffered", "zero", "stalled", "advertised-65537", "limit-65536"):
                connection.sendall(headers + body)
            else:
                connection.sendall(headers)
                if mode == "delayed":
                    time.sleep(.05)
                connection.sendall(body)
        except OSError as error:
            result["send_error"] = repr(error)
        response = http.client.HTTPResponse(connection)
        try:
            response.begin()
            result["status"] = response.status
            result["headers"] = {k.lower(): v for k, v in response.getheaders()}
            data = response.read()
            result["json"] = json.loads(data)
            result["received_bytes"] = len(data)
        except (OSError, http.client.HTTPException, ValueError) as error:
            result["response_error"] = repr(error)
        finally:
            response.close()
    result["elapsed_ms"] = (time.monotonic() - started) * 1000
    try:
        result["alive_after"] = h.request(port, "/api/books")["status"]
    except Exception as error:
        result["alive_error"] = repr(error)
    return result


def run(args):
    output = h.m1.fresh_output(args.output)
    canonical = output / "canonical"
    h.fixture(canonical)
    before = h.m1.hashes(canonical)
    results = []
    for language in ("python", "rust"):
        cases = [(warm, mode, sample) for warm in (False, True)
                 for mode in ("zero", "buffered", "split", "delayed") for sample in range(args.samples)]
        cases += [(False, mode, 0) for mode in ("limit-65536", "advertised-65537", "stalled")]
        for warm, mode, sample in cases:
            label = f"{language}-{warm}-{mode}-{sample}"
            root = output / label
            shutil.copytree(canonical, root)
            context = h.oracle(root) if language == "python" else h.m1.child(
                h.command(Path(args.binary).resolve(), root), output / (label + ".log"))
            with context as (port, owner):
                if warm:
                    assert h.request(port, "/api/books")["status"] == 200
                row = {"language": language, "warm": warm, "sample": sample,
                       **rejected_request(port, mode)}
                if language == "rust":
                    row["exit_after"] = owner.poll()
                results.append(row)
    assert h.m1.hashes(canonical) == before
    h.m1.write_json(output / "rejection.json", results)
    expected = {(r["warm"], r["mode"], r["sample"]): r for r in results if r["language"] == "python"}
    failures = [r for r in results if r.get("status") != 403 or "response_error" in r or "send_error" in r
                or r.get("alive_after") != 200 or r.get("exit_after") is not None]
    for row in results:
        reference = expected[(row["warm"], row["mode"], row["sample"])]
        if row.get("json") != reference.get("json") or any(
                row.get("headers", {}).get(key) != reference.get("headers", {}).get(key) for key in h.m1.SECURITY):
            if row not in failures:
                failures.append(row)
    print(json.dumps({"report": str(output / "rejection.json"), "requests": len(results),
                      "wire_failures": len(failures),
                      "failed_cases": [[r["language"], r["warm"], r["mode"], r["sample"]] for r in failures]}))
    return bool(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    assert 1 <= args.samples <= 10
    sys.exit(run(args))
