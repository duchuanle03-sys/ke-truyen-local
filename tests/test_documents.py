import base64
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from documents import DocumentError, UnsupportedFormatError, capabilities, import_document


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, data):
        path = self.root / name
        path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
        return path

    def archive(self, name, entries, compression=zipfile.ZIP_STORED):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=compression) as archive:
            for key, value in entries.items():
                archive.writestr(key, value)
        return path

    def test_txt_utf16_preserves_chapters_and_escapes_markup(self):
        path = self.file('story.txt', 'Chương 1: Đến\n<script>hi</script>\n\nChương 2: Đi\nTạm biệt'.encode('utf-16'))
        result = import_document(path)
        self.assertEqual([c['title'] for c in result['chapters']], ['Chương 1: Đến', 'Chương 2: Đi'])
        self.assertIn('&lt;script&gt;', result['chapters'][0]['html'])
        self.assertEqual(result['book']['encoding'], 'utf-16')
        self.assertEqual(path.read_bytes(), 'Chương 1: Đến\n<script>hi</script>\n\nChương 2: Đi\nTạm biệt'.encode('utf-16'))

    def test_gb18030_and_binary_rejection(self):
        result = import_document(self.file('cn.txt', '第一章 开始\n你好世界'.encode('gb18030')))
        self.assertIn('你好世界', result['chapters'][0]['text'])
        with self.assertRaises(DocumentError):
            import_document(self.file('fake.txt', b'\x00\x01\x02\xffbinary'))

    def test_html_titles_chapters_and_active_content_removed(self):
        result = import_document(self.file('web.html', '<title>My Book</title><script>alert(1)</script><h1>One</h1><p onclick="x()">Hello <b>reader</b></p><iframe src="file:///x"></iframe><h1>Two</h1><a href="javascript:evil()">Next</a><img src="http://127.0.0.1/private">'))
        self.assertEqual(result['book']['name'], 'My Book')
        self.assertEqual([c['title'] for c in result['chapters']], ['One', 'Two'])
        joined = ''.join(c['html'] for c in result['chapters'])
        for forbidden in ('<script', 'alert(', 'onclick', 'javascript:', '<iframe', '127.0.0.1'):
            self.assertNotIn(forbidden, joined)
        self.assertIn('<b>reader</b>', joined)

    def test_markdown_headings_and_plain_paragraphs(self):
        result = import_document(self.file('story.md', '# First\n\nHello **world**.\n\n# Second\n\nGoodbye.'))
        self.assertEqual([c['title'] for c in result['chapters']], ['First', 'Second'])
        self.assertIn('<strong>world</strong>', result['chapters'][0]['html'])

    def test_epub_spine_order_metadata_and_embedded_image(self):
        path = self.archive('book.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
            'OPS/book.opf': '<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>A book</dc:title><dc:creator>An Author</dc:creator></metadata><manifest><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
            'OPS/two.xhtml': '<h1>Second</h1><p>End</p>',
            'OPS/one.xhtml': '<h1>First</h1><p>Start</p><img src="images/a.png">',
            'OPS/images/a.png': PNG,
        })
        result = import_document(path)
        self.assertEqual(result['book']['name'], 'A book')
        self.assertEqual(result['book']['author'], 'An Author')
        self.assertEqual([c['title'] for c in result['chapters']], ['First', 'Second'])
        self.assertTrue(result['chapters'][0]['images'][0].startswith('data:image/png;base64,'))

    def test_epub_cover_uses_declared_image_outside_spine_in_epub2_and_epub3(self):
        for metadata, properties in [('<meta name="cover" content="jacket"/>', ''),
                                     ('', 'properties="cover-image"')]:
            with self.subTest(metadata=metadata, properties=properties):
                path = self.archive('cover.epub', {
                    'META-INF/container.xml': '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
                    'OPS/book.opf': '<package><metadata>' + metadata + '</metadata><manifest>'
                        '<item id="logo" href="images/logo.png" media-type="image/png"/>'
                        '<item id="jacket" href="images/jacket%20image.png" media-type="image/png" ' + properties + '/>'
                        '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                        '</manifest><spine><itemref idref="chapter"/></spine></package>',
                    'OPS/chapter.xhtml': '<h1>One</h1><p>Readable text.</p>',
                    'OPS/images/logo.png': PNG + b'logo',
                    'OPS/images/jacket image.png': PNG,
                })
                original = path.read_bytes()
                result = import_document(path)
                self.assertEqual(result['book'].get('cover', ''), 'data:image/png;base64,' + base64.b64encode(PNG).decode())
                self.assertIn('Readable text.', result['chapters'][0]['text'])
                self.assertEqual(path.read_bytes(), original)

    def test_epub_cover_cannot_reference_outside_archive_or_non_raster_bytes(self):
        for href, body in [('../../private.png', PNG), ('https://example.com/cover.png', PNG),
                           ('images/cover.png', b'<svg onload="bad()"/>')]:
            with self.subTest(href=href):
                path = self.archive('unsafe-cover.epub', {
                    'META-INF/container.xml': '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
                    'OPS/book.opf': '<package><metadata><meta name="cover" content="jacket"/></metadata>'
                        '<manifest><item id="jacket" href="' + href + '" media-type="image/png"/>'
                        '<item id="chapter" href="chapter.xhtml"/></manifest>'
                        '<spine><itemref idref="chapter"/></spine></package>',
                    'OPS/chapter.xhtml': '<p>Readable text.</p>',
                    'OPS/images/cover.png': body,
                })
                with self.assertRaises(DocumentError):
                    import_document(path)

    def test_epub_recovers_image_case_and_skips_only_missing_optional_images(self):
        path = self.archive('case.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><metadata><meta name="cover" content="cover"/></metadata><manifest>'
                '<item id="cover" href="images/COVER.png"/><item id="one" href="one.xhtml"/></manifest>'
                '<spine><itemref idref="one"/></spine></package>',
            'images/cover.png': PNG,
            'one.xhtml': '<h1>One</h1><p>Keep before.</p><img src="images/COVER.png">'
                '<img src="images/missing.png"><p>Keep after.</p>',
        })
        result = import_document(path)
        self.assertTrue(result['book']['cover'].startswith('data:image/png;'))
        self.assertEqual(len(result['chapters'][0]['images']), 1)
        self.assertIn('Keep after.', result['chapters'][0]['text'])
        self.assertTrue(any('missing.png' in w for w in result['warnings']))

    def test_epub_rejects_ambiguous_image_case_and_missing_spine_document(self):
        for extra, chapter in [({'a.png': PNG, 'A.png': PNG}, '<p>Text</p><img src="a.PNG">'),
                               ({}, None)]:
            with self.subTest(chapter=chapter):
                entries = {
                    'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
                    'book.opf': '<package><manifest><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/></spine></package>',
                } | extra
                if chapter is not None:
                    entries['one.xhtml'] = chapter
                with self.assertRaises(DocumentError):
                    import_document(self.archive('missing.epub', entries))

    def test_epub_repairs_sparse_utf8_damage_with_warning_without_legacy_mojibake(self):
        content = ('<?xml version="1.0" encoding="utf-8"?><p>' + 'Tiếng Việt vẫn đúng. ' * 100 + 'Lỗi: ').encode() + b'\xe1i</p>'
        entries = {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><manifest><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/></spine></package>',
            'one.xhtml': content,
        }
        result = import_document(self.archive('damaged.epub', entries))
        self.assertIn('Tiếng Việt vẫn đúng.', result['chapters'][0]['text'])
        self.assertIn('\ufffdi', result['chapters'][0]['text'])
        self.assertTrue(any('UTF-8' in w and 'one.xhtml' in w for w in result['warnings']))
        entries['one.xhtml'] = content.replace(b'\xe1i', '\ufffdi'.encode())
        valid = import_document(self.archive('valid-utf8.epub', entries))
        self.assertEqual(valid['warnings'], [])
        entries['one.xhtml'] = b'<p>\xff\x00\x01binary</p>'
        with self.assertRaises(DocumentError):
            import_document(self.archive('binary.epub', entries))

    def test_epub_static_svg_is_preserved_as_inert_document_image(self):
        from server import public_content
        path = self.archive('vector.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><manifest><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/></spine></package>',
            'one.xhtml': '<p>Diagram below.</p><img src="diagram.svg">',
            'diagram.svg': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
                '<defs><style>.cls-1 {fill-rule:evenodd;}</style></defs><path class="cls-1" d="M0 0L20 20"/></svg>',
        })
        chapter = import_document(path)['chapters'][0]
        self.assertEqual(len(chapter['images']), 1)
        image = chapter['images'][0]
        self.assertTrue(image.startswith('data:image/svg+xml;base64,'))
        self.assertIn('fill-rule', base64.b64decode(image.partition(',')[2]).decode())
        self.assertIn('data:image/svg+xml;base64,', public_content(chapter)['html'])

    def test_epub_repeated_images_cannot_amplify_generated_output(self):
        path = self.archive('images.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><manifest><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/></spine></package>',
            'one.xhtml': '<h1>Images</h1>' + '<img src="a.png">' * 100,
            'a.png': PNG + b'x' * 1024,
        })
        original = path.read_bytes()
        with patch('documents.MAX_OUTPUT_SIZE', 8_192, create=True):
            with self.assertRaises(DocumentError):
                import_document(path)
        self.assertEqual(path.read_bytes(), original)

    def test_epub_repeated_spine_references_are_rejected(self):
        path = self.archive('spine.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><manifest><item id="one" href="one.xhtml"/></manifest><spine>' + '<itemref idref="one"/>' * 100 + '</spine></package>',
            'one.xhtml': '<h1>Repeated chapter</h1><p>' + 'text ' * 500 + '</p>',
        })
        with patch('documents.MAX_OUTPUT_SIZE', 8_192, create=True):
            with self.assertRaises(DocumentError):
                import_document(path)

    def test_epub_output_budget_is_shared_across_distinct_chapters(self):
        path = self.archive('aggregate.epub', {
            'META-INF/container.xml': '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': '<package><manifest><item id="one" href="one.xhtml"/><item id="two" href="two.xhtml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
            'one.xhtml': '<h1>One</h1><p>' + 'a' * 1000 + '</p>',
            'two.xhtml': '<h1>Two</h1><p>' + 'b' * 1000 + '</p>',
        })
        with patch('documents.MAX_OUTPUT_SIZE', 4_096):
            with self.assertRaises(DocumentError):
                import_document(path)

    def test_html_escaping_obeys_generated_output_budget_and_resets_after_error(self):
        large = self.file('large.html', '<p>' + '&amp;' * 2000 + '</p>')
        with patch('documents.MAX_OUTPUT_SIZE', 4_096):
            with self.assertRaises(DocumentError):
                import_document(large)
            result = import_document(self.file('small.html', '<p>Readable</p>'))
            self.assertEqual(result['chapters'][0]['text'], 'Readable')

    def test_fb2_and_zipped_fb2_preserve_sections_and_metadata(self):
        fb2 = '<FictionBook xmlns:l="http://www.w3.org/1999/xlink"><description><title-info><book-title>Story</book-title><author><first-name>A</first-name><last-name>B</last-name></author></title-info></description><body><section><title><p>Chapter 1</p></title><p>Hello</p></section><section><title><p>Chapter 2</p></title><p>Bye</p></section></body></FictionBook>'
        for path in (self.file('book.fb2', fb2), self.archive('book.fbz', {'book.fb2': fb2})):
            with self.subTest(path=path):
                result = import_document(path)
                self.assertEqual(result['book']['name'], 'Story')
                self.assertEqual(result['book']['author'], 'A B')
                self.assertEqual([c['title'] for c in result['chapters']], ['Chapter 1', 'Chapter 2'])

    def test_docx_and_odt_heading_boundaries(self):
        docx = self.archive('book.docx', {'word/document.xml': '<w:document xmlns:w="urn:word"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>First</w:t></w:r></w:p><w:p><w:r><w:t>Hello</w:t></w:r></w:p><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Second</w:t></w:r></w:p><w:p><w:r><w:t>Bye</w:t></w:r></w:p></w:body></w:document>'})
        odt = self.archive('book.odt', {'content.xml': '<office:document-content xmlns:office="urn:office" xmlns:text="urn:text"><office:body><office:text><text:h text:outline-level="1">First</text:h><text:p>Hello</text:p><text:h text:outline-level="1">Second</text:h><text:p>Bye</text:p></office:text></office:body></office:document-content>'})
        for path in (docx, odt):
            with self.subTest(path=path):
                result = import_document(path)
                self.assertEqual([c['title'] for c in result['chapters']], ['First', 'Second'])
                self.assertIn('Hello', result['chapters'][0]['text'])

    def test_cbz_images_use_natural_order_and_survive_original_removal(self):
        path = self.archive('comic.cbz', {'10.png': PNG + b'10', '2.png': PNG + b'2', '1.png': PNG + b'1'})
        result = import_document(path)
        self.assertEqual(result['book']['kind'], 'comic')
        images = result['chapters'][0]['images']
        self.assertEqual([base64.b64decode(i.split(',')[1])[-1:] for i in images], [b'1', b'2', b'0'])
        self.assertEqual(len(images), 3)

    def test_zip_text_members_and_chapters_are_not_lost(self):
        path = self.archive('texts.zip', {'2.txt': 'Chapter 2\nSecond', '1.txt': 'Chapter 1\nFirst'})
        result = import_document(path)
        self.assertEqual([c['title'] for c in result['chapters']], ['Chapter 1', 'Chapter 2'])

    def test_archives_reject_traversal_absolute_drive_and_backslash(self):
        for name in ('../escape.txt', '/escape.txt', 'C:/escape.txt', 'a\\..\\escape.txt'):
            with self.subTest(name=name):
                path = self.archive('bad.zip', {name: 'unsafe'})
                with self.assertRaises(DocumentError):
                    import_document(path)
        self.assertFalse((self.root.parent / 'escape.txt').exists())

    def test_archives_reject_symlinks_and_suspicious_compression(self):
        path = self.root / 'link.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('link.txt')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, 'outside')
        with self.assertRaises(DocumentError):
            import_document(path)
        path = self.archive('bomb.zip', {'huge.txt': '0' * 2_000_000}, zipfile.ZIP_DEFLATED)
        with self.assertRaises(DocumentError):
            import_document(path)

    def test_malformed_xml_archive_and_empty_documents_fail(self):
        for name, data in (('bad.epub', b'not a zip'), ('empty.txt', ''), ('bad.fb2', '<FictionBook>'), ('entity.fb2', '<!DOCTYPE x [<!ENTITY y "boom">]><FictionBook>&y;</FictionBook>')):
            with self.subTest(name=name):
                with self.assertRaises(DocumentError):
                    import_document(self.file(name, data))
        with self.assertRaises(DocumentError):
            import_document(self.archive('broken.epub', {'content.txt': 'not epub'}))

    def test_unsupported_formats_report_actual_engine_need(self):
        with self.assertRaises(UnsupportedFormatError) as caught:
            import_document(self.file('scan.pdf', b'%PDF-1.4'))
        self.assertIn('PDF', str(caught.exception))
        self.assertFalse(capabilities()['pdf']['supported'])
        self.assertFalse(capabilities()['ocr']['supported'])


if __name__ == '__main__':
    unittest.main()
