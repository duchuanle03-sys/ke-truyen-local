"""Replay exact synthetic source responses in separate disposable HTTP processes."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit

from m2_capture import m1
from m3_support import command, stop
from server import make_server
from source_errors import SourceError
from sources import SourceService
from stv_source import StvSource


MANIFEST = "m5-sources.json"
FORMAT = "reader-local-m5-sources-1"


def seed_transport(root, pages, images=None, stv=None):
    images = {url: base64.b64encode(data).decode("ascii") for url, data in (images or {}).items()}
    manifest = {"format": FORMAT, "pages": pages, "images": images}
    if stv is not None:
        manifest["stv"] = stv
    m1.write_json(root / MANIFEST, manifest)


class RecordedTransport:
    def __init__(self, data):
        assert data["format"] == FORMAT
        self.pages = data["pages"]
        self.images = data["images"]

    def __call__(self, url):
        if url not in self.pages:
            raise SourceError(f"Synthetic source page missing: {url}", code="network_error")
        return self.pages[url]

    def _read(self, url, headers=None, max_bytes=None):
        if url not in self.images:
            raise SourceError(f"Synthetic source image missing: {url}", code="network_error")
        image = base64.b64decode(self.images[url], validate=True)
        assert max_bytes is None or len(image) <= max_bytes
        return image, None

    def close(self):
        pass


class RecordedStv:
    def __init__(self, replies):
        self.replies = list(replies)
        self.generation = 1
        self.seed_cookie = None

    def request(self, path, **options):
        origin = "https://sangtacviet.com"
        address = urljoin(origin, path)
        method = options.get("method", "GET")
        body = options.get("body", "")
        if options.get("seed"):
            assert self.seed_cookie
            body = self.seed_cookie
        for index, reply in enumerate(self.replies):
            captcha = origin + "/generate_captcha.php"
            same_url = address == reply["url"] or (
                reply["url"] == captcha and address.split("?", 1)[0] == captcha and
                urlsplit(address).query.startswith("random="))
            if not same_url or reply.get("method", "GET") != method or reply.get("request_body", body) != body:
                continue
            if not reply.get("repeat", False):
                self.replies.pop(index)
            if options.get("session") == "reading":
                for key, value in reply.get("headers", []):
                    if key.lower() == "set-cookie" and value.startswith("_ac="):
                        self.seed_cookie = value.split(";", 1)[0][len("_ac="):]
            if "body_base64" in reply:
                headers = {key.lower(): value for key, value in reply["headers"]}
                return {"body_base64": reply["body_base64"], "content_type": headers["content-type"]}
            assert reply.get("status", 200) == 200
            return {"text": reply.get("text", ""), "request_url": address, "request_method": method}
        raise SourceError(f"Synthetic STV response missing: {method} {address}", code="network_error")

    def close(self):
        pass


def serve_reference(root):
    data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    transport = RecordedTransport(data)
    sources = SourceService(fetcher=transport)
    sources._stv_source = StvSource(http=RecordedStv(data.get("stv", [])))
    app = make_server(root, port=0, source_service=sources)
    app.app._image_fetcher.close()
    app.app._image_fetcher = transport
    try:
        print(json.dumps({"port": app.server_port}), flush=True)
        app.serve_forever()
    finally:
        app.server_close()


@contextmanager
def fixture_server(binary, root, label):
    if binary is None:
        script = ("import sys;sys.path.insert(0,sys.argv[1]);from m5_support import Path,serve_reference;"
                  "serve_reference(Path(sys.argv[2]))")
        argv = [sys.executable, "-B", "-X", "utf8", "-c", script, str(Path(__file__).parent.resolve()), str(root)]
    else:
        argv = command(binary, root)
    with m1.child(argv, root.parent / f"{root.name}-{label}.log") as (port, owner):
        yield port, owner
        if owner.poll() is None:
            stop(port, owner)
