from contextlib import closing
import struct
import tempfile
import unittest
from pathlib import Path
import zipfile

from qt import QtDictionary
from qt_pack import PackError, _columns, install_pack


def dictionary_bytes(entries, trie=False):
    values = '\n'.join(value for _, value in entries)
    keys = '\n'.join(key for key, _ in entries)
    content = struct.pack('>I', len(entries)) + (values + '\n' + keys).encode('utf-8')
    if trie:
        content = struct.pack('>I2iI2i', 2, 1, 0, 2, 0, 1) + content
    return content


class QtPackTests(unittest.TestCase):
    def test_reads_flat_and_trie_dictionary_columns_without_losing_meaning_variants(self):
        entries = [('刘备', 'Lưu Bị'), ('苹果', 'táo¦quả táo')]
        for filename, trie in (('HV.dic', False), ('VP.dic', True)):
            with self.subTest(filename=filename):
                self.assertEqual(_columns(filename, dictionary_bytes(entries, trie)),
                                 (2, ['Lưu Bị', 'táo¦quả táo', '刘备', '苹果']))

    def test_rejects_corruption_and_incompatible_rule_grammar(self):
        bad = [struct.pack('>I', 9_000_000), struct.pack('>I', 2) + b'one\ntwo\nkey',
               struct.pack('>I', 1) + b'\xff\nkey']
        for data in bad:
            with self.subTest(data=data[:8]), self.assertRaises(PackError):
                _columns('HV.dic', data)
        with self.assertRaises(PackError):
            _columns('Rule.dic', dictionary_bytes([('<n>', '{0}')]))

    def test_installs_real_layout_missing_only_reports_bad_rows_and_pending_groups(self):
        with tempfile.TemporaryDirectory() as folder, closing(QtDictionary(folder)) as qt:
            qt.save('name', '刘备', 'tên riêng của tôi')
            self.assertEqual(qt.translate('刘备'), 'tên riêng của tôi')
            pack = Path(folder) / 'translate2.zip'
            with zipfile.ZipFile(pack, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('NE.dic', dictionary_bytes([('刘备', 'Lưu Bị')], True))
                archive.writestr('VP.dic', dictionary_bytes([('苹果', 'táo¦quả táo'), ('坏', 'bad\x07')], True))
                archive.writestr('HV.dic', dictionary_bytes([('山', 'sơn')]))
                archive.writestr('Pronouns.dic', struct.pack('>I2iI2iI', 2, 1, 0, 2, 0, 1, 2) + b'broken')
                archive.writestr('Rule.dic', b'incompatible rules')
            report = install_pack(qt, pack)
            self.assertEqual(qt.list('name')[0]['meaning'], 'tên riêng của tôi')
            self.assertEqual(qt.list('vietphrase')[0]['meaning'], 'táo¦quả táo')
            self.assertEqual(qt.list('hanviet')[0]['meaning'], 'sơn')
            self.assertEqual(qt.translate('山'), 'sơn')
            self.assertEqual(report['files']['VP.dic']['inserted'], 1)
            self.assertEqual(report['files']['VP.dic']['rejected'], 1)
            self.assertEqual(report['files']['NE.dic']['skipped_existing'], 1)
            self.assertEqual(set(report['pending']), {'pronouns', 'rules'})
            self.assertEqual(install_pack(qt, pack)['files']['VP.dic']['inserted'], 0)

    def test_invalid_zip_is_rejected_before_adding_partial_data(self):
        with tempfile.TemporaryDirectory() as folder, closing(QtDictionary(folder)) as qt:
            pack = Path(folder) / 'bad.zip'
            with zipfile.ZipFile(pack, 'w') as archive:
                archive.writestr('HV.dic', dictionary_bytes([('山', 'sơn')]))
            with self.assertRaises(PackError):
                install_pack(qt, pack)
            self.assertEqual(qt.list('hanviet'), [])

    def test_corrupt_main_dictionary_rolls_back_other_files(self):
        with tempfile.TemporaryDirectory() as folder, closing(QtDictionary(folder)) as qt:
            pack = Path(folder) / 'bad.zip'
            with zipfile.ZipFile(pack, 'w') as archive:
                archive.writestr('VP.dic', dictionary_bytes([('山', 'sơn')], True))
                archive.writestr('NE.dic', b'broken')
                archive.writestr('HV.dic', dictionary_bytes([('山', 'sơn')]))
            with self.assertRaises(PackError):
                install_pack(qt, pack)
            self.assertEqual(qt.list('vietphrase'), [])


if __name__ == '__main__':
    unittest.main()
