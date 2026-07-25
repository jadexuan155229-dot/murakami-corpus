import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from flask import url_for
from corpus import db


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp_dir.name) / "reader.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        self._seed()
        webapp.app.config.update(TESTING=True)
        self.client = webapp.app.test_client()

    def _seed(self):
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at) VALUES (?,?,?,?,?)""",
            [
                (1, 1, "zh", "epub", "2026-01-01T00:00:00+00:00"),
                (2, 1, "en", "epub", None),
                (3, 1, "ja", "epub", "2026-01-01T00:00:00+00:00"),
                (4, 1, "en", "epub", "2026-01-01T00:00:00+00:00"),
            ],
        )
        con.executemany(
            "INSERT INTO segments (id, edition_id, seq, chapter, content) VALUES (?,?,?,?,?)",
            [
                (101, 1, 1, "Cover", "封面文字"),
                (120, 1, 2, "Part 1", "第一部"),
                (137, 1, 3, "Chapter 1", "第一块甲"),
                (138, 1, 4, "Chapter 1", "第一块乙"),
                (150, 1, 5, "Interlude", "中间块"),
                (160, 1, 6, "Chapter 1", "第二块甲"),
                (161, 1, 7, "Chapter 1", "第二块乙"),
                (170, 1, 8, "Part 2", "第二部"),
                (180, 1, 9, "Chapter 2", "needle target paragraph"),
                (190, 1, 10, None, "无标题尾页"),
                (21, 2, 1, "Draft", "尚未索引"),
                (41, 4, 1, "Cover", "fallback cover"),
                (42, 4, 2, "正文", "fallback body"),
            ],
        )
        for row in con.execute("SELECT id, content FROM segments"):
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?,?)",
                (db.cjk_space(row["content"]), row["id"]),
            )
        con.commit()
        con.close()

    def _seed_structural_placeholder_edition(self):
        con = db.connect()
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at)
               VALUES (5, 1, 'en', 'epub', '2026-01-02')"""
        )
        con.executemany(
            """INSERT INTO segments
               (id, edition_id, seq, chapter, content) VALUES (?, 5, ?, ?, ?)""",
            [
                (501, 1, "Story Alpha", "Story Alpha"),
                (502, 2, "Story Alpha", "Invented opening body."),
                (503, 3, "Continued, Sample Collection", ""),
                (504, 4, "Continued, Sample Collection", "  \n\t  "),
                (505, 5, "Continued, Sample Collection", ".\u2003\u2003.\u2003\u2003."),
                (506, 6, "Story Beta", "Hi"),
                (507, 7, "Continued, Sample Collection", "publisher.example"),
                (508, 8, "Story Gamma", "needle merged target"),
            ],
        )
        for row in con.execute("SELECT id, content FROM segments WHERE edition_id=5"):
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (db.cjk_space(row["content"]), row["id"]),
            )
        con.commit()
        con.close()

    def _add_reader_segments(self, rows):
        con = db.connect()
        con.executemany(
            "INSERT INTO segments (id, edition_id, seq, chapter, content) "
            "VALUES (?, 1, ?, ?, ?)",
            rows,
        )
        for segment_id, _seq, _chapter, content in rows:
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (db.cjk_space(content), segment_id),
            )
        con.commit()
        con.close()

    def test_default_reader_redirects_to_first_chapter(self):
        response = self.client.get("/edition/1/read")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/edition/1/read/137#segment-137"))

    def test_read_edition_endpoint_builds_both_routes(self):
        rules = {rule.rule for rule in webapp.app.url_map.iter_rules("read_edition")}
        self.assertEqual(rules, {
            "/edition/<int:edition_id>/read",
            "/edition/<int:edition_id>/read/<int:segment_id>",
        })
        with webapp.app.test_request_context():
            self.assertEqual(url_for("read_edition", edition_id=1), "/edition/1/read")
            self.assertEqual(
                url_for("read_edition", edition_id=1, segment_id=160),
                "/edition/1/read/160",
            )

    def test_directory_order_classification_and_noncontiguous_names(self):
        con = db.connect()
        blocks = db.get_reader_blocks(con, 1)
        con.close()

        self.assertEqual(
            [block["title"] for block in blocks[:6]],
            ["Cover", "Part 1", "Chapter 1", "Interlude", "Chapter 1", "Part 2"],
        )
        self.assertEqual(blocks[0]["kind"], "frontmatter")
        self.assertEqual(blocks[1]["kind"], "part")
        self.assertEqual(blocks[2]["kind"], "chapter")
        self.assertEqual(blocks[4]["kind"], "chapter")
        self.assertEqual(blocks[-1]["kind"], "backmatter")
        self.assertEqual([b["first_segment_id"] for b in blocks if b["title"] == "Chapter 1"], [137, 160])

    def test_reader_uses_contiguous_chapter_and_adjacent_block_starts(self):
        response = self.client.get("/edition/1/read/160")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("第二块甲", body)
        self.assertIn("第二块乙", body)
        self.assertNotIn("第一块甲", body)
        self.assertNotIn("中间块</p>", body)
        self.assertIn("/edition/1/read/150#segment-150", body)
        self.assertIn("/edition/1/read/170#segment-170", body)

    def test_current_toc_item_is_highlighted_but_normal_body_is_not(self):
        body = self.client.get("/edition/1/read/160").get_data(as_text=True)

        self.assertEqual(body.count("toc-current"), 1)
        self.assertIn('class="toc-item toc-chapter toc-current"', body)
        self.assertNotIn("reader-paragraph reader-target", body)

    def test_search_highlight_only_marks_target_paragraph(self):
        body = self.client.get("/edition/1/read/180?highlight=1").get_data(as_text=True)

        self.assertIn(
            'id="segment-180" class="reader-paragraph reader-target segment-highlight-primary"',
            body,
        )
        self.assertEqual(body.count("reader-paragraph reader-target"), 1)
        self.assertNotIn("segment-highlight-related", body)

    def test_multiple_highlights_validate_ids_and_use_primary_and_related_styles(self):
        body = self.client.get(
            "/edition/1/read/138?highlights=137,138,138,nope,-1,21,150,160,999"
        ).get_data(as_text=True)

        self.assertIn(
            'id="segment-138" class="reader-paragraph reader-target segment-highlight-primary"',
            body,
        )
        self.assertIn(
            'id="segment-137" class="reader-paragraph segment-highlight-related"',
            body,
        )
        self.assertNotIn('id="segment-150" class="reader-paragraph segment-highlight', body)
        self.assertNotIn('id="segment-160" class="reader-paragraph segment-highlight', body)
        self.assertEqual(body.count("segment-highlight-primary"), 1)
        self.assertEqual(body.count("segment-highlight-related"), 1)

    def test_highlight_id_parser_is_ascii_deduplicated_and_capped(self):
        value = ",".join(["1", "1", "bad", "-2", "３", *map(str, range(2, 250))])

        parsed = webapp._parse_highlight_ids(value)

        self.assertEqual(parsed[0], 1)
        self.assertEqual(len(parsed), 200)
        self.assertEqual(len(set(parsed)), 200)
        self.assertNotIn(-2, parsed)

    def test_term_highlights_all_english_matches_with_word_boundaries(self):
        self._add_reader_segments([
            (201, 11, "Terms", "water Water WATER waterfall underwater"),
            (202, 12, "Terms", "water and Water in a related segment"),
            (203, 13, "Terms", "water remains plain outside highlights"),
        ])

        body = self.client.get(
            "/edition/1/read/201",
            query_string={"highlights": "201,202", "search_q": "water"},
        ).get_data(as_text=True)

        self.assertIn("segment-highlight-primary", body)
        self.assertIn("segment-highlight-related", body)
        self.assertEqual(body.count('class="search-term-highlight"'), 5)
        self.assertIn('class="search-term-highlight">water</mark>', body)
        self.assertIn('class="search-term-highlight">Water</mark>', body)
        self.assertIn('class="search-term-highlight">WATER</mark>', body)
        self.assertNotIn('class="search-term-highlight">waterfall</mark>', body)
        self.assertNotIn('class="search-term-highlight">underwater</mark>', body)
        untouched = body.split('id="segment-203"', 1)[1].split("</p>", 1)[0]
        self.assertIn("water remains plain", untouched)
        self.assertNotIn("search-term-highlight", untouched)

    def test_multiword_phrase_and_unicode_display_terms(self):
        self.assertEqual(db.extract_display_terms("water moon"), ["water", "moon"])
        self.assertEqual(
            db.extract_display_terms('"running water"'), ["running water"]
        )
        self.assertEqual(db.extract_display_terms("海辺"), ["海辺"])
        self.assertEqual(db.extract_display_terms("カフカ"), ["カフカ"])
        self.assertEqual(
            db.extract_display_terms("water AND moon OR sky NOT cloud NEAR river"),
            ["water", "moon", "sky", "cloud", "river"],
        )

        multi = webapp.highlight_query_terms("Water beneath the moon", "water moon")
        phrase = webapp.highlight_query_terms(
            "Running water and running   water", '"running water"'
        )
        unicode = webapp.highlight_query_terms("海辺で海辺を読む。カフカ。", "海辺")

        self.assertEqual([p["text"] for p in multi if p["is_match"]], ["Water", "moon"])
        self.assertEqual(
            [p["text"] for p in phrase if p["is_match"]],
            ["Running water", "running   water"],
        )
        self.assertEqual(
            [p["text"] for p in unicode if p["is_match"]], ["海辺", "海辺"]
        )

    def test_term_highlight_escapes_html_and_handles_regex_metacharacters(self):
        self._add_reader_segments([
            (201, 11, "Safety", "<script>alert(1)</script> is plain text"),
        ])

        body = self.client.get(
            "/edition/1/read/201",
            query_string={
                "highlight": "1",
                "search_q": "<script>alert(1)</script>",
            },
        ).get_data(as_text=True)

        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn(
            '<mark class="search-term-highlight">&lt;script&gt;alert(1)&lt;/script&gt;</mark>',
            body,
        )
        for query in (".*", "[abc]", "(", "\\"):
            self.assertIsInstance(
                webapp.highlight_query_terms(".* [abc] ( \\", query), list
            )

    def test_empty_missing_and_overlong_search_query_are_safe(self):
        without_query = self.client.get("/edition/1/read/180?highlight=1")
        empty_query = self.client.get(
            "/edition/1/read/180", query_string={"highlight": "1", "search_q": ""}
        )
        overlong = self.client.get(
            "/edition/1/read/180",
            query_string={"highlight": "1", "search_q": "x" * 5000},
        )

        self.assertEqual(without_query.status_code, 200)
        self.assertEqual(empty_query.status_code, 200)
        self.assertEqual(overlong.status_code, 200)
        self.assertNotIn("search-term-highlight", without_query.get_data(as_text=True))
        self.assertNotIn("search-term-highlight", empty_query.get_data(as_text=True))

    def test_search_result_links_to_reader_with_highlight_and_anchor(self):
        body = self.client.get("/search?q=needle").get_data(as_text=True)

        self.assertIn(
            "/edition/1/read/180?highlight=1&amp;search_q=needle#segment-180",
            body,
        )

    def test_null_chapter_is_a_contiguous_readable_block(self):
        response = self.client.get("/edition/1/read/190")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("未标注章节", body)
        self.assertIn("无标题尾页", body)
        self.assertNotIn("第二块甲", body)
        self.assertIn("/edition/1/read/180#segment-180", body)

    def test_edition_without_chapter_falls_back_to_first_block(self):
        response = self.client.get("/edition/4/read")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/edition/4/read/41#segment-41"))

    def test_work_page_only_links_readable_indexed_editions(self):
        response = self.client.get("/work/1")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body.count(">阅读</a>"), 2)
        self.assertIn('/edition/1/read', body)

    def test_reader_error_states(self):
        self.assertEqual(self.client.get("/edition/999/read").status_code, 404)
        self.assertEqual(self.client.get("/edition/2/read").status_code, 409)
        self.assertEqual(self.client.get("/edition/3/read").status_code, 404)
        self.assertEqual(self.client.get("/edition/1/read/21").status_code, 404)
        self.assertEqual(self.client.get("/edition/1/read/999").status_code, 404)

    def test_repaired_title_page_and_continued_body_form_one_reader_block(self):
        con = db.connect()
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at)
               VALUES (5, 1, 'en', 'epub', '2026-01-02')"""
        )
        con.executemany(
            """INSERT INTO segments
               (id, edition_id, seq, chapter, content) VALUES (?, 5, ?, ?, ?)""",
            [
                (501, 1, "Story Alpha", "Story Alpha"),
                (502, 2, "Story Alpha", "Invented first paragraph."),
                (503, 3, "Story Alpha", "Invented second paragraph."),
            ],
        )

        blocks = db.get_reader_blocks(con, 5)
        con.close()

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["title"], "Story Alpha")
        self.assertEqual(blocks[0]["first_segment_id"], 501)
        self.assertEqual(blocks[0]["last_seq"], 3)

    def test_empty_and_structural_segments_do_not_create_reader_blocks(self):
        self._seed_structural_placeholder_edition()
        con = db.connect()

        blocks = db.get_reader_blocks(con, 5)
        beta = db.get_reader_chapter(con, 5, 506)
        con.close()

        self.assertEqual(
            [block["title"] for block in blocks],
            ["Story Alpha", "Story Beta", "Story Gamma"],
        )
        self.assertNotIn("Continued, Sample Collection", [b["title"] for b in blocks])
        self.assertEqual(blocks[0]["_segment_ids"], [501, 502])
        # 一个极短但含正常文字的正文 segment 必须保留。
        self.assertEqual(blocks[1]["_segment_ids"], [506])
        self.assertEqual(beta["previous"]["first_segment_id"], 501)
        self.assertEqual(beta["next"]["first_segment_id"], 508)
        self.assertEqual([row["id"] for row in beta["segments"]], [506])

    def test_toc_and_previous_next_skip_structural_placeholder_chapters(self):
        self._seed_structural_placeholder_edition()

        body = self.client.get("/edition/5/read/506").get_data(as_text=True)

        toc = body.split('<nav class="toc-list"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("Continued", toc)
        self.assertEqual(toc.count("Story Alpha"), 1)
        self.assertEqual(toc.count("Story Beta"), 1)
        self.assertEqual(toc.count("Story Gamma"), 1)
        self.assertIn("/edition/5/read/501#segment-501", body)
        self.assertIn("/edition/5/read/508#segment-508", body)

    def test_search_jump_to_merged_story_still_targets_readable_segment(self):
        self._seed_structural_placeholder_edition()

        search_body = self.client.get("/search?q=needle").get_data(as_text=True)
        reader = self.client.get("/edition/5/read/508?highlight=1")
        reader_body = reader.get_data(as_text=True)

        self.assertIn(
            "/edition/5/read/508?highlight=1&amp;search_q=needle#segment-508",
            search_body,
        )
        self.assertEqual(reader.status_code, 200)
        self.assertIn(
            'id="segment-508" class="reader-paragraph reader-target segment-highlight-primary"',
            reader_body,
        )
        self.assertNotIn("Continued, Sample Collection", reader_body)


if __name__ == "__main__":
    unittest.main()
