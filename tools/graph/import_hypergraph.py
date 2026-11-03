#!/usr/bin/env python3
"""超图入库（统一入口）：entities/<stem>.json → entities + 论文实体 + 超边 + 向量。

两种模式：
  单篇    python tools/graph/import_hypergraph.py <stem> --paper-id 86
  批量    python tools/graph/import_hypergraph.py --batch [--limit N]
          自动挑出「有 json、库里还没有实体」的论文；批量模式整批只备份一次 DB
          （单篇模式每篇备份一次，逐篇备份会占用大量磁盘）

安全特性：
  - 只 INSERT，不做全量重建（删库重建已无脚本承担）
  - 单篇模式幂等：paper_id 已有实体则跳过；加 --update 覆盖重导入（先删旧实体/超边）
  - 入库前自动备份 papers.db，批量模式可用 --no-backup 关闭

写入逻辑的唯一实现在 modules/database/hyper_write.py。
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.database.schema import backup_db  # noqa: E402
from modules.database.schema import get_db, init_db, DB_PATH           # noqa: E402
from modules.database import hyper_write as hw                          # noqa: E402
from modules.ingest_status import set_stage                             # noqa: E402
from modules.paths import entities_dir                                  # noqa: E402


def sync_status(conn, pid: int, entities: int | None = None,
                hyperedges: int | None = None) -> None:
    """把超图导入结果同步进 ingest_status（失败不阻断）。

    此前该脚本从不写状态表 → 实体/超边已落库但 entities_done 仍为 0，
    前端面板据此显示「未完成」、批量模式又反复重导同一篇。
    """
    try:
        import json as _json
        ec = entities
        if ec is None:
            ec = conn.execute("SELECT COUNT(*) FROM entities WHERE paper_id=?",
                              (pid,)).fetchone()[0]
        hc = hyperedges
        if hc is None:
            hc = 0
            for (sp,) in conn.execute("SELECT source_papers FROM hyperedges"):
                try:
                    if pid in _json.loads(sp or "[]"):
                        hc += 1
                except (ValueError, TypeError):
                    pass
        set_stage(pid, conn=conn, entities_done=ec > 0, entity_count=ec,
                  hyperedges_done=hc > 0, hyperedge_count=hc)
    except Exception as e:  # noqa: BLE001
        print(f"[status] ingest_status 写入失败（不阻断）: {e}")



def auto_merge(paper_ids: list[int]):
    """入库后自动触发增量实体合并（失败不阻断入库）。"""
    try:
        from modules.hypergraph.merge.merge import run as merge_run
        print(f"\n[auto-merge] 增量实体合并（{len(paper_ids)} 篇）...")
        merge_run(incremental_paper_ids=paper_ids, db_path=DB_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-merge] 失败（不阻断入库）: {e}")


def stem_of(file_path: str) -> str:
    base = str(file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    if base.endswith("_raw"):
        base = base[:-4]
    return base


def run_single(args) -> int:
    stem, pid = args.stem, args.paper_id
    backup_db(f"hyper_{stem}")
    try:
        items, hes = hw.load_hyper_json(stem)
    except FileNotFoundError as e:
        print(f"[ERR] {e}")
        return 1

    conn = get_db()
    init_db(conn)
    existing = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE paper_id=?", (pid,)).fetchone()[0]
    if existing > 0:
        if args.update:
            n = hw.delete_paper_rows(conn, pid)
            print(f"[update] 已删除 paper {pid} 的旧实体与 {n} 条超边，准备重新插入")
        else:
            print(f"[skip] paper {pid} 已有 {existing} 实体，跳过插入（幂等）。"
                  f"加 --update 可覆盖重导入。")
            sync_status(conn, pid, entities=existing)
            conn.close()
            if not args.no_merge:
                auto_merge([pid])
            return 0

    stat = hw.insert_one(conn, pid, stem, items, hes)
    print(f"[import] 实体 {stat['entities']} 个（含论文实体）; 超边 {stat['hyperedges']} 条")
    v = hw.vectorize_paper(conn, pid)
    sync_status(conn, pid, entities=stat["entities"], hyperedges=stat["hyperedges"])
    conn.close()
    print(f"[embed] 实体 {v['ent_ok']}/{v['ent_total']}; 超边 {v['he_ok']}/{v['he_total']}")
    print("\n超图入库完成。")
    if not args.no_merge:
        auto_merge([pid])
    return 0


def run_batch(args) -> int:
    if not args.no_backup:
        bak = backup_db("hyperbatch")
        print(f"[backup] {bak} ({os.path.getsize(bak) / 1048576:.0f} MB)"
              if bak else "[backup] 跳过（库还不存在，首次建库）", flush=True)

    conn = get_db()
    init_db(conn)
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT paper_id FROM entities WHERE paper_id IS NOT NULL")}

    todo = []
    for pid, fp in conn.execute("SELECT id, file_path FROM papers"):
        if pid in have:
            continue
        stem = stem_of(fp)
        if (entities_dir() / f"{stem}.json").exists():
            todo.append((pid, stem))
    if args.limit:
        todo = todo[: args.limit]
    print(f"待导入 {len(todo)} 篇", flush=True)

    t0 = time.time()
    ok = skip = err = 0
    tot_e = tot_h = 0
    for i, (pid, stem) in enumerate(todo, 1):
        try:
            if conn.execute("SELECT COUNT(*) FROM entities WHERE paper_id=?",
                            (pid,)).fetchone()[0]:
                skip += 1
                continue
            items, hes = hw.load_hyper_json(stem)
            stat = hw.insert_one(conn, pid, stem, items, hes)
            v = hw.vectorize_paper(conn, pid)
            sync_status(conn, pid, entities=stat["entities"], hyperedges=stat["hyperedges"])
            ok += 1
            tot_e += v["ent_ok"]
            tot_h += v["he_ok"]
            print(f"[{i}/{len(todo)}] ok e={stat['entities']} h={stat['hyperedges']} "
                  f"| {stem[:52]} | {ok}ok/{skip}skip/{err}err | {time.time()-t0:.0f}s",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            err += 1
            conn.rollback()
            print(f"[{i}/{len(todo)}] ERR {type(e).__name__}: {e} | {stem[:50]}", flush=True)

    conn.close()
    print(f"\n=== 完成 ok={ok} skip={skip} err={err} | 向量化 实体{tot_e} 超边{tot_h} "
          f"| 用时 {time.time()-t0:.0f}s ===", flush=True)
    if not args.no_merge and ok:
        auto_merge([p for p, _ in todo])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stem", nargs="?", help="论文 STEM 名（单篇模式）")
    ap.add_argument("--paper-id", type=int, help="该论文在 papers 表中的 id（单篇模式）")
    ap.add_argument("--batch", action="store_true", help="批量模式：自动挑未入库的论文")
    ap.add_argument("--limit", type=int, default=0, help="批量模式下的篇数上限")
    ap.add_argument("--update", action="store_true", help="单篇模式：已有实体则先删再导入")
    ap.add_argument("--no-merge", action="store_true", help="入库后不自动触发增量实体合并")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if args.batch:
        return run_batch(args)
    if not args.stem or args.paper_id is None:
        ap.error("单篇模式需要 <stem> 与 --paper-id；批量模式请加 --batch")
    return run_single(args)


if __name__ == "__main__":
    sys.exit(main())
