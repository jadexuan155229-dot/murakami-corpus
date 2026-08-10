import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools import calibrate_ocr_pages as calibrator


def record(page, candidate, *, score=0.99, **extra):
    return {
        "pdf_page": page,
        "printed_page_candidate": candidate,
        "printed_page_score": score if candidate is not None else None,
        "text": f"synthetic page {page}",
        "blocks": [{"text": f"block {page}"}],
        **extra,
    }


class CalibrateOcrPagesTests(unittest.TestCase):
    def write_jsonl(self, directory, name, records):
        path = directory / name
        path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )
        return path

    def calibrate(self, records, **options):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        source = self.write_jsonl(root, "source.jsonl", records)
        output = root / "calibrated.jsonl"
        summary = calibrator.calibrate_jsonl(source, output, **options)
        return source, output, summary

    def read_output(self, path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_normal_continuous_sequence_is_calibrated(self):
        records = [record(page, str(page - 10)) for page in range(11, 18)]

        _, output, summary = self.calibrate(records)
        result = self.read_output(output)

        self.assertEqual(summary.dominant_offset, 10)
        self.assertEqual(summary.sequence_start_pdf_page, 11)
        self.assertEqual(summary.sequence_end_pdf_page, 17)
        self.assertEqual(summary.matched_ocr_candidates, 7)
        self.assertEqual(summary.pages_calibrated, 7)
        self.assertEqual(result[0]["printed_page"], "1")
        self.assertEqual(result[-1]["printed_page_source"], "validated_sequence")
        self.assertEqual(result[0]["printed_page_validation"], "matched")

    def test_missing_candidate_inside_sequence_is_filled(self):
        records = [record(page, None if page == 14 else str(page - 10)) for page in range(11, 18)]

        _, output, summary = self.calibrate(records)
        result = {item["pdf_page"]: item for item in self.read_output(output)}

        self.assertEqual(result[14]["printed_page"], "4")
        self.assertEqual(result[14]["printed_page_candidate"], "4")
        self.assertIsNone(result[14]["ocr_printed_page_candidate"])
        self.assertEqual(result[14]["printed_page_validation"], "inferred_missing")
        self.assertEqual(summary.missing_candidates_filled, 1)

    def test_chapter_number_candidate_is_corrected_by_majority_sequence(self):
        records = [record(page, "1" if page == 15 else str(page - 10)) for page in range(11, 18)]

        _, output, summary = self.calibrate(records)
        result = {item["pdf_page"]: item for item in self.read_output(output)}

        self.assertEqual(summary.dominant_offset, 10)
        self.assertEqual(result[15]["printed_page"], "5")
        self.assertEqual(result[15]["ocr_printed_page_candidate"], "1")
        self.assertEqual(result[15]["printed_page_validation"], "corrected_conflict")
        self.assertEqual(summary.conflicts, [(15, "1", "5")])

    def test_multiple_outliers_do_not_change_dominant_offset(self):
        records = [record(page, str(page - 20)) for page in range(21, 31)]
        records[2]["printed_page_candidate"] = "1"
        records[7]["printed_page_candidate"] = "1"

        _, output, summary = self.calibrate(records)
        result = {item["pdf_page"]: item for item in self.read_output(output)}

        self.assertEqual(summary.dominant_offset, 20)
        self.assertEqual(summary.conflicting_ocr_candidates, 2)
        self.assertEqual(result[23]["printed_page"], "3")
        self.assertEqual(result[28]["printed_page"], "8")

    def test_unstable_offsets_refuse_automatic_calibration(self):
        records = [
            record(10, "1"), record(20, "1"), record(30, "1"),
            record(40, "1"), record(50, "1"), record(60, "1"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self.write_jsonl(root, "source.jsonl", records)
            output = root / "calibrated.jsonl"

            with self.assertRaisesRegex(
                calibrator.NoReliableSequenceError,
                "No reliable printed-page sequence found. Manual review required.",
            ):
                calibrator.calibrate_jsonl(source, output)

            self.assertFalse(output.exists())

    def test_missing_page_one_starts_at_earliest_inlier_without_backfill(self):
        records = [record(10, "front")] + [record(page, str(page - 10)) for page in range(12, 18)]

        _, output, summary = self.calibrate(records)
        result = {item["pdf_page"]: item for item in self.read_output(output)}

        self.assertIsNone(summary.page_one_pdf_page)
        self.assertIsNone(summary.manual_anchor_pdf_page)
        self.assertEqual(summary.sequence_start_pdf_page, 12)
        self.assertEqual(result[10]["printed_page_candidate"], "front")
        self.assertEqual(result[10]["ocr_printed_page_candidate"], "front")
        self.assertNotIn("printed_page", result[10])
        self.assertEqual(result[12]["printed_page"], "2")
        self.assertEqual(summary.pages_left_untouched, 1)

    def test_matching_manual_anchor_fills_missing_first_page(self):
        records = [record(18, None)] + [
            record(page, str(page - 17)) for page in range(19, 25)
        ]

        _, output, summary = self.calibrate(
            records, anchor_pdf_page=18, anchor_printed_page=1
        )
        result = {item["pdf_page"]: item for item in self.read_output(output)}

        self.assertEqual(summary.dominant_offset, 17)
        self.assertEqual(summary.sequence_start_pdf_page, 18)
        self.assertEqual(summary.manual_anchor_pdf_page, 18)
        self.assertEqual(summary.manual_anchor_printed_page, 1)
        self.assertEqual(summary.pages_calibrated, 7)
        self.assertEqual(summary.missing_candidates_filled, 1)
        self.assertEqual(result[18]["printed_page"], "1")
        self.assertEqual(result[18]["printed_page_validation"], "inferred_missing")

    def test_conflicting_manual_anchor_is_rejected_before_output(self):
        records = [record(18, None)] + [
            record(page, str(page - 17)) for page in range(19, 25)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self.write_jsonl(root, "source.jsonl", records)
            output = root / "calibrated.jsonl"

            with self.assertRaisesRegex(
                calibrator.ManualAnchorError, "Anchor validation: rejected"
            ):
                calibrator.calibrate_jsonl(
                    source,
                    output,
                    anchor_pdf_page=18,
                    anchor_printed_page=2,
                )

            self.assertFalse(output.exists())

    def test_only_one_manual_anchor_argument_is_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            calibrator.parse_args([
                "--input", "source.jsonl", "--output", "output.jsonl",
                "--anchor-pdf-page", "18",
            ])

        self.assertEqual(raised.exception.code, 2)

    def test_preserves_text_blocks_unknown_fields_and_original_ocr_fields(self):
        original = record(
            13,
            "1",
            score=0.997,
            text="正文不能改变",
            blocks=[{"text": "正文", "box": [1, 2, 3, 4]}],
            unknown={"keep": ["all", "values"]},
        )
        records = [record(11, "1"), record(12, "2"), original] + [
            record(page, str(page - 10)) for page in range(14, 18)
        ]

        _, output, _ = self.calibrate(records)
        result = {item["pdf_page"]: item for item in self.read_output(output)}[13]

        self.assertEqual(result["text"], original["text"])
        self.assertEqual(result["blocks"], original["blocks"])
        self.assertEqual(result["unknown"], original["unknown"])
        self.assertEqual(result["ocr_printed_page_candidate"], "1")
        self.assertEqual(result["ocr_printed_page_score"], 0.997)
        self.assertEqual(result["printed_page"], "3")

    def test_input_is_never_overwritten_and_existing_output_is_refused(self):
        records = [record(page, str(page - 10)) for page in range(11, 18)]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self.write_jsonl(root, "source.jsonl", records)
            source_before = source.read_text(encoding="utf-8")

            with self.assertRaisesRegex(calibrator.CalibrationError, "must differ"):
                calibrator.calibrate_jsonl(source, source)
            self.assertEqual(source.read_text(encoding="utf-8"), source_before)

            output = self.write_jsonl(root, "existing.jsonl", [{"already": "here"}])
            with self.assertRaises(calibrator.CalibrationError):
                calibrator.calibrate_jsonl(source, output)
            self.assertEqual(output.read_text(encoding="utf-8"), '{"already": "here"}\n')

    def test_main_reports_manual_review_on_unsafe_input(self):
        records = [record(page, "1") for page in (10, 20, 30, 40, 50, 60)]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self.write_jsonl(root, "source.jsonl", records)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = calibrator.main(["--input", str(source), "--output", str(root / "out.jsonl")])

        self.assertEqual(exit_code, 2)
        self.assertIn("No reliable printed-page sequence found. Manual review required.", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
