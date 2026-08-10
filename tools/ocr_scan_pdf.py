#!/usr/bin/env python3
"""将扫描 PDF 按页 OCR 为可审核的 JSONL，不接触主语料库数据库。

在专用 OCR 环境中运行：
    ~/.venvs/murakami-ocr/bin/python tools/ocr_scan_pdf.py \
        --input scan.pdf --output scan.ocr.jsonl --pages 18-20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any


LOW_CONFIDENCE_SCORE = 0.80
PRINTED_PAGE_MIN_SCORE = 0.90
TOP_PAGE_FRACTION = 0.15
MIN_LINE_TOLERANCE = 8.0
_PURE_PAGE_NUMBER = re.compile(r"\d{1,4}\Z")


def _plain_value(value: Any) -> Any:
    """把 NumPy/Paddle 值转为 JSON 可序列化的 Python 基础类型。"""
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _box_bounds(box: Sequence[Any]) -> tuple[float, float, float, float]:
    """返回 OCR 框的 left, top, right, bottom，兼容四边形和扁平坐标。"""
    plain_box = _plain_value(box)
    if not plain_box:
        raise ValueError("OCR block has an empty box")
    if isinstance(plain_box[0], Sequence) and not isinstance(plain_box[0], (str, bytes)):
        points = plain_box
    else:
        if len(plain_box) % 2:
            raise ValueError(f"OCR block has an invalid box: {plain_box!r}")
        points = list(zip(plain_box[::2], plain_box[1::2]))
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def make_blocks(
    rec_texts: Sequence[Any], rec_scores: Sequence[Any], rec_boxes: Sequence[Any]
) -> list[dict[str, Any]]:
    """将 PaddleOCR 的平行数组变成含几何信息、可 JSON 序列化的 block。"""
    if not (len(rec_texts) == len(rec_scores) == len(rec_boxes)):
        raise ValueError(
            "PaddleOCR returned mismatched rec_texts, rec_scores, and rec_boxes lengths"
        )
    blocks: list[dict[str, Any]] = []
    for text, score, box in zip(rec_texts, rec_scores, rec_boxes):
        numeric_score = float(_plain_value(score))
        plain_box = _plain_value(box)
        left, top, right, bottom = _box_bounds(plain_box)
        blocks.append(
            {
                "text": str(text).strip(),
                "score": numeric_score,
                "box": plain_box,
                "low_confidence": numeric_score < LOW_CONFIDENCE_SCORE,
                "_left": left,
                "_top": top,
                "_right": right,
                "_bottom": bottom,
                "_center_y": (top + bottom) / 2,
                "_height": max(bottom - top, 1.0),
            }
        )
    return blocks


def sort_blocks_reading_order(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按视觉行从上到下、行内从左到右排序 OCR block。

    扫描页有轻微倾斜时，同一行的框中心不会完全等高；容差随文字框高度
    自适应，并保留一个最小像素容差。
    """
    prepared = [dict(block) for block in blocks if block["text"]]
    if not prepared:
        return []
    prepared.sort(key=lambda block: (block["_center_y"], block["_left"]))
    typical_height = median(block["_height"] for block in prepared)
    tolerance = max(MIN_LINE_TOLERANCE, typical_height * 0.60)
    lines: list[dict[str, Any]] = []
    for block in prepared:
        if not lines or abs(block["_center_y"] - lines[-1]["center_y"]) > tolerance:
            lines.append({"center_y": block["_center_y"], "blocks": [block]})
            continue
        line = lines[-1]
        line["blocks"].append(block)
        line["center_y"] = sum(
            item["_center_y"] for item in line["blocks"]
        ) / len(line["blocks"])

    ordered: list[dict[str, Any]] = []
    for line in lines:
        ordered.extend(sorted(line["blocks"], key=lambda block: block["_left"]))
    return ordered


def _is_obvious_low_confidence_noise(block: Mapping[str, Any]) -> bool:
    """过滤正文中的孤立低分 ASCII 单字符噪声，原始 blocks 仍完整保留。"""
    text = str(block["text"])
    return bool(
        block["low_confidence"]
        and len(text) == 1
        and text.isascii()
        and text.isalnum()
    )


def _append_fragment(current: str, fragment: str) -> str:
    """拼接同一视觉行的框；英文词边界加空格，CJK 文本保持自然连写。"""
    if not current:
        return fragment
    if current[-1].isascii() and current[-1].isalnum() and fragment[:1].isascii() and fragment[:1].isalnum():
        return f"{current} {fragment}"
    return f"{current}{fragment}"


def build_text(blocks: Sequence[dict[str, Any]]) -> str:
    """按已排序 block 构成正文，并仅排除明显的低分单字符 ASCII 噪声。"""
    lines: list[list[dict[str, Any]]] = []
    for block in blocks:
        if _is_obvious_low_confidence_noise(block):
            continue
        center_y = block["_center_y"]
        if not lines:
            lines.append([block])
            continue
        previous = lines[-1]
        baseline = sum(item["_center_y"] for item in previous) / len(previous)
        tolerance = max(
            MIN_LINE_TOLERANCE,
            median(item["_height"] for item in previous + [block]) * 0.60,
        )
        if abs(center_y - baseline) <= tolerance:
            previous.append(block)
        else:
            lines.append([block])

    rendered_lines = []
    for line in lines:
        text = ""
        for block in sorted(line, key=lambda item: item["_left"]):
            text = _append_fragment(text, block["text"])
        if text:
            rendered_lines.append(text)
    return "\n".join(rendered_lines)


