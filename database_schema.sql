CREATE TABLE works (
    id          INTEGER PRIMARY KEY,
    title_zh    TEXT NOT NULL,           -- 中文书名（主标题，对应 Notion "书名"）
    title_ja    TEXT,                    -- 日文版
    title_en    TEXT,                    -- 英文版
    year        TEXT,                    -- 原版年份（保留文本，如 "1994（前两部）-1995（第三部）"）
    genres      TEXT DEFAULT '[]',       -- 文体，JSON 数组
    keywords    TEXT DEFAULT '[]',       -- 关键词，JSON 数组
    status      TEXT,                    -- 阅读状态
    notes       TEXT                     -- 备注
);
CREATE TABLE editions (
    id          INTEGER PRIMARY KEY,
    work_id     INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    language    TEXT NOT NULL CHECK (language IN ('zh','ja','en')),
    format      TEXT NOT NULL,           -- epub / txt / pdf ...
    filename    TEXT,                    -- data/files/ 下的文件名
    has_pages   INTEGER DEFAULT 0,       -- PDF 是否带可引用页码
    notes       TEXT,
    indexed_at  TEXT                     -- 完成全文索引的时间；NULL = 未索引
);
CREATE TABLE segments (
    id          INTEGER PRIMARY KEY,
    edition_id  INTEGER NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,        -- 版本内顺序
    chapter     TEXT,                    -- 章节标题（epub 的 spine 条目 / txt 的推断章节）
    page        INTEGER,                 -- PDF 页码（其他格式为 NULL）
    content     TEXT NOT NULL            -- 原文
);
CREATE INDEX idx_segments_edition ON segments(edition_id, seq);
CREATE VIRTUAL TABLE segments_fts USING fts5(
    body,
    segment_id UNINDEXED,
    tokenize = 'unicode61'
)
/* segments_fts(body,segment_id) */;
CREATE TABLE IF NOT EXISTS 'segments_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'segments_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'segments_fts_content'(id INTEGER PRIMARY KEY, c0, c1);
CREATE TABLE IF NOT EXISTS 'segments_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'segments_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
