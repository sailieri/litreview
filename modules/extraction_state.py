"""超图提取进度追踪（轻量、仅依赖 sqlite，不触发 database 包的 yaml 依赖）。

在 papers 表维护三列，作为"检查管理日志 / 断点续跑"的依据：
  entity_count   — 初始提取的实体数（0=尚未成功提取）
  hyperedge_count— 初始提取的超边数（0=尚未成功提取）
  ac_rounds      — A-C 增强已完成轮数（每成功一轮 +1）

设计要点（提取分批执行、各轮之间无强依赖，支持断点续跑）：
  - 初始：三列全 0
  - 初始提取成功：entity_count / hyperedge_count 置为实际数，ac_rounds 仍 0
  - 每成功一轮 A-C：ac_rounds +1
  - 智能体据此判断哪些论文需要补提取 / 补 A-C
"""
import sqlite3
import os
from pathlib import Path

from modules.paths import db_dir, db_path


def get_db() -> sqlite3.Connection:
    p = db_path()
    os.makedirs(p.parent, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")   # WAL 的 -shm 文件在挂载盘/跨文件系统上不可用，用 DELETE 模式
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_extraction_tracking(conn: sqlite3.Connection) -> None:
    """给 papers 表补加提取进度列（幂等，兼容存量库）。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    targets = {
        "entity_count": "INTEGER DEFAULT 0",
        "hyperedge_count": "INTEGER DEFAULT 0",
        "ac_rounds": "INTEGER DEFAULT 0",
    }
    for col, ddl in targets.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def paper_id_for_stem(conn: sqlite3.Connection, stem: str) -> int | None:
    """用 normalized md 的 stem 反查 papers.id（papers.file_path 存的就是 normalized md 路径）。

    路径分隔符无关匹配：库里 file_path 可能存成正斜杠形式，而 Windows 进程登记时
    写成反斜杠——POSIX 下 `Path("E:\\..\\x.md").stem` 返回整串而非文件名，
    提取进度就无法按 stem 回写。这里统一把反斜杠换成斜杠再取 stem，两种写法都能命中。
    """
    for r in conn.execute("SELECT id, file_path FROM papers").fetchall():
        fp = (r["file_path"] or "").replace("\\", "/")
        base = fp.rsplit("/", 1)[-1]
        for suf in (".md", ".pdf"):     # file_path 可能是 PDF 路径（ingest 写入 .pdf）
            base = base.removesuffix(suf)
        if base == stem:
            return r["id"]
    return None


def record_extraction(conn: sqlite3.Connection, paper_id: int,
                      n_entities: int, n_hyperedges: int) -> None:
    """持久化初始提取计数（实体 / 超边）。"""
    conn.execute(
        "UPDATE papers SET entity_count=?, hyperedge_count=? WHERE id=?",
        (n_entities, n_hyperedges, paper_id))
    conn.commit()


def bump_ac_rounds(conn: sqlite3.Connection, paper_id: int, n: int = 1) -> None:
    """A-C 增强每成功一轮 +1（默认 +1）。ac_rounds 为累计轮数，供兼容计数使用。"""
    if n <= 0:
        return
    conn.execute(
        "UPDATE papers SET ac_rounds = ac_rounds + ? WHERE id=?",
        (n, paper_id))
    conn.commit()


def mark_ac_enhanced(conn: sqlite3.Connection, paper_id: int,
                     rounds: int = 0) -> None:
    """标记论文已完成 A-C 增强。

    ac_enhanced: 0=未增强, 1=已增强（实际轮数写入 ac_rounds_v4 以便追溯）。
    """
    conn.execute(
        "UPDATE papers SET ac_enhanced=1, ac_rounds_v4=COALESCE(ac_rounds_v4,0)+? "
        "WHERE id=?", (rounds, paper_id))
    conn.commit()


def extraction_status(conn: sqlite3.Connection, target_ac: int = 2) -> list[dict]:
    """返回每篇论文的提取进度 + 待补跑项。

    判定：entity_count 或 hyperedge_count 为 0 → NEED_EXTRACT；
          ac_rounds < target_ac → NEED_AC；否则 OK。
    """
    rows = conn.execute(
        "SELECT id, title, entity_count, hyperedge_count, ac_rounds "
        "FROM papers ORDER BY id").fetchall()
    out = []
    for r in rows:
        ent = r["entity_count"] or 0
        he = r["hyperedge_count"] or 0
        ac = r["ac_rounds"] or 0
        needs_extract = (ent == 0) or (he == 0)
        needs_ac = ac < target_ac
        todo = "NEED_EXTRACT" if needs_extract else ("NEED_AC" if needs_ac else "OK")
        out.append({
            "id": r["id"], "title": r["title"],
            "entity_count": ent, "hyperedge_count": he,
            "ac_rounds": ac, "target_ac": target_ac, "status": todo,
        })
    return out
