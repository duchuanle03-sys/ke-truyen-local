"""M6 document, backup and PDF/OCR import contracts on disposable synthetic libraries."""
import base64
import hashlib
import io
import json
import re
import socket
from urllib.parse import quote
import zipfile
import zlib

from m2_capture import PNG, request
from m3_support import Capture as BaseCapture, db, finish, fixture, query, run, server, verify_db, wait_for

FILE = "/api/import/file"
BACKUP = "/api/import/backup"
GIF = base64.b64decode("R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")
SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><script>x()</script>'
       b'<rect width="4" height="4" fill="red" onclick="x()"/></svg>')


class Capture(BaseCapture):
    """Records request paths with engine-specific book IDs replaced by stable labels."""
    mapping = {}

    def record(self, response, path, status, method, project):
        for identity, label in self.mapping.items():
            path = path.replace(identity, label)
        return super().record(response, path, status, method, project)


def zipped(entries, method=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in entries:
            # Fixed timestamps keep content-addressed upload names identical across engines.
            archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data, method)
    return buffer.getvalue()


def epub(spine_twice=False):
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
 <metadata><dc:title>Sách EPUB  thử</dc:title><dc:creator>Tác giả A</dc:creator><dc:creator>B</dc:creator>
  <dc:description>Mô tả &amp; tóm tắt</dc:description><meta name="cover" content="cov"/></metadata>
 <manifest>
  <item id="cov" href="images/cover.png" media-type="image/png" properties="cover-image"/>
  <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="text/Ch2.xhtml" media-type="application/xhtml+xml"/>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
 </manifest>
 <spine><itemref idref="c1"/><itemref idref="c2"/>""" + ('<itemref idref="c1"/>' if spine_twice else "") + """</spine>
</package>"""
    ch1 = """<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>
<title>Mở đầu</title><style>p { color: red }</style></head><body><section>
<p>Đoạn <b>một</b> &amp; <i>hai</i> &copy; &notanentity; &#x1F600;</p>
<img src="../images/a%20b.png" alt="Ảnh &quot;một&quot;"/><img src="../images/missing.png"/>
<img src="../images/d.svg"/><script>alert(1)</script><p>Cuối<br/>dòng</p></section></body></html>"""
    ch2 = """<html><body><h1>Chương 2</h1><p>Nội dung hai</p><h2>Chương 3</h2><ul><li>Một</li><li>Hai</li></ul>
<table><tr><td>Ô</td></tr></table><p><a href="http://x">liên kết</a></p></body></html>"""
    return zipped([
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'),
        ("OEBPS/content.opf", opf.encode()),
        ("OEBPS/text/ch1.xhtml", ch1.encode()),
        ("OEBPS/text/Ch2.xhtml", ch2.encode()),
        ("OEBPS/nav.xhtml", b"<html><body><nav>TOC</nav></body></html>"),
        ("OEBPS/images/cover.png", PNG),
        ("OEBPS/images/a b.png", PNG),
        ("OEBPS/images/d.svg", SVG),
    ])


def docx():
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {w}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chương Một</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Dòng &lt;một&gt; </w:t></w:r><w:r><w:tab/><w:t>tab</w:t><w:br/><w:t>xuống dòng</w:t></w:r></w:p>
<w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="heading 2"/></w:pPr><w:r><w:t>Chương Hai</w:t></w:r></w:p>
<w:p><w:r><w:t>Nội dung hai</w:t></w:r></w:p></w:body></w:document>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId5" Type="image" Target="media/image1.gif"/><Relationship Id="rId9" Type="hyperlink" Target="http://x" TargetMode="External"/></Relationships>"""
    core = """<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Tài liệu Word</dc:title><dc:creator>Người viết</dc:creator></cp:coreProperties>"""
    return zipped([("[Content_Types].xml", b"<Types/>"), ("word/document.xml", document.encode()),
                   ("word/_rels/document.xml.rels", rels.encode()), ("word/media/image1.gif", GIF),
                   ("docProps/core.xml", core.encode())])


