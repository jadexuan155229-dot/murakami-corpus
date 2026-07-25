"""
数据库层：作品 (works) → 版本 (editions) → 文本片段 (segments) + 全文索引 (segments_fts)

数据模型直接对应 Notion 表的逻辑：
  一部"作品"（如《挪威的森林》）拥有多个"版本"（中文 epub、日文 epub、英文 PDF 带页码…），
  每个版本被切分为若干"片段"（段落级），片段进入 FTS5 全文索引。

CJK 检索方案：
  FTS5 默认分词器无法切分中文/日文。这里采用"字符级索引"：
  入库时在每个 CJK 字符之间插入空格（拉丁词保持原样），
  查询时对查询串做同样处理并作为短语查询（"羊 男"），
  从而支持任意长度的中日文子串检索（包括二字词）。
  segments.content 保存原文，FTS 表只存加空格后的索引文本。
"""

import csv
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from pathlib import PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 部署时把 CORPUS_DATA_DIR 指向持久化磁盘的挂载点；本地默认用项目下的 data/。
DATA_DIR = Path(os.environ.get("CORPUS_DATA_DIR") or PROJECT_ROOT / "data")
DB_PATH = DATA_DIR / "corpus.db"
FILES_DIR = DATA_DIR / "files"

# 随代码一起分发的作品元数据（Notion 导出），用于空库首次启动时载入书架。
WORKS_CSV = PROJECT_ROOT / "works_metadata.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id          INTEGER PRIMARY KEY,
    title_zh    TEXT NOT NULL,           -- 中文书名（主标题，对应 Notion "书名"）
    title_ja    TEXT,                    -- 日文版
    title_en    TEXT,                    -- 英文版
    year        TEXT,                    -- 原版年份（保留文本，如 "1994（前两部）-1995（第三部）"）
    genres      TEXT DEFAULT '[]',       -- 文体，JSON 数组
    keywords    TEXT DEFAULT '[]',       -- 关键词，JSON 数组
    status      TEXT,                    -- 阅读状态
    notes       TEXT                     -- 备注
);

CREATE TABLE IF NOT EXISTS editions (
    id          INTEGER PRIMARY KEY,
    work_id     INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    language    TEXT NOT NULL CHECK (language IN ('zh','ja','en')),
    format      TEXT NOT NULL,           -- epub / txt / pdf ...
    filename    TEXT,                    -- data/files/ 下的文件名
    has_pages   INTEGER DEFAULT 0,       -- PDF 是否带可引用页码
    notes       TEXT,
    indexed_at  TEXT                     -- 完成全文索引的时间；NULL = 未索引
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY,
    edition_id  INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,        -- 版本内顺序
    chapter     TEXT,                    -- 章节标题（epub 的 spine 条目 / txt 的推断章节）
    page        INTEGER,                 -- PDF 页码（其他格式为 NULL）
    content     TEXT NOT NULL            -- 原文
);
CREATE INDEX IF NOT EXISTS idx_segments_edition ON segments(edition_id, seq);

