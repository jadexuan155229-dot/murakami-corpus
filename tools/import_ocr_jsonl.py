#!/usr/bin/env python3
"""把审核后的 PaddleOCR JSONL 安全导入一个既有的 PDF edition。

默认仅检查 JSONL 和目标 edition，不写数据库：
    .venv/bin/python tools/import_ocr_jsonl.py --edition-id 123 --input book.ocr.jsonl

确认整本 JSONL 完整后才执行：
    .venv/bin/python tools/import_ocr_jsonl.py --edition-id 123 --input book.ocr.jsonl --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

# 允许以 `python tools/import_ocr_jsonl.py` 从项目根目录直接运行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from corpus import db


class OcrImportError(RuntimeError):
    """OCR JSONL 或导入前置条件不满足。"""


class ImportSafetyError(OcrImportError):
    """导入会覆盖既有数据或缺少完整页面覆盖时触发。"""


@dataclass(frozen=True)
class OcrRecord:
    pdf_page: int
    text: str
    printed_page: str | None
    chapter: str | None
    reading_seq: int | None
    include_in_corpus: bool


@dataclass(frozen=True)
class ImportPlan:
    edition_id: int
    work_title: str
    language: str
    filename: str
    pdf_page_count: int
    records: tuple[OcrRecord, ...]
    existing_segments: int

    @property
    def record_pages(self) -> set[int]:
        return {record.pdf_page for record in self.records}

    @property
    def expected_pages(self) -> set[int]:
        return set(range(1, self.pdf_page_count + 1))

    @property
    def missing_pages(self) -> set[int]:
        return self.expected_pages - self.record_pages

    @property
    def unexpected_pages(self) -> set[int]:
        return self.record_pages - self.expected_pages

    @property
    def has_complete_pdf_coverage(self) -> bool:
        return self.record_pages == self.expected_pages

    @property
    def nonempty_records(self) -> tuple[OcrRecord, ...]:
        return tuple(record for record in self.records if record.text.strip())

    @property
    def included_nonempty_records(self) -> tuple[OcrRecord, ...]:
        """正文非空且明确纳入语料库的页面。"""
        return tuple(
            record
            for record in self.nonempty_records
            if record.include_in_corpus
        )

    @property
    def empty_page_count(self) -> int:
        return len(self.records) - len(self.nonempty_records)

    @property
    def printed_page_count(self) -> int:
        return sum(record.printed_page is not None for record in self.records)

    def segments(self) -> list[dict[str, Any]]:
        records = sorted(
            self.included_nonempty_records,
            key=lambda record: (
                record.reading_seq
                if record.reading_seq is not None
                else record.pdf_page,
                record.pdf_page,
            ),
        )
        return [
            {
                "seq": seq,
                "chapter": record.chapter,
                "page": record.pdf_page,
                "printed_page": record.printed_page,
                "content": record.text,
            }
            for seq, record in enumerate(records, start=1)
        ]


def read_ocr_jsonl(input_path: Path) -> tuple[OcrRecord, ...]:
    """完整读取并校验 JSONL；空正文页保留为 coverage 记录。"""
    records: list[OcrRecord] = []
    seen_pages: set[int] = set()
    try:
        with input_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise OcrImportError(f"JSONL 第 {line_number} 行为空")
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OcrImportError(
                        f"JSONL 第 {line_number} 行无法解析：{exc.msg}"
                    ) from exc
                if not isinstance(raw, dict):
                    raise OcrImportError(f"JSONL 第 {line_number} 行必须是对象")
                pdf_page = raw.get("pdf_page")
                if isinstance(pdf_page, bool) or not isinstance(pdf_page, int) or pdf_page < 1:
                    raise OcrImportError(
                        f"JSONL 第 {line_number} 行的 pdf_page 必须是正整数"
                    )
                if pdf_page in seen_pages:
                    raise OcrImportError(f"JSONL 中存在重复 pdf_page：{pdf_page}")
                seen_pages.add(pdf_page)
                text = raw.get("text", "")
                if text is None:
                    text = ""
                if not isinstance(text, str):
                    raise OcrImportError(f"JSONL 第 {line_number} 行的 text 必须是字符串")
                printed_page = raw.get("printed_page_candidate")
                chapter = raw.get("chapter")
                reading_seq = raw.get("reading_seq")
                include_in_corpus = raw.get("include_in_corpus", True)
                if reading_seq is not None and (
                    isinstance(reading_seq, bool) or not isinstance(reading_seq, int)
                ):
                    raise OcrImportError(
                        f"JSONL 第 {line_number} 行的 reading_seq 必须是整数"
                    )
                if not isinstance(include_in_corpus, bool):
                    raise OcrImportError(
                        f"JSONL 第 {line_number} 行的 include_in_corpus 必须是布尔值"
                    )
                records.append(
                    OcrRecord(
                        pdf_page=pdf_page,
                        text=text,
                        printed_page=None if printed_page is None else str(printed_page),
                        chapter=None if chapter is None else str(chapter),
                        reading_seq=reading_seq,
                        include_in_corpus=include_in_corpus,
                    )
                )
    except OSError as exc:
        raise OcrImportError(f"无法读取 JSONL：{input_path}") from exc
    return tuple(sorted(records, key=lambda record: record.pdf_page))


def pdf_page_count(pdf_path: Path) -> int:
    """使用当前项目已有的 PyMuPDF 获取 PDF 文件页数。"""
    try:
        import fitz
    except ImportError as exc:
        raise OcrImportError("读取 PDF 页数需要 PyMuPDF（fitz）") from exc
    try:
        with fitz.open(pdf_path) as document:
            return document.page_count
    except Exception as exc:
        raise OcrImportError(f"无法读取 PDF：{pdf_path}") from exc


def build_import_plan(
    edition_id: int,
    input_path: Path,
    *,
    count_pdf_pages: Callable[[Path], int] | None = None,
) -> ImportPlan:
    """读取所有输入并验证目标 edition，不执行任何写入。"""
    if count_pdf_pages is None:
        count_pdf_pages = pdf_page_count
    records = read_ocr_jsonl(input_path)
    con = db.connect()
    try:
        edition = con.execute(
            """SELECT e.id, e.format, e.filename, e.language, w.title_zh
               FROM editions e JOIN works w ON w.id=e.work_id
               WHERE e.id=?""",
            (edition_id,),
        ).fetchone()
        if edition is None:
            raise OcrImportError(f"Edition {edition_id} does not exist.")
        if edition["format"] != "pdf":
            raise OcrImportError("OCR JSONL can only be imported into a PDF edition.")
        filename = edition["filename"]
        if not filename:
            raise OcrImportError("PDF edition has no filename.")
        try:
            pdf_path = db._safe_edition_file(filename)
        except db.UnsafeEditionFileError as exc:
            raise OcrImportError(f"Invalid edition filename: {filename!r}") from exc
        if not pdf_path.is_file():
            raise OcrImportError(f"Edition PDF file does not exist: {pdf_path}")
        total_pages = count_pdf_pages(pdf_path)
        if total_pages < 1:
            raise OcrImportError("Edition PDF has no pages.")
        existing_segments = con.execute(
            "SELECT COUNT(*) FROM segments WHERE edition_id=?", (edition_id,)
        ).fetchone()[0]
    finally:
        con.close()
    return ImportPlan(
        edition_id=edition_id,
        work_title=edition["title_zh"],
        language=edition["language"],
        filename=filename,
        pdf_page_count=total_pages,
        records=records,
        existing_segments=existing_segments,
    )


def ensure_apply_safe(
    plan: ImportPlan, *, replace_existing: bool, allow_partial: bool
) -> None:
    """执行写入前的覆盖与整本 PDF 防护。"""
    if plan.existing_segments and not replace_existing:
        raise ImportSafetyError("Refusing to overwrite existing indexed segments.")
    if not plan.has_complete_pdf_coverage and not allow_partial:
        raise ImportSafetyError(
            "Refusing to apply partial OCR data. Use --allow-partial only for an intentional partial import."
        )


def apply_import(plan: ImportPlan, *, replace_existing: bool, allow_partial: bool) -> dict[str, Any]:
    """在单一事务内清理（可选）、写入 segment/FTS，并更新 indexed_at。"""
    ensure_apply_safe(
        plan, replace_existing=replace_existing, allow_partial=allow_partial
    )
    con = db.connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        current_segments = con.execute(
            "SELECT COUNT(*) FROM segments WHERE edition_id=?", (plan.edition_id,)
        ).fetchone()[0]
        if current_segments and not replace_existing:
            raise ImportSafetyError("Refusing to overwrite existing indexed segments.")
        if current_segments:
            db.clear_edition_index(con, plan.edition_id)
        inserted = db.insert_segments(con, plan.edition_id, plan.segments())
        fts_rows = con.execute(
            """SELECT COUNT(*) FROM segments_fts f
               JOIN segments s ON s.id=f.segment_id
               WHERE s.edition_id=?""",
            (plan.edition_id,),
        ).fetchone()[0]
        if inserted != fts_rows:
            raise OcrImportError(
                f"FTS row count mismatch: inserted {inserted}, found {fts_rows}"
            )
        con.execute(
            "UPDATE editions SET indexed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), plan.edition_id),
        )
        con.commit()
        pages = [record.pdf_page for record in plan.included_nonempty_records]
        return {
            "inserted": inserted,
            "fts_rows": fts_rows,
            "page_range": f"{min(pages)}-{max(pages)}" if pages else "none",
            "printed_pages": sum(
                record.printed_page is not None for record in plan.nonempty_records
            ),
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _format_pages(pages: set[int], limit: int = 8) -> str:
    if not pages:
        return "none"
    ordered = sorted(pages)
    shown = ", ".join(str(page) for page in ordered[:limit])
    return shown if len(ordered) <= limit else f"{shown}, …"


def print_plan(plan: ImportPlan, *, replace_existing: bool = False) -> None:
    """打印 dry-run 与 apply 共用的审计摘要。"""
    print(f"Work: {plan.work_title}")
    print(f"Edition: {plan.edition_id}")
    print(f"Language: {plan.language}")
    print(f"File: {plan.filename}")
    print(f"Existing segments: {plan.existing_segments}")
    print(f"PDF pages: {plan.pdf_page_count}")
    print(f"OCR records: {len(plan.records)}")
    print(f"PDF pages covered: {len(plan.record_pages)} / {plan.pdf_page_count}")
    print(f"Missing pages: {len(plan.missing_pages)}")
    print(f"OCR pages with text: {len(plan.nonempty_records)}")
    print(f"OCR pages included in corpus: {len(plan.included_nonempty_records)}")
    print(f"Empty OCR pages: {plan.empty_page_count}")
    print(f"Printed-page candidates: {plan.printed_page_count}")
    if not plan.has_complete_pdf_coverage:
        print("PARTIAL OCR DATA:")
        print(f"{len(plan.record_pages)} / {plan.pdf_page_count} PDF pages covered.")
        print(f"Missing {len(plan.missing_pages)} pages.")
        if plan.unexpected_pages:
            print(f"Unexpected PDF pages: {_format_pages(plan.unexpected_pages)}")
        print("Apply would be refused without --allow-partial.")
    if plan.existing_segments and not replace_existing:
        print("Safe to apply: no (existing segments require --replace-existing).")
    elif plan.has_complete_pdf_coverage:
        print("Safe to apply: yes.")
    else:
        print("Safe to apply: no (partial OCR data requires --allow-partial).")
    print("Preview:")
    for record in plan.records[:5]:
        printed = f"p.{record.printed_page}" if record.printed_page is not None else "NULL"
        print(
            f"PDF {record.pdf_page} -> printed_page {printed} -> "
            f"{len(record.text.splitlines())} lines / {len(record.text)} chars"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edition-id", type=int, required=True)
    parser.add_argument("--input", type=Path, required=True, help="PaddleOCR JSONL 文件")
    parser.add_argument("--apply", action="store_true", help="实际写入数据库")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="与 --apply 一起使用时才清理该 edition 的既有 segments/FTS",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="与 --apply 一起使用时允许非整本 PDF 的 JSONL",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_import_plan(args.edition_id, args.input)
        print_plan(plan, replace_existing=args.replace_existing)
        if not args.apply:
            if plan.existing_segments and not args.replace_existing:
                raise ImportSafetyError("Refusing to overwrite existing indexed segments.")
            print("DRY RUN ONLY — database unchanged.")
            return 0
        result = apply_import(
            plan,
            replace_existing=args.replace_existing,
            allow_partial=args.allow_partial,
        )
    except OcrImportError as exc:
        print(f"Import refused: {exc}", file=sys.stderr)
        return 2
    print(f"Inserted segments: {result['inserted']}")
    print(f"FTS rows for edition: {result['fts_rows']}")
    print(f"PDF page range: {result['page_range']}")
    print(f"Printed pages populated: {result['printed_pages']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
