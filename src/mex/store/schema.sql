-- meX 数据库结构（v5）
-- 由 src/mex/store/connection.py 的 init_db() 加载执行
-- 全部语句幂等：重复执行不报错（CREATE ... IF NOT EXISTS）
--
-- v5 起：memories 加 expires_at 列（TTL 自动遗忘：惰性过滤 + 可选 gc）。
-- 查询在 forgotten_at IS NULL 之外追加 (expires_at IS NULL OR expires_at > now) 过滤。
-- v4 起：移除 layer 列。画像由 schema.yaml 的槽位声明定义（投影模型）：
-- sub_topic 非空的条目是画像槽位（唯一性/覆盖语义由 schema 的 unique 声明 +
-- 写入路径按声明决定 insert/update），sub_topic 为空的条目是画像外记录（按时间索引）。

CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,
    topic            TEXT,
    sub_topic        TEXT,
    content          TEXT NOT NULL,
    is_ai_inferred   INTEGER NOT NULL CHECK (is_ai_inferred IN (0, 1)),
    confidence       TEXT NOT NULL CHECK (confidence IN
                         ('confirmed', 'explicit', 'inferred', 'speculated', 'uncertain')),
    evidence         TEXT,
    embedding        BLOB,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    forgotten_at     TEXT,
    forgotten_reason TEXT,
    expires_at       TEXT
);

-- 全文检索索引（FTS5 + trigram 分词，中文按字切分）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='rowid', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    event       TEXT NOT NULL CHECK (event IN ('add', 'update', 'forget', 'restore', 'approve', 'delete')),
    old_content TEXT,
    new_content TEXT,
    actor       TEXT NOT NULL CHECK (actor IN ('user', 'ai')),
    evidence    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                TEXT PRIMARY KEY,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_state (
    source_file       TEXT PRIMARY KEY,
    last_position     INTEGER NOT NULL,
    last_extracted_at TEXT NOT NULL
);
