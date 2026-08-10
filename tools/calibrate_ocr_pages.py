#!/usr/bin/env python3
"""从 OCR JSONL 的连续阿拉伯数字页码候选校准印刷页码。

此工具只读输入并独占创建一个新的 JSONL 文件，不会写数据库，也不会覆盖
输入文件。它通过 ``pdf_page - printed_page_candidate`` 的多数投票找出稳定的
页码序列；无法确认稳定序列时会拒绝输出，要求人工审核。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence


MIN_OFFSET_SUPPORT = 5
MIN_DOMINANT_FRACTION = 0.70
_ARABIC_PAGE = re.compile(r"[0-9]+\Z")


class CalibrationError(RuntimeError):
    """输入 JSONL 或输出路径不安全时抛出。"""


class NoReliableSequenceError(CalibrationError):
    """数字候选未形成足够稳定的连续页码序列。"""


class ManualAnchorError(CalibrationError):
    """人工页码锚点与已验证的自动 offset 冲突。"""


@dataclass(frozen=True)
class NumericCandidate:
    pdf_page: int
    printed_page: int

    @property
    def offset(self) -> int:
        return self.pdf_page - self.printed_page


@dataclass(frozen=True)
class SequenceAnalysis:
    numeric_candidate_count: int
    dominant_offset: int
    dominant_support: int
    dominant_fraction: float
    sequence_start_pdf_page: int
    sequence_end_pdf_page: int
    page_one_pdf_page: int | None
    manual_anchor_pdf_page: int | None = None
    manual_anchor_printed_page: int | None = None


@dataclass
class CalibrationSummary:
    total_records: int = 0
    numeric_page_candidates: int = 0
    dominant_offset: int | None = None
    sequence_start_pdf_page: int | None = None
    sequence_end_pdf_page: int | None = None
    page_one_pdf_page: int | None = None
    manual_anchor_pdf_page: int | None = None
    manual_anchor_printed_page: int | None = None
    matched_ocr_candidates: int = 0
    conflicting_ocr_candidates: int = 0
    missing_candidates_filled: int = 0
    pages_calibrated: int = 0
    pages_left_untouched: int = 0
    conflicts: list[tuple[int, str, str]] = field(default_factory=list)


def _validate_pdf_page(record: dict[str, Any], *, line_number: int | None = None) -> int:
    pdf_page = record.get("pdf_page")
    if isinstance(pdf_page, bool) or not isinstance(pdf_page, int) or pdf_page < 1:
        suffix = f" 第 {line_number} 行" if line_number is not None else ""
        raise CalibrationError(f"JSONL{suffix} 的 pdf_page 必须是正整数")
    return pdf_page


def _ocr_candidate_and_score(record: dict[str, Any]) -> tuple[Any, Any]:
    """取原始 OCR 判断；已校准文件优先使用其保留的 OCR 审核字段。"""
    candidate = record.get("ocr_printed_page_candidate", record.get("printed_page_candidate"))
    score = record.get("ocr_printed_page_score", record.get("printed_page_score"))
    return candidate, score


def _as_arabic_page(value: Any) -> int | None:
    """仅接受正整数形式的阿拉伯数字页码，忽略其他 OCR 文本。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not _ARABIC_PAGE.fullmatch(stripped):
        return None
    page = int(stripped)
    return page if page > 0 else None


def numeric_candidates(records: Iterable[dict[str, Any]]) -> list[NumericCandidate]:
    candidates: list[NumericCandidate] = []
    for record in records:
        pdf_page = _validate_pdf_page(record)
        original_candidate, _ = _ocr_candidate_and_score(record)
        printed_page = _as_arabic_page(original_candidate)
        if printed_page is not None:
            candidates.append(NumericCandidate(pdf_page, printed_page))
    return candidates


def analyze_sequence(
    records: Sequence[dict[str, Any]],
    *,
    min_offset_support: int = MIN_OFFSET_SUPPORT,
    min_dominant_fraction: float = MIN_DOMINANT_FRACTION,
) -> SequenceAnalysis:
    """以 offset 多数投票分析可靠序列，不能确认时拒绝猜测。"""
    if min_offset_support < 1:
        raise ValueError("min_offset_support must be positive")
    if not 0 < min_dominant_fraction <= 1:
        raise ValueError("min_dominant_fraction must be in (0, 1]")

    candidates = numeric_candidates(records)
    votes = Counter(candidate.offset for candidate in candidates)
    if not votes:
        raise NoReliableSequenceError("No reliable printed-page sequence found. Manual review required.")
    dominant_offset, dominant_support = votes.most_common(1)[0]
    dominant_fraction = dominant_support / len(candidates)
    if (
        dominant_support < min_offset_support
        or dominant_fraction < min_dominant_fraction
    ):
        raise NoReliableSequenceError("No reliable printed-page sequence found. Manual review required.")

    inliers = [candidate for candidate in candidates if candidate.offset == dominant_offset]
    page_one_pages = [candidate.pdf_page for candidate in inliers if candidate.printed_page == 1]
    sequence_start = min(page_one_pages) if page_one_pages else min(
        candidate.pdf_page for candidate in inliers
    )
    sequence_end = max(candidate.pdf_page for candidate in inliers)
    return SequenceAnalysis(
        numeric_candidate_count=len(candidates),
        dominant_offset=dominant_offset,
        dominant_support=dominant_support,
        dominant_fraction=dominant_fraction,
        sequence_start_pdf_page=sequence_start,
        sequence_end_pdf_page=sequence_end,
        page_one_pdf_page=min(page_one_pages) if page_one_pages else None,
    )


