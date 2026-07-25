"""
网页端：浏览作品、上传文本、跨书全文检索（KWIC 展示）。

本地运行：
    pip install flask
    python app.py
    浏览器打开 http://127.0.0.1:5731
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import shutil
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, redirect, render_template, request, url_for
)
from werkzeug.utils import secure_filename

from corpus import db
from corpus.ingest import PARSERS, parse_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB
app.config["SECRET_KEY"] = (
    os.environ.get("CORPUS_SECRET_KEY")
    or "local-development-only-change-this-secret"
)

# 写操作（上传 / 删除）的口令。公开部署必须设置，否则写入面整个关闭。
ADMIN_PASSWORD = os.environ.get("CORPUS_ADMIN_PASSWORD") or ""

# 仅当以 `python app.py` 本地启动时置为 True；gunicorn 等生产服务器下始终为 False。
LOCAL_DEV = False

LANG_LABEL = {"zh": "中", "ja": "日", "en": "英"}
LANG_NAME = {"zh": "中文", "ja": "日文", "en": "英文"}
MAX_DISPLAY_QUERY_LENGTH = 300


def writes_allowed() -> bool:
    """本站是否开放上传 / 删除。"""
    return LOCAL_DEV or bool(ADMIN_PASSWORD)


@app.context_processor
def inject_flags():
    return {"writes_allowed": writes_allowed()}


def admin_required(view):
    """守卫写操作。

    本地直接放行；公开部署要求 HTTP Basic 口令，未配置口令则整个写入面失效——
    公开站点上任人上传，等于给陌生人一个往服务器塞受版权保护文本的入口。
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if LOCAL_DEV:
            return view(*args, **kwargs)
        if not ADMIN_PASSWORD:
            abort(403, "这是公开演示站，上传与删除已关闭。")
        auth = request.authorization
        if auth and secrets.compare_digest(auth.password or "", ADMIN_PASSWORD):
            return view(*args, **kwargs)
        return Response(
            "需要管理员口令。",
            401,
            {"WWW-Authenticate": 'Basic realm="murakami-corpus admin"'},
        )

    return wrapper


@app.template_filter("lang_label")
def lang_label(code):
    return LANG_LABEL.get(code, code)


@app.template_filter("json_list")
def json_list(value):
    return db.loads_list(value)


@app.route("/")
def index():
    con = db.connect()
    works = con.execute(
        """SELECT w.*, COUNT(e.id) AS n_editions,
                  SUM(CASE WHEN e.indexed_at IS NOT NULL THEN 1 ELSE 0 END) AS n_indexed
           FROM works w LEFT JOIN editions e ON e.work_id = w.id
           GROUP BY w.id ORDER BY w.year"""
    ).fetchall()
    stats = con.execute(
        """SELECT (SELECT COUNT(*) FROM works) AS works,
                  (SELECT COUNT(*) FROM editions WHERE indexed_at IS NOT NULL) AS indexed,
                  (SELECT COUNT(*) FROM segments) AS segments"""
    ).fetchone()
    genres = sorted({g for w in works for g in db.loads_list(w["genres"])})
    con.close()
    return render_template("index.html", works=works, stats=stats, genres=genres)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang") or None
    genre = request.args.get("genre") or None
    results = []
    if q:
        con = db.connect()
        rows = db.search_grouped(
            con, q, language=lang, genre=genre, per_work_limit=30
        )
        con.close()
        # 按作品分组展示
        grouped: dict[int, dict] = {}
        for r in rows:
            g = grouped.setdefault(
                r["work_id"],
                {
                    "title_zh": r["title_zh"],
                    "year": r["year"],
                    "work_id": r["work_id"],
                    "total_hits": r["total_hits"],
                    "displayed_hits": 0,
                    "is_truncated": False,
                    "_rows": [],
                },
            )
            g["displayed_hits"] += 1
            g["_rows"].append(r)
        for g in grouped.values():
            g["is_truncated"] = g["displayed_hits"] < g["total_hits"]
            g["chapter_groups"] = group_hits_by_chapter(g.pop("_rows"), q)
        results = list(grouped.values())
    return render_template("search.html", q=q, lang=lang, genre=genre, results=results)


