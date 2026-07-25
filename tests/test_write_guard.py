"""公开部署下写操作（上传 / 删除）守卫的行为。

安全属性：公开站点上任人上传，等于给陌生人一个往服务器塞受版权保护文本的入口。
所以未配置 CORPUS_ADMIN_PASSWORD 时，写入面必须整个失效，而不是默认放行。
"""

import io
import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


def basic_auth(password: str, user: str = "admin") -> dict:
    token = b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class WriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        for attr, value in (("DB_PATH", root / "corpus.db"), ("FILES_DIR", root / "files")):
            p = patch.object(db, attr, value)
            p.start()
            self.addCleanup(p.stop)

        # 模拟生产：非本地运行。
        p = patch.object(webapp, "LOCAL_DEV", False)
        p.start()
        self.addCleanup(p.stop)

        db.init_db()
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.execute(
            """INSERT INTO editions (id, work_id, language, format, filename, indexed_at)
               VALUES (1, 1, 'zh', 'txt', 'a.txt', '2026-01-01')"""
        )
        con.commit()
        con.close()
        webapp.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
        self.client = webapp.app.test_client()

    def _post_upload(self, headers=None):
        return self.client.post(
            "/work/1/upload",
            data={"lang": "zh", "file": (io.BytesIO("井".encode()), "a.txt")},
            content_type="multipart/form-data",
            headers=headers or {},
        )

    def _post_delete(self, headers=None):
        return self.client.post(
            "/work/1/edition/1/delete", headers=headers or {}
        )

    def _set_password(self, password: str):
        p = patch.object(webapp, "ADMIN_PASSWORD", password)
        p.start()
        self.addCleanup(p.stop)

    # ---- 未配置口令：写入面整个关闭 ----

    def test_upload_forbidden_without_admin_password(self):
        self._set_password("")
        self.assertEqual(self._post_upload().status_code, 403)

    def test_delete_forbidden_without_admin_password(self):
        self._set_password("")
        self.assertEqual(self._post_delete().status_code, 403)

    def test_upload_form_hidden_without_admin_password(self):
        self._set_password("")
        html = self.client.get("/work/1").get_data(as_text=True)
        self.assertNotIn("/work/1/upload", html)
        self.assertNotIn("/work/1/edition/1/delete", html)

    # ---- 配置了口令：需凭口令 ----

    def test_upload_challenges_without_credentials(self):
        self._set_password("s3cret")
        response = self._post_upload()
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers.get("WWW-Authenticate", ""))

    def test_upload_rejects_wrong_password(self):
        self._set_password("s3cret")
        self.assertEqual(self._post_upload(basic_auth("wrong")).status_code, 401)

    def test_upload_accepts_correct_password(self):
        """凭正确口令时请求被放行到上传处理逻辑（新建了 edition 行）。"""
        self._set_password("s3cret")
        response = self._post_upload(basic_auth("s3cret"))
        self.assertEqual(response.status_code, 302)
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM editions").fetchone()[0]
        con.close()
        self.assertEqual(n, 2)  # setUp 里的 1 个 + 刚上传的 1 个

    def test_upload_form_visible_with_admin_password(self):
        self._set_password("s3cret")
        html = self.client.get("/work/1").get_data(as_text=True)
        self.assertIn("/work/1/upload", html)

    # ---- 读取始终开放 ----

    def test_reading_stays_open(self):
        self._set_password("")
        for path in ("/", "/work/1", "/search?q=井"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
