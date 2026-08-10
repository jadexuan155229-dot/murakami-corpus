import unittest

from tools.ocr_scan_pdf import (
    find_printed_page_candidate,
    make_blocks,
    parse_pages,
    sort_blocks_reading_order,
    structure_ocr_result,
)


def box(left, top, right, bottom):
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


class OcrScanPdfTests(unittest.TestCase):
    def test_sorts_blocks_top_to_bottom_and_left_to_right_with_line_tolerance(self):
        blocks = make_blocks(
            ["右", "下一行", "左", "上方"],
            [0.99, 0.99, 0.99, 0.99],
            [
                box(220, 205, 280, 225),
                box(100, 270, 170, 290),
                box(100, 200, 160, 220),
                box(100, 100, 160, 120),
            ],
        )

        ordered = sort_blocks_reading_order(blocks)

        self.assertEqual([block["text"] for block in ordered], ["上方", "左", "右", "下一行"])

    def test_same_line_fragments_are_joined_and_low_confidence_noise_stays_in_blocks(self):
        record = structure_ocr_result(
            pdf_page=20,
            rec_texts=["右", "x", "左", "下一行"],
            rec_scores=[0.99, 0.109, 0.99, 0.99],
            rec_boxes=[
                box(220, 205, 280, 225),
                box(330, 204, 340, 220),
                box(100, 200, 160, 220),
                box(100, 270, 170, 290),
            ],
            page_height=1000,
        )

        self.assertEqual(record["text"], "左右\n下一行")
        self.assertEqual([block["text"] for block in record["blocks"]], ["左", "右", "x", "下一行"])
        self.assertTrue(record["blocks"][2]["low_confidence"])

    def test_printed_page_candidate_must_be_high_confidence_pure_number_near_top(self):
        blocks = make_blocks(
            ["003", "3", "002", "章节"],
            [0.9995, 0.9999, 0.85, 0.99],
            [
                box(300, 25, 340, 45),
                box(300, 400, 340, 440),
                box(50, 30, 90, 50),
                box(100, 100, 200, 130),
            ],
        )

        candidate, score = find_printed_page_candidate(blocks, page_height=1000)

        self.assertEqual(candidate, "3")
        self.assertEqual(score, 0.9995)
        self.assertEqual(find_printed_page_candidate(blocks[1:], 1000), (None, None))

    def test_page_range_parser(self):
        self.assertEqual(list(parse_pages(None, 20)), list(range(1, 21)))
        self.assertEqual(list(parse_pages("18-20", 20)), [18, 19, 20])
        self.assertEqual(list(parse_pages("18", 20)), [18])
        with self.assertRaises(ValueError):
            parse_pages("20-18", 20)


if __name__ == "__main__":
    unittest.main()