def group_hits_by_chapter(rows, query):
    """按 edition + chapter 整理匹配 segment，并保留组内 seq 顺序。"""
    grouped: dict[tuple[int, str | None], dict] = {}
    for row in rows:
        chapter = row["chapter"] or None
        group_key = (row["edition_id"], chapter)
        group = grouped.get(group_key)
        if group is None:
            digest = hashlib.sha256(
                f"{row['edition_id']}\0{chapter or ''}".encode("utf-8")
            ).hexdigest()[:16]
            group = grouped[group_key] = {
                "edition_id": row["edition_id"],
                "language": row["language"],
                "chapter": chapter,
                "chapter_key": f"edition-{row['edition_id']}-chapter-{digest}",
                "search_query": query[:MAX_DISPLAY_QUERY_LENGTH],
                "all_hits": [],
            }
        group["all_hits"].append({
            "edition_id": row["edition_id"],
            "language": row["language"],
            "chapter": chapter,
            "page": row["page"],
            "seq": row["seq"],
            "segment_id": row["segment_id"],
            "contexts": db.kwic(row["content"], query),
        })

    chapter_groups = list(grouped.values())
    for group in chapter_groups:
        group["all_hits"].sort(key=lambda hit: (hit["seq"], hit["segment_id"]))
        group["primary_hit"] = group["all_hits"][0]
        group["additional_hits"] = group["all_hits"][1:]
        group["chapter_hit_count"] = len(group["all_hits"])
        group["all_segment_ids"] = [
            hit["segment_id"] for hit in group["all_hits"]
        ]
        group["highlights_param"] = ",".join(
            str(segment_id) for segment_id in group["all_segment_ids"][:200]
        )
        group["is_multi_hit"] = group["chapter_hit_count"] > 1
    return chapter_groups


@app.route("/search/work/<int:work_id>")
def search_work_hits(work_id):
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang") or None
    genre = request.args.get("genre") or None
    con = db.connect()
    try:
        if con.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone() is None:
            abort(404)
        if not q:
            abort(400, "查询词不能为空")
        rows = db.search_work(con, q, work_id, language=lang, genre=genre)
    finally:
        con.close()
    chapter_groups = group_hits_by_chapter(rows, q)
    return render_template("_search_hits.html", chapter_groups=chapter_groups)


@app.route("/work/<int:work_id>")
def work_detail(work_id):
    con = db.connect()
    work = con.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if work is None:
        con.close()
        abort(404)
    editions = con.execute(
        """SELECT e.*, COUNT(s.id) AS n_segments
           FROM editions e LEFT JOIN segments s ON s.edition_id = e.id
           WHERE e.work_id=? GROUP BY e.id ORDER BY e.language""",
        (work_id,),
    ).fetchall()
    con.close()
    return render_template("work.html", work=work, editions=editions)


@app.route(
    "/work/<int:work_id>/edition/<int:edition_id>/delete",
    methods=["POST"],
)
@admin_required
def delete_edition(work_id, edition_id):
    try:
        result = db.delete_edition(work_id, edition_id)
    except (db.WorkNotFoundError, db.EditionNotFoundError, db.EditionWorkMismatchError):
        abort(404)
    except db.UnsafeEditionFileError as exc:
        abort(400, str(exc))
    except db.EditionFileRestoreError as exc:
        abort(500, str(exc))
    except (db.EditionDeleteError, sqlite3.Error, OSError) as exc:
        abort(500, f"删除文本版本失败：{exc}")

    language = LANG_NAME.get(result["language"], result["language"])
    edition_name = f"{language} {result['format'].upper()}"
    count = result["segment_count"]
    file_status = result["file_status"]
    if file_status == "missing":
        message = "版本及索引已删除；原始文件此前已不存在。"
        category = "info"
    elif file_status == "shared":
        message = "版本及索引已删除；原始文件因仍被其他版本引用而保留。"
        category = "info"
    elif file_status == "cleanup_failed":
        message = (
            "版本及索引已删除；原始文件已移入暂存区，但永久清理失败，"
            "暂存文件已保留，需稍后手动清理。"
        )
        category = "warning"
    elif file_status == "unrecorded":
        message = f"已删除{edition_name}：{count} 个片段及其全文索引；该版本未记录原始文件。"
        category = "success"
    else:
        message = f"已删除{edition_name}：{count} 个片段及其全文索引。"
        category = "success"
    flash(message, category)
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/edition/<int:edition_id>/read", endpoint="read_edition")
@app.route(
    "/edition/<int:edition_id>/read/<int:segment_id>",
    endpoint="read_edition",
)
def read_edition(edition_id, segment_id=None):
    con = db.connect()
    try:
        edition = db.get_reader_edition(con, edition_id)
        chapter = db.get_reader_chapter(con, edition_id, segment_id) if edition else None
    finally:
        con.close()

    if edition is None:
        abort(404, "版本不存在")
    if edition["indexed_at"] is None:
        abort(409, "该版本尚未完成索引")
    if chapter is None:
        if segment_id is None:
            abort(404, "该版本没有可阅读的文本片段")
        abort(404, "目标片段不存在或不属于该版本")

    target_id = chapter["target"]["id"]
    if segment_id is None:
        return redirect(url_for(
            "read_edition",
            edition_id=edition_id,
            segment_id=target_id,
            _anchor=f"segment-{target_id}",
        ))
    highlights_value = request.args.get("highlights")
    requested_highlights = _parse_highlight_ids(highlights_value)
    current_segment_ids = set(chapter["current"]["_segment_ids"])
    valid_highlights = [
        highlight_id for highlight_id in requested_highlights
        if highlight_id in current_segment_ids
    ]
    should_highlight = (
        request.args.get("highlight") == "1" or highlights_value is not None
    )
    primary_highlight_segment_id = target_id if should_highlight else None
    related_highlight_segment_ids = set(valid_highlights) - {target_id}
    search_query = (request.args.get("search_q") or "")[:MAX_DISPLAY_QUERY_LENGTH]
    highlighted_segment_ids = set(related_highlight_segment_ids)
    if primary_highlight_segment_id is not None:
        highlighted_segment_ids.add(primary_highlight_segment_id)
    term_highlights = {
        segment["id"]: highlight_query_terms(segment["content"], search_query)
        for segment in chapter["segments"]
        if segment["id"] in highlighted_segment_ids and search_query.strip()
    }
    return render_template(
        "reader.html",
        edition=edition,
        chapter=chapter,
        primary_highlight_segment_id=primary_highlight_segment_id,
        related_highlight_segment_ids=related_highlight_segment_ids,
        term_highlights=term_highlights,
    )


