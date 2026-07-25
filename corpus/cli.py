"""
命令行入口：

  python -m corpus.cli init                              初始化数据库
  python -m corpus.cli import-notion works_metadata.csv  导入作品元数据（Notion 导出）
  python -m corpus.cli add <文件> --work <作品ID> --lang zh   入库并索引一个文本文件
  python -m corpus.cli list                              列出全部作品与索引状态
  python -m corpus.cli search "羊男" [--lang ja]          终端全文检索
  python -m corpus.cli repair-chapters --edition 1       预览 EPUB 章节标题修复
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .ingest import parse_epub_documents, parse_file


def cmd_init(_args):
    db.init_db()
    print(f"数据库已就绪：{db.DB_PATH}")


def cmd_import_notion(args):
    """导入元数据 CSV。列：书名, 原版年份, 文体, 关键词, 英文版, 日文版, Status, 备注
    文体/关键词为 JSON 数组或用 、 / , 分隔的文本均可。"""
    print(f"导入 {db.import_works_csv(args.csv)} 部作品。")


def cmd_add(args):
    db.init_db()
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"文件不存在：{src}")
    con = db.connect()
    work = con.execute("SELECT * FROM works WHERE id=?", (args.work,)).fetchone()
    if work is None:
        sys.exit(f"作品 ID {args.work} 不存在，请先 import-notion 或在网页端创建。")

    dest = db.FILES_DIR / f"w{args.work}_{args.lang}_{src.name}"
    shutil.copy2(src, dest)

    cur = con.execute(
        "INSERT INTO editions (work_id, language, format, filename, has_pages, notes)"
        " VALUES (?,?,?,?,?,?)",
        (args.work, args.lang, src.suffix.lstrip(".").lower(), dest.name,
         1 if src.suffix.lower() == ".pdf" else 0, args.notes),
    )
    edition_id = cur.lastrowid

    print(f"解析 {src.name} …")
    segments = parse_file(dest)
    n = db.insert_segments(con, edition_id, segments)
    con.execute(
        "UPDATE editions SET indexed_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), edition_id),
    )
    con.commit()
    con.close()
    print(f"《{work['title_zh']}》[{args.lang}] 入库完成：{n} 个片段已进入全文索引。")


def cmd_list(_args):
    db.init_db()
    con = db.connect()
    rows = con.execute(
        """SELECT w.id, w.title_zh, w.year,
                  COUNT(e.id) AS n_editions,
                  SUM(CASE WHEN e.indexed_at IS NOT NULL THEN 1 ELSE 0 END) AS n_indexed
           FROM works w LEFT JOIN editions e ON e.work_id = w.id
           GROUP BY w.id ORDER BY w.year"""
    ).fetchall()
    for r in rows:
        print(f"[{r['id']:>3}] {r['year'] or '----':<12} {r['title_zh']}"
              f"   版本 {r['n_editions']} / 已索引 {r['n_indexed'] or 0}")
    con.close()


def cmd_search(args):
    con = db.connect()
    rows = db.search(con, args.query, language=args.lang, limit=args.limit)
    for r in rows:
        for hit in db.kwic(r["content"], args.query, width=30):
            loc = f"p.{r['page']}" if r["page"] else (r["chapter"] or f"#{r['seq']}")
            print(f"《{r['title_zh']}》[{r['language']}] {loc}")
            print(f"  …{hit['left']}【{hit['match']}】{hit['right']}…\n")
    con.close()
    print(f"共 {len(rows)} 个片段命中。")


def _chapter_label(value: str | None) -> str:
    return value if value is not None else "（空）"


def _print_repair_report(edition, source: Path, report: dict) -> None:
    print("EPUB 章节标题修复预览")
    print(
        f"版本：edition {edition['id']} · 《{edition['title_zh']}》"
        f" · {edition['language']} · {edition['format']}"
    )
    print(f"文件：{source}")
    print(f"数据库总片段数：{report['db_count']}")
    print(f"新版正文片段数：{report['parsed_count']}")
    print(f"识别出的旧版 head/title 伪片段数：{report['legacy_title_count']}")
    print(f"成功对齐的正文片段数：{report['matched_count']}")
    print(f"将更新：{len(report['changes'])} 个片段")
    print("旧章节名 → 新章节名：")
    if report["mappings"]:
        for (old, new), count in report["mappings"].items():
            print(f"  {_chapter_label(old)} → {_chapter_label(new)}：{count} 个片段")
    else:
        print("  （无可应用的映射）")
    print("无法匹配或存在歧义：")
    if report["issues"]:
        for issue in report["issues"]:
            print(f"  - {issue}")
    else:
        print("  无")
    if report["legacy_titles"]:
        print("已识别伪片段示例：")
        for item in report["legacy_titles"][:10]:
            print(
                f"  seq {item['seq']} · {_chapter_label(item['old_chapter'])}"
                f" · {item['content'][:100]!r}"
            )


def cmd_repair_chapters(args):
    edition_id = getattr(args, "edition", None)
    if edition_id is None:
        edition_id = getattr(args, "edition_option", None)
    if edition_id is None:
        sys.exit("请提供要检查的版本 ID。")
    con = db.connect()
    try:
        edition = con.execute(
            """SELECT e.*, w.title_zh FROM editions e
               JOIN works w ON w.id=e.work_id WHERE e.id=?""",
            (edition_id,),
        ).fetchone()
        if edition is None:
            sys.exit(f"版本 edition {edition_id} 不存在。")
        if edition["format"].lower() != "epub":
            sys.exit(f"版本 edition {edition_id} 不是 EPUB，不能修复 EPUB 章节标题。")
        if not edition["filename"]:
            sys.exit(f"版本 edition {edition_id} 没有记录原文件名。")

        files_root = db.FILES_DIR.resolve()
        source = (db.FILES_DIR / edition["filename"]).resolve()
        if files_root not in source.parents or not source.is_file():
            sys.exit(f"找不到版本原文件，或文件路径不安全：{source}")

        print(f"重新解析 {source.name} …")
        parsed_segments = parse_file(source)
        epub_documents = parse_epub_documents(source)
        report = db.plan_chapter_repair(
            con,
            edition_id,
            parsed_segments,
            epub_documents=epub_documents,
        )
        _print_repair_report(edition, source, report)
        if not report["valid"]:
            sys.exit("校验失败，已中止；数据库未作任何修改。")

        if not args.apply:
            print("DRY-RUN：数据库未作任何修改。确认无误后可显式加入 --apply。")
            return

        print(f"请先备份数据库：{db.DB_PATH}")
        print("开始应用；事务内将再次执行完整校验。")
        try:
            updated = db.apply_chapter_repair(
                con,
                edition_id,
                parsed_segments,
                epub_documents=epub_documents,
            )
        except db.ChapterRepairError as exc:
            _print_repair_report(edition, source, exc.report)
            sys.exit("事务内重新校验失败，已整体回滚。")
        print(f"修复完成：原地更新 {updated} 个 chapter；segment ID 与全文索引均未改变。")
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(prog="corpus")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    sp = sub.add_parser("import-notion")
    sp.add_argument("csv")
    sp.set_defaults(func=cmd_import_notion)

    sp = sub.add_parser("add")
    sp.add_argument("file")
    sp.add_argument("--work", type=int, required=True, help="作品 ID（见 list）")
    sp.add_argument("--lang", choices=["zh", "ja", "en"], required=True)
    sp.add_argument("--notes", default=None)
    sp.set_defaults(func=cmd_add)

    sub.add_parser("list").set_defaults(func=cmd_list)

    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--lang", choices=["zh", "ja", "en"], default=None)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("repair-chapters", help="预览或应用现有 EPUB 的章节标题修复")
    sp.add_argument("edition", nargs="?", type=int, help="要检查的版本 ID")
    sp.add_argument("--edition", dest="edition_option", type=int, help="要检查的版本 ID（兼容旧用法）")
    sp.add_argument("--apply", action="store_true", help="通过完整校验后实际更新 chapter")
    sp.set_defaults(func=cmd_repair_chapters)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
