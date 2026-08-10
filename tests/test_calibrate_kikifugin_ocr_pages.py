import json
import tempfile
import unittest
from pathlib import Path

from tools.calibrate_kikifugin_ocr_pages import (
    CalibrationSummary,
    calibrate_jsonl,
    calibrate_record,
)


class CalibrateKikifuginOcrPagesTests(unittest.TestCase):
    def test_frontmatter_keeps_ocr_page_fields_and_marks_their_source(self):
        original = {
            "pdf_page": 17,
            "printed_page_candidate": "xii",
            "printed_page_score": 0.91,
            "text": "front matter",
            "blocks": [{"text": "front matter"}],
            "extra": {"untouched": True},
        }
        summary = CalibrationSummary()

        calibrated = calibrate_record(original, summary)

        self.assertEqual(calibrated, {**original, "printed_page_source": "ocr"})
        self.assertEqual(original["printed_page_candidate"], "xii")
        self.assertEqual(summary.calibrated_body_pages, 0)

    def test_body_pages_replace_with_sequence_and_preserve_ocr_provenance(self):
        original = {
            "pdf_page": 28,
            "printed_page_candidate": "1",
            "printed_page_score": 0.9200129508972168,
            "text": "正文完全不变",
            "blocks": [{"text": "正文完全不变", "box": [1, 2, 3, 4]}],
        }
        summary = CalibrationSummary()

        calibrated = calibrate_record(original, summary)

        self.assertEqual(calibrated["printed_page_candidate"], "11")
        self.assertIsNone(calibrated["printed_page_score"])
        self.assertEqual(calibrated["printed_page_source"], "validated_sequence")
        self.assertEqual(calibrated["ocr_printed_page_candidate"], "1")
        self.assertEqual(calibrated["ocr_printed_page_score"], 0.9200129508972168)
        self.assertEqual(calibrated["text"], original["text"])
        self.assertEqual(calibrated["blocks"], original["blocks"])
        self.assertEqual(summary.conflicts, [(28, "1", "11")])

    def test_missing_body_candidate_is_filled_without_ocr_backup(self):
        summary = CalibrationSummary()

        page_18 = calibrate_record(
            {"pdf_page": 18, "printed_page_candidate": None, "text": "body"},
            summary,
        )
        page_107 = calibrate_record(
            {"pdf_page": 107, "printed_page_candidate": "1", "printed_page_score": 0.98},
            summary,
        )
        page_169 = calibrate_record(
            {"pdf_page": 169, "printed_page_candidate": "152", "text": "end"},
            summary,
        )

        self.assertEqual(page_18["printed_page_candidate"], "1")
        self.assertNotIn("ocr_printed_page_candidate", page_18)
        self.assertEqual(page_107["printed_page_candidate"], "90")
        self.assertEqual(page_169["printed_page_candidate"], "152")
        self.assertEqual(summary.filled_missing_candidates, 1)
        self.assertIn((107, "1", "90"), summary.conflicts)

    def test_jsonl_writes_a_new_copy_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "source.jsonl"
            output_path = root / "calibrated.jsonl"
            records = [
                {"pdf_page": 1, "printed_page_candidate": None, "printed_page_score": None, "text": "front", "blocks": []},
                {"pdf_page": 18, "printed_page_candidate": None, "printed_page_score": None, "text": "body", "blocks": []},
                {"pdf_page": 28, "printed_page_candidate": "1", "printed_page_score": 0.92, "text": "body 11", "blocks": []},
            ]
            source_text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
            input_path.write_text(source_text, encoding="utf-8")

            summary = calibrate_jsonl(input_path, output_path)

            self.assertEqual(input_path.read_text(encoding="utf-8"), source_text)
            output_records = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(output_records[0], {**records[0], "printed_page_source": None})
            self.assertEqual(output_records[1]["printed_page_candidate"], "1")
            self.assertEqual(output_records[2]["printed_page_candidate"], "11")
            self.assertEqual(output_records[2]["ocr_printed_page_candidate"], "1")
            self.assertEqual(summary.total_records, 3)
            self.assertEqual(summary.calibrated_body_pages, 2)


if __name__ == "__main__":
    unittest.main()
