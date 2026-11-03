"""
Database schema.

Tables:
  papers      — Paper metadata (title, authors, year, etc.)
  sections    — Tree-structured sections (heading level, parent)
  chunks      — Text chunks with embedding vectors
  papers_fts  — FTS5 index on paper metadata
  sections_fts — FTS5 index on section content
"""

import sqlite3
import os
from pathlib import Path

from modules.extraction_state import migrate_extraction_tracking
from modules.paths import db_dir as _db_dir, db_path as _db_path

# DB_DIR / DB_PATH 是模块级常量（导入时求值），其他模块可直接引用。
# 需要运行期切换路径时用 get_db()，它每次动态向 modules.paths 取值。
DB_DIR = str(_db_dir())
DB_PATH = str(_db_path())

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    authors     TEXT DEFAULT '',
    year        INTEGER DEFAULT NULL,
    journal     TEXT DEFAULT '',
    doi         TEXT DEFAULT '',
    file_path   TEXT DEFAULT '',
    favorite    INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    entity_count   INTEGER DEFAULT 0,   -- 初始提取的实体数（0=尚未成功提取）
    hyperedge_count INTEGER DEFAULT 0,  -- 初始提取的超边数（0=尚未成功提取）
    ac_rounds      INTEGER DEFAULT 0    -- A-C 增强已完成轮数（每成功一轮 +1）
);

-- 入库进度（8 阶段）：每列是一个阶段的完成标记或计数。
-- 前端「入库状态」面板以本表为数据源；表不存在时面板显示「暂无记录」。
CREATE TABLE IF NOT EXISTS ingest_status (
        paper_id          INTEGER PRIMARY KEY,
        extracted         INTEGER NOT NULL DEFAULT 0,
        normalized        INTEGER NOT NULL DEFAULT 0,
        metadata_done     INTEGER NOT NULL DEFAULT 0,
        summarized        INTEGER NOT NULL DEFAULT 0,
        indexed           INTEGER NOT NULL DEFAULT 0,
        entities_done     INTEGER NOT NULL DEFAULT 0,
        entity_count      INTEGER NOT NULL DEFAULT 0,
        hyperedges_done   INTEGER NOT NULL DEFAULT 0,
        hyperedge_count   INTEGER NOT NULL DEFAULT 0,
        entities_mapped   INTEGER NOT NULL DEFAULT 0,
        updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (paper_id) REFERENCES papers(id)
    );

CREATE TABLE IF NOT EXISTS sections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id      INTEGER NOT NULL,
    title         TEXT NOT NULL,
    level         INTEGER NOT NULL,
    parent_id     INTEGER DEFAULT NULL,
    content       TEXT NOT NULL,
    chunk_index   INTEGER DEFAULT 0,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES sections(id) ON DELETE SET NULL
);

-- Full-text search index on papers
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    title, authors, content='papers', content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, authors)
    VALUES (new.id, new.title, new.authors);
END;

CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors)
    VALUES ('delete', old.id, old.title, old.authors);
END;

CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
    INSERT INTO papers_fts(papers_fts, rowid, title, authors)
    VALUES ('delete', old.id, old.title, old.authors);
    INSERT INTO papers_fts(rowid, title, authors)
    VALUES (new.id, new.title, new.authors);
END;

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    title, content, content='sections', content_rowid='rowid'
);

-- Chunks for embedding-based search
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id  INTEGER NOT NULL,
    paper_id    INTEGER NOT NULL,
    content     TEXT NOT NULL,
    chunk_index INTEGER DEFAULT 0,
    vector      BLOB DEFAULT NULL,
    FOREIGN KEY (section_id) REFERENCES sections(id) ON DELETE CASCADE,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_id);
CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id);

-- Chunk full-text index (inline content mode, populated explicitly by ingest)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content
);

-- Entities for hypergraph-enhanced retrieval
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT DEFAULT '',
    description TEXT DEFAULT '',
    paper_id    INTEGER DEFAULT NULL,
    vector      BLOB DEFAULT NULL,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE SET NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, type, description, content='entities', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, type, description)
    VALUES (new.id, new.name, new.type, new.description);
END;
CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, type, description)
    VALUES ('delete', old.id, old.name, old.type, old.description);
END;