ODT_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:xlink="http://www.w3.org/1999/xlink"')
ODT_BODY = """<office:body><office:text><text:h>Phần Một</text:h><text:p>A<text:s text:c="3"/>B<text:tab/>C<text:line-break/>D</text:p>
<text:p><draw:frame><draw:image xlink:href="Pictures/p.png"/></draw:frame></text:p><text:h>Phần Hai</text:h><text:p>Kết <text:span>thúc</text:span></text:p></office:text></office:body>"""


def odt():
    content = f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODT_NS}>{ODT_BODY}</office:document-content>'
    meta = '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta><dc:title>Văn bản ODT</dc:title><dc:creator>C</dc:creator></office:meta></office:document-meta>'
    return zipped([("mimetype", b"application/vnd.oasis.opendocument.text"), ("content.xml", content.encode()),
                   ("meta.xml", meta.encode()), ("Pictures/p.png", PNG)])


def fb2(doctype=False):
    data = base64.b64encode(PNG).decode()
    return (('<!DOCTYPE x>' if doctype else '') + f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info><author><first-name>Lev</first-name><last-name>Tolstoy</last-name></author><book-title>Chiến tranh</book-title></title-info></description>
<body><title><p>Tập 1</p></title><section><title><p>Chương I</p></title><p>Đoạn <emphasis>nghiêng</emphasis> và <strong>đậm</strong>.</p><empty-line/>
<image l:href="#pic"/><section><title><p>Mục con</p></title><p>Bên trong</p></section></section>
<section><title><p>Chương II</p></title><subtitle>Phụ đề</subtitle><epigraph><p>Lời đề</p></epigraph><poem><stanza><v>Thơ</v></stanza></poem></section></body>
<binary id="pic" content-type="image/png">{data[:20]}
{data[20:]}</binary></FictionBook>""").encode()


def hwpx():
    package = """<?xml version="1.0"?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<opf:metadata><dc:title>HWPX thử</dc:title><dc:creator>Kim</dc:creator></opf:metadata><opf:manifest>
<opf:item id="header" href="Contents/header.xml"/><opf:item id="section0" href="Contents/section0.xml"/><opf:item id="image1" href="BinData/image1.png"/></opf:manifest>
<opf:spine><opf:itemref idref="header"/><opf:itemref idref="section0"/></opf:spine></opf:package>"""
    section = """<?xml version="1.0"?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">
<hp:p><hp:run><hp:t>Chương 1 Khởi đầu</hp:t></hp:run></hp:p><hp:p><hp:run><hp:t>Văn bản<hp:tab/>có tab<hp:lineBreak/>dòng mới</hp:t></hp:run></hp:p>
<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:p><hp:run><hp:t>Ô bảng</hp:t></hp:run></hp:p></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>
<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p><hp:p><hp:run><hp:script>bad()</hp:script></hp:run></hp:p></hs:sec>"""
    return zipped([("mimetype", b"application/hwp+zip"), ("Contents/content.hpf", package.encode()),
                   ("Contents/header.xml", b'<hh:head xmlns:hh="urn:h"/>'), ("Contents/section0.xml", section.encode()),
                   ("BinData/image1.png", PNG), ("Scripts/default.js", b"x")])


HWPML = """<?xml version="1.0" encoding="UTF-8"?><HWPML><HEAD><DOCSUMMARY><TITLE>Tài liệu HWPML</TITLE><AUTHOR>Lee</AUTHOR></DOCSUMMARY></HEAD>
<BODY><SECTION><P><TEXT><CHAR>Chương 1 Bắt đầu</CHAR></TEXT></P><P><TEXT><CHAR>Nội<TAB/>dung<LINEBREAK/>tiếp</CHAR></TEXT></P>
<P><TEXT><PICTURE/></TEXT></P><TABLE><ROW><CELL><P><TEXT><CHAR>Ô</CHAR></TEXT></P></CELL></ROW></TABLE></SECTION>
<SECTION><P><TEXT><CHAR>Phần thứ hai</CHAR></TEXT></P></SECTION></BODY></HWPML>""".encode()

RTF = (rb"{\rtf1\ansi\ansicpg1258\deff0{\fonttbl{\f0\fcharset163 Times;}{\f1\fcharset0 Arial;}}{\colortbl;\red0;}"
       rb"{\info{\title Ignored}}\f0 Ch\'f2\u432?ng 1\par N\'f4i dung \b \'e0\b0 \tab tab\line{\v \'e2n}"
       rb"\par{\*\generator x;}\pard\plain\f1 Ch\u432?\u417?ng 2\par Xin \ldblquote ch\u224?o\rdblquote\~!\par}")


def corpus():
    deep = "<div>" * 150 + "x" + "</div>" * 150
    bomb = zipped([("story/a.txt", b"\0" * (3 * 1024 * 1024))])
    return [
        ("Truyện UTF8.txt", "Lời tựa\nChương 1 Khởi đầu\nDòng một & <b>\r\n\r\nChương 2: Tiếp\n第一章 开始\nDòng hai\n".encode()),
        ("ascii-cp.txt", "Xin chào bạn\n".encode("cp1258", errors="ignore")),
        ("tieng-trung.txt", "第一章 开始\n内容一\n第二章 结束\n内容二\n".encode("gb18030")),
        ("utf16.TXT", "﻿Chapter 1 Begin\nText\nChapter 2 End\nMore\n".encode("utf-16")),
        ("ghi-chu.md", b"# Tieu de\nDoan *nghieng* va **dam** `ma`\n### Muc nho\n```\n# not heading\n```\n## Hai ##\nCuoi\n"),
        ("trang.html", """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sách &amp; Truyện</title></head><body>
<h1>Phần 1</h1><p class=x>Đoạn <b>một</b> &copy; &amp &notit; &#128512;<br/>xuống</p><img src="x.png"><script>if (a<b) x()</script>
<h2>Phần 2</h2><div><section><p>Lồng</p></section></div><textarea>a &amp; <b></textarea><pre>  giữ   </pre></body></html>""".encode()),
        ("khai-bao.xhtml", '<?xml version="1.0" encoding="windows-1258"?><html><body><p>Ch\xe0o</p></body></html>'.encode("cp1258")),
        ("sach.epub", epub()),
        ("tai-lieu.docx", docx()),
        ("van-ban.odt", odt()),
        ("phang.fodt", f'<?xml version="1.0"?><office:document {ODT_NS}>{ODT_BODY}</office:document>'.encode()),
        ("chien-tranh.fb2", fb2()),
        ("goi.fbz", zipped([("book/war.fb2", fb2())])),
        ("tong-hop.zip", zipped([("story/index.html", "<html><head><title>Trong ZIP</title></head><body><p>Có ảnh</p><img src='img/1.png'><img src='http://x/y.png'></body></html>".encode()),
                                ("story/img/1.png", PNG), ("story/notes.bin", b"x"), ("__MACOSX/story/._index.html", b"x")])),
        ("truyen-tranh.cbz", zipped([("tap 1/10.png", PNG), ("tap 1/2.png", PNG), ("tap 1/02.png", GIF), ("bia.gif", GIF),
                                     (".DS_Store", b"x"), ("tap 1/thumbs.db", b"x"), ("tap 1/readme.txt", b"x")], zipfile.ZIP_STORED)),
        ("van-ban.rtf", RTF),
        ("han.hwpx", hwpx()),
        ("han.hwpml", HWPML),
    ], [
        ("sach.mobi", b"BOOKMOBI", 400),
        ("README", b"text", 400),
        ("rong.txt", b"  \n\t\n", 400),
        ("nhi-phan.txt", b"a\x00b", 400),
        ("khong-ro.txt", b"\x81\x00", 400),
        ("dtd.fb2", fb2(doctype=True), 400),
        ("long-sau.fb2", ("<FictionBook>" + "<a>" * 50_000 + "x" + "</a>" * 50_000 + "</FictionBook>").encode(), 400),
        ("sau.html", deep.encode(), 400),
        ("gia.pdf", b"not a pdf", 400),
        ("lap.epub", epub(spine_twice=True), 400),
        ("bom.zip", bomb, 400),
        ("thoat.zip", zipped([("../evil.txt", b"x")]), 400),
        ("loi.zip", b"PK\x03\x04broken", 400),
        ("tep.dot", b"x", 400),
    ]


def ids(value, mapping):
    if isinstance(value, dict):
        return {key: ids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [ids(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def projector(root, mapping):
    text_root = str(root)
    variants = [json.dumps(text_root)[1:-1], text_root]

    def project(value):
        if value is None:
            return None
        text = json.dumps(value, ensure_ascii=False)
        for variant in variants:
            text = text.replace(variant, "<root>")
        value = json.loads(text)
        value = ids(value, mapping)
        def walk(item):
            if isinstance(item, dict):
                return {key: ("<time>" if key in ("added_at", "updated_at", "read_at", "trashed_at", "purge_at", "trash_expires_at")
                              and isinstance(val, (int, float)) and val else walk(val)) for key, val in item.items()}
            if isinstance(item, list):
                return [walk(v) for v in item]
            return item
        return walk(value)
    return project


def upload(api, name, data, status=200, project=None):
    headers = {"Content-Type": "application/octet-stream", "X-Filename": quote(name)}
    return api.record(raw_request(api.port, name, data, headers), FILE, status, "POST", project or (lambda v: v))


def raw_request(port, name, data, headers):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        conn.request("POST", FILE, data, headers)
        response = conn.getresponse()
        body = response.read()
        fields = {k.lower(): v for k, v in response.getheaders()}
        result = {"status": response.status, "headers": fields, "body_hex": body.hex(), "json": None}
        if body and fields.get("content-type", "").startswith("application/json"):
            result["json"] = json.loads(body)
        return result
    finally:
        conn.close()


def truncated(port):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"POST /api/import/file HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/octet-stream\r\n"
                     b"X-Filename: a.txt\r\nContent-Length: 100\r\n\r\nshort" % port)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def book_id(root, name, data):
    extension = name[name.rfind("."):].lower()
    path = str(root.resolve() / "files" / (hashlib.sha256(data).hexdigest() + extension))
    return hashlib.sha256(("file:" + path).encode()).hexdigest()[:32]


def documents(root, binary, events):
    finish(fixture(root))
    good, bad = corpus()
    mapping = {book_id(root, name, data): f"<book:{name}>" for name, data in good}
    project = projector(root, mapping)
    with server(binary, root, "documents") as (port, _):
        api = Capture(port, events)
        api.mapping = mapping
        for name, data in good:
            book = upload(api, name, data, project=project)
            identity = book["id"]
            chapters = api.request(f"/api/books/{identity}/chapters", project=project)
            for chapter in chapters:
                api.request(f"/api/books/{identity}/chapters/{chapter['index']}", project=project)
        for name, data, status in bad:
            upload(api, name, data, status, project=project)
        events.append(("truncated", truncated(port)))
        events.append(("oversize", raw_request(port, "x.txt", b"", {"Content-Length": str(128 * 1024 * 1024 + 1),
                                                                      "X-Filename": "x.txt"})["status"]))
        name, data = good[0]
        identity = book_id(root, name, data)
        api.post(f"/api/books/{identity}/progress", {"index": 1, "position": 0.5}, project=project)
        upload(api, name, data, project=project)
        api.post(f"/api/books/{identity}/trash", {}, project=project)
        restored = upload(api, name, data, project=project)
        assert restored["trashed_at"] == 0 and restored["chapter_index"] == 1, restored
        api.request(f"/api/books/{identity}/file", 400, project=project)
        # Equal titles tie-break on book id, which embeds each engine's own data path.
        api.request("/api/books", project=lambda v: {**project(v), "items": sorted(project(v)["items"], key=lambda b: b["id"])})
        # Leased uploads are retried by the background collector; compare the settled state, not a race.
        wait_for(lambda: not query(root, "SELECT 1 FROM asset_gc_candidates WHERE kind='files' LIMIT 1"), timeout=30)
    with db(root) as connection:
        stored = connection.execute("SELECT kind,name FROM asset_gc_candidates WHERE kind='files' ORDER BY name").fetchall()
        events.append(("candidates", [row[1] for row in stored]))
        events.append(("cache", connection.execute("SELECT kind,count(*) FROM chapter_cache GROUP BY kind ORDER BY kind").fetchall()))
    files = sorted(p.name for p in (root / "files").iterdir())
    events.append(("files", files))
    verify_db(root)


def make_pdf(lines):
    content = "BT /F1 48 Tf 72 700 Td 56 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 6 0 R >> >> >>",
               f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def pdf(root, binary, events):
    finish(fixture(root))
    data = make_pdf(["HELLO READER", "SECOND LINE 42"])
    mapping = {book_id(root, "scan.pdf", data): "<book:scan.pdf>"}
    project = projector(root, mapping)
    with server(binary, root, "pdf") as (port, _):
        api = Capture(port, events)
        api.mapping = mapping
        capabilities = api.request("/api/capabilities", project=lambda v: {
            "documents": project(v["documents"]), "ocr_supported": v["ocr"]["supported"],
            "keys": list(v), "sources": v["sources"], "limitations": v["limitations"], "tts": v["tts"]})
        assert capabilities["documents"]["pdf"]["supported"], capabilities["documents"]["pdf"]
        book = upload(api, "scan.pdf", data, project=project)
        assert book["format"] == "pdf" and book["ocr"]["pages"] == 2, book
        assert "HELLO" in api.request(f"/api/books/{book['id']}/chapters/0", project=project)["text"].upper()
        api.request(f"/api/books/{book['id']}/chapters/1", 400, project=project)
        api.request(f"/api/books/{book['id']}/file", project=project)
        api.request(f"/api/books/{book['id']}/file", method="HEAD", project=project)
    verify_db(root)


def backup_tree():
    books = {
        "a1": ({"book_id": "a1", "name": "Truyện QQ", "book_source": "TruyenQQ", "url": "https://truyenqq.example/truyen/a1",
                "book_type": 2, "last_index": 2, "last_position": 0.25, "percent": "0.5", "book_folder": [3, "-1"],
                "timestamp": 1700000000000, "chapter_count": 3},
               [{"name": "C1", "url": "https://truyenqq.example/truyen/a1/c1.html"},
                {"name": "", "url": "https://truyenqq.example/truyen/a1/c2.html"}],
               {"description": "Tóm tắt"}),
        "b2": ({"book_id": "b2", "name": "Sách tệp", "source": "local", "path": "/sdcard/b2.epub", "chapter_count": 1},
               [{"title": "Một", "path": "x/1", "is_vip": False, "access": {"state": "x"}}], None),
    }
    files = {}
    for key, (book, chapters, novel) in books.items():
        files[f"books/{key}/book.json"] = json.dumps(book, ensure_ascii=False)
        files[f"books/{key}/chapters.json"] = json.dumps(chapters, ensure_ascii=False)
        if novel is not None:
            files[f"books/{key}/novel.json"] = json.dumps(novel, ensure_ascii=False)
    files["books/a1/names.json"] = '{"x": "y"}'
    files["prefs/setting.json"] = json.dumps({"shelf.group.list": json.dumps({"3": "Nhóm ba", "4": "Bốn"})})
    files["data/notes.json"] = json.dumps([{"id": 7, "book_id": "a1", "chap_index": 1, "content": "Ghi chú", "position": 0.5,
                                            "timestamp": 1700000000500}, {"a": "z", "name": "missing", "content": "Mồ côi"}])
    files["data/bookmarks.json"] = json.dumps([{"id": "m", "book_id": "b2", "title": "Dấu"}])
    files["data/trashes.json"] = "[]"
    return files


def backup(root, binary, events):
    finish(fixture(root))
    folder = root.parent / f"{root.name}-backup"
    for relative, text in backup_tree().items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("﻿" + text if relative.endswith("names.json") else text, encoding="utf-8")
    archive = root.parent / f"{root.name}-backup.zip"
    archive.write_bytes(zipped([("export/" + k, v.encode()) for k, v in backup_tree().items()]))
    two = root.parent / f"{root.name}-two.zip"
    two.write_bytes(zipped([("x/books/a/book.json", b"{}"), ("y/books/b/book.json", b"{}")]))
    project = projector(root, {})
    with server(binary, root, "backup") as (port, _):
        api = Capture(port, events)
        api.post(BACKUP, {"path": str(folder)}, project=project)
        api.post(BACKUP, {"path": str(archive)}, project=project)
        api.request("/api/books?limit=10", project=project)
        for identity in ("a1", "b2"):
            api.request(f"/api/books/{identity}", project=project)
            api.request(f"/api/books/{identity}/chapters", project=project)
        api.request("/api/groups", project=project)
        api.request("/api/notes?book_id=a1", project=project)
        api.post(BACKUP, {"path": str(two)}, 400, project=project)
        api.post(BACKUP, {"path": str(root / "missing")}, 400, project=project)
        api.post(BACKUP, {"path": str(root / "library.sqlite3")}, 400, project=project)
        api.post(BACKUP, {"path": str(root)}, 400, project=project)
        reordered = folder / "books" / "a1" / "chapters.json"
        reordered.write_text(json.dumps([{"name": "C2", "url": "https://truyenqq.example/truyen/a1/c2.html"}]), encoding="utf-8")
        api.post(BACKUP, {"path": str(folder)}, 400, project=project)
    with db(root) as connection:
        events.append(("legacy", connection.execute("SELECT path FROM legacy ORDER BY path").fetchall()))
        events.append(("notes", connection.execute("SELECT id,book_id,chapter_index,quote,note,type,position,created_at FROM notes ORDER BY id").fetchall()))
    verify_db(root)


def stall(root, binary, events):
    """A client that stops sending its upload body is disconnected like the reference's 30 s socket timeout."""
    import time
    finish(fixture(root))
    with server(binary, root, "stall") as (port, _):
        with socket.create_connection(("127.0.0.1", port), timeout=60) as sock:
            sock.sendall(b"POST /api/import/file HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Filename: a.txt\r\n"
                         b"Content-Length: 1000\r\n\r\npartial" % port)
            start = time.monotonic()
            try:
                data = sock.recv(65536)
                while data and b"\r\n\r\n" not in data:
                    data += sock.recv(65536)
            except OSError:
                data = b""
            waited = time.monotonic() - start
        assert 25 <= waited <= 45, waited
        events.append(("stalled-upload-closed", True))
        assert request(port, "/api/books")["status"] == 200


def crash(root, binary, events):
    """Kill the server mid-import; restart must keep SQLite consistent and collect an orphaned upload."""
    import threading
    import time
    from m3_support import wait_for
    finish(fixture(root))
    chapters = "".join(f"Chương {i} Tên\n" + "Nội dung dài. " * 400 + "\n" for i in range(6000))
    data = chapters.encode()
    upload_path = root / "files" / (hashlib.sha256(data).hexdigest() + ".txt")
    with server(binary, root, "crash-import") as (port, owner):
        sender = threading.Thread(target=lambda: _quiet(raw_request, port, "lon.txt", data,
                                                       {"X-Filename": "lon.txt"}), daemon=True)
        sender.start()
        wait_for(upload_path.exists, timeout=60)
        time.sleep(.05)
        owner.kill()
        owner.wait(timeout=20)
    with server(binary, root, "crash-restart") as (port, _):
        api = Capture(port, events)
        books = api.request("/api/books", project=lambda v: {"total": v["total"]})
        if books["total"]:
            assert upload_path.exists(), "a committed import must keep its source file"
        else:
            wait_for(lambda: not upload_path.exists(), timeout=30)
        events.append(("consistent", True))
    verify_db(root)


def _quiet(function, *args):
    try:
        function(*args)
    except OSError:
        pass


if __name__ == "__main__":
    run("m6", {"documents": documents, "backup": backup, "pdf": pdf, "crash": crash, "stall": stall})
