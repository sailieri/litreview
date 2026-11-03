"""实体 ID 双域工具：私有 `entities.id` ↔ 公有 `entities_merged.id`。

背景：两张表各自自增，同一数字指两个不同实体（`entities.id=14652` 是 wheat，
`entities_merged.id=14652` 是 RayExtract）。所以超边必须同时记住两套 id：

    participants         私有 entities.id      —— 原始锚点，抽取时就知道，合并重跑也不变
    participants_merged  公有 entities_merged.id —— 检索/语义用，合并后归一

规则（唯一入口，别在别处自己映射）：
  - 写入：先落私有 id，再经 `to_merged()` 填公有列；
  - 合并（`merge.py`）跑完后调 `refresh_table()` 重算公有列；
  - 读取：优先用公有列，若为空或有悬空（公有实体被重建）再退回私有列 + `_o2m` 现算；
  - 对外展示一律带前缀：`e:12`（私有）/ `m:3`（公有），避免搬运时把两套 id 弄混。

`participants_merged` 是**派生列**，可以随时由私有列重算，丢了不心疼。
"""
from __future__ import annotations

import json
import sqlite3

# 允许带前缀的对外格式，也允许裸 int（内部用）
PRIVATE_PREFIX = "e"
MERGED_PREFIX = "m"

# 需要维护双域的超边表
EDGE_TABLES = ("hyperedges", "cross_doc_hyperedges")


