"""ingest_status 表的统一读写入口。

每篇论文的入库阶段都记在这张表里；入库脚本必须在每个阶段结束时写入，
否则前端「入库状态」与实际数据不一致。禁止各脚本直接 UPDATE，统一走 set_stage()。

表结构：
    paper_id · extracted · normalized · metadata_done · summarized · indexed
    · entities_done · entity_count · hyperedges_done · hyperedge_count
    · entities_mapped · updated_at（字符串 'YYYY-MM-DD HH:MM:SS'）

约定：
  - 所有阶段写入一律走 set_stage()，禁止各脚本直接 UPDATE（避免各写各的）。
  - 连接用 schema.get_db()（可写）；litread server 的只读连接不受影响。
"""
import sqlite3
import time

from modules.database.schema import get_db

# 8 个布尔阶段（元组顺序即管线顺序，前端进度条按此展示）
STAGES = (
    "extracted",        # PDF → data/extracted/<stem>.md
    "normalized",       # → data/normalized/<stem>.md
    "metadata_done",    # 元数据 title/authors/year/journal/doi
    "summarized",       # 摘要
    "indexed",          # papers + sections + chunks + chunks_fts
    "entities_done",    # 实体提取
    "hyperedges_done",  # 超边提取
    "entities_mapped",  # 实体映射到公共库
)
COUNT_COLS = ("entity_count", "hyperedge_count")
_ALL_COLS = set(STAGES) | set(COUNT_COLS)

STAGE_LABELS = {
    "extracted": "PDF 解析",
    "normalized": "文本规范化",
    "metadata_done": "元数据抽取",
    "summarized": "摘要生成",
    "indexed": "索引入库",
    "entities_done": "实体提取",
    "hyperedges_done": "超边提取",
    "entities_mapped": "实体映射",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_row(paper_id: int, conn: sqlite3.Connection) -> None:
    """确保状态行存在（不存在则建全 0 行）。"""
    conn.execute(
        "INSERT OR IGNORE INTO ingest_status (paper_id, updated_at) VALUES (?, ?)",
        (paper_id, _now()),
    )


def set_stage(paper_id: int, conn: sqlite3.Connection | None = None, **kwargs) -> None:
    """更新阶段/计数列并刷新 updated_at。

    例：
        set_stage(42, extracted=True)
        set_stage(42, entity_count=37, entities_done=True)
    """
    bad = set(kwargs) - _ALL_COLS
    if bad:
        raise ValueError(f"未知状态列: {sorted(bad)}；可用: {sorted(_ALL_COLS)}")
    if not kwargs:
        return
    own = conn is None
    c = conn or get_db()
    try:
        ensure_row(paper_id, c)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = [int(v) if isinstance(v, bool) else v for v in kwargs.values()]
        c.execute(
            f"UPDATE ingest_status SET {sets}, updated_at=? WHERE paper_id=?",
            (*vals, _now(), paper_id),
        )
        c.commit()
    finally:
        if own:
            c.close()


def get_status(paper_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    """单篇状态；无记录返回 None。"""
    own = conn is None
    c = conn or get_db()
    try:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM ingest_status WHERE paper_id=?", (paper_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            c.close()


def list_recent(limit: int = 20, conn: sqlite3.Connection | None = None) -> list[dict]:
    """最近更新的状态行（前端进度列表用）。"""
    own = conn is None
    c = conn or get_db()
    try:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM ingest_status ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


def progress_of(status: dict) -> dict:
    """把一行状态翻译成前端可直接渲染的进度结构。"""
    done = [s for s in STAGES if status.get(s)]
    nxt = next((s for s in STAGES if not status.get(s)), None)
    return {
        "stages": [
            {"key": s, "label": STAGE_LABELS[s], "done": bool(status.get(s))}
            for s in STAGES
        ],
        "done_count": len(done),
        "total": len(STAGES),
        "percent": round(len(done) / len(STAGES) * 100, 1),
        "next": nxt,
        "entity_count": status.get("entity_count") or 0,
        "hyperedge_count": status.get("hyperedge_count") or 0,
        "updated_at": status.get("updated_at"),
    }
