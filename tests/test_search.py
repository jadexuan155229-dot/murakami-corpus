import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


class SearchPageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", Path(self.temp_dir.name) / "search.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        self._seed()
        webapp.app.config.update(TESTING=True)
        self.client = webapp.app.test_client()

    def _seed(self):
        con = db.connect()
        con.executemany(
            "INSERT INTO works (id, title_zh, year) VALUES (?, ?, ?)",
            [(12, "海边的卡夫卡", 2002), (27, "挪威的森林", 1987)],
        )
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at) VALUES (?, ?, ?, ?, ?)""",
            [
                (120, 12, "zh", "epub", "2026-01-01T00:00:00+00:00"),
                (270, 27, "zh", "epub", "2026-01-01T00:00:00+00:00"),
            ],
        )
        con.executemany(
            """INSERT INTO segments
               (id, edition_id, seq, chapter, content) VALUES (?, ?, ?, ?, ?)""",
            [
                (1201, 120, 1, "第一章", "needle 在海边出现"),
                (2701, 270, 1, "第二章", "森林里的 needle"),
            ],
        )
        for row in con.execute("SELECT id, content FROM segments"):
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (db.cjk_space(row["content"]), row["id"]),
            )
        con.commit()
        con.close()

    def test_multiple_work_results_have_navigation_and_matching_unique_anchors(self):
        body = self.client.get("/search?q=needle").get_data(as_text=True)

        self.assertIn('aria-label="命中作品导航"', body)
        self.assertIn("海边的卡夫卡", body)
        self.assertIn("挪威的森林", body)
        for work_id in (12, 27):
            anchor = f"work-results-{work_id}"
            self.assertEqual(body.count(f'id="{anchor}"'), 1)
            self.assertEqual(body.count(f'href="#{anchor}"'), 1)
        nav_order = re.findall(r'href="#work-results-(\d+)"', body)
        result_order = re.findall(r'<section class="result-work" id="work-results-(\d+)"', body)
        self.assertEqual(nav_order, result_order)

    def test_no_results_do_not_render_work_navigation(self):
        body = self.client.get("/search?q=absent").get_data(as_text=True)

        self.assertIn("没有命中", body)
        self.assertNotIn('aria-label="命中作品导航"', body)
        self.assertNotIn("search-results-layout", body)

    def test_existing_results_and_reader_links_remain(self):
        body = self.client.get("/search?q=needle").get_data(as_text=True)

        self.assertIn('class="kwic-match"', body)
        self.assertIn("needle", body)
        self.assertIn(
            "/edition/120/read/1201?highlight=1&amp;search_q=needle#segment-1201",
            body,
        )
        self.assertIn(
            "/edition/270/read/2701?highlight=1&amp;search_q=needle#segment-2701",
            body,
        )
        self.assertNotIn("本章 1 处", body)
        self.assertNotIn('class="search-chapter-toggle"', body)

    def test_group_hits_by_chapter_separates_editions_and_sorts_segments(self):
        def row(edition_id, chapter, seq, segment_id):
            return {
                "edition_id": edition_id,
                "language": "en",
                "chapter": chapter,
                "page": None,
                "seq": seq,
                "segment_id": segment_id,
                "content": f"needle {segment_id}",
            }

        groups = webapp.group_hits_by_chapter(
            [
                row(10, "Chapter 7", 3, 103),
                row(10, "Chapter 8", 1, 108),
                row(11, "Chapter 7", 1, 201),
                row(10, "Chapter 7", 1, 101),
                row(10, "Chapter 7", 2, 102),
            ],
            "needle",
        )

        self.assertEqual(len(groups), 3)
        chapter_seven = next(
            group for group in groups
            if group["edition_id"] == 10 and group["chapter"] == "Chapter 7"
        )
        other_edition = next(group for group in groups if group["edition_id"] == 11)
        other_chapter = next(group for group in groups if group["chapter"] == "Chapter 8")
        self.assertEqual(chapter_seven["chapter_hit_count"], 3)
        self.assertEqual(chapter_seven["primary_hit"]["segment_id"], 101)
        self.assertEqual(
            [hit["segment_id"] for hit in chapter_seven["additional_hits"]],
            [102, 103],
        )
        self.assertEqual(chapter_seven["all_segment_ids"], [101, 102, 103])
        self.assertTrue(chapter_seven["is_multi_hit"])
        self.assertNotEqual(chapter_seven["chapter_key"], other_edition["chapter_key"])
        self.assertEqual(other_edition["chapter_hit_count"], 1)
        self.assertFalse(other_edition["is_multi_hit"])
        self.assertEqual(other_edition["additional_hits"], [])
        self.assertEqual(other_chapter["chapter_hit_count"], 1)

    def test_multi_hit_chapter_links_keep_clicked_segment_and_all_highlights(self):
        con = db.connect()
        con.executemany(
            """INSERT INTO segments
               (id, edition_id, seq, chapter, content) VALUES (?, 120, ?, ?, ?)""",
            [
                (1202, 2, "第一章", "第二个 needle 片段"),
                (1203, 3, "第一章", "第三个 needle 片段"),
            ],
        )
        for segment_id in (1202, 1203):
            content = con.execute(
                "SELECT content FROM segments WHERE id=?", (segment_id,)
            ).fetchone()[0]
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (db.cjk_space(content), segment_id),
            )
        con.commit()
        con.close()

        body = self.client.get("/search?q=needle").get_data(as_text=True)

        self.assertIn("本章 3 处", body)
        self.assertIn('aria-expanded="false"', body)
        self.assertRegex(
            body,
            r'/edition/120/read/1201\?highlights=1201,1202,1203&amp;search_q=needle#segment-1201',
        )
        self.assertRegex(
            body,
            r'/edition/120/read/1202\?highlights=1201,1202,1203&amp;search_q=needle#segment-1202',
        )

    def test_initial_and_work_links_url_encode_the_original_query(self):
        con = db.connect()
        db.insert_segments(
            con,
            120,
            [{
                "seq": 2,
                "chapter": "第一章",
                "page": None,
                "content": "needle under the moon",
            }],
        )
        con.commit()
        con.close()

        initial = self.client.get(
            "/search", query_string={"q": "needle moon"}
        ).get_data(as_text=True)
        expanded = self.client.get(
            "/search/work/12", query_string={"q": "needle moon"}
        ).get_data(as_text=True)

        encoded_query = r"search_q=needle(?:\+|%20)moon#segment-"
        self.assertRegex(initial, encoded_query)
        self.assertRegex(expanded, encoded_query)

    def test_per_work_limit_keeps_every_matching_work_and_full_counts(self):
        con = db.connect()
        con.executemany(
            "INSERT INTO works (id, title_zh, year) VALUES (?, ?, ?)",
            [(35, "舞！舞！舞！", 1988), (48, "奇鸟行状录", 1994)],
        )
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at) VALUES (?, ?, ?, ?, ?)""",
            [
                (350, 35, "zh", "epub", "2026-01-01T00:00:00+00:00"),
                (480, 48, "zh", "epub", "2026-01-01T00:00:00+00:00"),
            ],
        )
        # 作品 27 已有一个命中片段；再添加 204 个，使完整总数达到 205。
        db.insert_segments(
            con,
            270,
            [
                {
                    "seq": seq,
                    "chapter": "大量命中",
                    "page": None,
                    "content": f"needle 片段 {seq}",
                }
                for seq in range(2, 206)
            ],
        )
        for edition_id in (350, 480):
            db.insert_segments(
                con,
                edition_id,
                [{"seq": 1, "chapter": "命中", "page": None, "content": "needle"}],
            )
        con.commit()
        con.close()

        body = self.client.get("/search?q=needle").get_data(as_text=True)

        self.assertIn("4 部作品中有命中", body)
        for work_id in (12, 27, 35, 48):
            self.assertIn(f'id="work-results-{work_id}"', body)
            section = re.search(
                rf'<section class="result-work" id="work-results-{work_id}".*?</section>',
                body,
                re.DOTALL,
            ).group()
            self.assertLessEqual(section.count('class="search-segment-hit"'), 30)
        crowded_nav = re.search(
            r'<a class="search-work-link[^>]+href="#work-results-27".*?</a>',
            body,
            re.DOTALL,
        ).group()
        self.assertIn('<span class="search-work-count">205</span>', crowded_nav)
        crowded = re.search(
            r'<section class="result-work" id="work-results-27".*?</section>',
            body,
            re.DOTALL,
        ).group()
        self.assertEqual(crowded.count('class="search-segment-hit"'), 30)
        self.assertIn("本章 29 处", crowded)
        self.assertIn('class="search-chapter-additional"', crowded)
        self.assertIn("hidden", crowded)
        self.assertIn("205 个命中片段", crowded)
        self.assertIn("当前显示前 30 个命中片段，共 205 个。", crowded)
        self.assertIn('class="search-expand-button"', crowded)
        self.assertIn('data-url="/search/work/27?q=needle"', crowded)
        self.assertIn('data-expand-label="展开全部 205 条"', crowded)
        self.assertIn('data-collapse-label="收起至前 30 条"', crowded)
        self.assertIn('aria-expanded="false"', crowded)

        untruncated = re.search(
            r'<section class="result-work" id="work-results-12".*?</section>',
            body,
            re.DOTALL,
        ).group()
        self.assertEqual(untruncated.count('class="search-segment-hit"'), 1)
        self.assertIn("1 个命中片段", untruncated)
        self.assertNotIn("当前显示前", untruncated)
        self.assertNotIn("search-expand-button", untruncated)

    def test_language_filter_counts_only_matching_editions(self):
        con = db.connect()
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at) VALUES (?, ?, ?, ?, ?)""",
            (121, 12, "en", "epub", "2026-01-01T00:00:00+00:00"),
        )
        db.insert_segments(
            con,
            121,
            [
                {
                    "seq": seq,
                    "chapter": "English",
                    "page": None,
                    "content": f"needle segment {seq}",
                }
                for seq in range(1, 36)
            ],
        )
        con.commit()
        con.close()

        english = self.client.get("/search?q=needle&lang=en").get_data(as_text=True)
        english_work = re.search(
            r'<section class="result-work" id="work-results-12".*?</section>',
            english,
            re.DOTALL,
        ).group()
        self.assertEqual(english_work.count('class="search-segment-hit"'), 30)
        self.assertIn("本章 30 处", english_work)
        self.assertIn("35 个命中片段", english_work)
        self.assertIn("当前显示前 30 个命中片段，共 35 个。", english_work)
        self.assertNotIn('lang-zh', english_work)

        chinese = self.client.get("/search?q=needle&lang=zh").get_data(as_text=True)
        chinese_work = re.search(
            r'<section class="result-work" id="work-results-12".*?</section>',
            chinese,
            re.DOTALL,
        ).group()
        self.assertEqual(chinese_work.count('class="search-segment-hit"'), 1)
        self.assertIn("1 个命中片段", chinese_work)
        self.assertNotIn("当前显示前", chinese_work)

    def test_work_expansion_endpoint_returns_all_hits_for_only_requested_work(self):
        con = db.connect()
        db.insert_segments(
            con,
            270,
            [
                {
                    "seq": seq,
                    "chapter": "更多命中",
                    "page": None,
                    "content": f"needle segment {seq}",
                }
                for seq in range(2, 36)
            ],
        )
        con.commit()
        con.close()

        body = self.client.get("/search/work/27?q=needle").get_data(as_text=True)

        self.assertEqual(body.count('class="search-segment-hit"'), 35)
        self.assertIn("本章 34 处", body)
        self.assertIn('class="search-chapter-group"', body)
        self.assertIn('class="search-chapter-additional"', body)
        self.assertIn("/edition/270/read/2702?highlights=", body)
        self.assertIn("&amp;search_q=needle#segment-2702", body)
        self.assertNotIn("/edition/120/read/1201", body)
        self.assertNotIn("海边的卡夫卡", body)

    def test_work_expansion_endpoint_applies_language_and_genre_filters(self):
        con = db.connect()
        con.execute("UPDATE works SET genres=? WHERE id=12", ('["essay"]',))
        con.execute("UPDATE works SET genres=? WHERE id=27", ('["novel"]',))
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at) VALUES (?, ?, ?, ?, ?)""",
            (271, 27, "en", "epub", "2026-01-01T00:00:00+00:00"),
        )
        db.insert_segments(
            con,
            271,
            [
                {
                    "seq": seq,
                    "chapter": "English",
                    "page": None,
                    "content": f"needle translation {seq}",
                }
                for seq in range(1, 3)
            ],
        )
        con.commit()
        con.close()

        english = self.client.get(
            "/search/work/27?q=needle&lang=en&genre=novel"
        ).get_data(as_text=True)
        self.assertEqual(english.count('class="search-segment-hit"'), 2)
        self.assertIn("lang-en", english)
        self.assertNotIn("lang-zh", english)

        chinese = self.client.get(
            "/search/work/27?q=needle&lang=zh&genre=novel"
        ).get_data(as_text=True)
        self.assertEqual(chinese.count('class="search-segment-hit"'), 1)
        self.assertIn("lang-zh", chinese)
        self.assertNotIn("lang-en", chinese)

        wrong_genre = self.client.get(
            "/search/work/27?q=needle&genre=essay"
        ).get_data(as_text=True)
        self.assertEqual(wrong_genre.count('class="search-segment-hit"'), 0)

    def test_work_expansion_endpoint_validates_query_and_work(self):
        empty = self.client.get("/search/work/27?q=%20")
        missing = self.client.get("/search/work/999?q=needle")

        self.assertEqual(empty.status_code, 400)
        self.assertIn("查询词不能为空", empty.get_data(as_text=True))
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