def parse_ids(raw) -> list[int]:
    """把 JSON 文本 / 列表解析成去重保序的 int 列表（忽略坏值）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return False
    return col in cols


def canonical_map(conn: sqlite3.Connection) -> dict[int, int]:
    """merged_id → 根 merged_id（沿 canonical_id 传递闭包，防链过长/成环）。"""
    rows = {}
    try:
        for rid, canon in conn.execute(
                "SELECT id, canonical_id FROM entities_merged"):
            rows[rid] = canon
    except sqlite3.Error:
        return {}
    out: dict[int, int] = {}
    for start in rows:
        cur_i, seen = start, set()
        while True:
            nxt = rows.get(cur_i)
            if not nxt or nxt in seen:
                break
            seen.add(cur_i)
            cur_i = nxt
        out[start] = cur_i
    return out


def resolve(conn: sqlite3.Connection, merged_id: int) -> int:
    """把任意一代的公有 id 解析到当前根（并查集 find）。"""
    return canonical_map(conn).get(merged_id, merged_id)


def to_merged(conn: sqlite3.Connection, ids: list[int],
              canon: dict | None = None) -> list[int]:
    """私有 id → **根**公有 id（经 `_o2m` 再沿 canonical 链归一到根）。

    canon 传 `canonical_map(conn)` 可避免批量场景下重复全表扫描。
    """
    ids = parse_ids(ids)
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT source_id, merged_id FROM _o2m WHERE source_id IN ({ph})", ids
        ).fetchall()
    except sqlite3.Error:
        return []
    m = {r[0]: r[1] for r in rows}
    if canon is None:
        canon = canonical_map(conn)
    out: list[int] = []
    for i in ids:
        v = m.get(i)
        if v is None:
            continue
        v = canon.get(v, v)
        if v not in out:
            out.append(v)
    return out


def to_private(conn: sqlite3.Connection, ids: list[int]) -> list[int]:
    """公有 id → 一组私有 id（`_o2m` 反查，取该合并组的所有成员）。"""
    ids = parse_ids(ids)
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT source_id FROM _o2m WHERE merged_id IN ({ph}) ORDER BY merged_id, source_id",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[int] = []
    for r in rows:
        v = r[0]
        if v not in out:
            out.append(v)
    return out


def dangling(conn: sqlite3.Connection, table: str) -> list[int]:
    """该表 `participants_merged` 里指向已不存在公有实体的 id。"""
    if not _has_col(conn, table, "participants_merged"):
        return []
    alive = {r[0] for r in conn.execute("SELECT id FROM entities_merged")}
    bad: list[int] = []
    for (raw,) in conn.execute(f"SELECT participants_merged FROM {table}"):
        for i in parse_ids(raw):
            if i not in alive and i not in bad:
                bad.append(i)
    return bad


def refresh_table(conn: sqlite3.Connection, table: str, dry: bool = False,
                  batch: int = 500) -> dict:
    """由 `participants` 重算 `participants_merged`，只写有变化的行。

    返回 {"rows", "updated", "dangling"}。
    """
    if not _has_col(conn, table, "participants_merged"):
        return {"rows": 0, "updated": 0, "dangling": [], "error": "列不存在"}
    rows = conn.execute(
        f"SELECT id, participants, participants_merged FROM {table}").fetchall()
    canon = canonical_map(conn)          # 只算一次，别在循环里全表扫描
    updated = 0
    for rid, parts, cur in rows:
        want = to_merged(conn, parse_ids(parts), canon=canon)
        if parse_ids(cur) == want:
            continue
        updated += 1
        if not dry:
            conn.execute(
                f"UPDATE {table} SET participants_merged=? WHERE id=?",
                (json.dumps(want, ensure_ascii=False), rid))
    if not dry:
        conn.commit()
    return {"rows": len(rows), "updated": updated,
            "dangling": dangling(conn, table)}


def migrate_merged_domain_rows(conn: sqlite3.Connection, dry: bool = True) -> dict:
    """把历史上"公有 id 存在 participants 列"的行搬进新列。

    识别：`participants_from='entities_merged'`（cross_doc 历史行）。
    搬完后 participants 改存私有 id（由 `_o2m` 反查），保持"两列各有明确语义"。
    """
    out = {"moved": 0, "rows": 0}
    if not _has_col(conn, "cross_doc_hyperedges", "participants_from"):
        return out
    rows = conn.execute(
        "SELECT id, participants FROM cross_doc_hyperedges "
        "WHERE participants_from='entities_merged'").fetchall()
    out["rows"] = len(rows)
    for rid, parts in rows:
        merged_ids = parse_ids(parts)
        priv = to_private(conn, merged_ids)
        out["moved"] += 1
        if dry:
            continue
        conn.execute(
            "UPDATE cross_doc_hyperedges SET participants=?, participants_merged=?, "
            "participants_from='entities' WHERE id=?",
            (json.dumps(priv, ensure_ascii=False),
             json.dumps(merged_ids, ensure_ascii=False), rid))
    if not dry:
        conn.commit()
    return out


def identity_health(conn: sqlite3.Connection) -> dict:
    """公有实体身份健康度：状态分布 / 多根 / 环 / 悬空 canonical。"""
    try:
        rows = conn.execute(
            "SELECT id, canonical_id, COALESCE(status,'active') FROM entities_merged"
        ).fetchall()
    except sqlite3.Error as e:
        return {"error": str(e)}
    ids = {r[0] for r in rows}
    status = {}
    for r in rows:
        status[r[2]] = status.get(r[2], 0) + 1
    canon = canonical_map(conn)
    # 环：某 id 的根不等于自己，但走不到头（canonical_map 遇环会停在环内某点）
    cycles = [r[0] for r in rows
              if r[1] and canon.get(r[0], r[0]) == r[0]]
    orphans = [r[0] for r in rows if r[1] and r[1] not in ids]
    return {"rows": len(rows), "status": status,
            "cycles_or_multiroot": len(cycles),
            "canonical_dangling": len(orphans)}


def stats(conn: sqlite3.Connection) -> dict:
    """双域覆盖率概览（doctor / 排查用）。"""
    res = {}
    alive = {r[0] for r in conn.execute("SELECT id FROM entities_merged")}
    for table in EDGE_TABLES:
        if not _has_col(conn, table, "participants_merged"):
            res[table] = {"error": "缺 participants_merged 列"}
            continue
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        with_priv = with_pub = 0
        for parts, merged in conn.execute(
                f"SELECT participants, participants_merged FROM {table}"):
            if parse_ids(parts):
                with_priv += 1
            mids = parse_ids(merged)
            if mids and all(m in alive for m in mids):
                with_pub += 1
        res[table] = {"rows": total, "has_private": with_priv,
                      "has_valid_public": with_pub,
                      "dangling_ids": len(dangling(conn, table))}
    return res


def fmt(ids, domain: str) -> list[str]:
    """对外展示：私有加 e: 前缀，公有加 m: 前缀。"""
    p = PRIVATE_PREFIX if domain in ("entities", "private", "e") else MERGED_PREFIX
    return [f"{p}:{i}" for i in parse_ids(ids)]
