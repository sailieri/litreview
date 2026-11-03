"""超图提取：检查管理日志（断点续跑用）。

读取 papers 表中 entity_count / hyperedge_count / ac_rounds 三列，
报告每篇论文的提取与 A-C 增强进度，并标出需要补跑的论文。

判定：
  - entity_count 或 hyperedge_count 为 0        → NEED_EXTRACT（需重新提取）
  - ac_rounds < --target-ac                     → NEED_AC（需补 A-C 轮）
  - 其余                                          → OK

可选回填（适用于已有 data/entities/*.json 但 papers 表无计数的库）：
  --backfill           从 json 回填 entity_count / hyperedge_count
  --mark-ac-done       同时把"有 json"的论文 ac_rounds 置为 --target-ac
                       （这些论文的提取与 A-C 增强在同一个流程内完成，视为已增强到位）

用法：
  python tools/doctor/doctor.py status
  python tools/doctor/doctor.py status --target-ac 2
  python tools/doctor/doctor.py status --backfill --mark-ac-done
"""
import sys, json, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.paths import entities_dir  # noqa: E402
from modules.extraction_state import (
    get_db, migrate_extraction_tracking,
    paper_id_for_stem, record_extraction, extraction_status,
)


def main():
    ap = argparse.ArgumentParser(description="超图提取检查管理日志")
    ap.add_argument("--target-ac", type=int, default=2, help="A-C 增强目标轮数")
    ap.add_argument("--backfill", action="store_true",
                    help="从 data/entities/*.json 回填 entity_count / hyperedge_count")
    ap.add_argument("--mark-ac-done", action="store_true",
                    help="把有 json 的论文 ac_rounds 置为 --target-ac（仅回填时有效）")
    args = ap.parse_args()

    conn = get_db()
    migrate_extraction_tracking(conn)

    if args.backfill:
        ent_dir = entities_dir()
        n_fill = 0
        for fp in sorted(ent_dir.glob("*.json")):
            stem = fp.stem
            pid = paper_id_for_stem(conn, stem)
            if pid is None:
                print(f"  [backfill] 跳过（DB 无匹配论文）: {stem[:50]}")
                continue
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            n_ent = len(d.get("items", []))
            n_he = len(d.get("hyperedges", []))
            record_extraction(conn, pid, n_ent, n_he)
            if args.mark_ac_done:
                conn.execute(
                    "UPDATE papers SET ac_rounds=? WHERE id=? AND ac_rounds < ?",
                    (args.target_ac, pid, args.target_ac))
                conn.commit()
            n_fill += 1
        print(f"  [backfill] 已回填 {n_fill} 篇")

    rows = extraction_status(conn, target_ac=args.target_ac)
    need_extract = [r for r in rows if r["status"] == "NEED_EXTRACT"]
    need_ac = [r for r in rows if r["status"] == "NEED_AC"]
    ok = [r for r in rows if r["status"] == "OK"]

    print(f"\n=== 超图提取进度（target_ac={args.target_ac}）===")
    print(f"OK={len(ok)}  NEED_EXTRACT={len(need_extract)}  "
          f"NEED_AC={len(need_ac)}  / 共 {len(rows)}")

    if need_extract:
        print("\n-- 需重新提取（实体或超边为 0）--")
        for r in need_extract:
            print(f"  [{r['id']:>3}] {r['title'][:55]}")
    if need_ac:
        print("\n-- 需补 A-C 轮（ac_rounds < target）--")
        for r in need_ac:
            print(f"  [{r['id']:>3}] ac={r['ac_rounds']}/{args.target_ac}  "
                  f"实体{r['entity_count']} 超边{r['hyperedge_count']}  {r['title'][:40]}")

    if not need_extract and not need_ac:
        print("\n✅ 全部论文提取完成且 A-C 达标，无需补跑。")

    conn.close()


if __name__ == "__main__":
    main()
