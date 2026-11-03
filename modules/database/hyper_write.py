# -*- coding: utf-8 -*-
"""超图写入核心：实体 / 论文实体 / 超边 的插入、删除与向量化。

单篇导入与批量导入共用这里的插入逻辑，保证两边的写入行为一致。

注意：本模块只做增量写入，不做全量重建（重建会删库，已无脚本承担，改库请手工迁移）；
这里只做增量 INSERT，可重复调用。
"""
import json
import sqlite3

from modules.database.embedder import compute_embedding, vector_to_blob
from modules.database.id_domain import to_merged
from modules.paths import entities_dir, metadata_dir, normalized_dir


def read_title(stem: str) -> str:
    """论文标题：优先 normalized 的首行 `# `，否则用 stem。"""
    np_ = normalized_dir() / f"{stem}.md"
    if np_.exists():
        first = np_.read_text(encoding="utf-8", errors="replace").splitlines()
        if first and first[0].strip().startswith("# "):
            return first[0].strip()[2:].strip()
    return stem


def load_hyper_json(stem: str) -> tuple[list, list]:
    """读 entities/<stem>.json，返回 (items, hyperedges)。"""
    p = entities_dir() / f"{stem}.json"
    if not p.exists():
        raise FileNotFoundError(f"找不到 {p}，请先跑 tools/extract/extract_one.py")
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("items", []), data.get("hyperedges", [])


def paper_entity_description(stem: str, title: str) -> str:
    """论文实体描述：优先 metadata 的 title/year/journal。"""
    desc = f"Paper: {title}"
    mp = metadata_dir() / f"{stem}.json"
    if mp.exists():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            desc = f"{m.get('title', '')} ({m.get('year', '')}) {m.get('journal', '')}"
        except Exception:  # noqa: BLE001
            pass
    return desc[:500]


def delete_paper_rows(conn: sqlite3.Connection, paper_id: int) -> int:
    """删除某论文的全部实体与超边（覆盖更新用）。返回删除的超边数。"""
    he_ids = []
    for hid, sp in conn.execute("SELECT id, source_papers FROM hyperedges").fetchall():
        try:
            if paper_id in json.loads(sp or "[]"):
                he_ids.append(hid)
        except Exception:  # noqa: BLE001
            pass
    if he_ids:
        q = "DELETE FROM hyperedges WHERE id IN ({})".format(",".join("?" * len(he_ids)))
        conn.execute(q, he_ids)
    conn.execute("DELETE FROM entities WHERE paper_id=?", (paper_id,))
    # 顺手清掉指向已删实体的反向映射（否则 _o2m 会越积越多孤儿行）
    try:
        conn.execute(
            "DELETE FROM _o2m WHERE source_id NOT IN (SELECT id FROM entities)")
    except Exception:  # noqa: BLE001  _o2m 可能尚未建立
        pass
    conn.commit()
    return len(he_ids)


def insert_one(conn: sqlite3.Connection, paper_id: int, stem: str,
               items: list, hyperedges: list) -> dict:
    """把一篇论文的实体 + 论文实体 + 超边插入库中（调用方负责先提交/后向量化）。"""
    title = read_title(stem)
    name_to_id: dict[str, int] = {}
    n_ent = 0
    for item in items:
        ename = (item.get("name") or "").strip()
        if not ename:
            continue
        conn.execute(
            "INSERT INTO entities (name, type, description, paper_id) VALUES (?,?,?,?)",
            (ename, item.get("type", ""), item.get("description", ""), paper_id))
        name_to_id[ename.lower()] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        n_ent += 1

    conn.execute(
        "INSERT INTO entities (name, type, description, paper_id) VALUES (?, 'paper', ?, ?)",
        (title, paper_entity_description(stem, title), paper_id))
    paper_eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    name_to_id[title.lower()] = paper_eid

    n_he = 0
    for he in hyperedges:
        parts = he.get("participants", [])
        if not parts:
            continue
        resolved = [paper_eid]
        for p in parts:
            if isinstance(p, int):
                resolved.append(p)
                continue
            pl = (p or "").strip().lower()
            eid = name_to_id.get(pl)
            if eid is None:
                conn.execute(
                    "INSERT INTO entities (name, type, description, paper_id) VALUES (?,?,?,?)",
                    (p, "concept", "", paper_id))
                eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                name_to_id[pl] = eid
            resolved.append(eid)
        conn.execute(
            "INSERT INTO hyperedges "
            "(participants, participants_merged, description, strength, source_papers) "
            "VALUES (?,?,?,?,?)",
            (json.dumps(resolved, ensure_ascii=False),
             json.dumps(to_merged(conn, resolved), ensure_ascii=False),
             he.get("description", ""),
             "strong", json.dumps([paper_id], ensure_ascii=False)))
        n_he += 1
    conn.commit()
    return {"entities": n_ent + 1, "hyperedges": n_he}


def vectorize_paper(conn: sqlite3.Connection, paper_id: int) -> dict:
    """补该论文新插入实体的向量与相关超边向量。"""
    rows = conn.execute(
        "SELECT id, name, type, description FROM entities "
        "WHERE paper_id=? AND vector IS NULL", (paper_id,)).fetchall()
    e_ok = 0
    for eid, name, typ, desc in rows:
        text = f"{name} ({typ}): {desc}"[:500] if typ else f"{name}: {desc}"[:500]
        vec = compute_embedding(text)
        if vec:
            conn.execute("UPDATE entities SET vector=? WHERE id=?",
                         (vector_to_blob(vec), eid))
            e_ok += 1

    hrows = conn.execute(
        "SELECT id, description FROM hyperedges WHERE source_papers LIKE ? AND vector IS NULL",
        (f"%{paper_id}%",)).fetchall()
    h_ok = 0
    for hid, desc in hrows:
        vec = compute_embedding(desc or "")
        if vec:
            conn.execute("UPDATE hyperedges SET vector=? WHERE id=?",
                         (vector_to_blob(vec), hid))
            h_ok += 1
    conn.commit()
    return {"ent_ok": e_ok, "ent_total": len(rows), "he_ok": h_ok, "he_total": len(hrows)}
