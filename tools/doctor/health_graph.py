#!/usr/bin/env python3
"""超图质量检查：规模分布、重复实体、极短名、空描述。"""
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.paths import db_path  # noqa: E402

c = sqlite3.connect(str(db_path()))

he = {}
for (src,) in c.execute("SELECT source_papers FROM hyperedges"):
    try:
        ids = json.loads(src or "[]")
    except Exception:
        continue
    for p in ids:
        he[p] = he.get(p, 0) + 1

pid_list = [r[0] for r in c.execute("SELECT id FROM papers")]
vals = [he.get(p, 0) for p in pid_list]
print("papers 实际 id 数:", len(pid_list), "最大 id:", max(pid_list))
print("超边数/篇  min", min(vals), "median", statistics.median(vals),
      "max", max(vals), "mean", round(statistics.mean(vals), 1))
print("超边<5 的论文:", len([v for v in vals if v < 5]))

sizes = []
for (parts,) in c.execute("SELECT participants FROM hyperedges"):
    try:
        sizes.append(len(json.loads(parts or "[]")))
    except Exception:
        pass
print("超边参与者数 min", min(sizes), "median", statistics.median(sizes),
      "max", max(sizes))
print("参与者<3 的超边:", len([s for s in sizes if s < 3]), "/", len(sizes))

dup = c.execute(
    "SELECT COUNT(*) FROM (SELECT paper_id,name,COUNT(*) n FROM entities "
    "WHERE type != 'paper' GROUP BY paper_id,name HAVING n>1)"
).fetchone()[0]
print("同篇同名重复实体组:", dup)

n1 = c.execute(
    "SELECT COUNT(*) FROM entities WHERE type != 'paper' "
    "AND LENGTH(TRIM(name)) <= 2").fetchone()[0]
print("极短实体名(<=2字符):", n1)

n2 = c.execute(
    "SELECT COUNT(*) FROM entities WHERE type != 'paper' "
    "AND (description IS NULL OR TRIM(description) = '')").fetchone()[0]
print("无描述私有实体:", n2)

print("\n=== 超边少的论文（<5 条）Top10 ===")
rows = sorted(he.items(), key=lambda x: x[1])[:10]
for pid, n in rows:
    t = c.execute("SELECT title FROM papers WHERE id=?", (pid,)).fetchone()
    print(f"  [{pid}] {n} 条超边 | {(t[0] if t else '?')[:50]}")

print("\n=== 同篇同名重复 Top5 ===")
for r in c.execute(
        "SELECT paper_id, name, COUNT(*) n FROM entities WHERE type!='paper' "
        "GROUP BY paper_id,name HAVING n>1 ORDER BY n DESC LIMIT 5"):
    print("  ", r)

print("\n=== 极短实体名样例 ===")
for r in c.execute(
        "SELECT paper_id,name,type FROM entities WHERE type!='paper' "
        "AND LENGTH(TRIM(name))<=2 LIMIT 8"):
    print("  ", r)
