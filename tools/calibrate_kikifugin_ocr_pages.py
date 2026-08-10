#!/usr/bin/env python3
"""为《且听风吟》整本 OCR JSONL 写出带验证印刷页码的新副本。

原始 JSONL 不会被修改。PDF 18–169 的印刷页码已人工验证为
``pdf_page - 17``；其余页面只保留 OCR 原候选，不推断、不补页。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


DEFAULT_INPUT = Path("/mnt/c/Users/solus/Downloads/且听风吟_OCR整本.jsonl")
BODY_PDF_START = 18
BODY_PDF_END = 169


@dataclass
class CalibrationSummary:
    total_records: int = 0
    calibrated_body_pages: int = 0
    conflicts: list[tuple[int, str, str]] = field(default_factory=list)
    filled_missing_candidates: int = 0


def calibrate_record(
    record: dict[str, Any], summary: CalibrationSummary
) -> dict[str, Any]:
    """返回一条校准副本，保留原记录中除页码校准字段外的所有内容。"""
    pdf_page = record.get("pdf_page")
    if isinstance(pdf_page, bool) or not isinstance(pdf_page, int) or pdf_page < 1:
        raise ValueError("每条 OCR JSONL 记录都必须有正整数 pdf_page")

    calibrated = dict(record)
    original_candidate = record.get("printed_page_candidate")
    original_score = record.get("printed_page_score")
    if BODY_PDF_START <= pdf_page <= BODY_PDF_END:
        expected = str(pdf_page - 17)
        if original_candidate is None:
            summary.filled_missing_candidates += 1
        else:
            original_text = str(original_candidate)
            if original_text != expected:
                summary.conflicts.append((pdf_page, original_text, expected))
            calibrated["ocr_printed_page_candidate"] = original_candidate
            calibrated["ocr_printed_page_score"] = original_score
        calibrated["printed_page_candidate"] = expected
        # 这是人工验证序列，不把旧 OCR 分数伪装成新页码的置信度。
        calibrated["printed_page_score"] = None
        calibrated["printed_page_source"] = "validated_sequence"
        summary.calibrated_body_pages += 1
    else:
        calibrated["printed_page_source"] = (
            "ocr" if original_candidate is not None else None
        )
    summary.total_records += 1
    return calibrated


def calibrate_jsonl(input_path: Path, output_path: Path) -> CalibrationSummary:
    """逐行校准并以独占方式创建输出，避免覆盖原始或既有校准文件。"""
    summary = CalibrationSummary()
    with input_path.open(encoding="utf-8") as source, output_path.open(
        "x", encoding="utf-8"
    ) as output:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                raise ValueError(f"JSONL 第 {line_number} 行为空")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_number} 行无法解析") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL 第 {line_number} 行必须是对象")
            calibrated = calibrate_record(record, summary)
            output.write(json.dumps(calibrated, ensure_ascii=False) + "\n")
    return summary


def print_summary(summary: CalibrationSummary) -> None:
    print(f"Total records: {summary.total_records}")
    print(f"Calibrated body pages: {summary.calibrated_body_pages}")
    print(f"OCR candidate conflicts: {len(summary.conflicts)}")
    print(f"Missing OCR candidates filled by sequence: {summary.filled_missing_candidates}")
    conflicts_by_page = {page: (old, new) for page, old, new in summary.conflicts}
    for page in (28, 107):
        if page in conflicts_by_page:
            old, new = conflicts_by_page[page]
            print(f"PDF {page}: OCR p.{old} conflicts with calibrated p.{new}")
        else:
            print(f"PDF {page}: no OCR conflict recorded")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_INPUT.with_name("且听风吟_OCR整本_校准.jsonl"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = calibrate_jsonl(args.input, args.output)
    except (OSError, ValueError) as exc:
        print(f"Calibration failed: {exc}")
        return 2
    print(f"Wrote: {args.output}")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
