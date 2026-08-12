import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


class DeleteEditionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_path = root / "corpus.db"
        self.files_dir = root / "files"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.files_patch = patch.object(db, "FILES_DIR", self.files_dir)
        self.db_patch.start()
        self.files_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.files_patch.stop)

        # 这些用例测的是删除本身，模拟本地运行（写操作免口令）。
        # 公开部署下守卫的行为由 WriteGuardTests 覆盖。
        self.local_patch = patch.object(webapp, "LOCAL_DEV", True)
        self.local_patch.start()
        self.addCleanup(self.local_patch.stop)

        db.init_db()
        self._seed()
        webapp.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
        self.client = webapp.app.test_client()

    def _seed(self):
        con = db.connect()
        con.executemany(
            "INSERT INTO works (id, title_zh) VALUES (?,?)",
            [(1, "作品一"), (2, "作品二")],
        )
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (?,?,?,?,?,?)""",
            [
                (1, 1, "en", "epub", "one.epub", "2026-01-01"),
                (2, 1, "ja", "txt", "other.txt", "2026-01-01"),
                (3, 1, "zh", "epub", "missing.epub", None),
                (4, 1, "en", "epub", "shared.epub", "2026-01-01"),
                (5, 2, "en", "epub", "shared.epub", "2026-01-01"),
            ],
        )
        con.executemany(
            """INSERT INTO segments
               (id, edition_id, seq, chapter, content) VALUES (?,?,?,?,?)""",
            [
                (11, 1, 1, "Chapter 1", "edition one first"),
                (12, 1, 2, "Chapter 1", "edition one second"),
                (21, 2, 1, "第一章", "other edition"),
                (41, 4, 1, "Chapter 1", "shared one"),
                (51, 5, 1, "Chapter 1", "shared two"),
            ],
        )
        for row in con.execute("SELECT id, content FROM segments"):
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?,?)",
                (db.cjk_space(row["content"]), row["id"]),
            )
        con.commit()
        con.close()
        for filename in ("one.epub", "other.txt", "shared.epub"):
            (self.files_dir / filename).write_text(filename, encoding="utf-8")

    def _ids(self, table, column="id"):
        con = db.connect()
        rows = con.execute(f"SELECT {column} FROM {table} ORDER BY {column}").fetchall()
        con.close()
        return [int(row[0]) for row in rows]

    def _edition_snapshot(self, edition_id):
        con = db.connect()
        edition = con.execute(
            "SELECT * FROM editions WHERE id=?", (edition_id,)
        ).fetchone()
        segments = con.execute(
            """SELECT id, seq, chapter, content FROM segments
               WHERE edition_id=? ORDER BY seq, id""",
            (edition_id,),
        ).fetchall()
        fts = con.execute(
            """SELECT segment_id, body FROM segments_fts
               WHERE segment_id IN (
                   SELECT id FROM segments WHERE edition_id=?
               ) ORDER BY segment_id""",
            (edition_id,),
        ).fetchall()
        con.close()
        return (
            tuple(edition) if edition else None,
            [tuple(row) for row in segments],
            [tuple(row) for row in fts],
        )

    def test_delete_removes_only_target_edition_segments_and_fts(self):
        result = db.delete_edition(1, 1)

        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(result["file_status"], "deleted")
        self.assertEqual(self._ids("works"), [1, 2])
        self.assertEqual(self._ids("editions"), [2, 3, 4, 5])
        self.assertEqual(self._ids("segments"), [21, 41, 51])
        self.assertEqual(self._ids("segments_fts", "segment_id"), [21, 41, 51])
        self.assertFalse((self.files_dir / "one.epub").exists())
        self.assertTrue((self.files_dir / "other.txt").exists())

    def test_zero_segment_edition_with_missing_file_can_be_deleted(self):
        result = db.delete_edition(1, 3)

        self.assertEqual(result["segment_count"], 0)
        self.assertEqual(result["file_status"], "missing")
        self.assertNotIn(3, self._ids("editions"))

    def test_shared_file_is_retained_until_last_reference_is_deleted(self):
        first = db.delete_edition(1, 4)

        self.assertEqual(first["file_status"], "shared")
        self.assertTrue((self.files_dir / "shared.epub").exists())
        self.assertIn(5, self._ids("editions"))
        self.assertIn(51, self._ids("segments_fts", "segment_id"))

        last = db.delete_edition(2, 5)
        self.assertEqual(last["file_status"], "deleted")
        self.assertFalse((self.files_dir / "shared.epub").exists())
        self.assertEqual(self._ids("works"), [1, 2])

    def test_unsafe_filename_is_rejected_without_database_changes(self):
        unsafe_names = [
            "../escape.epub",
            "/tmp/escape.epub",
            "sub/inner/book.epub",
            "sub//book.epub",
            "sub/./book.epub",
            "sub\\book.epub",
            "C:\\escape.epub",
        ]
        con = db.connect()
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, filename) VALUES (?,?,?,?,?)""",
            [(100 + i, 1, "en", "epub", name) for i, name in enumerate(unsafe_names)],
        )
        con.commit()
        con.close()

        for i, name in enumerate(unsafe_names):
            edition_id = 100 + i
            with self.subTest(filename=name):
                with self.assertRaises(db.UnsafeEditionFileError):
                    db.delete_edition(1, edition_id)
                self.assertIn(edition_id, self._ids("editions"))

    def test_safe_file_accepts_legacy_and_one_level_work_directory(self):
        self.assertEqual(db._safe_edition_file("one.epub"), self.files_dir / "one.epub")
        folder = self.files_dir / "w1_作品一"
        folder.mkdir()
        nested = folder / "one.epub"
        nested.write_text("nested", encoding="utf-8")
        self.assertEqual(
            db._safe_edition_file("w1_作品一/one.epub"), nested
        )

    def test_safe_file_rejects_symlink_directory_and_file(self):
        outside = self.files_dir.parent / "outside.epub"
        outside.write_text("outside", encoding="utf-8")
        directory_link = self.files_dir / "w1_link"
        os.symlink(self.files_dir.parent, directory_link)
        with self.assertRaises(db.UnsafeEditionFileError):
            db._safe_edition_file("w1_link/outside.epub")

        file_link = self.files_dir / "linked.epub"
        os.symlink(outside, file_link)
        with self.assertRaises(db.UnsafeEditionFileError):
            db._safe_edition_file("linked.epub")

    def test_delete_removes_file_in_work_directory_and_empty_folder(self):
        folder = self.files_dir / "w1_作品一"
        folder.mkdir()
        source = folder / "one.epub"
        (self.files_dir / "one.epub").replace(source)
        con = db.connect()
        con.execute(
            "UPDATE editions SET filename=? WHERE id=1", ("w1_作品一/one.epub",)
        )
        con.commit()
        con.close()

        result = db.delete_edition(1, 1)

        self.assertEqual(result["file_status"], "deleted")
        self.assertFalse(folder.exists())

    def test_file_staging_failure_leaves_database_unchanged(self):
        before = self._edition_snapshot(1)
        with patch("pathlib.Path.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                db.delete_edition(1, 1)

        self.assertEqual(self._edition_snapshot(1), before)
        self.assertTrue((self.files_dir / "one.epub").exists())

    def test_database_failure_rolls_back_and_restores_staged_file(self):
        before = self._edition_snapshot(1)

        def fail_after_fts_delete(con, work_id, edition_id):
            con.execute(
                """DELETE FROM segments_fts WHERE segment_id IN (
                       SELECT id FROM segments WHERE edition_id=?
                   )""",
                (edition_id,),
            )
            raise sqlite3.OperationalError("forced database failure")

        with patch.object(db, "_delete_edition_records", side_effect=fail_after_fts_delete):
            with self.assertRaises(sqlite3.OperationalError):
                db.delete_edition(1, 1)

        self.assertEqual(self._edition_snapshot(1), before)
        self.assertTrue((self.files_dir / "one.epub").exists())
        self.assertEqual(list((self.files_dir / ".trash").iterdir()), [])

    def test_database_failure_restores_file_to_work_directory(self):
        folder = self.files_dir / "w1_作品一"
        folder.mkdir()
        source = folder / "one.epub"
        (self.files_dir / "one.epub").replace(source)
        con = db.connect()
        con.execute(
            "UPDATE editions SET filename=? WHERE id=1", ("w1_作品一/one.epub",)
        )
        con.commit()
        con.close()

        with patch.object(db, "_delete_edition_records", side_effect=sqlite3.OperationalError("forced")):
            with self.assertRaises(sqlite3.OperationalError):
                db.delete_edition(1, 1)

        self.assertTrue(source.exists())
        self.assertIn(1, self._ids("editions"))

    def test_cleanup_failure_reports_partial_success_and_keeps_staged_file(self):
        with patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
            result = db.delete_edition(1, 1)

        self.assertEqual(result["file_status"], "cleanup_failed")
        self.assertNotIn(1, self._ids("editions"))
        self.assertFalse((self.files_dir / "one.epub").exists())
        self.assertTrue(Path(result["staged_path"]).exists())

    def test_route_is_post_only_and_redirects_to_owning_work(self):
        url = "/work/1/edition/1/delete"
        self.assertEqual(self.client.get(url).status_code, 405)

        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/work/1"))
        self.assertNotIn(1, self._ids("editions"))

    def test_route_rejects_an_edition_owned_by_another_work(self):
        response = self.client.post("/work/2/edition/1/delete")

        self.assertEqual(response.status_code, 404)
        self.assertIn(1, self._ids("editions"))
        self.assertTrue((self.files_dir / "one.epub").exists())

    def test_route_rejects_missing_work_and_edition(self):
        self.assertEqual(
            self.client.post("/work/999/edition/1/delete").status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/work/1/edition/999/delete").status_code,
            404,
        )
        self.assertIn(1, self._ids("editions"))

    def test_work_page_has_post_delete_forms_and_confirmation(self):
        long_filename = "w1_en_Kafka_on_the_Shore_Haruki_Murakami_complete_edition.epub"
        con = db.connect()
        con.execute(
            "UPDATE editions SET filename=? WHERE id=1",
            (long_filename,),
        )
        con.commit()
        con.close()

        body = self.client.get("/work/1").get_data(as_text=True)

        self.assertIn('class="shelf edition-table"', body)
        self.assertIn('class="edition-filename dim"', body)
        self.assertIn(
            f'class="edition-filename-text" title="{long_filename}">{long_filename}</span>',
            body,
        )
        self.assertIn('class="edition-count">2</td>', body)
        self.assertIn('class="edition-index"><span class="ok">已索引</span>', body)
        self.assertIn('<th class="edition-read">阅读</th>', body)
        self.assertIn('<th class="edition-note">备注</th>', body)
        self.assertIn('<th class="edition-delete">删除</th>', body)
        self.assertIn('<td class="edition-read">', body)
        self.assertIn('<td class="edition-note">', body)
        self.assertIn('<td class="edition-delete">', body)
        self.assertLess(body.index('<td class="edition-read">'), body.index('<td class="edition-note">'))
        self.assertLess(body.index('<td class="edition-note">'), body.index('<td class="edition-delete">'))
        self.assertNotIn('class="edition-actions"', body)
        self.assertIn('class="read-link" href="/edition/1/read">阅读</a>', body)
        self.assertIn('action="/work/1/edition/1/delete" method="post"', body)
        self.assertIn("确定删除这个文本版本及其全部索引吗？此操作不可撤销。", body)
        self.assertIn('class="delete-edition" type="submit">删除</button>', body)
        self.assertIn('action="/work/1/edition/3/delete" method="post"', body)

    def test_work_page_renders_note_drawers_and_existing_note_indicator(self):
        no_note_body = self.client.get("/work/1").get_data(as_text=True)
        self.assertIn('<th class="edition-book">分册</th>', no_note_body)
        self.assertNotIn('aria-label="备注（已有备注）"', no_note_body)

        con = db.connect()
        con.execute("UPDATE editions SET notes=?, book_no=? WHERE id=1", ("校对版备注", 1))
        con.commit()
        con.close()

        body = self.client.get("/work/1").get_data(as_text=True)
        self.assertIn('class="edition-book">BOOK 1</td>', body)
        self.assertIn('class="edition-note-toggle"', body)
        self.assertIn('aria-label="备注（已有备注）"', body)
        self.assertIn('class="edition-note-dot"', body)
        self.assertIn('id="edition-note-1" hidden', body)
        self.assertIn('colspan="9"', body)
        self.assertIn('action="/work/1/edition/1/note" method="post"', body)
        self.assertIn(
            'class="edition-note-textarea" id="edition-notes-1" name="notes" rows="3">校对版备注</textarea>',
            body,
        )
        self.assertIn('aria-expanded="false"', body)
        self.assertIn('aria-controls="edition-note-1"', body)

    def test_note_route_saves_and_clears_notes_without_changing_text_or_index(self):
        con = db.connect()
        before_segments = [
            tuple(row) for row in con.execute(
                "SELECT id, edition_id, seq, chapter, content FROM segments WHERE edition_id=1"
            )
        ]
        before_fts = [
            tuple(row) for row in con.execute(
                "SELECT segment_id, body FROM segments_fts WHERE segment_id IN (11, 12)"
            )
        ]
        con.close()

        saved = self.client.post(
            "/work/1/edition/1/note", data={"notes": "  版本校注  "}
        )
        self.assertEqual(saved.status_code, 302)
        self.assertTrue(saved.headers["Location"].endswith("/work/1"))

        con = db.connect()
        self.assertEqual(
            con.execute("SELECT notes FROM editions WHERE id=1").fetchone()["notes"],
            "版本校注",
        )
        after_segments = [
            tuple(row) for row in con.execute(
                "SELECT id, edition_id, seq, chapter, content FROM segments WHERE edition_id=1"
            )
        ]
        after_fts = [
            tuple(row) for row in con.execute(
                "SELECT segment_id, body FROM segments_fts WHERE segment_id IN (11, 12)"
            )
        ]
        con.close()
        self.assertEqual(after_segments, before_segments)
        self.assertEqual(after_fts, before_fts)

        cleared = self.client.post(
            "/work/1/edition/1/note", data={"notes": "  \n  "}
        )
        self.assertEqual(cleared.status_code, 302)
        con = db.connect()
        self.assertIsNone(
            con.execute("SELECT notes FROM editions WHERE id=1").fetchone()["notes"]
        )
        con.close()

    def test_note_route_rejects_an_edition_owned_by_another_work_without_changes(self):
        con = db.connect()
        con.execute("UPDATE editions SET notes=? WHERE id=1", ("原备注",))
        con.commit()
        con.close()

        response = self.client.post(
            "/work/2/edition/1/note", data={"notes": "不应写入"}
        )

        self.assertEqual(response.status_code, 404)
        con = db.connect()
        self.assertEqual(
            con.execute("SELECT notes FROM editions WHERE id=1").fetchone()["notes"],
            "原备注",
        )
        con.close()

    def test_note_route_is_post_only(self):
        self.assertEqual(self.client.get("/work/1/edition/1/note").status_code, 405)

    def test_success_and_missing_file_flash_messages(self):
        success = self.client.post(
            "/work/1/edition/1/delete", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("已删除英文 EPUB：2 个片段及其全文索引。", success)

        missing = self.client.post(
            "/work/1/edition/3/delete", follow_redirects=True
        ).get_data(as_text=True)
        self.assertIn("版本及索引已删除；原始文件此前已不存在。", missing)


if __name__ == "__main__":
    unittest.main()