-- 字符级 FTS 索引：body 列存放加空格后的文本
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    body,
    segment_id UNINDEXED,
    tokenize = 'unicode61'
);
"""

_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)
_DISPLAY_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})
_QUOTED_DISPLAY_TERM = re.compile(r'"([^"\r\n]+)"')
_DISPLAY_TOKEN_TRIM = "\"()[]{}*:^~+,.!?;\\"


def cjk_space(text: str) -> str:
    """在每个 CJK 字符前后插入空格，用于字符级索引与查询。"""
    return re.sub(_CJK, lambda m: f" {m.group(0)} ", text)


def build_fts_query(user_query: str) -> str:
    """把用户查询转为 FTS5 查询串。

    - 含 CJK：整体作为字符级短语，如 羊男 -> "羊 男"
    - 纯拉丁：按词处理，多个词为 AND
    """
    q = user_query.strip()
    if not q:
        return ""
    if _CJK.search(q):
        spaced = " ".join(cjk_space(q).split())
        return f'"{spaced}"'
    words = [w for w in re.split(r"\s+", q) if w]
    return " AND ".join(f'"{w}"' for w in words)


def extract_display_terms(user_query: str, max_length: int = 300) -> list[str]:
    """提取页面高亮使用的字面词，不把 FTS 控制词当作正文词。

    这是展示层的保守解析，不试图完整复刻 FTS5 语法：双引号内容作为
    一个短语，普通拉丁查询按空白拆词，CJK 查询保持原始字符串。
    """
    q = (user_query or "")[:max_length].strip()
    if not q:
        return []

    quoted_matches = list(_QUOTED_DISPLAY_TERM.finditer(q))
    quoted_terms = [match.group(1).strip() for match in quoted_matches]
    remainder = list(q)
    for match in quoted_matches:
        remainder[match.start():match.end()] = " " * (match.end() - match.start())
    remainder_text = "".join(remainder).strip()

    # 当前 CJK FTS 查询把整串字符作为短语；没有显式引号或控制词时，
    # 展示层同样保持原始字符串，不引入英文式词边界。
    remainder_tokens = [token for token in re.split(r"\s+", remainder_text) if token]
    has_operator = any(token.upper() in _DISPLAY_OPERATORS for token in remainder_tokens)
    if _CJK.search(q) and not quoted_terms and not has_operator:
        term = q.strip(_DISPLAY_TOKEN_TRIM).strip()
        return [term] if term else []

    terms = quoted_terms
    for token in remainder_tokens:
        token = token.strip(_DISPLAY_TOKEN_TRIM).strip()
        if not token or token.upper() in _DISPLAY_OPERATORS:
            continue
        terms.append(token)

    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def build_display_term_pattern(
    user_query: str, max_length: int = 300
) -> re.Pattern[str] | None:
    """为纯文本展示生成转义后的保守匹配模式。"""
    terms = extract_display_terms(user_query, max_length=max_length)
    if not terms:
        return None

    alternatives = []
    for term in sorted(terms, key=len, reverse=True):
        if _CJK.search(term):
            expression = re.escape(term)
        else:
            # 引号短语允许正文使用不同数量的空白，但不跨越其他文字。
            expression = r"\s+".join(
                re.escape(part) for part in re.split(r"\s+", term) if part
            )
            if term[0].isalnum() or term[0] == "_":
                expression = r"(?<!\w)" + expression
            if term[-1].isalnum() or term[-1] == "_":
                expression += r"(?!\w)"
        alternatives.append(expression)
    return re.compile("|".join(alternatives), re.IGNORECASE)


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def as_json_list(value: str | None) -> str:
    """把 Notion 的"文体""关键词"列（JSON 数组或顿号/逗号分隔的文本）统一成 JSON 数组。"""
    v = (value or "").strip()
    if not v:
        return "[]"
    if v.startswith("["):
        try:
            return json.dumps(json.loads(v), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return json.dumps([p for p in re.split(r"[、,，/;；]\s*", v) if p], ensure_ascii=False)


def import_works_csv(csv_path: Path | str) -> int:
    """导入 Notion 导出的作品元数据；按中文书名去重，返回新增条数。"""
    init_db()
    con = connect()
    n = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            title = (row.get("书名") or "").strip()
            if not title:
                continue
            if con.execute("SELECT 1 FROM works WHERE title_zh=?", (title,)).fetchone():
                continue
            con.execute(
                "INSERT INTO works (title_zh, title_ja, title_en, year, genres, keywords, status, notes)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    title,
                    (row.get("日文版") or "").strip() or None,
                    (row.get("英文版") or "").strip() or None,
                    (row.get("原版年份") or "").strip() or None,
                    as_json_list(row.get("文体")),
                    as_json_list(row.get("关键词")),
                    (row.get("Status") or "").strip() or None,
                    (row.get("备注") or "").strip() or None,
                ),
            )
            n += 1
    con.commit()
    con.close()
    return n


def bootstrap() -> None:
    """启动时调用：建表；若是全新的空库，顺带载入书架元数据。

    部署到带空白持久磁盘的服务器时，这一步让首次启动就有 69 部作品可浏览。
    """
    init_db()
    con = connect()
    empty = con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 0
    con.close()
    if empty and WORKS_CSV.exists():
        import_works_csv(WORKS_CSV)


def insert_segments(con: sqlite3.Connection, edition_id: int, segments: list[dict]) -> int:
    """写入片段并建立 FTS 索引。segments: [{seq, chapter, page, content}]"""
    cur = con.cursor()
    n = 0
    for seg in segments:
        cur.execute(
            "INSERT INTO segments (edition_id, seq, chapter, page, content) VALUES (?,?,?,?,?)",
            (edition_id, seg["seq"], seg.get("chapter"), seg.get("page"), seg["content"]),
        )
        cur.execute(
            "INSERT INTO segments_fts (body, segment_id) VALUES (?,?)",
            (cjk_space(seg["content"]), cur.lastrowid),
        )
        n += 1
    return n


def clear_edition_index(con: sqlite3.Connection, edition_id: int) -> None:
    """重新索引前清除某版本的旧片段。"""
    ids = [r[0] for r in con.execute("SELECT id FROM segments WHERE edition_id=?", (edition_id,))]
    if ids:
        marks = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM segments_fts WHERE segment_id IN ({marks})", ids)
        con.execute("DELETE FROM segments WHERE edition_id=?", (edition_id,))


class EditionDeleteError(RuntimeError):
    """删除文本版本失败。"""


class WorkNotFoundError(EditionDeleteError):
    pass


class EditionNotFoundError(EditionDeleteError):
    pass


class EditionWorkMismatchError(EditionDeleteError):
    pass


class UnsafeEditionFileError(EditionDeleteError):
    pass


class EditionFileRestoreError(EditionDeleteError):
    pass


def _safe_edition_file(filename: str) -> Path:
    """把 edition filename 限制为 FILES_DIR 下的普通 basename。"""
    if (
        not filename
        or Path(filename).is_absolute()
        or PureWindowsPath(filename).is_absolute()
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise UnsafeEditionFileError(f"非法版本文件名：{filename!r}")
    root = FILES_DIR.resolve()
    source = FILES_DIR / filename
    try:
        resolved = source.resolve(strict=False)
    except OSError as exc:
        raise UnsafeEditionFileError(f"无法验证版本文件路径：{filename!r}") from exc
    if resolved.parent != root:
        raise UnsafeEditionFileError(f"版本文件位于 data/files 之外：{filename!r}")
    if source.is_symlink():
        raise UnsafeEditionFileError(f"版本文件不能是符号链接：{filename!r}")
    if source.exists() and not source.is_file():
        raise UnsafeEditionFileError(f"版本文件不是普通文件：{filename!r}")
    return source


def _delete_edition_records(
    con: sqlite3.Connection,
    work_id: int,
    edition_id: int,
) -> None:
    """在调用方事务内清理 FTS，并删除 edition（segments 由外键级联）。"""
    con.execute(
        """DELETE FROM segments_fts WHERE segment_id IN (
               SELECT id FROM segments WHERE edition_id=?
           )""",
        (edition_id,),
    )
    cur = con.execute(
        "DELETE FROM editions WHERE id=? AND work_id=?",
        (edition_id, work_id),
    )
    if cur.rowcount != 1:
        raise EditionDeleteError("删除 edition 时目标记录不再唯一或已经消失")


def delete_edition(work_id: int, edition_id: int) -> dict:
    """安全删除一个 edition、其片段/FTS，以及不再被引用的原文件。

    文件先原子移动到 ``data/files/.trash``；数据库提交后才永久清理。
    数据库失败时会回滚并将暂存文件恢复到原路径。
    """
    con = connect()
    source: Path | None = None
    staged: Path | None = None
    committed = False
    summary: dict = {}
    try:
        con.execute("BEGIN IMMEDIATE")
        work = con.execute("SELECT id FROM works WHERE id=?", (work_id,)).fetchone()
        if work is None:
            raise WorkNotFoundError(f"作品 {work_id} 不存在")
        edition = con.execute(
            """SELECT id, work_id, language, format, filename
               FROM editions WHERE id=?""",
            (edition_id,),
        ).fetchone()
        if edition is None:
            raise EditionNotFoundError(f"版本 {edition_id} 不存在")
        if edition["work_id"] != work_id:
            raise EditionWorkMismatchError(
                f"版本 {edition_id} 不属于作品 {work_id}"
            )

        segment_count = con.execute(
            "SELECT COUNT(*) FROM segments WHERE edition_id=?",
            (edition_id,),
        ).fetchone()[0]
        filename = edition["filename"]
        shared_references = 0
        file_status = "unrecorded"
        if filename:
            source = _safe_edition_file(filename)
            shared_references = con.execute(
                """SELECT COUNT(*) FROM editions
                   WHERE filename=? AND id<>?""",
                (filename, edition_id),
            ).fetchone()[0]
            if not source.exists():
                file_status = "missing"
            elif shared_references:
                file_status = "shared"
            else:
                trash_dir = FILES_DIR / ".trash"
                trash_dir.mkdir(parents=False, exist_ok=True)
                if (
                    trash_dir.is_symlink()
                    or trash_dir.resolve().parent != FILES_DIR.resolve()
                ):
                    raise UnsafeEditionFileError("data/files/.trash 不是安全的暂存目录")
                staged = trash_dir / f"edition-{edition_id}-{uuid.uuid4().hex}"
                source.replace(staged)
                file_status = "staged"

        _delete_edition_records(con, work_id, edition_id)
        con.commit()
        committed = True
        summary = {
            "work_id": work_id,
            "edition_id": edition_id,
            "language": edition["language"],
            "format": edition["format"],
            "filename": filename,
            "segment_count": segment_count,
            "shared_references": shared_references,
            "file_status": file_status,
        }
    except Exception as exc:
        if con.in_transaction:
            con.rollback()
        if staged is not None and source is not None and staged.exists():
            try:
                if source.exists() or source.is_symlink():
                    raise OSError(f"原路径已被占用：{source}")
                staged.replace(source)
            except OSError as restore_exc:
                raise EditionFileRestoreError(
                    f"删除失败，且暂存文件无法恢复：{staged}"
                ) from restore_exc
        raise
    finally:
        con.close()

    if committed and staged is not None:
        try:
            staged.unlink()
            summary["file_status"] = "deleted"
        except OSError as exc:
            summary["file_status"] = "cleanup_failed"
            summary["staged_path"] = str(staged)
            summary["cleanup_error"] = str(exc)
    return summary


class ChapterRepairError(RuntimeError):
    """章节修复在事务内重新校验失败。"""

    def __init__(self, report: dict):
        super().__init__("章节修复校验失败")
        self.report = report


def plan_chapter_repair(
    con: sqlite3.Connection,
    edition_id: int,
    parsed_segments: list[dict],
    epub_documents: list[dict] | None = None,
) -> dict:
    """只读比对重解析结果，唯一对齐后生成 chapter 修复计划。

    若提供 EPUB 文档边界，允许数据库在每个文档正文前多出一条与该文档
    ``legacy_head_title`` 完全相同的旧解析器伪片段。所有可能对齐会被全局
    求解；没有解或有多个解都视为不安全。
    """
    rows = con.execute(
        """SELECT id, seq, chapter, content FROM segments
           WHERE edition_id=? ORDER BY seq, id""",
        (edition_id,),
    ).fetchall()
    issues: list[str] = []
    db_count = len(rows)
    parsed_count = len(parsed_segments)
    db_seqs = [row["seq"] for row in rows]
    parsed_seqs = [seg.get("seq") for seg in parsed_segments]
    if len(set(db_seqs)) != len(db_seqs):
        issues.append("数据库中存在重复 seq，无法按 seq 唯一定位")
    if len(set(parsed_seqs)) != len(parsed_seqs):
        issues.append("重新解析结果中存在重复 seq，无法按 seq 唯一定位")
    if db_seqs != list(range(1, db_count + 1)):
        issues.append("数据库 seq 不是从 1 开始的连续唯一序列")
    if parsed_seqs != list(range(1, parsed_count + 1)):
        issues.append("重新解析 seq 不是从 1 开始的连续唯一序列")

    changes: list[dict] = []
    mappings: dict[tuple[str | None, str | None], int] = {}
    legacy_titles: list[dict] = []
    matched_count = 0
    assignments: list[tuple[sqlite3.Row, str | None]] = []

    if not issues and epub_documents is not None:
        for document in epub_documents:
            blocks = document.get("blocks", [])
            block_chapters = document.get("block_chapters")
            if block_chapters is not None and len(block_chapters) != len(blocks):
                issues.append(
                    f"EPUB 文档 {document.get('source_path')!r} 的正文块与章节分配数量不一致"
                )
                break

    if not issues and epub_documents is not None:
        flattened = [
            (chapter, block)
            for document in epub_documents
            for block, chapter in zip(
                document.get("blocks", []),
                document.get(
                    "block_chapters",
                    [document.get("chapter")] * len(document.get("blocks", [])),
                ),
            )
        ]
        parsed_flattened = [
            (segment.get("chapter"), segment.get("content"))
            for segment in parsed_segments
        ]
        if flattened != parsed_flattened:
            issues.append("EPUB 文档边界数据与新版扁平解析结果不一致")

    if not issues and epub_documents is not None:
        memo: dict[tuple[int, int], list[tuple[bool, ...]]] = {}

        def blocks_match(row_index: int, blocks: list[str]) -> bool:
            if row_index + len(blocks) > db_count:
                return False
            return all(
                rows[row_index + offset]["content"] == block
                for offset, block in enumerate(blocks)
            )

        def align(document_index: int, row_index: int) -> list[tuple[bool, ...]]:
            key = (document_index, row_index)
            if key in memo:
                return memo[key]
            if document_index == len(epub_documents):
                result = [()] if row_index == db_count else []
                memo[key] = result
                return result

            document = epub_documents[document_index]
            blocks = document.get("blocks", [])
            title = document.get("legacy_head_title")
            solutions: list[tuple[bool, ...]] = []

            if blocks_match(row_index, blocks):
                for tail in align(document_index + 1, row_index + len(blocks)):
                    solutions.append((False,) + tail)
                    if len(solutions) >= 2:
                        break

            if (
                len(solutions) < 2
                and title is not None
                and row_index < db_count
                and rows[row_index]["content"] == title
                and blocks_match(row_index + 1, blocks)
            ):
                for tail in align(document_index + 1, row_index + 1 + len(blocks)):
                    solutions.append((True,) + tail)
                    if len(solutions) >= 2:
                        break

            memo[key] = solutions[:2]
            return memo[key]

        solutions = align(0, 0)
        if not solutions:
            issues.append("无法对齐：存在正文错位或非 head/title 的额外数据库片段")
            row_index = 0
            for document in epub_documents:
                blocks = document.get("blocks", [])
                title = document.get("legacy_head_title")
                if (
                    title is not None
                    and row_index < db_count
                    and rows[row_index]["content"] == title
                ):
                    row_index += 1
                for block in blocks:
                    if row_index >= db_count:
                        issues.append(f"数据库提前结束；下一条新版正文为 {block[:80]!r}")
                        break
                    if rows[row_index]["content"] != block:
                        issues.append(
                            f"seq {rows[row_index]['seq']} 无法匹配："
                            f"数据库为 {rows[row_index]['content'][:80]!r}，"
                            f"新版正文为 {block[:80]!r}"
                        )
                        break
                    row_index += 1
                else:
                    continue
                break
            if row_index < db_count and len(issues) == 1:
                for row in rows[row_index:row_index + 10]:
                    issues.append(
                        f"未识别额外片段：seq {row['seq']}，content={row['content'][:80]!r}"
                    )
        elif len(solutions) > 1:
            issues.append("对齐存在多个有效解，可能由重复正文造成，已按歧义中止")
        else:
            row_index = 0
            for document, has_legacy_title in zip(epub_documents, solutions[0]):
                blocks = document.get("blocks", [])
                block_chapters = document.get(
                    "block_chapters",
                    [document.get("chapter")] * len(blocks),
                )
                chapter = block_chapters[0] if block_chapters else document.get("chapter")
                if has_legacy_title:
                    row = rows[row_index]
                    assignments.append((row, chapter))
                    legacy_titles.append({
                        "segment_id": row["id"],
                        "seq": row["seq"],
                        "old_chapter": row["chapter"],
                        "new_chapter": chapter,
                        "content": row["content"],
                        "source_path": document.get("source_path"),
                    })
                    row_index += 1
                for block_chapter in block_chapters:
                    assignments.append((rows[row_index], block_chapter))
                    row_index += 1
                    matched_count += 1

    elif not issues:
        if db_count != parsed_count:
            issues.append(f"片段数不一致：数据库 {db_count}，重新解析 {parsed_count}")
        else:
            for row, parsed in zip(rows, parsed_segments):
                if row["content"] != parsed.get("content"):
                    issues.append(
                        f"seq {row['seq']} 正文不一致（segment_id={row['id']}）"
                    )
            if not issues:
                assignments = [
                    (row, parsed.get("chapter"))
                    for row, parsed in zip(rows, parsed_segments)
                ]
                matched_count = parsed_count

    if not issues:
        for row, new_chapter in assignments:
            if row["chapter"] == new_chapter:
                continue
            change = {
                "segment_id": row["id"],
                "seq": row["seq"],
                "old_chapter": row["chapter"],
                "new_chapter": new_chapter,
            }
            changes.append(change)
            key = (row["chapter"], new_chapter)
            mappings[key] = mappings.get(key, 0) + 1

    return {
        "valid": not issues,
        "db_count": db_count,
        "parsed_count": parsed_count,
        "legacy_title_count": len(legacy_titles),
        "legacy_titles": legacy_titles,
        "matched_count": matched_count,
        "changes": changes,
        "mappings": mappings,
        "issues": issues,
    }


def apply_chapter_repair(
    con: sqlite3.Connection,
    edition_id: int,
    parsed_segments: list[dict],
    epub_documents: list[dict] | None = None,
) -> int:
    """在独占写事务中再次校验，并原地更新 chapter。"""
    if con.in_transaction:
        raise RuntimeError("章节修复要求一个尚未开始事务的数据库连接")
    try:
        con.execute("BEGIN IMMEDIATE")
        report = plan_chapter_repair(
            con, edition_id, parsed_segments, epub_documents=epub_documents
        )
        if not report["valid"]:
            raise ChapterRepairError(report)
        for change in report["changes"]:
            cur = con.execute(
                "UPDATE segments SET chapter=? WHERE id=? AND edition_id=? AND seq=?",
                (
                    change["new_chapter"],
                    change["segment_id"],
                    edition_id,
                    change["seq"],
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"更新 seq {change['seq']} 时目标不再唯一，已中止"
                )
        con.commit()
        return len(report["changes"])
    except Exception:
        con.rollback()
        raise


def get_reader_edition(con: sqlite3.Connection, edition_id: int) -> sqlite3.Row | None:
    """返回阅读页需要的版本与作品信息。"""
    return con.execute(
        """SELECT e.id AS edition_id, e.work_id, e.language, e.format,
                  e.filename, e.indexed_at, w.title_zh
           FROM editions e JOIN works w ON w.id=e.work_id
           WHERE e.id=?""",
        (edition_id,),
    ).fetchone()


_CHAPTER_TITLE = re.compile(
    r"^(?:chapter\s+[0-9ivxlcdm]+\b|第\s*[0-9一二三四五六七八九十百千零〇两]+\s*[章話话回節节])",
    re.IGNORECASE,
)
_PART_TITLE = re.compile(
    r"^(?:part\s+[0-9ivxlcdm]+\b|第\s*[0-9一二三四五六七八九十百千零〇两]+\s*部)",
    re.IGNORECASE,
)
_FRONTMATTER_TITLES = {
    "cover", "title page", "copyright", "contents", "table of contents",
    "epigraph", "前言", "序言", "目次", "目录", "著作権", "奥付",
}
_BACKMATTER_TITLES = {
    "afterword", "acknowledgments", "acknowledgements", "后记", "後書き",
    "あとがき", "致谢", "謝辞",
}

_STANDALONE_WEB_ADDRESS = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?:[/#?][^\s]*)?$",
    re.IGNORECASE,
)


def _has_readable_segment_content(content: str | None) -> bool:
    """判断 segment 是否含有可阅读正文，而非空白或独立结构占位。"""
    text = (content or "").strip()
    if not text or not any(character.isalnum() for character in text):
        return False
    if _STANDALONE_WEB_ADDRESS.fullmatch(text):
        return False
    return True


def _explicit_chapter_kind(chapter: str | None) -> str:
    title = " ".join((chapter or "").split())
    folded = title.casefold()
    if _CHAPTER_TITLE.match(title):
        return "chapter"
    if _PART_TITLE.match(title):
        return "part"
    if (
        folded in _FRONTMATTER_TITLES
        or folded.startswith("also by ")
        or folded.startswith("continued")
        or folded.startswith("preface")
        or folded.startswith("foreword")
    ):
        return "frontmatter"
    if (
        folded in _BACKMATTER_TITLES
        or folded.startswith("a note about the author")
        or folded.startswith("about the author")
        or folded.startswith("what’s next")
        or folded.startswith("what's next")
    ):
        return "backmatter"
    return "other"


def get_reader_blocks(con: sqlite3.Connection, edition_id: int) -> list[dict]:
    """按 (seq, id) 从含可阅读正文的 segment 生成连续章节块。"""
    rows = con.execute(
        """SELECT id, seq, chapter, content FROM segments
           WHERE edition_id=? ORDER BY seq, id""",
        (edition_id,),
    ).fetchall()
    blocks: list[dict] = []
    for row in rows:
        if not _has_readable_segment_content(row["content"]):
            continue
        if not blocks or blocks[-1]["chapter"] != row["chapter"]:
            blocks.append({
                "title": row["chapter"] or "未标注章节",
                "chapter": row["chapter"],
                "first_segment_id": row["id"],
                "first_seq": row["seq"],
                "last_seq": row["seq"],
                "kind": _explicit_chapter_kind(row["chapter"]),
                "is_current": False,
                "_segment_ids": [row["id"]],
            })
        else:
            blocks[-1]["last_seq"] = row["seq"]
            blocks[-1]["_segment_ids"].append(row["id"])

    chapter_indexes = [i for i, block in enumerate(blocks) if block["kind"] == "chapter"]
    if chapter_indexes:
        first_chapter, last_chapter = chapter_indexes[0], chapter_indexes[-1]
        for index, block in enumerate(blocks):
            if block["kind"] != "other":
                continue
            if index < first_chapter:
                block["kind"] = "frontmatter"
            elif index > last_chapter:
                block["kind"] = "backmatter"
    return blocks


def get_reader_chapter(
    con: sqlite3.Connection,
    edition_id: int,
    segment_id: int | None = None,
) -> dict | None:
    """用统一章节块列表定位正文、完整目录及相邻章节。

    片段按 ``(seq, id)`` 排序。章节名相同但中间被其他章节隔开的片段
    属于不同章节块；chapter 为 NULL 时也按连续的 NULL 块处理。
    """
    blocks = get_reader_blocks(con, edition_id)
    if not blocks:
        return None

    if segment_id is None:
        current_index = next(
            (i for i, block in enumerate(blocks) if block["kind"] == "chapter"),
            0,
        )
        segment_id = blocks[current_index]["first_segment_id"]
    else:
        current_index = next(
            (
                i for i, block in enumerate(blocks)
                if segment_id in block["_segment_ids"]
            ),
            -1,
        )
        if current_index < 0:
            return None

    current = blocks[current_index]
    current["is_current"] = True
    target = con.execute(
        "SELECT * FROM segments WHERE edition_id=? AND id=?",
        (edition_id, segment_id),
    ).fetchone()
    if target is None:
        return None
    block_segment_ids = set(current["_segment_ids"])
    segments = [
        row for row in con.execute(
            """SELECT * FROM segments WHERE edition_id=? AND seq BETWEEN ? AND ?
               ORDER BY seq, id""",
            (edition_id, current["first_seq"], current["last_seq"]),
        ).fetchall()
        if row["id"] in block_segment_ids
    ]

    return {
        "target": target,
        "segments": segments,
        "toc": blocks,
        "current": current,
        "previous": blocks[current_index - 1] if current_index > 0 else None,
        "next": blocks[current_index + 1] if current_index + 1 < len(blocks) else None,
    }


def search(
    con: sqlite3.Connection,
    query: str,
    language: str | None = None,
    genre: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """全文检索，返回片段 + 作品/版本上下文。"""
    fts_q = build_fts_query(query)
    if not fts_q:
        return []
    sql = """
        SELECT s.id AS segment_id, s.content, s.chapter, s.page, s.seq,
               e.id AS edition_id, e.language, e.format,
               w.id AS work_id, w.title_zh, w.title_ja, w.title_en, w.year, w.genres
        FROM segments_fts f
        JOIN segments s ON s.id = f.segment_id
        JOIN editions e ON e.id = s.edition_id
        JOIN works w    ON w.id = e.work_id
        WHERE segments_fts MATCH ?
    """
    params: list = [fts_q]
    if language:
        sql += " AND e.language = ?"
        params.append(language)
    if genre:
        sql += " AND w.genres LIKE ?"
        params.append(f'%"{genre}"%')
    sql += " ORDER BY w.year, e.language, s.seq LIMIT ?"
    params.append(limit)
    return con.execute(sql, params).fetchall()


def search_grouped(
    con: sqlite3.Connection,
    query: str,
    language: str | None = None,
    genre: str | None = None,
    per_work_limit: int = 30,
) -> list[sqlite3.Row]:
    """为网页分组展示检索，每部作品返回至多指定数量的命中片段。

    ``total_hits`` 是该作品在当前查询和筛选条件下的完整命中片段数；
    此查询不限制命中作品的数量。原有 ``search()`` 保持全局限制语义，供
    CLI 等调用方继续使用。
    """
    fts_q = build_fts_query(query)
    if not fts_q or per_work_limit <= 0:
        return []
    filters = ""
    params: list = [fts_q]
    if language:
        filters += " AND e.language = ?"
        params.append(language)
    if genre:
        filters += " AND w.genres LIKE ?"
        params.append(f'%"{genre}"%')
    sql = f"""
        WITH ranked AS (
            SELECT s.id AS segment_id, s.content, s.chapter, s.page, s.seq,
                   e.id AS edition_id, e.language, e.format,
                   w.id AS work_id, w.title_zh, w.title_ja, w.title_en,
                   w.year, w.genres,
                   COUNT(*) OVER (PARTITION BY w.id) AS total_hits,
                   ROW_NUMBER() OVER (
                       PARTITION BY w.id
                       ORDER BY e.language, s.seq
                   ) AS hit_rank
            FROM segments_fts f
            JOIN segments s ON s.id = f.segment_id
            JOIN editions e ON e.id = s.edition_id
            JOIN works w    ON w.id = e.work_id
            WHERE segments_fts MATCH ?{filters}
        )
        SELECT segment_id, content, chapter, page, seq,
               edition_id, language, format,
               work_id, title_zh, title_ja, title_en, year, genres,
               total_hits
        FROM ranked
        WHERE hit_rank <= ?
        ORDER BY year, language, seq
    """
    params.append(per_work_limit)
    return con.execute(sql, params).fetchall()


def search_work(
    con: sqlite3.Connection,
    query: str,
    work_id: int,
    language: str | None = None,
    genre: str | None = None,
) -> list[sqlite3.Row]:
    """返回指定作品的全部匹配片段，供搜索页按作品渐进展开。"""
    fts_q = build_fts_query(query)
    if not fts_q:
        return []
    sql = """
        SELECT s.id AS segment_id, s.content, s.chapter, s.page, s.seq,
               e.id AS edition_id, e.language, e.format,
               w.id AS work_id, w.title_zh, w.title_ja, w.title_en,
               w.year, w.genres
        FROM segments_fts f
        JOIN segments s ON s.id = f.segment_id
        JOIN editions e ON e.id = s.edition_id
        JOIN works w    ON w.id = e.work_id
        WHERE segments_fts MATCH ? AND w.id = ?
    """
    params: list = [fts_q, work_id]
    if language:
        sql += " AND e.language = ?"
        params.append(language)
    if genre:
        sql += " AND w.genres LIKE ?"
        params.append(f'%"{genre}"%')
    sql += " ORDER BY e.language, s.seq"
    return con.execute(sql, params).fetchall()


def kwic(content: str, query: str, width: int = 60) -> list[dict]:
    """在原文中定位查询词，生成关键词居中的上下文片段（KWIC）。"""
    hits = []
    pattern = build_display_term_pattern(query)
    if pattern is None:
        return hits
    for m in pattern.finditer(content):
        start, end = m.start(), m.end()
        left = content[max(0, start - width): start]
        right = content[end: end + width]
        hits.append({"left": left, "match": content[start:end], "right": right})
        if len(hits) >= 5:  # 每个片段最多展示 5 处命中
            break
    return hits


def loads_list(value: str | None) -> list:
    try:
        v = json.loads(value or "[]")
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