def apply_manual_anchor(
    analysis: SequenceAnalysis,
    *,
    anchor_pdf_page: int | None = None,
    anchor_printed_page: int | None = None,
) -> SequenceAnalysis:
    """验证人工锚点；它只能前移或确定已验证序列的应用起点。"""
    if (anchor_pdf_page is None) != (anchor_printed_page is None):
        raise CalibrationError(
            "--anchor-pdf-page and --anchor-printed-page must be provided together."
        )
    if anchor_pdf_page is None:
        return analysis
    if (
        isinstance(anchor_pdf_page, bool)
        or not isinstance(anchor_pdf_page, int)
        or anchor_pdf_page < 1
        or isinstance(anchor_printed_page, bool)
        or not isinstance(anchor_printed_page, int)
        or anchor_printed_page < 1
    ):
        raise CalibrationError("Manual anchor PDF and printed pages must be positive integers.")
    anchor_offset = anchor_pdf_page - anchor_printed_page
    if anchor_offset != analysis.dominant_offset:
        raise ManualAnchorError(
            "Manual anchor conflicts with dominant offset "
            f"(anchor {anchor_offset}, dominant {analysis.dominant_offset}). "
            "Anchor validation: rejected"
        )
    return replace(
        analysis,
        sequence_start_pdf_page=anchor_pdf_page,
        manual_anchor_pdf_page=anchor_pdf_page,
        manual_anchor_printed_page=anchor_printed_page,
    )


def calibrate_records(
    records: Sequence[dict[str, Any]], analysis: SequenceAnalysis
) -> tuple[list[dict[str, Any]], CalibrationSummary]:
    """仅在可靠序列范围内覆盖校准页码，其他字段保持原样。"""
    summary = CalibrationSummary(
        total_records=len(records),
        numeric_page_candidates=analysis.numeric_candidate_count,
        dominant_offset=analysis.dominant_offset,
        sequence_start_pdf_page=analysis.sequence_start_pdf_page,
        sequence_end_pdf_page=analysis.sequence_end_pdf_page,
        page_one_pdf_page=analysis.page_one_pdf_page,
        manual_anchor_pdf_page=analysis.manual_anchor_pdf_page,
        manual_anchor_printed_page=analysis.manual_anchor_printed_page,
    )
    calibrated_records: list[dict[str, Any]] = []
    for record in records:
        pdf_page = _validate_pdf_page(record)
        calibrated = dict(record)
        original_candidate, original_score = _ocr_candidate_and_score(record)
        # 每条输出记录都保留 OCR 原判断，即使该页不在可靠连续范围内。
        calibrated.setdefault("ocr_printed_page_candidate", original_candidate)
        calibrated.setdefault("ocr_printed_page_score", original_score)
        if not (
            analysis.sequence_start_pdf_page <= pdf_page <= analysis.sequence_end_pdf_page
        ):
            summary.pages_left_untouched += 1
            calibrated_records.append(calibrated)
            continue

        inferred_page = str(pdf_page - analysis.dominant_offset)
        candidate_number = _as_arabic_page(original_candidate)
        if original_candidate is None:
            validation = "inferred_missing"
            summary.missing_candidates_filled += 1
        elif candidate_number is not None and candidate_number == int(inferred_page):
            validation = "matched"
            summary.matched_ocr_candidates += 1
        else:
            validation = "corrected_conflict"
            summary.conflicting_ocr_candidates += 1
            summary.conflicts.append((pdf_page, str(original_candidate), inferred_page))

        # printed_page 是明确的校准结果；同时更新旧字段以保持现有 importer 兼容。
        calibrated["printed_page"] = inferred_page
        calibrated["printed_page_candidate"] = inferred_page
        calibrated["printed_page_score"] = None
        calibrated["printed_page_source"] = "validated_sequence"
        calibrated["printed_page_validation"] = validation
        summary.pages_calibrated += 1
        calibrated_records.append(calibrated)
    return calibrated_records, summary