-- Hyperedges for per-paper and cross-document relationships
CREATE TABLE IF NOT EXISTS hyperedges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    participants    TEXT NOT NULL DEFAULT '[]',  -- 私有 entities.id（原始锚点）
    participants_merged TEXT NOT NULL DEFAULT '[]',  -- 公有 entities_merged.id（派生，检索用）
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '[]',
    strength        TEXT DEFAULT 'moderate',
    source_papers   TEXT DEFAULT '[]',           -- JSON list of paper IDs
    vector          BLOB DEFAULT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS hyperedges_fts USING fts5(
    description, keywords, content='hyperedges', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS hyperedges_ai AFTER INSERT ON hyperedges BEGIN
    INSERT INTO hyperedges_fts(rowid, description, keywords)
    VALUES (new.id, new.description, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS hyperedges_ad AFTER DELETE ON hyperedges BEGIN
    INSERT INTO hyperedges_fts(hyperedges_fts, rowid, description, keywords)
    VALUES ('delete', old.id, old.description, old.keywords);
END;

-- Cross-document hyperedges (built AFTER entity merge, participants reference
-- merged entity IDs in entities_merged; complements the per-paper `hyperedges` table)
CREATE TABLE IF NOT EXISTS cross_doc_hyperedges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT DEFAULT '',
    participants    TEXT NOT NULL DEFAULT '[]',  -- 私有 entities.id（原始锚点）
    participants_merged TEXT NOT NULL DEFAULT '[]',  -- 公有 entities_merged.id（派生，检索用）
    description     TEXT DEFAULT '',
    keywords        TEXT DEFAULT '[]',
    strength        TEXT DEFAULT 'moderate',
    source_papers   TEXT DEFAULT '[]',           -- JSON list of paper IDs
    vector          BLOB DEFAULT NULL,
    qa_msg_id       INTEGER DEFAULT NULL,        -- 问答来源溯源（NULL=管线提取）
    participants_from TEXT DEFAULT 'entities_merged'  -- id 域：entities_merged | entities（QA 衍生边）
);

CREATE VIRTUAL TABLE IF NOT EXISTS cross_doc_hyperedges_fts USING fts5(
    title, description, keywords, content='cross_doc_hyperedges', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS cross_doc_hyperedges_ai AFTER INSERT ON cross_doc_hyperedges BEGIN
    INSERT INTO cross_doc_hyperedges_fts(rowid, title, description, keywords)
    VALUES (new.id, new.title, new.description, new.keywords);
END;
CREATE TRIGGER IF NOT EXISTS cross_doc_hyperedges_ad AFTER DELETE ON cross_doc_hyperedges BEGIN
    INSERT INTO cross_doc_hyperedges_fts(cross_doc_hyperedges_fts, rowid, title, description, keywords)
    VALUES ('delete', old.id, old.title, old.description, old.keywords);
END;

-- ===== 多收藏夹 =====
-- collections：收藏夹；is_default=1 的是默认夹（夹本身不可删除，可改名）
-- collection_papers：夹↔论文 多对多，同一篇论文可同时存在于多个夹（主键去重）
-- papers.favorite 只作「是否在任意夹中」的粗标记保留，归属关系以 collection_papers 为准
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collection_papers (
    collection_id INTEGER NOT NULL,
    paper_id      INTEGER NOT NULL,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection_id, paper_id),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cp_paper ON collection_papers(paper_id);
CREATE INDEX IF NOT EXISTS idx_cp_coll ON collection_papers(collection_id);
"""

DEFAULT_COLLECTION_NAME = "默认收藏夹"


def migrate_collections(conn: sqlite3.Connection) -> None:
    """建立默认收藏夹，并把 papers.favorite=1 的论文纳入该夹（幂等）。

    幂等性来自「只在默认夹不存在时执行」：建完即止，不做二次同步，
    所以用户从默认夹移除的论文不会因 favorite 标记在重启时被重新加回。
    """
    row = conn.execute("SELECT id FROM collections WHERE is_default = 1 LIMIT 1").fetchone()
    if row is not None:
        return
    clash = conn.execute("SELECT id FROM collections WHERE name = ?",
                         (DEFAULT_COLLECTION_NAME,)).fetchone()
    if clash is not None:            # 用户手工建过同名夹 → 改名避让，保证默认夹独立
        conn.execute("UPDATE collections SET name = ? WHERE id = ?",
                     (DEFAULT_COLLECTION_NAME + "（旧）", clash[0]))
    conn.execute("INSERT INTO collections(name, is_default) VALUES (?, 1)",
                 (DEFAULT_COLLECTION_NAME,))
    cid = conn.execute("SELECT id FROM collections WHERE is_default = 1 LIMIT 1").fetchone()[0]
    n = conn.execute(
        "INSERT OR IGNORE INTO collection_papers(collection_id, paper_id) "
        "SELECT ?, id FROM papers WHERE COALESCE(favorite, 0) = 1", (cid,)).rowcount
    print(f"[collections] 已创建默认收藏夹 id={cid}，迁入旧收藏 {n} 篇")


def get_db() -> sqlite3.Connection:
    """Get a connection to the papers database (creates if not exists)."""
    db_file = _db_path()
    os.makedirs(db_file.parent, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")   # WAL 的 -shm 文件在挂载盘/跨文件系统上不可用，用 DELETE 模式
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def backup_db(tag: str, db_file=None) -> str:
    """写库前整库备份：<db>.bak_pre_<tag>_<epoch>。

    返回备份路径；库还不存在（首次入库）时返回空串，调用方据此跳过。
    """
    import shutil
    import time as _time
    src = Path(db_file or _db_path())
    if not src.exists():
        return ""                       # 干净环境第一次建库，没有东西可备份
    src.parent.mkdir(parents=True, exist_ok=True)
    bak = f"{src}.bak_pre_{tag}_{int(_time.time())}"
    shutil.copy2(src, bak)
    return bak


def migrate_id_domains(conn: sqlite3.Connection) -> None:
    """给两张超边表补 participants_merged 列（老库升级用，可重复执行）。"""
    for table in ("hyperedges", "cross_doc_hyperedges"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if cols and "participants_merged" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN participants_merged "
                f"TEXT NOT NULL DEFAULT '[]'")
    conn.commit()


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Initialize the database schema."""
    if conn is None:
        conn = get_db()
    conn.executescript(SCHEMA_SQL)
    migrate_id_domains(conn)
    migrate_extraction_tracking(conn)
    migrate_collections(conn)
    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db()
    print(f"Database initialized at {DB_PATH}")
    conn.close()
