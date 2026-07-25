import argparse
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from corpus import cli, db
from corpus.ingest import parse_epub_documents, parse_file


CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles>
</container>
"""


class RepairChaptersTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_path = root / "corpus.db"
        self.files_dir = root / "files"
        self.files_dir.mkdir()
        self.db_path_patch = patch.object(db, "DB_PATH", self.db_path)
        self.files_dir_patch = patch.object(db, "FILES_DIR", self.files_dir)
        self.db_path_patch.start()
        self.files_dir_patch.start()
        self.addCleanup(self.db_path_patch.stop)
        self.addCleanup(self.files_dir_patch.stop)
        db.init_db()
        self.epub = self.files_dir / "old.epub"
        self._write_epub()
        self.parsed = parse_file(self.epub)
        self.documents = parse_epub_documents(self.epub)
        self._seed_database()

    def _write_epub(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="c1" href="c001.xhtml" media-type="application/xhtml+xml"/>
            <item id="c2" href="c002.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol>
            <li><a href="c001.xhtml#start">Chapter 1</a></li>
            <li><a href="c002.xhtml">Chapter 2</a></li>
          </ol></nav></body></html>"""
        with zipfile.ZipFile(self.epub, "w") as zf:
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OPS/package.opf", opf)
            zf.writestr("OPS/nav.xhtml", nav)
            zf.writestr(
                "OPS/c001.xhtml",
                "<html><head><title>Title One</title></head>"
                "<body><p>Alpha</p><p>Beta</p></body></html>",
            )
            zf.writestr(
                "OPS/c002.xhtml",
                "<html><head><title>Title Two</title></head>"
                "<body><p>Gamma</p></body></html>",
            )

    def _seed_database(self):
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (1, 1, 'en', 'epub', 'old.epub', '2026-01-01')"""
        )
        seq = 0
        for index, document in enumerate(self.documents, start=1):
            old_chapter = f"internal_c{index:03d}"
            contents = [document["legacy_head_title"], *document["blocks"]]
            for content in contents:
                seq += 1
                cur = con.execute(
                    """INSERT INTO segments (edition_id, seq, chapter, page, content)
                       VALUES (1, ?, ?, NULL, ?)""",
                    (seq, old_chapter, content),
                )
                con.execute(
                    "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                    (f"indexed-{seq}", cur.lastrowid),
                )
        con.commit()
        con.close()

    def _seed_fragment_edition(self) -> Path:
        epub = self.files_dir / "fragments.epub"
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="story" href="story.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="story"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol>
            <li><a href="story.xhtml#dawn">Dawn Path</a></li>
            <li><a href="story.xhtml#dusk">Dusk Path</a></li>
          </ol></nav></body></html>"""
        story = """<html><head><title>Tiny Walk</title></head><body>
          <p id="dawn">A paper sun rises.</p>
          <p id="dusk">A paper moon glows.</p>
        </body></html>"""
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OPS/package.opf", opf)
            zf.writestr("OPS/nav.xhtml", nav)
            zf.writestr("OPS/story.xhtml", story)

        con = db.connect()
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (2, 1, 'en', 'epub', 'fragments.epub', '2026-01-02')"""
        )
        for seq, content in enumerate(("A paper sun rises.", "A paper moon glows."), start=1):
            cur = con.execute(
                """INSERT INTO segments (edition_id, seq, chapter, page, content)
                   VALUES (2, ?, 'Unknown', ?, ?)""",
                (seq, seq + 10, content),
            )
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (f"fragment-index-{seq}", cur.lastrowid),
            )
        con.commit()
        con.close()
        return epub

    def _seed_continued_edition(self) -> Path:
        epub = self.files_dir / "continued.epub"
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
            <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="title"/><itemref idref="body"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol>
            <li><a href="title.xhtml">Story Alpha</a></li>
            <li><a href="body.xhtml">Continued, Sample Collection</a></li>
          </ol></nav></body></html>"""
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("META-INF/container.xml", CONTAINER)
            zf.writestr("OPS/package.opf", opf)
            zf.writestr("OPS/nav.xhtml", nav)
            zf.writestr("OPS/title.xhtml", "<html><body><h1>Story Alpha</h1></body></html>")
            zf.writestr(
                "OPS/body.xhtml",
                "<html><body><p>Invented first paragraph.</p>"
                "<p>Invented second paragraph.</p></body></html>",
            )

        con = db.connect()
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (3, 1, 'en', 'epub', 'continued.epub', '2026-01-03')"""
        )
        old_segments = [
            (301, 1, "Story Alpha", "Story Alpha"),
            (302, 2, "Continued, Sample Collection", "Invented first paragraph."),
            (303, 3, "Continued, Sample Collection", "Invented second paragraph."),
        ]
        for segment_id, seq, chapter, content in old_segments:
            con.execute(
                """INSERT INTO segments
                   (id, edition_id, seq, chapter, page, content)
                   VALUES (?, 3, ?, ?, NULL, ?)""",
                (segment_id, seq, chapter, content),
            )
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (f"continued-index-{seq}", segment_id),
            )
        con.commit()
        con.close()
        return epub

    def _run(self, apply=False):
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_repair_chapters(argparse.Namespace(edition=1, apply=apply))
        return output.getvalue()

    def _chapters(self):
        con = db.connect()
        rows = con.execute(
            """SELECT id, seq, chapter, content FROM segments
               WHERE edition_id=1 ORDER BY seq"""
        ).fetchall()
        fts = con.execute(
            "SELECT body, segment_id FROM segments_fts ORDER BY segment_id"
        ).fetchall()
        con.close()
        return [tuple(r) for r in rows], [tuple(r) for r in fts]

    def test_dry_run_reports_without_writing(self):
        before = self._chapters()

        output = self._run()

        self.assertIn("数据库总片段数：5", output)
        self.assertIn("新版正文片段数：3", output)
        self.assertIn("识别出的旧版 head/title 伪片段数：2", output)
        self.assertIn("成功对齐的正文片段数：3", output)
        self.assertIn("将更新：5 个片段", output)
        self.assertIn("internal_c001 → Chapter 1：3 个片段", output)
        self.assertIn("DRY-RUN：数据库未作任何修改", output)
        self.assertEqual(self._chapters(), before)

    def test_apply_updates_only_chapters(self):
        before_rows, before_fts = self._chapters()

        output = self._run(apply=True)

        after_rows, after_fts = self._chapters()
        self.assertIn("请先备份数据库", output)
        self.assertIn("原地更新 5 个 chapter", output)
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in after_rows],
            [(row[0], row[1], row[3]) for row in before_rows],
        )
        self.assertEqual(
            [row[2] for row in after_rows],
            ["Chapter 1", "Chapter 1", "Chapter 1", "Chapter 2", "Chapter 2"],
        )
        self.assertEqual(after_rows[0][3], "Title One")
        self.assertEqual(after_rows[3][3], "Title Two")
        self.assertEqual(after_fts, before_fts)

    def test_non_title_extra_refuses_apply(self):
        con = db.connect()
        con.execute("UPDATE segments SET content='not a title' WHERE edition_id=1 AND seq=1")
        con.commit()
        con.close()
        before = self._chapters()
        output = io.StringIO()

        with self.assertRaises(SystemExit), redirect_stdout(output):
            cli.cmd_repair_chapters(argparse.Namespace(edition=1, apply=True))

        self.assertIn("无法对齐", output.getvalue())
        self.assertEqual(self._chapters(), before)

    def test_body_misalignment_refuses_apply(self):
        con = db.connect()
        con.execute("UPDATE segments SET content='changed body' WHERE edition_id=1 AND seq=3")
        con.commit()
        con.close()
        before = self._chapters()
        output = io.StringIO()

        with self.assertRaises(SystemExit), redirect_stdout(output):
            cli.cmd_repair_chapters(argparse.Namespace(edition=1, apply=True))

        self.assertIn("无法对齐", output.getvalue())
        self.assertEqual(self._chapters(), before)

    def test_fragment_repair_updates_only_chapter_per_block(self):
        epub = self._seed_fragment_edition()
        parsed = parse_file(epub)
        documents = parse_epub_documents(epub)
        con = db.connect()
        before_segments = [
            tuple(row) for row in con.execute(
                """SELECT id, edition_id, seq, chapter, page, content
                   FROM segments WHERE edition_id=2 ORDER BY seq"""
            )
        ]
        before_fts = [
            tuple(row) for row in con.execute(
                """SELECT f.body, f.segment_id FROM segments_fts f
                   JOIN segments s ON s.id=f.segment_id
                   WHERE s.edition_id=2 ORDER BY f.segment_id"""
            )
        ]

        updated = db.apply_chapter_repair(
            con, 2, parsed, epub_documents=documents
        )

        after_segments = [
            tuple(row) for row in con.execute(
                """SELECT id, edition_id, seq, chapter, page, content
                   FROM segments WHERE edition_id=2 ORDER BY seq"""
            )
        ]
        after_fts = [
            tuple(row) for row in con.execute(
                """SELECT f.body, f.segment_id FROM segments_fts f
                   JOIN segments s ON s.id=f.segment_id
                   WHERE s.edition_id=2 ORDER BY f.segment_id"""
            )
        ]
        con.close()

        self.assertEqual(updated, 2)
        self.assertEqual([row[3] for row in after_segments], ["Dawn Path", "Dusk Path"])
        self.assertEqual(
            [row[:3] + row[4:] for row in after_segments],
            [row[:3] + row[4:] for row in before_segments],
        )
        self.assertEqual(after_fts, before_fts)

    def test_fragment_repair_misalignment_aborts_without_writes(self):
        epub = self._seed_fragment_edition()
        parsed = parse_file(epub)
        documents = parse_epub_documents(epub)
        con = db.connect()
        con.execute(
            "UPDATE segments SET content='Different invented line.' WHERE edition_id=2 AND seq=2"
        )
        con.commit()
        before = [
            tuple(row) for row in con.execute(
                """SELECT id, seq, chapter, page, content FROM segments
                   WHERE edition_id=2 ORDER BY seq"""
            )
        ]

        with self.assertRaises(db.ChapterRepairError):
            db.apply_chapter_repair(con, 2, parsed, epub_documents=documents)

        after = [
            tuple(row) for row in con.execute(
                """SELECT id, seq, chapter, page, content FROM segments
                   WHERE edition_id=2 ORDER BY seq"""
            )
        ]
        con.close()
        self.assertEqual(after, before)

    def test_continued_repair_dry_run_apply_and_second_run_are_safe(self):
        epub = self._seed_continued_edition()
        parsed = parse_file(epub)
        documents = parse_epub_documents(epub)
        con = db.connect()
        before_segments = [
            tuple(row) for row in con.execute(
                """SELECT id, edition_id, seq, chapter, page, content
                   FROM segments WHERE edition_id=3 ORDER BY seq, id"""
            )
        ]
        before_fts = [
            tuple(row) for row in con.execute(
                """SELECT f.body, f.segment_id FROM segments_fts f
                   JOIN segments s ON s.id=f.segment_id
                   WHERE s.edition_id=3 ORDER BY f.segment_id"""
            )
        ]
        report = db.plan_chapter_repair(
            con, 3, parsed, epub_documents=documents
        )
        con.close()

        self.assertTrue(report["valid"])
        self.assertEqual(len(report["changes"]), 2)
        self.assertEqual(
            report["mappings"][("Continued, Sample Collection", "Story Alpha")],
            2,
        )

        output = io.StringIO()
        with patch("sys.argv", ["corpus", "repair-chapters", "3"]), redirect_stdout(output):
            cli.main()
        self.assertIn("Continued, Sample Collection → Story Alpha：2 个片段", output.getvalue())
        self.assertIn("DRY-RUN：数据库未作任何修改", output.getvalue())

        con = db.connect()
        unchanged_after_dry_run = [
            tuple(row) for row in con.execute(
                """SELECT id, edition_id, seq, chapter, page, content
                   FROM segments WHERE edition_id=3 ORDER BY seq, id"""
            )
        ]
        updated = db.apply_chapter_repair(
            con, 3, parsed, epub_documents=documents
        )
        after_segments = [
            tuple(row) for row in con.execute(
                """SELECT id, edition_id, seq, chapter, page, content
                   FROM segments WHERE edition_id=3 ORDER BY seq, id"""
            )
        ]
        after_fts = [
            tuple(row) for row in con.execute(
                """SELECT f.body, f.segment_id FROM segments_fts f
                   JOIN segments s ON s.id=f.segment_id
                   WHERE s.edition_id=3 ORDER BY f.segment_id"""
            )
        ]
        second_report = db.plan_chapter_repair(
            con, 3, parsed, epub_documents=documents
        )
        con.close()

        self.assertEqual(unchanged_after_dry_run, before_segments)
        self.assertEqual(updated, 2)
        self.assertEqual([row[3] for row in after_segments], ["Story Alpha"] * 3)
        self.assertEqual(
            [row[:3] + row[4:] for row in after_segments],
            [row[:3] + row[4:] for row in before_segments],
        )
        self.assertEqual(after_fts, before_fts)
        self.assertTrue(second_report["valid"])
        self.assertEqual(second_report["changes"], [])

        second_output = io.StringIO()
        with redirect_stdout(second_output):
            cli.cmd_repair_chapters(argparse.Namespace(edition=3, apply=False))
        self.assertIn("将更新：0 个片段", second_output.getvalue())


if __name__ == "__main__":
    unittest.main()