def read_jsonl(input_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    try:
        with input_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise CalibrationError(f"JSONL 第 {line_number} 行为空")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CalibrationError(f"JSONL 第 {line_number} 行无法解析") from exc
                if not isinstance(record, dict):
                    raise CalibrationError(f"JSONL 第 {line_number} 行必须是对象")
                pdf_page = _validate_pdf_page(record, line_number=line_number)
                if pdf_page in seen_pages:
                    raise CalibrationError(f"JSONL 中存在重复 pdf_page：{pdf_page}")
                seen_pages.add(pdf_page)
                records.append(record)
    except OSError as exc:
        raise CalibrationError(f"无法读取 JSONL：{input_path}") from exc
    return records


def calibrate_jsonl(
    input_path: Path,
    output_path: Path,
    *,
    min_offset_support: int = MIN_OFFSET_SUPPORT,
    min_dominant_fraction: float = MIN_DOMINANT_FRACTION,
    anchor_pdf_page: int | None = None,
    anchor_printed_page: int | None = None,
) -> CalibrationSummary:
    """分析完整输入后独占创建输出，失败时不会覆盖或创建校准副本。"""
    if input_path.resolve() == output_path.resolve():
        raise CalibrationError("Output path must differ from input path.")
    records = read_jsonl(input_path)
    analysis = analyze_sequence(
        records,
        min_offset_support=min_offset_support,
        min_dominant_fraction=min_dominant_fraction,
    )
    analysis = apply_manual_anchor(
        analysis,
        anchor_pdf_page=anchor_pdf_page,
        anchor_printed_page=anchor_printed_page,
    )
    calibrated_records, summary = calibrate_records(records, analysis)
    try:
        with output_path.open("x", encoding="utf-8") as output:
            for record in calibrated_records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise CalibrationError(f"无法创建输出 JSONL：{output_path}") from exc
    return summary


def print_summary(summary: CalibrationSummary) -> None:
    print(f"Total records: {summary.total_records}")
    print(f"Numeric page candidates: {summary.numeric_page_candidates}")
    print(f"Dominant offset: {summary.dominant_offset}")
    print(f"Sequence start: PDF {summary.sequence_start_pdf_page}")
    print(f"Sequence end: PDF {summary.sequence_end_pdf_page}")
    if summary.manual_anchor_pdf_page is not None:
        print(
            "Manual anchor: "
            f"PDF {summary.manual_anchor_pdf_page} → "
            f"p.{summary.manual_anchor_printed_page}"
        )
        print("Anchor validation: accepted")
    if summary.page_one_pdf_page is None:
        if summary.manual_anchor_pdf_page is not None:
            print("No reliable OCR p.1 candidate found; manual anchor determines sequence start.")
        else:
            print("No reliable p.1 candidate found; calibration starts at the earliest reliable inlier.")
    else:
        print(f"Sequence p.1: PDF {summary.page_one_pdf_page}")
    print(f"Matched OCR candidates: {summary.matched_ocr_candidates}")
    print(f"Conflicting OCR candidates: {summary.conflicting_ocr_candidates}")
    print(f"Missing candidates filled: {summary.missing_candidates_filled}")
    print(f"Pages calibrated: {summary.pages_calibrated}")
    print(f"Pages left untouched: {summary.pages_left_untouched}")
    for pdf_page, ocr_candidate, inferred_page in summary.conflicts:
        print(f"PDF {pdf_page}: OCR p.{ocr_candidate} → inferred p.{inferred_page}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="原始 OCR JSONL")
    parser.add_argument("--output", type=Path, required=True, help="新的校准 JSONL")
    parser.add_argument("--anchor-pdf-page", type=int, help="人工确认的 PDF 页号")
    parser.add_argument("--anchor-printed-page", type=int, help="人工确认的印刷页号")
    parser.add_argument(
        "--min-offset-support", type=int, default=MIN_OFFSET_SUPPORT,
        help=f"主 offset 至少需要的数字候选数（默认：{MIN_OFFSET_SUPPORT}）",
    )
    parser.add_argument(
        "--min-dominant-fraction", type=float, default=MIN_DOMINANT_FRACTION,
        help=f"主 offset 在数字候选中的最低占比（默认：{MIN_DOMINANT_FRACTION:.0%}）".replace("%", "%%"),
    )
    args = parser.parse_args(argv)
    if (args.anchor_pdf_page is None) != (args.anchor_printed_page is None):
        parser.error("--anchor-pdf-page and --anchor-printed-page must be provided together")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = calibrate_jsonl(
            args.input,
            args.output,
            min_offset_support=args.min_offset_support,
            min_dominant_fraction=args.min_dominant_fraction,
            anchor_pdf_page=args.anchor_pdf_page,
            anchor_printed_page=args.anchor_printed_page,
        )
    except (CalibrationError, ValueError) as exc:
        print(f"Calibration failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote: {args.output}")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
