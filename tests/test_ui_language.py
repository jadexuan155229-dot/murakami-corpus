import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


class UiLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = patch.object(
            db, "DB_PATH", Path(self.temp_dir.name) / "ui-language.db"
        )
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        webapp.app.config.update(TESTING=True)
        self.client = webapp.app.test_client()

    def test_index_defaults_to_chinese_without_cookie(self):
        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('<html lang="zh">', body)
        self.assertIn(">检索</button>", body)

    def test_english_cookie_translates_global_navigation(self):
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('<html lang="en">', body)
        self.assertIn(">Search</button>", body)
        self.assertIn(">All languages</option>", body)

    def test_header_renders_two_labeled_independent_controls_in_both_languages(self):
        chinese = self.client.get("/").get_data(as_text=True)
        self.assertRegex(
            chinese,
            r'<button[^>]*data-theme-switcher[^>]*>原版夜色</button>',
        )
        self.assertRegex(
            chinese,
            r'<button[^>]*data-ui-language-switcher[^>]*>EN</button>',
        )

        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")
        english = self.client.get("/").get_data(as_text=True)
        self.assertRegex(
            english,
            r'<button[^>]*data-theme-switcher[^>]*>Original Night</button>',
        )
        self.assertRegex(
            english,
            r'<button[^>]*data-ui-language-switcher[^>]*>中文</button>',
        )

    def test_invalid_cookie_falls_back_to_chinese(self):
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "fr")

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn('<html lang="zh">', body)
        self.assertIn(">检索</button>", body)

    def test_ui_language_does_not_change_search_corpus_language(self):
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")
        with patch.object(db, "search_grouped", return_value=[]) as search_grouped:
            body = self.client.get("/search?q=test&lang=ja").get_data(as_text=True)

        self.assertIn('<html lang="en">', body)
        self.assertIn('<option value="ja" selected>Japanese</option>', body)
        self.assertEqual(search_grouped.call_args.kwargs["language"], "ja")

    def _seed_translatable_pages(self):
        con = db.connect()
        con.execute(
            """INSERT INTO works
               (id, title_zh, title_ja, title_en, year, genres, keywords, notes)
               VALUES (1, ?, ?, ?, 1987, ?, ?, ?)""",
            (
                "测试作品",
                "テスト作品",
                "Test Work",
                '["小说"]',
                '["test keyword"]',
                "用户备注保持原样",
            ),
        )
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, filename, has_pages, indexed_at, notes)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
            [
                (1, "zh", "epub", "test-zh.epub", 0, "2026-01-01T00:00:00+00:00", "edition note"),
                (2, "ja", "epub", "test-ja.epub", 0, "2026-01-01T00:00:00+00:00", None),
                (3, "en", "pdf", "test-en.pdf", 1, "2026-01-01T00:00:00+00:00", None),
                (4, "en", "epub", "test-en.epub", 0, "2026-01-01T00:00:00+00:00", None),
            ],
        )
        con.executemany(
            """INSERT INTO segments (id, edition_id, seq, chapter, content)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, 1, 1, "原始章节标题", "原始正文 test content"),
                (2, 1, 2, "第二章", "第二段原始正文"),
                (3, 2, 1, "日本語章", "Japanese test content"),
                (4, 1, 3, "第三章", "第三段原始正文"),
                (5, 3, 1, None, "English test content"),
                (6, 4, 1, "English chapter", "Another English test content"),
            ],
        )
        for row in con.execute("SELECT id, content FROM segments"):
            con.execute(
                "INSERT INTO segments_fts (body, segment_id) VALUES (?, ?)",
                (db.cjk_space(row["content"]), row["id"]),
            )
        con.commit()
        con.close()

    def test_english_search_page_and_japanese_corpus_filter_stay_independent(self):
        self._seed_translatable_pages()
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")

        body = self.client.get("/search?q=test&lang=ja").get_data(as_text=True)

        self.assertIn('<html lang="en">', body)
        self.assertIn(">Search</button>", body)
        self.assertIn("Matching works", body)
        self.assertIn("Japanese", body)
        self.assertIn('<option value="ja" selected>Japanese</option>', body)
        self.assertIn("— matches in 1 works", body)
        self.assertNotIn("命中作品导航", body)

    def test_search_uses_compact_badges_but_full_language_names_in_english(self):
        self._seed_translatable_pages()
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")

        body = self.client.get("/search?q=test").get_data(as_text=True)

        self.assertIn('search-language-badge lang-zh">CN</span>', body)
        self.assertIn('search-language-badge lang-ja">JP</span>', body)
        self.assertIn('search-language-badge lang-en">EN</span>', body)
        self.assertIn("English · PDF · Page numbers", body)
        for code, language in (("zh", "Chinese"), ("ja", "Japanese"), ("en", "English")):
            filtered = self.client.get(f"/search?q=test&lang={code}")
            self.assertIn(language, filtered.get_data(as_text=True))

    def test_english_genre_labels_preserve_original_filter_values(self):
        self._seed_translatable_pages()
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")

        body = self.client.get("/").get_data(as_text=True)

        self.assertIn(">Novel</button>", body)
        self.assertIn('data-genre="小说"', body)
        self.assertIn('data-genre-label="Novel"', body)
        with patch.object(db, "search_grouped", return_value=[]) as search_grouped:
            self.client.get("/search?q=test&genre=小说")
        self.assertEqual(search_grouped.call_args.kwargs["genre"], "小说")

    def test_english_work_page_translates_labels_without_changing_data(self):
        self._seed_translatable_pages()
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")
        with patch.object(webapp, "LOCAL_DEV", True):
            body = self.client.get("/work/1").get_data(as_text=True)

        for label in (
            "Text editions", "Language", "Format", "Indexed", "Read", "Notes",
            "Delete", "Upload and index", "Novel", "Japanese title", "English title",
            "PDF · <span class=\"dim\">pages</span>",
        ):
            self.assertIn(label, body)
        self.assertIn('edition-language-badge lang-en">English</span>', body)
        self.assertIn('edition-language-badge lang-ja">Japanese</span>', body)
        self.assertIn('edition-language-badge lang-zh">Chinese</span>', body)
        self.assertIn("テスト作品", body)
        self.assertIn("Test Work", body)
        self.assertIn("用户备注保持原样", body)
        self.assertIn('option value="ja">Japanese</option>', body)

    def test_english_reader_translates_chrome_but_not_chapter_or_body(self):
        self._seed_translatable_pages()
        self.client.set_cookie(webapp.UI_LANGUAGE_COOKIE, "en")

        body = self.client.get("/edition/1/read/2").get_data(as_text=True)

        for label in (
            "Table of contents", "← Back to work", "← Previous chapter",
            "Next chapter →", 'title="Back to top"', 'aria-label="Back to top"',
        ):
            self.assertIn(label, body)
        self.assertIn("第二章", body)
        self.assertIn("第二段原始正文", body)

    def test_chinese_reader_chrome_remains_available(self):
        self._seed_translatable_pages()

        body = self.client.get("/edition/1/read/2").get_data(as_text=True)

        self.assertIn("章节目录", body)
        self.assertIn("← 返回作品页", body)
        self.assertIn("返回顶部", body)

    def test_chinese_search_compact_badges_remain_single_characters(self):
        self._seed_translatable_pages()

        body = self.client.get("/search?q=test").get_data(as_text=True)

        self.assertIn('search-language-badge lang-zh">中</span>', body)
        self.assertIn('search-language-badge lang-ja">日</span>', body)
        self.assertIn('search-language-badge lang-en">英</span>', body)


if __name__ == "__main__":
    unittest.main()
