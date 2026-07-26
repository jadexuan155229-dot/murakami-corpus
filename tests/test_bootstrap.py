"""bootstrap() 的书架同步行为。

契约：每次启动都把 works_metadata.csv 同步进来，按中文书名去重。
这样从 Notion 重新导出含新作品的 CSV 后，下次启动书架上就能看到。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corpus import db

HEADER = "书名,原版年份,文体,关键词,英文版,日文版,Status,备注,格式\n"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.csv_path = root / "works_metadata.csv"
        for attr, value in (
            ("DB_PATH", root / "corpus.db"),
            ("FILES_DIR", root / "files"),
            ("WORKS_CSV", self.csv_path),
        ):
            p = patch.object(db, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def write_csv(self, *rows: str):
        self.csv_path.write_text(HEADER + "".join(rows), encoding="utf-8")

    def titles(self):
        con = db.connect()
        rows = [r[0] for r in con.execute("SELECT title_zh FROM works ORDER BY id")]
        con.close()
        return rows

    def test_seeds_empty_database(self):
        self.write_csv('且听风吟,1979,"[""小说""]",[],,風の歌を聴け,Not started,,[]\n')
        self.assertEqual(db.bootstrap(), 1)
        self.assertEqual(self.titles(), ["且听风吟"])

    def test_is_idempotent(self):
        self.write_csv('且听风吟,1979,"[""小说""]",[],,風の歌を聴け,Not started,,[]\n')
        db.bootstrap()
        self.assertEqual(db.bootstrap(), 0, "重复启动不应重复插入")
        self.assertEqual(self.titles(), ["且听风吟"])

    def test_picks_up_works_added_to_csv_later(self):
        """这是修复的核心：非空库也要能收到 CSV 里新增的作品。"""
        self.write_csv('且听风吟,1979,"[""小说""]",[],,風の歌を聴け,Not started,,[]\n')
        db.bootstrap()
        self.write_csv(
            '且听风吟,1979,"[""小说""]",[],,風の歌を聴け,Not started,,[]\n',
            '夏帆,2026,"[""小说""]",[],,夏帆,Not started,,[]\n',
        )
        self.assertEqual(db.bootstrap(), 1)
        self.assertEqual(self.titles(), ["且听风吟", "夏帆"])

    def test_new_work_keeps_its_metadata(self):
        self.write_csv('夏帆,2026,"[""小说""]",[],,夏帆,Not started,,[]\n')
        db.bootstrap()
        con = db.connect()
        row = con.execute("SELECT * FROM works WHERE title_zh='夏帆'").fetchone()
        con.close()
        self.assertEqual(row["year"], "2026")
        self.assertEqual(row["title_ja"], "夏帆")
        self.assertIsNone(row["title_en"])
        self.assertEqual(db.loads_list(row["genres"]), ["小说"])

    def test_survives_missing_csv(self):
        """打包时漏了 CSV 也不该让程序起不来。"""
        self.assertFalse(self.csv_path.exists())
        self.assertEqual(db.bootstrap(), 0)
        self.assertEqual(self.titles(), [])


if __name__ == "__main__":
    unittest.main()
