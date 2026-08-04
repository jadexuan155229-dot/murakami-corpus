import tempfile
import unittest
import zipfile
from pathlib import Path

from corpus.ingest import (
    _is_continued_label,
    _merge_continued_documents,
    parse_epub,
    parse_epub_documents,
)


CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles>
</container>
"""


class ParseEpubTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def make_epub(self, files: dict[str, str]) -> Path:
        path = Path(self.temp_dir.name) / "test.epub"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/container.xml", CONTAINER)
            for name, content in files.items():
                zf.writestr(name, content)
        return path

    def continued_document(
        self,
        source_path,
        blocks,
        block_tags,
        chapter,
        *,
        nav_title=None,
        nav_order=None,
        head_title=None,
    ):
        return {
            "source_path": source_path,
            "chapter": chapter,
            "head_title": head_title,
            "legacy_head_title": head_title,
            "blocks": blocks,
            "block_tags": block_tags,
            "block_chapters": [chapter] * len(blocks),
            "chapter_source": "nav" if nav_title else "body_heading",
            "nav_entries": (
                [{"title": nav_title, "fragment": None, "order": nav_order}]
                if nav_title else []
            ),
            "ncx_entries": [],
        }

    def test_epub3_nav_is_preferred_and_hidden_text_is_ignored(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="navigation/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="ncx" href="navigation/toc.ncx" media-type="application/x-dtbncx+xml"/>
            <item id="chapter" href="Text/Chapter%201.xhtml" media-type="application/xhtml+xml"/>
          </manifest>
          <spine toc="ncx"><itemref idref="chapter"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol><li>
            <a href="../Text/Chapter%201.xhtml#opening">Chapter 1 from nav</a>
          </li></ol></nav></body>
        </html>"""
        ncx = """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
          <navPoint><navLabel><text>Chapter 1 from NCX</text></navLabel>
            <content src="../Text/Chapter%201.xhtml"/></navPoint>
        </navMap></ncx>"""
        chapter = """<html><head><title>Title fallback</title>
          <style>style text</style><script>script text</script></head><body>
          <noscript><p>noscript text</p></noscript>
          <h1>Visible heading</h1><p>Chapter 9</p><p>Visible paragraph</p></body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/navigation/nav.xhtml": nav,
            "OPS/navigation/toc.ncx": ncx,
            "OPS/Text/Chapter 1.xhtml": chapter,
        })

        segments = parse_epub(path)

        self.assertEqual([s["chapter"] for s in segments], ["Chapter 1 from nav"] * 3)
        self.assertEqual(
            [s["content"] for s in segments],
            ["Visible heading", "Chapter 9", "Visible paragraph"],
        )

    def test_epub2_ncx_is_used_when_nav_is_unavailable(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
          <manifest>
            <item id="ncx" href="toc/toc.ncx" media-type="application/x-dtbncx+xml"/>
            <item id="chapter" href="Text/Chapter%202.xhtml" media-type="application/xhtml+xml"/>
          </manifest>
          <spine toc="ncx"><itemref idref="chapter"/></spine>
        </package>"""
        ncx = """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
          <navPoint><navLabel><text>Chapter 2 from NCX</text></navLabel>
            <content src="..\\Text\\Chapter%202.xhtml#start"/></navPoint>
        </navMap></ncx>"""
        chapter = """<html><head><title>Title fallback</title></head><body>
          <p>Chapter 8</p><p>Body text</p></body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/toc/toc.ncx": ncx,
            "OPS/Text/Chapter 2.xhtml": chapter,
        })

        segments = parse_epub(path)

        self.assertEqual([s["chapter"] for s in segments], ["Chapter 2 from NCX"] * 2)
        self.assertEqual([s["content"] for s in segments], ["Chapter 8", "Body text"])

    def test_heading_title_and_stem_fallback_order(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="heading" href="heading.xhtml" media-type="application/xhtml+xml"/>
            <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
            <item id="stem" href="stem.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine>
            <itemref idref="heading"/><itemref idref="title"/><itemref idref="stem"/>
          </spine>
        </package>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/heading.xhtml": "<html><head><title>Head title</title></head><body><h2>Body heading</h2></body></html>",
            "OPS/title.xhtml": "<html><head><title>Head title</title></head><body><p>Title body</p></body></html>",
            "OPS/stem.xhtml": "<html><head></head><body><p>Stem body</p></body></html>",
        })

        segments = parse_epub(path)

        self.assertEqual([s["chapter"] for s in segments], ["Body heading", "Head title", "stem"])
        self.assertEqual([s["content"] for s in segments], ["Body heading", "Title body", "Stem body"])

    def test_epub3_nav_splits_multiple_fragments_in_one_xhtml(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="story" href="Text/Small%20Story.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="story"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol>
            <li><a href="../Text/Small%20Story.xhtml#first">First Room</a></li>
            <li><a href="../Text/Small%20Story.xhtml#second">Second Room</a></li>
          </ol></nav></body></html>"""
        story = """<html><head><title>Small Story</title></head><body>
          <h2 id="first">A Door</h2><p>A blue bird waits.</p>
          <a name="second"></a><h2>Another Door</h2><p>A red kite turns.</p>
        </body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/nav/nav.xhtml": nav,
            "OPS/Text/Small Story.xhtml": story,
        })

        segments = parse_epub(path)

        self.assertEqual(
            [s["chapter"] for s in segments],
            ["First Room", "First Room", "Second Room", "Second Room"],
        )
        self.assertEqual(
            [s["content"] for s in segments],
            ["A Door", "A blue bird waits.", "Another Door", "A red kite turns."],
        )

    def test_epub2_ncx_splits_multiple_fragments_in_one_xhtml(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
          <manifest>
            <item id="ncx" href="toc/book.ncx" media-type="application/x-dtbncx+xml"/>
            <item id="story" href="Text/tiny.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine toc="ncx"><itemref idref="story"/></spine>
        </package>"""
        ncx = """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
          <navPoint><navLabel><text>Green Hill</text></navLabel>
            <content src="..\\Text\\tiny.xhtml#green"/></navPoint>
          <navPoint><navLabel><text>Gold Hill</text></navLabel>
            <content src="..\\Text\\tiny.xhtml#gold%20hill"/></navPoint>
        </navMap></ncx>"""
        story = """<html><head><title>Tiny Tale</title></head><body>
          <div id="green"><p>Moss wakes.</p></div>
          <h3 id="gold hill">Sunlight arrives.</h3><p>A bell rings.</p>
        </body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/toc/book.ncx": ncx,
            "OPS/Text/tiny.xhtml": story,
        })

        segments = parse_epub(path)

        self.assertEqual(
            [s["chapter"] for s in segments],
            ["Green Hill", "Gold Hill", "Gold Hill"],
        )
        self.assertEqual(
            [s["content"] for s in segments],
            ["Moss wakes.", "Sunlight arrives.", "A bell rings."],
        )

    def test_fragmented_numeric_chapters_do_not_label_front_matter_as_a_later_chapter(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
          <manifest>
            <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
            <item id="book" href="book.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine toc="ncx"><itemref idref="book"/></spine>
        </package>"""
        ncx = """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
          <navPoint><navLabel><text>1</text></navLabel><content src="book.xhtml#one"/></navPoint>
          <navPoint><navLabel><text>10</text></navLabel><content src="book.xhtml#ten"/></navPoint>
        </navMap></ncx>"""
        book = """<html><head><title>One-file book</title></head><body>
          <p>Also by this author</p><p>Copyright notice</p>
          <h1 id="one">1</h1><p>First chapter text.</p>
          <h1 id="ten">10</h1><p>Tenth chapter text.</p>
        </body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/toc.ncx": ncx,
            "OPS/book.xhtml": book,
        })

        segments = parse_epub(path)

        self.assertEqual(
            [(s["chapter"], s["content"]) for s in segments],
            [
                (None, "Also by this author"),
                (None, "Copyright notice"),
                ("1", "First chapter text."),
                ("10", "10"),
                ("10", "Tenth chapter text."),
            ],
        )

    def test_missing_fragment_safely_uses_existing_file_level_toc_fallback(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="story" href="plain.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="story"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><a href="plain.xhtml#missing">Safe Chapter</a></nav></body>
        </html>"""
        story = """<html><head><title>Plain Tale</title></head><body>
          <h2>Visible Start</h2><p>Clouds drift.</p>
        </body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/nav.xhtml": nav,
            "OPS/plain.xhtml": story,
        })

        segments = parse_epub(path)

        self.assertEqual([s["chapter"] for s in segments], ["Safe Chapter"] * 2)
        self.assertEqual([s["content"] for s in segments], ["Visible Start", "Clouds drift."])

    def test_strict_body_chapter_markers_split_without_matching_sentences(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="story" href="story.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="story"/></spine>
        </package>"""
        story = """<html><head><title>Fallback Tale</title></head><body>
          <p>Opening note.</p>
          <p>  Chapter   1  </p><p>A tiny lamp shines.</p>
          <p>In Chapter 1, a map appears.</p>
          <p>This is not Chapter 2 but a sentence.</p>
          <p>Chapter 3 begins with a discussion of rain.</p>
          <p>chapter 12</p><p>A small clock ticks.</p>
        </body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/story.xhtml": story,
        })

        segments = parse_epub(path)

        self.assertEqual(
            [s["chapter"] for s in segments],
            [
                "Fallback Tale",
                "Chapter 1",
                "Chapter 1",
                "Chapter 1",
                "Chapter 1",
                "Chapter 1",
                "chapter 12",
                "chapter 12",
            ],
        )
        self.assertEqual(segments[1]["content"], "Chapter   1")
        self.assertEqual(segments[6]["content"], "chapter 12")

    def test_adjacent_title_page_and_continued_nav_are_merged(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
          <manifest>
            <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
            <item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
            <item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
          </manifest><spine><itemref idref="title"/><itemref idref="body"/></spine>
        </package>"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
          <body><nav epub:type="toc"><ol>
            <li><a href="title.xhtml">Story Alpha</a></li>
            <li><a href="body.xhtml">Continued, Sample Collection</a></li>
          </ol></nav></body></html>"""
        path = self.make_epub({
            "OPS/package.opf": opf,
            "OPS/nav.xhtml": nav,
            "OPS/title.xhtml": "<html><body><h1>Story Alpha</h1></body></html>",
            "OPS/body.xhtml": (
                "<html><body><p>The first invented paragraph.</p>"
                "<p>The second invented paragraph.</p></body></html>"
            ),
        })

        documents = parse_epub_documents(path)
        segments = parse_epub(path)

        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0]["source_path"], "OPS/title.xhtml")
        self.assertEqual(documents[1]["source_path"], "OPS/body.xhtml")
        self.assertEqual(documents[0]["block_chapters"], ["Story Alpha"])
        self.assertEqual(documents[1]["block_chapters"], ["Story Alpha", "Story Alpha"])
        self.assertEqual([segment["chapter"] for segment in segments], ["Story Alpha"] * 3)
        self.assertEqual(
            [segment["content"] for segment in segments],
            ["Story Alpha", "The first invented paragraph.", "The second invented paragraph."],
        )

    def test_continued_label_variants_are_strict(self):
        accepted = [
            "Continued", "continued", "Continued, Sample", "Continued: Sample",
            "Continued — Sample", "Continued – Sample", "Continued - Sample",
            "Continued. Sample", "Continued; Sample",
        ]
        rejected = [
            "To Be Continued", "The Story Continued", "Continuation",
            "A title with continued inside", "Continued Reading",
        ]

        for label in accepted:
            with self.subTest(label=label):
                self.assertTrue(_is_continued_label(label))
        for label in rejected:
            with self.subTest(label=label):
                self.assertFalse(_is_continued_label(label))

    def test_multiple_continued_pairs_do_not_cross(self):
        documents = []
        for pair, title in enumerate(("Story Alpha", "Story Beta")):
            documents.extend([
                self.continued_document(
                    f"title-{pair}.xhtml", [title], ["h1"], title,
                    nav_title=title, nav_order=pair * 2 + 1,
                ),
                self.continued_document(
                    f"body-{pair}.xhtml", [f"Body {pair} one.", f"Body {pair} two."],
                    ["p", "p"], "Continued",
                    nav_title="Continued", nav_order=pair * 2 + 2,
                ),
            ])

        merged = _merge_continued_documents(documents)

        self.assertEqual(merged[1]["block_chapters"], ["Story Alpha"] * 2)
        self.assertEqual(merged[3]["block_chapters"], ["Story Beta"] * 2)

    def test_heading_and_short_subtitle_can_form_title_page(self):
        title = self.continued_document(
            "title.xhtml", ["Story Alpha", "A Brief Tale"], ["h1", "h2"],
            "Story Alpha", nav_title="Story Alpha", nav_order=1,
        )
        body = self.continued_document(
            "body.xhtml", ["Invented body text."], ["p"], "Continued",
            nav_title="Continued", nav_order=2,
        )

        merged = _merge_continued_documents([title, body])

        self.assertEqual(merged[0]["block_chapters"], ["Story Alpha"] * 2)
        self.assertEqual(merged[1]["block_chapters"], ["Story Alpha"])

    def test_continued_merge_rejects_unsafe_title_documents(self):
        rejected_titles = [
            self.continued_document(
                "normal.xhtml", ["Opening", "Body one.", "Body two."],
                ["h1", "p", "p"], "Opening", nav_title="Opening", nav_order=1,
            ),
            self.continued_document(
                "long.xhtml", ["L" * 161], ["h1"], "Long Story",
                nav_title="Long Story", nav_order=1,
            ),
            self.continued_document(
                "continued-title.xhtml", ["Continued"], ["h1"], "Continued",
                nav_title="Continued", nav_order=1,
            ),
            self.continued_document(
                "tiny-chapter.xhtml", ["A small clock ticks."], ["p"],
                "A small clock ticks.", nav_order=1,
            ),
            self.continued_document(
                "short-complete.xhtml", ["Tiny Chapter", "A small clock ticks."],
                ["h1", "p"], "Tiny Chapter", nav_title="Tiny Chapter", nav_order=1,
            ),
        ]
        for title in rejected_titles:
            body = self.continued_document(
                "body.xhtml", ["Invented body."], ["p"], "Continued",
                nav_title="Continued", nav_order=2,
            )
            with self.subTest(source=title["source_path"]):
                merged = _merge_continued_documents([title, body])
                self.assertEqual(merged[1]["block_chapters"], ["Continued"])

    def test_front_and_back_matter_titles_are_rejected(self):
        matter_titles = [
            "Copyright", "Contents", "Also by the Author", "Title Page",
            "Acknowledgments", "About the Author",
        ]
        for matter in matter_titles:
            title = self.continued_document(
                "matter.xhtml", [matter], ["h1"], matter,
                nav_title=matter, nav_order=1,
            )
            body = self.continued_document(
                "body.xhtml", ["Invented body."], ["p"], "Continued",
                nav_title="Continued", nav_order=2,
            )
            with self.subTest(title=matter):
                merged = _merge_continued_documents([title, body])
                self.assertEqual(merged[1]["block_chapters"], ["Continued"])

    def test_continued_merge_rejects_gaps_same_document_and_heading_conflicts(self):
        def title_document(order=1, source="title.xhtml"):
            return self.continued_document(
                source, ["Story Alpha"], ["h1"], "Story Alpha",
                nav_title="Story Alpha", nav_order=order,
            )

        gap_body = self.continued_document(
            "body.xhtml", ["Invented body."], ["p"], "Continued",
            nav_title="Continued", nav_order=3,
        )
        self.assertEqual(
            _merge_continued_documents([title_document(), gap_body])[1]["chapter"],
            "Continued",
        )

        same_file_body = self.continued_document(
            "title.xhtml", ["Invented body."], ["p"], "Continued",
            nav_title="Continued", nav_order=2,
        )
        self.assertEqual(
            _merge_continued_documents([title_document(), same_file_body])[1]["chapter"],
            "Continued",
        )

        conflict_body = self.continued_document(
            "body.xhtml", ["Different Story", "Invented body."], ["h1", "p"],
            "Continued", nav_title="Continued", nav_order=2,
        )
        self.assertEqual(
            _merge_continued_documents([title_document(), conflict_body])[1]["chapter"],
            "Continued",
        )

    def test_only_continued_coverage_is_renamed_before_later_fragment_chapter(self):
        title = self.continued_document(
            "title.xhtml", ["Story Alpha"], ["h1"], "Story Alpha",
            nav_title="Story Alpha", nav_order=1,
        )
        body = self.continued_document(
            "body.xhtml",
            ["Continued", "Earlier body.", "Story Beta", "Later body."],
            ["h1", "p", "h1", "p"], "Continued",
            nav_title="Continued", nav_order=2,
        )
        body["block_chapters"] = ["Continued", "Continued", "Story Beta", "Story Beta"]

        merged = _merge_continued_documents([title, body])

        self.assertEqual(
            merged[1]["block_chapters"],
            ["Story Alpha", "Story Alpha", "Story Beta", "Story Beta"],
        )

    def test_continued_body_heading_can_override_noncontinued_nav_label(self):
        title = self.continued_document(
            "title.xhtml", ["Story Alpha"], ["h1"], "Story Alpha",
            nav_title="Story Alpha", nav_order=1,
        )
        body = self.continued_document(
            "body.xhtml", ["Continued", "Invented body."], ["h1", "p"],
            "Sample Collection", nav_title="Sample Collection", nav_order=2,
        )

        merged = _merge_continued_documents([title, body])

        self.assertEqual(merged[1]["block_chapters"], ["Story Alpha", "Story Alpha"])

    def test_filename_alone_is_not_a_reliable_continued_source(self):
        title = self.continued_document(
            "title.xhtml", ["Story Alpha"], ["h1"], "Story Alpha",
            nav_title="Story Alpha", nav_order=1,
        )
        body = self.continued_document(
            "continued.xhtml", ["Invented body."], ["p"], "continued"
        )
        body["chapter_source"] = "filename"

        merged = _merge_continued_documents([title, body])

        self.assertEqual(merged[1]["block_chapters"], ["continued"])


if __name__ == "__main__":
    unittest.main()
