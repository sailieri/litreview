#!/usr/bin/env python3
"""实体 ID 双域体检（只读）：看两列是否都齐、公有 id 有没有悬空。"""
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.database.schema import get_db        # noqa: E402
from modules.database import id_domain as dom     # noqa: E402


def main() -> int:
    conn = get_db()
    stats = dom.stats(conn)
    bad = 0
    print("=== 超边表双域覆盖（私有 entities.id / 公有 entities_merged.id）===")
    for table, s in stats.items():
        if "error" in s:
            print(f"  {table:22s} {s['error']}")
            bad += 1
            continue
        miss = s["rows"] - s["has_valid_public"]
        print(f"  {table:22s} 行 {s['rows']:6d} | 有私有 {s['has_private']:6d} | "
              f"有有效公有 {s['has_valid_public']:6d} | 悬空 id {s['dangling_ids']}")
        if miss or s["dangling_ids"]:
            bad += 1

    h = dom.identity_health(conn)
    print("\n=== 公有实体身份（并查集）===")
    if "error" in h:
        print(f"  {h['error']}")
        bad += 1
    else:
        print(f"  行数 {h['rows']} | 状态 {h['status']}")
        print(f"  链断裂/多根 {h['cycles_or_multiroot']} | canonical 指向不存在 {h['canonical_dangling']}")
        if h["cycles_or_multiroot"] or h["canonical_dangling"]:
            bad += 1

    print("\n=== 建议 ===")
    if bad:
        print("  用 `python tools/maintain/sync_id_domains.py` 看计划，"
              "`--apply` 重算公有列（合并后应自动重算，这里用于补跑）")
    else:
        print("  两列一致，无需处理。")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
