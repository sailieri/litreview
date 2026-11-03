"""实体 ID 双域同步：给两张超边表补齐 / 重算 participants_merged。

什么时候跑：
  - 老库第一次升级到"双域"结构后（回填历史行）；
  - 实体合并之后（`merge.py` 会自动跑，这里用于手工补跑或核对）；
  - 怀疑公有 id 悬空（`entities_merged` 被重建过）时。

用法：
  python tools/maintain/sync_id_domains.py            # 只看计划（dry-run，默认）
  python tools/maintain/sync_id_domains.py --apply    # 真写库
  python tools/maintain/sync_id_domains.py --apply --table hyperedges
"""
import argparse
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.database.schema import backup_db, get_db, init_db        # noqa: E402
from modules.database import id_domain as dom                          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="同步超边的私有/公有实体 ID 两列")
    ap.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
    ap.add_argument("--table", default="", choices=["", *dom.EDGE_TABLES],
                    help="只处理某张表；缺省两张都处理")
    ap.add_argument("--no-backup", action="store_true", help="跳过写库前的整库备份")
    args = ap.parse_args()
    dry = not args.apply

    conn = init_db(get_db())            # 顺手补齐可能的缺列
    tables = [args.table] if args.table else list(dom.EDGE_TABLES)

    if args.apply and not args.no_backup:
        print(f"[backup] {backup_db('id_domains')}")

    print(f"模式: {'dry-run（加 --apply 才写）' if dry else 'APPLY'}\n")
    # 先搬历史"公有 id 存在 participants 列"的行（只针对 cross_doc）
    mv = dom.migrate_merged_domain_rows(conn, dry=dry)
    if mv.get("moved"):
        print(f"[migrate] 历史公有域行搬迁 {mv['moved']} 行"
              f"{'（dry）' if dry else ''}\n")

    total = 0
    for table in tables:
        res = dom.refresh_table(conn, table, dry=dry)
        dang = res.get("dangling") or []
        print(f"[{table}] 行数 {res.get('rows', 0)}，待更新 {res.get('updated', 0)}，"
              f"悬空公有 id {len(dang)}")
        if dang:
            print(f"  悬空样例: {dang[:8]}")
        total += res.get("updated", 0)

    print(f"\n合计待更新 {total} 行" + ("（dry-run，未写入）" if dry else "（已写入）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
