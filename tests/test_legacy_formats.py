import base64
from pathlib import Path
import tempfile
import unittest
import zipfile

from documents import DocumentError, UnsupportedFormatError
from legacy_formats import capabilities, import_legacy


class LegacyFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, value):
        path = self.root / name
        path.write_bytes(value.encode('utf-8') if isinstance(value, str) else value)
        return path

    def archive(self, entries):
        path = self.root / 'sample.hwpx'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return path

    def test_rtf_unicode_signed_surrogates_fallback_and_chapters(self):
        data = br'{\rtf1\ansi\ansicpg1252\uc1 Chapter 1\par \u20013?\u25991? \u-10179?\u-8704?\par Chapter 2\par Done}'
        path = self.file('unicode.rtf', data)
        result = import_legacy(path)
        self.assertEqual([chapter['title'] for chapter in result['chapters']], ['Chapter 1', 'Chapter 2'])
        self.assertIn('中文 😀', result['chapters'][0]['text'])
        self.assertNotIn('?', result['chapters'][0]['text'])
        self.assertEqual(path.read_bytes(), data)

    def test_rtf_codepages_multibyte_hex_and_font_charset(self):
        examples = [
            (br"{\rtf1\ansi\ansicpg1252 caf\'e9}", 'café'),
            (br"{\rtf1\ansi\ansicpg936 \'c4\'e3\'ba\'c3}", '你好'),
            (br"{\rtf1\ansi\deff0{\fonttbl{\f0\fcharset204 Arial;}}\f0 \'cf\'f0\'e8\'e2\'e5\'f2}", 'Привет'),
            (br"{\rtf1\ansi\uc1 \u65?{\uc0\u66}\u67?}", 'ABC'),
        ]
        for data, expected in examples:
            with self.subTest(expected=expected):
                result = import_legacy(self.file('codepage.rtf', data))
                self.assertEqual(result['chapters'][0]['text'], expected)

    def test_rtf_unicode_alternative_uses_unicode_once(self):
        data = br'{\rtf1\ansi {\upr{ANSI alternative}{\*\ud \uc1\u20013?\u25991?}}}'
        result = import_legacy(self.file('alternative.rtf', data))
        self.assertEqual(result['chapters'][0]['text'], '中文')

    def test_rtf_rejects_unpaired_surrogates_and_excessive_group_depth(self):
        for data in (br'{\rtf1\u-10179?}', br'{\rtf1' + b'{' * 101 + b'x' + b'}' * 102):
            with self.subTest(data=data[:50]):
                with self.assertRaises(DocumentError):
                    import_legacy(self.file('bad.rtf', data))

    def test_rtf_groups_objects_fields_binary_and_escaped_punctuation(self):
        data = br'{\rtf1\ansi Safe {\*\unknown hidden}{\object\objdata EVIL}{\pict 0000}{\v secret}{\field{\*\fldinst HYPERLINK "file:///evil"}{\fldrslt label}} \{ok\} \\ done\par {\*\datastore\bin5 {\}{} }<script>}'
        result = import_legacy(self.file('safe.rtf', data))
        text = result['chapters'][0]['text']
        self.assertIn('Safe label {ok} \\ done', text)
        self.assertIn('&lt;script&gt;', result['chapters'][0]['html'])
        for omitted in ('EVIL', 'hidden', 'secret', 'HYPERLINK', 'file:///'):
            self.assertNotIn(omitted, text)
        self.assertTrue(result['book']['warnings'])

    def test_rtf_malformed_groups_hex_binary_and_unsupported_codepage_rejected(self):
        for data in (b'plain text', br'{\rtf1 missing close', br"{\rtf1 \'zz}", br'{\rtf1\bin999 short}', br'{\rtf1\ansicpg99999 text}', br'{\rtf1\uc-1 x}', br'{\rtf1\u-1?}', br'{\rtf1 }trailing'):
            with self.subTest(data=data):
                with self.assertRaises(DocumentError):
                    import_legacy(self.file('bad.rtf', data))

    def test_hwpx_spine_order_metadata_tables_and_image_references(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        section = '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"><hp:p><hp:run><hp:t>안녕하세요 &amp; Hello</hp:t><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>Cell 1</hp:t></hp:run></hp:p></hp:subList></hp:tc><hp:tc><hp:subList><hp:p><hp:run><hp:t>Cell 2</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr></hp:tbl><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p></hs:sec>'
        path = self.archive({
            'mimetype': 'application/hwp+zip',
            'Contents/content.hpf': '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" xmlns:dc="http://purl.org/dc/elements/1.1/"><opf:metadata><dc:title>Korean Book</dc:title><dc:creator>Author</dc:creator></opf:metadata><opf:manifest><opf:item id="sec1" href="Contents/section1.xml"/><opf:item id="sec0" href="Contents/section0.xml"/><opf:item id="image1" href="BinData/image1.png" media-type="image/png"/></opf:manifest><opf:spine><opf:itemref idref="sec0"/><opf:itemref idref="sec1"/></opf:spine></opf:package>',
            'Contents/section1.xml': section.replace('안녕하세요 &amp; Hello', 'Goodbye'),
            'Contents/section0.xml': section,
            'BinData/image1.png': png,
            'Scripts/sourceScripts': 'never execute',
        })
        result = import_legacy(path)
        self.assertEqual(result['book']['name'], 'Korean Book')
        self.assertEqual(result['book']['author'], 'Author')
        self.assertEqual(len(result['chapters']), 2)
        self.assertIn('안녕하세요 & Hello', result['chapters'][0]['text'])
        self.assertIn('Goodbye', result['chapters'][1]['text'])
        self.assertIn('<table>', result['chapters'][0]['html'])
        self.assertEqual(result['chapters'][0]['text'].count('Cell 1'), 1)
        self.assertTrue(result['chapters'][0]['images'][0].startswith('data:image/png;'))

    def test_hwpx_requires_real_package_and_all_spine_members(self):
        bad_archives = [
            {'Contents/section0.xml': '<sec><p>not hwpx</p></sec>'},
            {'mimetype': 'application/hwp+zip', '../evil': 'x', 'Contents/content.hpf': '<package/>'},
            {'mimetype': 'application/hwp+zip', 'Contents/content.hpf': '<package><manifest><item id="one" href="../outside.xml"/></manifest><spine><itemref idref="one"/></spine></package>'},
            {'mimetype': 'application/hwp+zip', 'Contents/content.hpf': '<package><manifest><item id="one" href="Contents/missing.xml"/></manifest><spine><itemref idref="one"/></spine></package>'},
        ]
        for entries in bad_archives:
            with self.subTest(entries=entries):
                with self.assertRaises(DocumentError):
                    import_legacy(self.archive(entries))

    def test_hwpml_metadata_sections_unicode_tables_and_ignored_objects(self):
        data = '<HWPML Version="2.8"><HEAD><DOCSUMMARY><TITLE>문서</TITLE><AUTHOR>Writer</AUTHOR></DOCSUMMARY></HEAD><BODY><SECTION Id="0"><P><TEXT><CHAR>Hello<TAB/>reader<LINEBREAK/>中文 &amp; Việt</CHAR><TABLE><ROW><CELL><PARALIST><P><TEXT><CHAR>Table cell</CHAR></TEXT></P></PARALIST></CELL></ROW></TABLE><OLE><SCRIPT>evil()</SCRIPT></OLE></TEXT></P></SECTION><SECTION Id="1"><P><TEXT><CHAR>Goodbye</CHAR></TEXT></P></SECTION></BODY></HWPML>'
        result = import_legacy(self.file('book.hwpml', data))
        self.assertEqual(result['book']['name'], '문서')
        self.assertEqual(result['book']['author'], 'Writer')
        self.assertEqual(len(result['chapters']), 2)
        self.assertIn('中文 & Việt', result['chapters'][0]['text'])
        self.assertEqual(result['chapters'][0]['text'].count('Table cell'), 1)
        self.assertNotIn('evil', result['chapters'][0]['text'])
        self.assertIn('<table>', result['chapters'][0]['html'])

    def test_hwpml_dtd_entities_wrong_root_empty_and_malformed_rejected(self):
        for data in ('<html><P>Hello</P></html>', '<HWPML>', '<HWPML><BODY/></HWPML>', '<!DOCTYPE HWPML [<!ENTITY a "bad">]><HWPML>&a;</HWPML>'):
            with self.subTest(data=data):
                with self.assertRaises(DocumentError):
                    import_legacy(self.file('bad.hwpml', data))

    def test_proprietary_binary_formats_remain_explicitly_unsupported(self):
        for extension in ('mobi', 'prc', 'azw', 'azw3', 'hwp', 'chm', 'doc', 'wps', 'umd'):
            with self.subTest(extension=extension):
                with self.assertRaises(UnsupportedFormatError):
                    import_legacy(self.file('book.' + extension, b'binary'))
        self.assertEqual(capabilities()['supported_extensions'], ['.hwpml', '.hwpx', '.rtf'])


if __name__ == '__main__':
    unittest.main()
