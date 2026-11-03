#!/usr/bin/env python3
"""实体合并（统一入口）：把分簇结果交给 LLM 判定，写入 entities_merged。

两种模式：
  --mode incremental （默认）只处理「含未映射私有实体的簇」，追加更新 entities_merged，
                              已有合并行保留，批内标注 [已归入: X] 让 LLM 优先沿用规范名
  --mode full                 对全库私有实体重跑一遍（不 DROP：公有实体 id 永不复用，
                              旧的挂接到更老的根上，status 标 merged_into/superseded）

分簇来源：tools/graph/cluster_pipeline.py 产出的 clusters_final.json（必须晚于新实体入库计算）。

用法：
  python tools/graph/merge_entities.py                      # 增量（日常用这个）
  python tools/graph/merge_entities.py --mode full          # 全量重建
  python tools/graph/merge_entities.py --clusters data/entity_merge/clusters_final.json
合并后记得补向量：python tools/embed/embed_missing.py --table entities_merged
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.paths import db_path                       # noqa: E402
from modules.hypergraph.merge.merge import run           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    ap.add_argument("--clusters", default="data/entity_merge/clusters_final.json",
                    help="分簇结果路径（相对仓库根）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不调 LLM")
    args = ap.parse_args()

    clusters = Path(args.clusters)
    if not clusters.is_absolute():
        clusters = ROOT / clusters
    if not clusters.exists():
        print(f"[ERR] 找不到分簇文件 {clusters}，请先跑 tools/graph/cluster_pipeline.py")
        return 1

    db = str(db_path())
    print(f"[db] {db}")
    print(f"[clusters] {clusters}  模式 = {args.mode}")
    if args.dry_run:
        print("[dry-run] 到此为止（不备份、不调 LLM）")
        return 0

    if not args.no_backup:
        bak = f"{db}.bak_pre_{args.mode}merge_{int(time.time())}"
        shutil.copy2(db, bak)
        print(f"[backup] {bak}")

    # incremental_paper_ids: None = 全量重建；[] = 增量（只处理未映射簇）
    inc = [] if args.mode == "incremental" else None
    t0 = time.time()
    run(cluster_path=str(clusters), db_path=db, max_workers=args.workers,
        batch_size=args.batch_size, incremental_paper_ids=inc)
    print(f"[done] 用时 {(time.time() - t0) / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