def _parse_highlight_ids(value: str | None, limit: int = 200) -> list[int]:
    """解析逗号分隔的 ASCII 十进制 segment ID，去重并限制数量。"""
    if value is None:
        return []
    parsed: list[int] = []
    seen: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not re.fullmatch(r"[0-9]+", token):
            continue
        segment_id = int(token)
        if segment_id <= 0 or segment_id in seen:
            continue
        seen.add(segment_id)
        parsed.append(segment_id)
        if len(parsed) >= limit:
            break
    return parsed


def highlight_query_terms(text: str, query: str) -> list[dict]:
    """把纯文本拆为普通片段与命中片段，由模板负责安全转义和包裹。"""
    pattern = db.build_display_term_pattern(
        query, max_length=MAX_DISPLAY_QUERY_LENGTH
    )
    if pattern is None:
        return []
    parts = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            parts.append({"text": text[cursor:match.start()], "is_match": False})
        parts.append({"text": match.group(0), "is_match": True})
        cursor = match.end()
    if cursor < len(text):
        parts.append({"text": text[cursor:], "is_match": False})
    return parts


@app.route("/work/<int:work_id>/upload", methods=["POST"])
@admin_required
def upload(work_id):
    file = request.files.get("file")
    lang = request.form.get("lang", "zh")
    if not file or not file.filename:
        abort(400, "未选择文件")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in PARSERS:
        abort(400, f"暂不支持 {suffix}，支持：{', '.join(PARSERS)}")

    raw_stem = Path(file.filename).stem
    safe_stem = secure_filename(raw_stem) or "upload"
    safe = f"{safe_stem}{suffix}"
    dest = db.FILES_DIR / f"w{work_id}_{lang}_{safe}"
    committed = False
    try:
        file.save(dest)
        segments = parse_file(dest)
        con = db.connect()
        try:
            cur = con.execute(
                "INSERT INTO editions (work_id, language, format, filename, has_pages)"
                " VALUES (?,?,?,?,?)",
                (work_id, lang, suffix.lstrip("."), dest.name,
                 1 if suffix == ".pdf" else 0),
            )
            edition_id = cur.lastrowid
            db.insert_segments(con, edition_id, segments)
            con.execute(
                "UPDATE editions SET indexed_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), edition_id),
            )
            con.commit()
            committed = True
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    except Exception:
        if not committed:
            dest.unlink(missing_ok=True)
        raise
    return redirect(url_for("work_detail", work_id=work_id))


@app.route("/segment/<int:segment_id>")
def segment_context(segment_id):
    """查看某个命中片段的前后文（前后各 5 段）。"""
    con = db.connect()
    seg = con.execute(
        """SELECT s.*, e.language, w.title_zh, w.id AS work_id
           FROM segments s JOIN editions e ON e.id=s.edition_id
           JOIN works w ON w.id=e.work_id WHERE s.id=?""",
        (segment_id,),
    ).fetchone()
    if seg is None:
        con.close()
        abort(404)
    context = con.execute(
        """SELECT * FROM segments WHERE edition_id=? AND seq BETWEEN ? AND ? ORDER BY seq""",
        (seg["edition_id"], seg["seq"] - 5, seg["seq"] + 5),
    ).fetchall()
    con.close()
    return render_template("segment.html", seg=seg, context=context)


if __name__ == "__main__":
    # 本地开发入口。生产环境走 wsgi.py + gunicorn，不会执行到这里，
    # 因此 LOCAL_DEV 保持 False，写操作必须凭 CORPUS_ADMIN_PASSWORD。
    LOCAL_DEV = True
    db.bootstrap()
    app.run(host="127.0.0.1", port=5731, debug=True)
