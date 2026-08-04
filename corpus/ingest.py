"""
文本入库：把 epub / txt / pdf 解析为段落级片段。

- epub：用标准库 zipfile 直接解包，按 spine 顺序解析各 XHTML 文件，
        块级元素（p, h1-h6, blockquote, li）作为片段，章节名优先取自 EPUB 目录。
- txt：自动探测编码（utf-8 / gb18030 / shift_jis / big5），按空行分段，
        识别常见章节行（第X章 / 第X部 / Chapter N 等）。
- pdf：可选依赖 pymupdf。按页抽取文本层，每页一个片段并记录页码，
        便于引用（对应 Notion 里标注"带页码"的英文版 PDF）。
"""

from __future__ import annotations

import html.parser
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}
MIN_SEG_LEN = 2  # 过滤空段与纯符号段


# ---------------------------------------------------------------- epub ----

class _XHTMLExtractor(html.parser.HTMLParser):
    """把 XHTML 抽成块级文本列表，并记录标题与 fragment 锚点。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.block_tags: list[str | None] = []
        self.anchors: dict[str, int] = {}
        self.first_heading: str | None = None
        self.head_title: str | None = None
        self.legacy_head_title: str | None = None
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._head_depth = 0
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self._head_depth += 1
        if tag == "title" and self._head_depth:
            self._in_title = True
            self._title_buf = []
        if tag in ("head", "style", "script", "noscript"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        attr_map = dict(attrs)
        anchors = [attr_map.get("id")]
        if tag == "a":
            anchors.append(attr_map.get("name"))
        for anchor in anchors:
            if anchor:
                self.anchors.setdefault(unquote(anchor), len(self.blocks))
        if tag in BLOCK_TAGS:
            self._stack.append(tag)
        if tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            raw_title = "".join(self._title_buf).strip()
            title = " ".join(raw_title.split())
            if title and self.head_title is None:
                self.head_title = title
            if len(raw_title) >= MIN_SEG_LEN and self.legacy_head_title is None:
                self.legacy_head_title = raw_title
            self._in_title = False
            self._title_buf = []
        if tag == "head" and self._head_depth:
            self._head_depth -= 1
        if tag in ("head", "style", "script", "noscript") and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in BLOCK_TAGS and self._stack and self._stack[-1] == tag:
            self._flush(closing=tag)
            self._stack.pop()

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
        if not self._skip:
            self._buf.append(data)

    def _flush(self, closing: str | None = None):
        text = "".join(self._buf).strip()
        self._buf = []
        if len(text) >= MIN_SEG_LEN:
            self.blocks.append(text)
            self.block_tags.append(closing)
            if closing in HEADING_TAGS and self.first_heading is None:
                self.first_heading = text

    def close(self):
        self._flush()
        super().close()


_NS = {
    "cn": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}
_EPUB_NS = "http://www.idpf.org/2007/ops"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve_href(base_file: str, href: str) -> str:
    """把相对于 EPUB 内文件的 href 规范化为 ZIP 成员路径。"""
    href_path = unquote(urlsplit(href.replace("\\", "/")).path)
    base_dir = posixpath.dirname(base_file.replace("\\", "/"))
    return posixpath.normpath(posixpath.join(base_dir, href_path)).lstrip("/")


def _resolve_toc_href(base_file: str, href: str) -> tuple[str, str | None]:
    """解析目录 href，同时保留 URL 解码后的 fragment。"""
    normalized = href.replace("\\", "/")
    parts = urlsplit(normalized)
    path = _resolve_href(base_file, normalized)
    fragment = unquote(parts.fragment) or None
    return path, fragment


def _archive_names(zf: zipfile.ZipFile) -> dict[str, str]:
    """规范化 ZIP 成员名，同时保留 zipfile 读取所需的原始名称。"""
    return {
        posixpath.normpath(name.replace("\\", "/")).lstrip("/"): name
        for name in zf.namelist()
    }


def _element_text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _nav_titles(
    zf: zipfile.ZipFile,
    manifest_items: list[ET.Element],
    opf_path: str,
    names: dict[str, str],
) -> dict[str, list[dict]]:
    """读取 EPUB 3 toc nav，按正文路径保留有序目录项。"""
    for item in manifest_items:
        properties = (item.get("properties") or "").split()
        if "nav" not in properties or not item.get("href"):
            continue
        nav_path = _resolve_href(opf_path, item.get("href"))
        actual_name = names.get(nav_path)
        if actual_name is None:
            continue
        try:
            root = ET.fromstring(zf.read(actual_name))
        except (ET.ParseError, KeyError):
            continue
        for nav in root.iter():
            if _local_name(nav.tag) != "nav":
                continue
            nav_type = nav.get(f"{{{_EPUB_NS}}}type") or nav.get("epub:type") or ""
            if "toc" not in nav_type.split():
                continue
            titles: dict[str, list[dict]] = {}
            order = 0
            for link in nav.iter():
                if _local_name(link.tag) != "a" or not link.get("href"):
                    continue
                title = _element_text(link)
                if title:
                    target, fragment = _resolve_toc_href(nav_path, link.get("href"))
                    order += 1
                    titles.setdefault(target, []).append({
                        "title": title,
                        "fragment": fragment,
                        "order": order,
                    })
            if titles:
                return titles
    return {}


def _ncx_titles(
    zf: zipfile.ZipFile,
    manifest_items: list[ET.Element],
    spine: ET.Element | None,
    opf_path: str,
    names: dict[str, str],
) -> dict[str, list[dict]]:
    """读取 EPUB 2 NCX，按正文路径保留有序目录项。"""
    by_id = {item.get("id"): item for item in manifest_items if item.get("id")}
    candidates: list[ET.Element] = []
    toc_id = spine.get("toc") if spine is not None else None
    if toc_id and toc_id in by_id:
        candidates.append(by_id[toc_id])
    candidates.extend(
        item for item in manifest_items
        if item.get("media-type") == "application/x-dtbncx+xml" and item not in candidates
    )
    for item in candidates:
        href = item.get("href")
        if not href:
            continue
        ncx_path = _resolve_href(opf_path, href)
        actual_name = names.get(ncx_path)
        if actual_name is None:
            continue
        try:
            root = ET.fromstring(zf.read(actual_name))
        except (ET.ParseError, KeyError):
            continue
        titles: dict[str, list[dict]] = {}
        order = 0
        for point in root.iter():
            if _local_name(point.tag) != "navPoint":
                continue
            label = next((child for child in point if _local_name(child.tag) == "navLabel"), None)
            content = next((child for child in point if _local_name(child.tag) == "content"), None)
            if label is None or content is None or not content.get("src"):
                continue
            title = _element_text(label)
            if title:
                target, fragment = _resolve_toc_href(ncx_path, content.get("src"))
                order += 1
                titles.setdefault(target, []).append({
                    "title": title,
                    "fragment": fragment,
                    "order": order,
                })
        if titles:
            return titles
    return {}


def _toc_block_chapters(
    entries: list[dict[str, str | None]],
    anchors: dict[str, int],
    block_count: int,
    fallback: str | None,
) -> list[str | None] | None:
    """把可用目录项投射到正文块；没有可用目标时返回 None。"""
    changes: dict[int, str] = {}
    for entry in entries:
        title = entry.get("title")
        fragment = entry.get("fragment")
        if not title:
            continue
        if fragment is None:
            block_index = 0
        else:
            block_index = anchors.get(fragment, -1)
        if 0 <= block_index < block_count:
            changes.setdefault(block_index, title)
    if not changes:
        first_title = next((entry.get("title") for entry in entries if entry.get("title")), None)
        return [first_title] * block_count if first_title else None

    # 同一个 XHTML 可以同时包含版权页、书目和全部正文。若目录的第一个
    # fragment 在后面，前面的块尚未属于任何目录章节；把它们标为 None，
    # 而不是误借用该文件中第一个（往往是较晚才出现的）标题。
    chapter = None if min(changes) > 0 else fallback
    chapters: list[str | None] = []
    for block_index in range(block_count):
        chapter = changes.get(block_index, chapter)
        chapters.append(chapter)
    return chapters


_STRICT_EPUB_CHAPTER = re.compile(r"^chapter [1-9][0-9]*$", re.IGNORECASE)
_EPUB_CHAPTER_MARKER_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6"}


def _body_chapter_blocks(
    blocks: list[str],
    block_tags: list[str | None],
    fallback: str,
) -> list[str] | None:
    """从独立的严格 ``Chapter N`` 正文块推断章节边界。"""
    changes: dict[int, str] = {}
    for block_index, (block, tag) in enumerate(zip(blocks, block_tags)):
        title = " ".join(block.split())
        if (
            len(title) <= 32
            and tag in _EPUB_CHAPTER_MARKER_TAGS
            and _STRICT_EPUB_CHAPTER.fullmatch(title)
        ):
            changes[block_index] = title
    if not changes:
        return None

    chapters: list[str] = []
    chapter = fallback
    for block_index in range(len(blocks)):
        chapter = changes.get(block_index, chapter)
        chapters.append(chapter)
    return chapters


_CONTINUED_LABEL = re.compile(
    r"^\s*continued(?:\s*[,.:;—–-]\s*.*)?\s*$",
    re.IGNORECASE,
)
_FRONT_OR_BACK_MATTER = {
    "copyright", "contents", "table of contents", "also by",
    "also by the author", "title page", "half title", "dedication",
    "epigraph", "preface", "foreword", "introduction",
    "acknowledgments", "acknowledgements", "notes", "endnotes",
    "bibliography", "about the author", "about the publisher", "cover",
    "frontispiece", "colophon",
}
_FRONT_OR_BACK_PREFIXES = ("also by", "praise for", "other books by")
_TITLE_HEADING_TAGS = {"h1", "h2", "h3"}
_SENTENCE_END = re.compile(r"[.!?。！？](?:[\"'’”»）)]|\s|$)")
_DIALOGUE_START = re.compile(r"^\s*[\"'“‘—–-]")


def _is_continued_label(label: str | None) -> bool:
    return bool(label and _CONTINUED_LABEL.fullmatch(label))


def _normalize_matter_title(title: str) -> str:
    normalized = " ".join(title.lower().split())
    return normalized.strip(" \t\r\n.,:;!?。！？—–-_'\"‘’“”()[]{}")


def _is_front_or_back_matter(title: str) -> bool:
    normalized = _normalize_matter_title(title)
    return (
        normalized in _FRONT_OR_BACK_MATTER
        or any(normalized.startswith(prefix) for prefix in _FRONT_OR_BACK_PREFIXES)
    )


def _first_title_heading(document: dict) -> tuple[int, str] | None:
    for index, (block, tag) in enumerate(zip(
        document.get("blocks", []), document.get("block_tags", [])
    )):
        if tag in _TITLE_HEADING_TAGS and block.strip():
            return index, " ".join(block.split())
    return None


def _document_toc_titles(document: dict) -> list[str]:
    entries = document.get("nav_entries") or document.get("ncx_entries") or []
    return [
        " ".join(str(entry.get("title")).split())
        for entry in entries
        if entry.get("title")
    ]


def _extract_title_candidate(document: dict) -> str | None:
    heading = _first_title_heading(document)
    if heading is not None:
        return heading[1]
    toc_title = next(iter(_document_toc_titles(document)), None)
    if toc_title:
        return toc_title
    head_title = document.get("head_title")
    if head_title and str(head_title).strip():
        return " ".join(str(head_title).split())
    blocks = [block.strip() for block in document.get("blocks", []) if block.strip()]
    if len(blocks) == 1:
        return " ".join(blocks[0].split())
    return None


def _looks_like_body_text(text: str) -> bool:
    compact = " ".join(text.split())
    return len(_SENTENCE_END.findall(compact)) >= 2 or bool(_DIALOGUE_START.match(compact))


def _valid_story_title(title: str | None) -> bool:
    if not title:
        return False
    title = title.strip()
    if not title or len(title) > 120 or _is_continued_label(title):
        return False
    if _is_front_or_back_matter(title):
        return False
    if not any(character.isalpha() for character in title):
        return False
    return not bool(re.fullmatch(r"(?:page\s*)?[ivxlcdm\d]+", title, re.IGNORECASE))


def _is_short_title_document(document: dict) -> bool:
    blocks = [block.strip() for block in document.get("blocks", []) if block.strip()]
    tags = document.get("block_tags", [])
    if not 1 <= len(blocks) <= 2:
        return False
    if len(re.sub(r"\s+", "", "".join(blocks))) > 160:
        return False
    title = _extract_title_candidate(document)
    if not _valid_story_title(title):
        return False
    if _looks_like_body_text(" ".join(blocks)):
        return False

    heading = _first_title_heading(document)
    normalized_title = " ".join(title.split()).casefold()
    if len(blocks) == 1:
        block_matches_title = " ".join(blocks[0].split()).casefold() == normalized_title
        if heading is None and _SENTENCE_END.search(blocks[0]):
            return False
        return heading is not None or block_matches_title

    if heading is None:
        return False
    other_index = 1 - heading[0]
    if other_index not in (0, 1):
        return False
    other = " ".join(blocks[other_index].split())
    other_repeats_title = other.casefold() == normalized_title
    if (
        len(other) > 60
        or _looks_like_body_text(other)
        or (not other_repeats_title and _SENTENCE_END.search(other))
    ):
        return False
    # 同级的两个不同 heading 更像两个正常章节；h1 后的 h2/h3 可作为副标题。
    if len(tags) >= 2 and all(tag in _TITLE_HEADING_TAGS for tag in tags[:2]):
        hierarchical_subtitle = tags[0] == "h1" and tags[1] in {"h2", "h3"}
        if other.casefold() != normalized_title and not hierarchical_subtitle:
            return False
    return True


def _toc_has_intervening_entry(previous: dict, following: dict) -> bool:
    for key in ("nav_entries", "ncx_entries"):
        before = [entry.get("order") for entry in previous.get(key, []) if entry.get("order")]
        after = [entry.get("order") for entry in following.get(key, []) if entry.get("order")]
        if before and after and min(after) - max(before) > 1:
            return True
    return False


def _continued_block_indexes(document: dict) -> list[int]:
    blocks = document.get("blocks", [])
    chapters = document.get("block_chapters", [])
    chapter_source_is_reliable = document.get("chapter_source") != "filename"
    indexes = (
        [
            index for index, chapter in enumerate(chapters)
            if _is_continued_label(chapter)
        ]
        if chapter_source_is_reliable else []
    )
    continued_heading_indexes = [
        index for index, (block, tag) in enumerate(zip(
            blocks, document.get("block_tags", [])
        ))
        if tag in _TITLE_HEADING_TAGS and _is_continued_label(block)
    ]

    # 目录未把 Continued 投射到 block 时，允许正文首个明确 heading 接管，
    # 但只覆盖到下一个明确标题或已有 block chapter 边界。
    if not indexes and continued_heading_indexes:
        start = continued_heading_indexes[0]
        initial_chapter = chapters[start] if start < len(chapters) else None
        end = len(blocks)
        tags = document.get("block_tags", [])
        for index in range(start + 1, len(blocks)):
            tag = tags[index] if index < len(tags) else None
            if tag in _TITLE_HEADING_TAGS and not _is_continued_label(blocks[index]):
                end = index
                break
            if index < len(chapters) and chapters[index] != initial_chapter:
                end = index
                break
        indexes = list(range(start, end))
    elif not indexes:
        reliable_labels = [
            *_document_toc_titles(document),
            document.get("head_title"),
        ]
        if chapter_source_is_reliable:
            reliable_labels.append(document.get("chapter"))
        if any(_is_continued_label(label) for label in reliable_labels):
            indexes = list(range(len(blocks)))

    # 如果 Continued 覆盖范围内出现另一个明确标题，来源发生冲突，拒绝。
    for index in indexes:
        if index >= len(document.get("block_tags", [])):
            continue
        if (
            document["block_tags"][index] in _TITLE_HEADING_TAGS
            and not _is_continued_label(blocks[index])
        ):
            return []
    return indexes


def _merge_continued_documents(documents: list[dict]) -> list[dict]:
    """保守地统一相邻“短标题页 + Continued 正文页”的章节名。"""
    for index in range(len(documents) - 1):
        title_document = documents[index]
        continued_document = documents[index + 1]
        if title_document.get("source_path") == continued_document.get("source_path"):
            continue
        if not _is_short_title_document(title_document):
            continue
        if _toc_has_intervening_entry(title_document, continued_document):
            continue
        continued_indexes = _continued_block_indexes(continued_document)
        if not continued_indexes:
            continue
        title = _extract_title_candidate(title_document)
        if not _valid_story_title(title):
            continue
        chapters = list(continued_document.get("block_chapters", []))
        if len(chapters) != len(continued_document.get("blocks", [])):
            continue
        if any(block_index >= len(chapters) for block_index in continued_indexes):
            continue

        title_document["chapter"] = title
        title_document["block_chapters"] = [title] * len(title_document.get("blocks", []))
        for block_index in continued_indexes:
            chapters[block_index] = title
        continued_document["block_chapters"] = chapters
        if chapters:
            continued_document["chapter"] = chapters[0]
    return documents


def parse_epub_documents(path: Path) -> list[dict]:
    """按 spine 返回 EPUB 文档边界，供安全迁移比对使用。

    ``blocks`` 与正常 EPUB 解析使用完全相同的正文块；
    ``legacy_head_title`` 仅描述旧解析器曾误写入正文的 title 文本。
    """
    documents: list[dict] = []
    with zipfile.ZipFile(path) as zf:
        names = _archive_names(zf)
        # 1. container.xml -> OPF 路径
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//cn:rootfile", _NS)
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("EPUB container.xml 中缺少 OPF 路径")
        opf_path = _resolve_href("", rootfile.get("full-path"))
        opf = ET.fromstring(zf.read(names.get(opf_path, opf_path)))
        # 2. manifest id -> href；spine 决定阅读顺序
        manifest_items = opf.findall(".//opf:manifest/opf:item", _NS)
        manifest = {
            item.get("id"): item.get("href")
            for item in manifest_items
        }
        spine = opf.find(".//opf:spine", _NS)
        spine_ids = [i.get("idref") for i in opf.findall(".//opf:spine/opf:itemref", _NS)]
        # 每个正文文件都优先使用 EPUB 3 nav，再回退 EPUB 2 NCX。
        nav_titles = _nav_titles(zf, manifest_items, opf_path, names)
        ncx_titles = _ncx_titles(zf, manifest_items, spine, opf_path, names)
        for idref in spine_ids:
            href = manifest.get(idref)
            if not href:
                continue
            full = _resolve_href(opf_path, href)
            if not re.search(r"\.x?html?$", full, re.I) or full not in names:
                continue
            ex = _XHTMLExtractor()
            ex.feed(zf.read(names[full]).decode("utf-8", errors="ignore"))
            ex.close()
            fallback = ex.first_heading or ex.head_title or PurePosixPath(full).stem
            block_chapters = _toc_block_chapters(
                nav_titles.get(full, []), ex.anchors, len(ex.blocks), fallback
            )
            chapter_source = "nav" if block_chapters is not None else None
            if block_chapters is None:
                block_chapters = _toc_block_chapters(
                    ncx_titles.get(full, []), ex.anchors, len(ex.blocks), fallback
                )
                if block_chapters is not None:
                    chapter_source = "ncx"
            if block_chapters is None and ex.first_heading is None:
                block_chapters = _body_chapter_blocks(
                    ex.blocks, ex.block_tags, fallback
                )
                if block_chapters is not None:
                    chapter_source = "body_heading"
            if block_chapters is None:
                block_chapters = [fallback] * len(ex.blocks)
                if ex.first_heading is not None:
                    chapter_source = "heading"
                elif ex.head_title is not None:
                    chapter_source = "xhtml_title"
                else:
                    chapter_source = "filename"
            chapter = block_chapters[0] if block_chapters else fallback
            documents.append({
                "source_path": full,
                "chapter": chapter,
                "head_title": ex.head_title,
                "legacy_head_title": ex.legacy_head_title,
                "blocks": ex.blocks,
                "block_tags": ex.block_tags,
                "block_chapters": block_chapters,
                "chapter_source": chapter_source,
                "nav_entries": nav_titles.get(full, []),
                "ncx_entries": ncx_titles.get(full, []),
            })
    return _merge_continued_documents(documents)


def parse_epub(path: Path) -> list[dict]:
    segments: list[dict] = []
    seq = 0
    for document in parse_epub_documents(path):
        for block, chapter in zip(document["blocks"], document["block_chapters"]):
            seq += 1
            segments.append({
                "seq": seq,
                "chapter": chapter,
                "page": None,
                "content": block,
            })
    return segments


# ----------------------------------------------------------------- txt ----

_CHAPTER_LINE = re.compile(
    r"^\s*(第\s*[0-9一二三四五六七八九十百千]+\s*[章部回节話话]|Chapter\s+\d+|CHAPTER\s+\d+|[0-9１-９]{1,3}\s*$)",
)

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "big5", "utf-16")


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_txt(path: Path) -> list[dict]:
    text = read_text_auto(path)
    segments: list[dict] = []
    chapter = None
    seq = 0
    # 双换行分段；没有双换行的文件退化为逐行
    parts = re.split(r"\n\s*\n", text) if "\n\n" in text else text.splitlines()
    for part in parts:
        block = part.strip()
        if len(block) < MIN_SEG_LEN:
            continue
        first_line = block.splitlines()[0].strip()
        if _CHAPTER_LINE.match(first_line) and len(first_line) < 40:
            chapter = first_line
        seq += 1
        segments.append({"seq": seq, "chapter": chapter, "page": None, "content": block})
    return segments


# ----------------------------------------------------------------- pdf ----

def parse_pdf(path: Path) -> list[dict]:
    try:
        import fitz  # pymupdf
    except ImportError as e:
        raise RuntimeError("解析 PDF 需要 pymupdf：pip install pymupdf") from e
    segments: list[dict] = []
    seq = 0
    with fitz.open(path) as doc:
        for page_no, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            if len(text) < MIN_SEG_LEN:
                continue
            seq += 1
            segments.append({"seq": seq, "chapter": None, "page": page_no, "content": text})
    return segments


# ------------------------------------------------------------- dispatch ----

PARSERS = {".epub": parse_epub, ".txt": parse_txt, ".pdf": parse_pdf}


def parse_file(path: Path) -> list[dict]:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"暂不支持的格式：{path.suffix}（支持 epub / txt / pdf）")
    return parser(path)
