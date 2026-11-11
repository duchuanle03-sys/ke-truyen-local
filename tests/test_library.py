import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from library import Store

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.export = self.root / "export"
        d = self.export / "books" / "old-id"
        d.mkdir(parents=True)
        (d / "book.json").write_text(json.dumps({
            "book_id": "old-id", "name": "Truyện thử", "author": "Tác giả",
            "book_source": "Hako Novel", "book_type": 1,
            "path": "https://docln.sbs/truyen/1-test",
            "percent": .25, "last_index": 1, "last_position": .5,
            "book_folder": 42}), encoding="utf-8")
        (d / "chapters.json").write_text(json.dumps([
            {"id": 4, "name": "Chương 1", "url": "https://docln.sbs/c1", "downloaded": False},
            {"id": 8, "name": "Chương 2", "url": "https://docln.sbs/c2", "downloaded": False}
        ]), encoding="utf-8")
        (d / "novel.json").write_text(json.dumps({"description": "Mô tả"}), encoding="utf-8")
        (self.export / "prefs").mkdir()
        (self.export / "prefs" / "setting.json").write_text(json.dumps({
            "shelf.group.list": json.dumps({"42": "Yêu thích"})}), encoding="utf-8")
        (self.export / "data").mkdir()
        (self.export / "data" / "notes.json").write_text(json.dumps([
            {"book_id": "missing-book", "content": "Ghi chú còn lại", "id": 7}
        ]), encoding="utf-8")
        self.store = Store(self.root / "user-data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_restore_preserves_library_progress_and_orphan_note(self):
        result = self.store.import_backup(self.export)
        self.assertEqual(result["books"], 1)
        self.assertEqual(result["chapters"], 2)
        book = self.store.get_book("old-id")
        self.assertEqual(book["name"], "Truyện thử")
        self.assertEqual(book["chapter_index"], 1)
        self.assertEqual(book["position"], .5)
        self.assertEqual(book["progress"], .25)
        self.assertEqual(book["groups"], ["42"])
        self.assertEqual(self.store.groups()[0]["name"], "Yêu thích")
        self.assertEqual(len(self.store.notes()), 1)
        self.assertEqual(result["orphan_annotations"], 1)
        self.assertFalse(self.store.chapters("old-id")[0]["downloaded"])

    def test_reimport_does_not_reset_new_progress_or_cached_content(self):
        self.store.import_backup(self.export)
        self.store.progress("old-id", 1, .8)
        self.store.save_content("old-id", 1, {"html": "<p>Đã tải</p>", "text": "Đã tải", "images": []})
        self.store.import_backup(self.export)
        self.assertEqual(len(self.store.list_books()['items']), 1)
        self.assertEqual(self.store.get_book("old-id")["position"], .8)
        self.assertEqual(self.store.content("old-id", 1)["text"], "Đã tải")

    def test_backup_reimport_does_not_undo_removal_from_shelf(self):
        self.store.import_backup(self.export)
        self.store.trash_book('old-id')
        self.store.import_backup(self.export)
        self.assertEqual(self.store.list_books()['items'], [])
        self.assertEqual(len(self.store.list_books(trashed=True)['items']), 1)

    def test_zip_backup_rejects_traversal_without_writing_outside(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../escape.json", "{}")
        with self.assertRaises(ValueError):
            self.store.import_backup(archive)
        self.assertFalse((self.root / "escape.json").exists())

    def test_zip_backup_folder_name_can_contain_books(self):
        archive = self.root / 'backup.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for path in self.export.rglob('*.json'):
                z.write(path, 'my-books/' + path.relative_to(self.export).as_posix())
        result = self.store.import_backup(archive)
        self.assertEqual(result['books'], 1)
        self.assertEqual(self.store.get_book('old-id')['position'], .5)
        self.assertEqual(result['orphan_annotations'], 1)

    def test_zip_with_two_backup_roots_is_rejected_before_import(self):
        archive = self.root / 'ambiguous.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for prefix in ('first/', 'second/'):
                for path in self.export.rglob('*.json'):
                    z.write(path, prefix + path.relative_to(self.export).as_posix())
        with self.assertRaises(ValueError):
            self.store.import_backup(archive)
        self.assertEqual(self.store.list_books()['items'], [])

    def test_note_settings_and_search_round_trip(self):
        self.store.import_backup(self.export)
        self.store.save_note({"id": "n1", "book_id": "old-id", "chapter_index": 1,
                              "note": "<script>x</script>", "quote": "Đoạn", "position": .2})
        self.assertEqual(len(self.store.notes("old-id")), 1)
        self.store.settings({"fontSize": 23})
        self.assertEqual(self.store.settings()["fontSize"], 23)
        self.assertEqual(len(self.store.list_books(query="truyện")['items']), 1)
        self.assertEqual(len(self.store.list_books(query="absent")['items']), 0)
        self.store.delete_note("n1")
        self.assertEqual(self.store.notes("old-id"), [])

    def test_reject_invalid_progress_and_missing_chapter(self):
        self.store.import_backup(self.export)
        for index, position in [(999, .2), (0, -1), (0, float("nan")), (0, 2)]:
            with self.subTest(index=index, position=position):
                with self.assertRaises(ValueError):
                    self.store.progress("old-id", index, position)

    def test_same_source_path_on_new_domain_reuses_imported_book(self):
        self.store.import_backup(self.export)
        book = self.store.upsert_book({"name": "Truyện thử", "source": "hako",
                    "path": "https://ln.hako.vn/truyen/1-test", "kind": "text"},
                    [{"title": "Chương 1", "url": "https://ln.hako.vn/c1"},
                     {"title": "Chương 2", "url": "https://ln.hako.vn/c2"}])
        self.assertEqual(book["id"], "old-id")
        self.assertEqual(len(self.store.list_books()['items']), 1)
        self.assertEqual(book["position"], .5)

    def test_legacy_last_read_timestamp_is_preserved_in_seconds(self):
        path = self.export / 'books' / 'old-id' / 'book.json'
        book = json.loads(path.read_text(encoding='utf-8'))
        book['timestamp'] = 1750000000000
        path.write_text(json.dumps(book), encoding='utf-8')
        self.store.import_backup(self.export)
        self.assertEqual(self.store.get_book('old-id')['read_at'], 1750000000)
        self.store.progress('old-id', 1, .6)
        last_read = self.store.get_book('old-id')['read_at']
        self.store.import_backup(self.export)
        self.assertEqual(self.store.get_book('old-id')['read_at'], last_read)

    def test_reordered_toc_is_rejected_without_losing_edits_notes_or_position(self):
        self.store.import_backup(self.export)
        self.store.save_content('old-id', 1, {'html': '<p>Original</p>', 'text': 'Original', 'images': []})
        self.store.edit_content('old-id', 1, {'html': '<p>My edit</p>', 'text': 'My edit'})
        self.store.save_note({'id': 'keep', 'book_id': 'old-id', 'chapter_index': 1, 'quote': 'My edit'})
        with self.assertRaises(ValueError):
            self.store.upsert_book({'id': 'old-id', 'name': 'Changed', 'source': 'hako'}, [
                {'title': 'Inserted', 'url': 'https://docln.sbs/c0'},
                {'title': 'One', 'url': 'https://docln.sbs/c1'},
                {'title': 'Two', 'url': 'https://docln.sbs/c2'}])
        self.assertEqual(self.store.get_book('old-id')['chapter_index'], 1)
        self.assertEqual(self.store.get_book('old-id')['chapter_count'], 2)
        self.assertEqual(self.store.content('old-id', 1)['text'], 'My edit')
        self.assertEqual(self.store.content('old-id', 1)['original_text'], 'Original')
        self.assertEqual(self.store.notes('old-id')[0]['chapter_index'], 1)

    def test_shorter_toc_is_rejected_instead_of_leaving_stale_trailing_rows(self):
        self.store.import_backup(self.export)
        with self.assertRaises(ValueError):
            self.store.upsert_book({'id': 'old-id', 'name': 'Changed', 'source': 'hako'}, [
                {'title': 'One', 'url': 'https://docln.sbs/c1'}])
        self.assertEqual(self.store.get_book('old-id')['chapter_count'], len(self.store.chapters('old-id')))

if __name__ == "__main__":
    unittest.main()
