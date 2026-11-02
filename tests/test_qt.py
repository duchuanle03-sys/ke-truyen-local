from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qt import QtDictionary, QtError


class QtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qt = QtDictionary(self.tmp.name)
        self.addCleanup(self.qt.close)

    def test_name_priority_longest_match_and_hanviet_fallback(self):
        self.qt.save('name', '刘备', 'Lưu Bị')
        self.qt.save('vp', '刘备去了', 'wrong')
        self.qt.save('vp', '去', 'đi')
        self.qt.save('vp', '去了', 'đã đi')
        self.qt.save('hanviet', '山', 'sơn')
        self.qt.save('hanviet', '川', 'xuyên')
        self.assertEqual(self.qt.translate('刘备去了山川。'), 'Lưu Bị đã đi sơn xuyên。')

    def test_book_override_does_not_leak_and_delete_reveals_global(self):
        self.qt.save('name', '小明', 'Tiểu Minh')
        self.qt.save('name', '小明', 'Minh', 'book-a')
        self.qt.save('pronoun', '他', 'anh ấy')
        self.assertEqual(self.qt.translate('小明他', 'book-a'), 'Minh anh ấy')
        self.assertEqual(self.qt.translate('小明他', 'book-b'), 'Tiểu Minh anh ấy')
        self.qt.delete('name', '小明', 'book-a')
        self.assertEqual(self.qt.translate('小明', 'book-a'), 'Tiểu Minh')

    def test_book_scope_precedes_global_kind_priority(self):
        self.qt.save('name', '大人', 'Đại Nhân')
        self.qt.save('vp', '大人', 'người lớn', 'story')
        self.assertEqual(self.qt.translate('大人', 'story'), 'người lớn')
        self.assertEqual(self.qt.translate('大人'), 'Đại Nhân')

    def test_switching_books_shares_large_global_vocabulary(self):
        self.qt.save('vp', '中文', 'tiếng Trung')
        self.qt.save('name', '小明', 'Minh', 'book-a')
        global_trie = self.qt._compiled('*')[0][0]
        for book in ['book-a'] + [f'book-{number}' for number in range(12)]:
            self.assertEqual(self.qt.translate('中文', book), 'tiếng Trung')
            self.assertTrue(any(trie is global_trie for trie in self.qt._compiled(book)[0]))
        self.assertIs(self.qt._compiled('*')[0][0], global_trie)
        self.assertEqual(self.qt.translate('小明', 'book-a'), 'Minh')

    def test_translation_uses_first_meaning_without_losing_alternatives(self):
        self.qt.save('vp', '他们', 'bọn họ¦họ¦bọn chúng')
        self.assertEqual(self.qt.translate('他们来了'), 'bọn họ 来了')
        self.assertEqual(self.qt.list('vp')[0]['meaning'], 'bọn họ¦họ¦bọn chúng')
        self.assertIn('他们=bọn họ¦họ¦bọn chúng', self.qt.export_text('vp'))

    def test_dictionary_pages_search_unicode_without_loading_other_pages(self):
        for word, meaning in [('一', 'MỘT'), ('二', 'hai'), ('三', 'Straße')]:
            self.qt.save('vp', word, meaning)
        first = self.qt.page('vp', limit=2)
        last = self.qt.page('vp', offset=2, limit=2)
        self.assertEqual(first['total'], 3)
        self.assertEqual(len(first['items']), 2)
        self.assertEqual(len(last['items']), 1)
        self.assertNotIn(last['items'][0], first['items'])
        self.assertEqual(self.qt.page('vp', query='một')['items'][0]['word'], '一')
        self.assertEqual(self.qt.page('vp', query='STRASSE')['items'][0]['word'], '三')
        self.assertEqual(self.qt.page('vp', query='%')['total'], 0)
        for options in ({'limit': 0}, {'limit': 501}, {'offset': -1}):
            with self.assertRaises(QtError):
                self.qt.page('vp', **options)

    def test_empty_dictionary_is_explicit_and_foreign_text_stays_readable(self):
        with self.assertRaises(QtError):
            self.qt.translate('中文')
        self.assertEqual(self.qt.translate('Tiếng Việt\n123'), 'Tiếng Việt\n123')
        self.qt.save('vp', '你', 'bạn')
        self.assertEqual(self.qt.translate('你！\nunknown 字'), 'bạn！\nunknown 字')

    def test_search_scope_persistence_and_database_separation(self):
        self.qt.save('name', '张三', 'Trương Tam', 'book-a')
        self.assertEqual(self.qt.list('name'), [])
        self.assertEqual(self.qt.list('name', 'book-a', 'Tam')[0]['word'], '张三')
        self.assertTrue((Path(self.tmp.name) / 'qt.sqlite3').exists())
        other = QtDictionary(self.tmp.name)
        self.addCleanup(other.close)
        self.assertEqual(other.translate('张三', 'book-a'), 'Trương Tam')
        other.save('name', '张三', 'Tam', 'book-a')
        self.assertEqual(self.qt.translate('张三', 'book-a'), 'Tam')

    def test_import_merge_missing_replace_with_backup_restore(self):
        self.qt.save('vp', '一', 'một')
        report = self.qt.import_text('vp', '一=nhất\n二=hai\nbad line\n# comment')
        self.assertEqual(report['inserted'], 1)
        self.assertEqual(report['updated'], 1)
        self.assertEqual(report['errors'][0]['line'], 3)
        report = self.qt.import_text('vp', '一=wrong\n三=ba', 'missing')
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(self.qt.translate('一二三'), 'nhất hai ba')
        report = self.qt.import_text('vp', '四=bốn', 'replace')
        self.assertTrue(report['backup_id'])
        self.assertEqual(len(self.qt.list('vp')), 1)
        self.qt.restore_backup(report['backup_id'])
        self.assertEqual(self.qt.translate('一二三'), 'nhất hai ba')

    def test_invalid_replace_is_atomic_and_does_not_clear_dictionary(self):
        self.qt.save('vp', '一', 'một')
        report = self.qt.import_text('vp', 'bad line', 'replace')
        self.assertFalse(report['applied'])
        self.assertEqual(self.qt.translate('一'), 'một')

    def test_export_import_preserves_scope_equals_and_unicode(self):
        self.qt.save('name', '张三', 'Trương = Tam', 'book-1')
        exported = self.qt.export_text('name', 'book-1')
        self.qt.delete('name', '张三', 'book-1')
        report = self.qt.import_text('name', exported)
        self.assertEqual(report['scope'], 'book-1')
        self.assertEqual(self.qt.list('name', 'book-1')[0]['meaning'], 'Trương = Tam')
        self.assertEqual(self.qt.list('name'), [])

    def test_rule_validation_preview_and_dictionary_lookup(self):
        self.qt.save('vp', '苹果', 'táo')
        self.qt.save('rule', '{n}个{word}', '{n} quả {lookup:word}')
        self.assertEqual(self.qt.translate('3个苹果。'), '3 quả táo。')
        preview = self.qt.preview_rule('{n1}年{n2}月', 'tháng {n2} năm {n1}', '2026年11月')
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['output'], 'tháng 11 năm 2026')
        self.assertFalse(self.qt.validate_rule('(a+)+', '{missing}')['valid'])
        for pattern, meaning in (('{x}', 'x'), ('{n}', '{missing}'), ('{n}{n}', '{n}')):
            with self.subTest(pattern=pattern):
                with self.assertRaises(QtError):
                    self.qt.save('rule', pattern, meaning)

    def test_ambiguous_cjk_rule_rejected_without_regex_backtracking(self):
        program = '''
import tempfile
from qt import QtDictionary, QtError
with tempfile.TemporaryDirectory() as directory:
    qt = QtDictionary(directory)
    pattern = '的'.join('{word' + str(i) + '}' for i in range(1, 9)) + '终'
    try:
        qt.save('rules', pattern, '{word1}')
    except QtError:
        pass
    else:
        qt.translate('的' * 260)
        raise SystemExit('ambiguous rule was accepted')
    qt.close()
'''
        try:
            result = subprocess.run([sys.executable, '-c', program], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail('Ambiguous QT rule exceeded the isolated 2 second limit')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))

    def test_ambiguous_numeric_capture_delimiters_are_rejected(self):
        with self.assertRaises(QtError):
            self.qt.save('rules', '1'.join('{n' + str(i) + '}' for i in range(1, 9)) + '终', '{n1}')

    def test_dictionary_validation_blocks_unroundtrippable_entries(self):
        for kind, word, meaning in (('unknown', '字', 'a'), ('name', '', 'a'), ('vp', '字', ''), ('name', 'x=y', 'a'), ('vp', '字', 'a\nb')):
            with self.subTest(kind=kind, word=word):
                with self.assertRaises(QtError):
                    self.qt.save(kind, word, meaning)


if __name__ == '__main__':
    unittest.main()
