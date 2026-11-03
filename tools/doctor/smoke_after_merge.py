#!/usr/bin/env python3
"""合并后冒烟：实体解析 + 向量检索 + 混合检索。"""
import sys
from pathlib import Path

import sqlite3  # noqa: E402

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.paths import db_path  # noqa: E402

c = sqlite3.connect(str(db_path()))
c.row_factory = sqlite3.Row

print("=== 表规模 ===")
for t in ("papers", "chunks", "entities", "entities_merged", "_m2o", "_o2m"):
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:18s} {n}")
    except Exception as e:  # noqa: BLE001
        print(f"  {t:18s} ERR {e}")

print("\n=== 向量完整性 ===")
for t, col in (("chunks", "vector"), ("entities_merged", "vector")):
    tot = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    ok = c.execute(
        f"SELECT COUNT(*) FROM {t} WHERE {col} IS NOT NULL").fetchone()[0]
    print(f"  {t:18s} {ok}/{tot}")

print("\n=== 实体解析：私有实体 → 共有实体 ===")
for kw in ("mIoU", "RMSE", "R-square", "AGB", "Random Forest"):
    row = c.execute(
        "SELECT id,name,rtrim(source_ids,'[]') FROM entities_merged "
        "WHERE is_merged=1 AND aliases LIKE ? LIMIT 1", (f"%{kw}%",)).fetchone()
    if row:
        print(f"  {kw:14s} -> [{row['id']}] {row['name']}")

print("\n=== _o2m 映射抽查 ===")
rows = c.execute("SELECT * FROM _o2m LIMIT 3").fetchall()
print(f"  样例: {[dict(r) for r in rows]}")

print("\n=== 检索冒烟（向量 + FTS） ===")
q = "aboveground biomass estimation using GEDI lidar"
try:
    from modules.database.retriever import search   # noqa: E402
    res = search(q)
    print(f"  混合检索 OK：chunks {len(res.get('chunks', []))} 条，"
          f"超边 {len(res.get('hyperedges', []))} 条")
except Exception as e:  # noqa: BLE001
    print(f"  混合检索失败（检查 LLM/embedding 端点配置）：{type(e).__name__}: {e}")

rows = c.execute(
    "SELECT p.title FROM chunks c JOIN papers p ON p.id=c.paper_id "
    "WHERE c.content LIKE ? LIMIT 3", (f"%biomass%",)).fetchall()
print(f"  FTS/内容命中 {len(rows)} 条，例: "
      f"{[r[0][:50] for r in rows]}")
