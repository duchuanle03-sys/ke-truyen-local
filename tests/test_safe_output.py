import base64
import unittest
from htmltools import sanitize_html
from server import public_content

class OutputTests(unittest.TestCase):
    def test_library_covers_use_book_identity_to_allow_recovery(self):
        out = public_content({'id': 'book-id', 'source': 'hako', 'cover': 'https://cdn.example/old.jpg'})
        self.assertEqual(out['cover'], '/api/books/book-id/cover')

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
