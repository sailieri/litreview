#!/usr/bin/env python3
"""补算缺失向量（统一入口，两张表）。

--table chunks           遍历 chunks WHERE vector IS NULL
                         批量导入时若未设 LITREVIEW_EMBED_URL，ingest 会静默 0 向量
                         （只打印计数、不报错），需事后补。
--table entities_merged  补合并组向量。merge 只写 name/aliases/type/description，**不写 vector**，
                         所以每次实体合并后都要跑一次，否则合并组语义检索失效。

用法：
  python tools/embed/embed_missing.py                          # 默认补 chunks
  python tools/embed/embed_missing.py --table entities_merged  # 合并后补合并组
  python tools/embed/embed_missing.py --table entities_merged --dry
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.database.schema import get_db                          # noqa: E402
from modules.database.embedder import compute_embedding, vector_to_blob  # noqa: E402


def fill_chunks(conn, args, t0):
    q = "SELECT id, content FROM chunks WHERE vector IS NULL ORDER BY id"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = conn.execute(q).fetchall()
    total = len(rows)
    print(f"待补向量 {total} 个 chunk", flush=True)
    if not total:
        return 0, 0
    ok = fail = 0
    for i, (cid, content) in enumerate(rows, 1):
        vec = compute_embedding(content)
        if vec:
            conn.execute("UPDATE chunks SET vector=? WHERE id=?", (vector_to_blob(vec), cid))
            ok += 1
        else:
            fail += 1
        if i % args.commit_every == 0:
            conn.commit()
            print(f"[{i}/{total}] ok={ok} fail={fail} | {time.time()-t0:.0f}s", flush=True)
    conn.commit()
    return ok, fail


def fill_entities_merged(conn, args, t0):
    q = ("SELECT id, name, type, description FROM entities_merged "
         "WHERE vector IS NULL ORDER BY id")
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = conn.execute(q).fetchall()
    print(f"[plan] 待补 {len(rows)} 条 entities_merged 向量")
    if args.dry or not rows:
        return 0, 0
    ok = fail = 0
    for i, (mid, name, typ, desc) in enumerate(rows, 1):
        text = f"{name} ({typ}): {desc}"[:500] if typ else f"{name}: {desc}"[:500]
        vec = compute_embedding(text)
        if vec:
            conn.execute("UPDATE entities_merged SET vector=? WHERE id=?",
                         (vector_to_blob(vec), mid))
            ok += 1
        else:
            fail += 1
        if i % args.commit_every == 0:
            conn.commit()
            print(f"  {i}/{len(rows)} ok={ok} fail={fail} {time.time()-t0:.0f}s", flush=True)
    conn.commit()
    return ok, fail


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=["chunks", "entities_merged"], default="chunks")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit-every", type=int, default=50)
    ap.add_argument("--dry", action="store_true", help="只统计待补数量，不写库")
    args = ap.parse_args()

    conn = get_db()
    t0 = time.time()
    fn = fill_chunks if args.table == "chunks" else fill_entities_merged
    ok, fail = fn(conn, args, t0)
    left = conn.execute(
        f"SELECT COUNT(*) FROM {args.table} WHERE vector IS NULL").fetchone()[0]
    print(f"\n=== 完成 table={args.table} ok={ok} fail={fail} 剩余缺向量={left} "
          f"用时 {time.time()-t0:.0f}s ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
