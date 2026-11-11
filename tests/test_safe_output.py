import base64
import unittest
from htmltools import sanitize_html
from server import public_content

class OutputTests(unittest.TestCase):
    def test_svg_document_images_drop_active_content_and_external_resources(self):
        from htmltools import sanitize_svg
        raw = b'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="bad()" viewBox="0 0 20 20">
          <script>bad()</script><foreignObject><iframe src="http://localhost"/></foreignObject>
          <image href="http://localhost/private"/><use xlink:href="file:///secret"/>
          <animate attributeName="href" values="javascript:bad()"/>
          <style>@import 'http://localhost/x'; .a {fill:url(http://localhost/x);}</style>
          <path onclick="bad()" d="M0 0L10 10" style="fill:url(http://localhost/x);stroke:#000"/>
        </svg>'''
        clean = sanitize_svg(raw).decode()
        for bad in ('script', 'foreignObject', 'iframe', 'localhost', 'file:', 'javascript:', 'onload', 'onclick', 'animate', '<use', '<image'):
            self.assertNotIn(bad, clean)
        self.assertIn('path', clean)
        self.assertIn('stroke:#000', clean)
        for bad in (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]><svg>&x;</svg>',
                    b'<html/>', b'<svg>' + b'<g>' * 110 + b'</g>' * 110 + b'</svg>'):
            self.assertEqual(sanitize_svg(bad), b'')

    def test_library_covers_use_book_identity_to_allow_recovery(self):
        out = public_content({'id': 'book-id', 'source': 'hako', 'cover': 'https://cdn.example/old.jpg'})
        self.assertTrue(out['cover'].startswith('/api/books/book-id/cover?v='))
        self.assertEqual(out, public_content({'id': 'book-id', 'source': 'hako', 'cover': 'https://cdn.example/old.jpg'}))
        changed = public_content({'id': 'book-id', 'source': 'hako', 'cover': 'https://cdn.example/new.jpg'})
        self.assertNotEqual(out['cover'], changed['cover'])

    def test_stored_backup_html_cannot_inject_active_markup(self):
        out = public_content({"html": '<p onclick="x()">Text<script>alert(1)</script><img src="http://127.0.0.1/private"></p>'})
        self.assertNotIn("<script", out["html"])
        self.assertNotIn("onclick", out["html"])
        self.assertNotIn("127.0.0.1/private", out["html"])

    def test_remote_covers_and_images_always_use_validated_proxy(self):
        out = public_content({"cover": "https://cdn.example/cover.jpg",
                              "html": '<p>A<img src="https://cdn.example/page.png"></p>'})
        self.assertTrue(out["cover"].startswith("/api/image?"))
        self.assertIn('src="/api/image?', out["html"])
        self.assertNotIn('src="https://', out["html"])

    def test_embedded_raster_document_image_survives_but_svg_does_not(self):
        data = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n"+b"x"*20).decode()
        out = sanitize_html('<img src="'+data+'"><img src="data:image/svg+xml;base64,PHN2Zz4=">',
                            allow_data_images=True)
        self.assertIn(data, out)
        self.assertNotIn("svg", out)

if __name__ == "__main__":
    unittest.main()