def find_printed_page_candidate(
    blocks: Sequence[dict[str, Any]], page_height: int | float
) -> tuple[str | None, float | None]:
    """从页面顶部的高分纯数字块找印刷页码候选，不推算缺失页。"""
    top_limit = float(page_height) * TOP_PAGE_FRACTION
    candidates = []
    for block in blocks:
        text = block["text"]
        if (
            block["score"] >= PRINTED_PAGE_MIN_SCORE
            and block["_top"] <= top_limit
            and _PURE_PAGE_NUMBER.fullmatch(text)
        ):
            normalized = str(int(text))
            candidates.append((block["_top"], -block["score"], normalized, block["score"]))
    if not candidates:
        return None, None
    _, _, printed_page, score = min(candidates)
    return printed_page, score


def _public_block(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": block["text"],
        "score": block["score"],
        "box": block["box"],
        "low_confidence": block["low_confidence"],
    }


def structure_ocr_result(
    *,
    pdf_page: int,
    rec_texts: Sequence[Any],
    rec_scores: Sequence[Any],
    rec_boxes: Sequence[Any],
    page_height: int | float,
) -> dict[str, Any]:
    """整理单页 PaddleOCR 返回值为 JSONL 行。"""
    blocks = sort_blocks_reading_order(make_blocks(rec_texts, rec_scores, rec_boxes))
    printed_page, candidate_score = find_printed_page_candidate(blocks, page_height)
    return {
        "pdf_page": pdf_page,
        "printed_page_candidate": printed_page,
        "printed_page_score": candidate_score,
        "candidate_score": candidate_score,
        "text": build_text(blocks),
        "blocks": [_public_block(block) for block in blocks],
    }


def parse_pages(value: str | None, total_pages: int) -> range:
    """解析可选的 1-based `18-20` 页范围，并限制在 PDF 实际页数内。"""
    if value is None:
        return range(1, total_pages + 1)
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", value.strip())
    if not match:
        raise ValueError("--pages 必须是单页号或闭区间，例如 18 或 18-20")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start or end > total_pages:
        raise ValueError(f"--pages 必须在 1-{total_pages} 之间，且起始页不得大于结束页")
    return range(start, end + 1)


def create_ocr_model():
    """只初始化一次 PP-OCRv6 中文 CPU 模型；MKL-DNN 必须关闭。"""
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang="ch",
        ocr_version="PP-OCRv6",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
        enable_mkldnn=False,
    )


def render_pdf_page(pdf: Any, page_number: int, dpi: int):
    """渲染一页 PDF 为独立 RGB PIL 图像，并立即释放 PDFium 页资源。"""
    page = pdf[page_number - 1]
    bitmap = None
    rendered = None
    try:
        bitmap = page.render(scale=dpi / 72)
        rendered = bitmap.to_pil()
        return rendered.convert("RGB").copy()
    finally:
        if rendered is not None:
            rendered.close()
        if bitmap is not None:
            bitmap.close()
        page.close()


def _result_mapping(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        return result
    json_result = getattr(result, "json", None)
    if isinstance(json_result, Mapping):
        return json_result
    raise TypeError(f"Unexpected PaddleOCR result type: {type(result).__name__}")


def ocr_pdf(input_path: Path, output_path: Path, pages: range, dpi: int) -> None:
    """逐页渲染、OCR、append JSONL；单页失败也保留此前输出。"""
    import numpy as np
    import pypdfium2 as pdfium

    ocr = create_ocr_model()
    pdf = pdfium.PdfDocument(str(input_path))
    total_pages = len(pdf)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("w", encoding="utf-8") as output_file:
            for page_number in pages:
                print(f"OCR page {page_number} / {total_pages}", flush=True)
                image = None
                try:
                    image = render_pdf_page(pdf, page_number, dpi)
                    result = ocr.predict(np.asarray(image))
                    if not result:
                        raise RuntimeError("PaddleOCR returned no result")
                    raw = _result_mapping(result[0])
                    record = structure_ocr_result(
                        pdf_page=page_number,
                        rec_texts=raw["rec_texts"],
                        rec_scores=raw["rec_scores"],
                        rec_boxes=raw["rec_boxes"],
                        page_height=image.height,
                    )
                except Exception as exc:
                    record = {
                        "pdf_page": page_number,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(record["error"], file=sys.stderr, flush=True)
                finally:
                    if image is not None:
                        image.close()
                output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                output_file.flush()
                os.fsync(output_file.fileno())
    finally:
        pdf.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="扫描 PDF 路径")
    parser.add_argument("--output", type=Path, help="JSONL 输出路径（默认与 PDF 同名）")
    parser.add_argument("--pages", help="可选的 1-based 页范围，例如 18-20")
    parser.add_argument(
        "--dpi", type=int, default=150, help="PDF 渲染 DPI（默认：150）"
    )
    args = parser.parse_args(argv)
    if args.dpi <= 0:
        parser.error("--dpi 必须为正整数")
    if args.input.suffix.lower() != ".pdf":
        parser.error("--input 必须是 PDF 文件")
    if not args.input.is_file():
        parser.error(f"找不到输入文件：{args.input}")
    if args.output is None:
        args.output = args.input.with_suffix(".ocr.jsonl")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(args.input))
        try:
            pages = parse_pages(args.pages, len(pdf))
        finally:
            pdf.close()
        ocr_pdf(args.input, args.output, pages, args.dpi)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"OCR failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
