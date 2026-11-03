# -*- coding: utf-8 -*-
"""论文重复行合并（把重复 papers 行并入保留行，然后删掉重复行及其派生数据）。

场景：同一篇论文被两条入库路径各建一行（同标题重复行）时，把重复行并入保留行，
再删掉重复行及其派生数据。

策略（保守，优先不丢数据）：
  - 重复行的超边若只由本篇实体构成 → 连同实体一起删（重复行自成一组，删掉不影响他篇）
  - 若超边含他篇实体 → 保留超边，只把 source_papers 里的重复 id 换成保留 id
  - chunks / sections / papers 行 / ingest_status → 删除（重复内容）
  - papers_fts / entities_fts 等外部内容 FTS 由触发器自动同步；chunks_fts 是独立表需手动删

用法：
  python tools/maintain/orphans.py dedupe --dry        # 只看计划
  python tools/maintain/orphans.py dedupe --apply      # 执行（自动备份 papers.db）
"""
import argparse
import json
import shutil
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
from modules.paths import db_path  # noqa: E402

def normalize_title(s: str) -> str:
    """标题归一：小写、去首尾与连续空白、去尾部标点。用于判定“同标题重复行”。"""
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(" .,;:，。；：")


def find_pairs(conn) -> list[tuple[int, int]]:
    """按归一化标题自动找重复行，返回 [(重复行 id, 保留行 id)]，保留行取 id 最小者。

    只对“归一后标题完全相同”的行下手，属于保守判据；标题改写/中译版不会命中。
    """
    groups: dict[str, list[int]] = {}
    for pid, title in conn.execute(
            "SELECT id, title FROM papers WHERE deleted_at IS NULL ORDER BY id"):
        key = normalize_title(title)
        if key:
            groups.setdefault(key, []).append(pid)
    pairs = []
    for ids in groups.values():
        if len(ids) > 1:
            keeper = min(ids)
            pairs += [(d, keeper) for d in ids if d != keeper]
    return pairs


def parse_pairs(spec: str) -> list[tuple[int, int]]:
    """解析 --pairs "502:236,530:243"（重复 id:保留 id）。"""
    out = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        dup, _, keeper = item.partition(":")
        out.append((int(dup), int(keeper)))
    return out


def plan(conn, dup, keeper):
    p = {"dup": dup, "keeper": keeper}
    p["hyperedges"] = [r[0] for r in conn.execute(
        "SELECT id FROM hyperedges WHERE source_papers LIKE ?", (f"%{dup}%",))]
    p["entities"] = [r[0] for r in conn.execute(
        "SELECT id FROM entities WHERE paper_id=?", (dup,))]
    p["chunks"] = [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE paper_id=?", (dup,))]
    p["sections"] = [r[0] for r in conn.execute(
        "SELECT id FROM sections WHERE paper_id=?", (dup,))]
    ent_owner = {r[0]: r[1] for r in conn.execute("SELECT id,paper_id FROM entities")}
    keep_he, drop_he = [], []
    for hid in p["hyperedges"]:
        parts = json.loads(conn.execute(
            "SELECT participants FROM hyperedges WHERE id=?", (hid,)).fetchone()[0] or "[]")
        others = [e for e in parts if ent_owner.get(e) not in (None, dup)]
        (keep_he if others else drop_he).append(hid)
    p["he_keep"] = keep_he
    p["he_drop"] = drop_he
    # entities_merged / _o2m 里对这些实体的引用
    p["merged_refs"] = []
    if p["entities"]:
        ph = ",".join("?" * len(p["entities"]))
        p["merged_refs"] = [r[0] for r in conn.execute(
            f"SELECT source_id FROM _o2m WHERE source_id IN ({ph})", p["entities"])]
        p["merged_ids"] = [r[0] for r in conn.execute(
            f"SELECT id FROM entities_merged WHERE id IN ({ph})", p["entities"])]
    return p


def apply(conn, p):
    dup, keeper = p["dup"], p["keeper"]
    # 1. 涉及他篇实体的超边：把 source_papers 里的 dup 换成 keeper
    for hid in p["he_keep"]:
        row = conn.execute("SELECT source_papers FROM hyperedges WHERE id=?", (hid,)).fetchone()
        if not row:
            continue
        try:
            ids = json.loads(row[0] or "[]")
        except Exception:
            ids = []
        ids = [keeper if i == dup else i for i in ids]
        ids = sorted(set(ids))
        conn.execute("UPDATE hyperedges SET source_papers=? WHERE id=?",
                     (json.dumps(ids, ensure_ascii=False), hid))
    # 2. 删除自成一组的超边
    for hid in p["he_drop"]:
        conn.execute("DELETE FROM hyperedges WHERE id=?", (hid,))
    # 3. 删除重复行的实体（同时清 _o2m 映射，避免悬空）
    for eid in p["entities"]:
        conn.execute("DELETE FROM _o2m WHERE source_id=?", (eid,))
        conn.execute("DELETE FROM entities WHERE id=?", (eid,))
    # 4. chunks（chunks_fts 独立表需手动删）
    for cid in p["chunks"]:
        conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE id=?", (cid,))
    # 5. sections
    for sid in p["sections"]:
        conn.execute("DELETE FROM sections WHERE id=?", (sid,))
    # 6. papers 行 + ingest_status
    conn.execute("DELETE FROM ingest_status WHERE paper_id=?", (dup,))
    conn.execute("DELETE FROM papers WHERE id=?", (dup,))
    conn.commit()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--pairs", default="",
                    help='手动指定重复对，形如 "502:236,530:243"（重复 id:保留 id）；'
                         '缺省则按归一化标题自动检测')
    args = ap.parse_args()

    db = Path(db_path())
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    pairs = parse_pairs(args.pairs) or find_pairs(conn)
    if not pairs:
        print("未发现同标题重复行（可用 --pairs 手动指定）")
        return
    print(f"待处理的重复行 {len(pairs)} 组")
    plans = []
    for dup, keeper in pairs:
        t = conn.execute("SELECT title FROM papers WHERE id=?", (dup,)).fetchone()
        if not t:
            print(f"[skip] id={dup} 不存在")
            continue
        k = conn.execute("SELECT title FROM papers WHERE id=?", (keeper,)).fetchone()
        p = plan(conn, dup, keeper)
        plans.append(p)
        print(f"=== 重复行 {dup} → 并入 {keeper} ===")
        print(f"    标题: {t[0][:70]}")
        print(f"    保留行标题: {k[0][:70] if k else '(不存在!)'}")
        print(f"    超边: 共 {len(p['hyperedges'])} → 删除 {len(p['he_drop'])} / 改写 {len(p['he_keep'])}")
        print(f"    实体 {len(p['entities'])}  chunks {len(p['chunks'])}  sections {len(p['sections'])}")
        if p.get("merged_refs"):
            print(f"    ⚠ _o2m 映射引用了其中 {len(p['merged_refs'])} 个实体（需一并清理）")
        if p.get("merged_ids"):
            print(f"    ⚠ 其中 {len(p['merged_ids'])} 个是 entities_merged 的合并主实体")
    if args.apply:
        bak = db.with_suffix(db.suffix + f".bak_pre_dedupe_{int(time.time())}")
        shutil.copy2(str(db), str(bak))
        print(f"\n[backup] {bak}")
        for p in plans:
            apply(conn, p)
            print(f"[done] id={p['dup']} 已并入 {p['keeper']}")
    else:
        print("\n(dry-run，加 --apply 执行)")


if __name__ == "__main__":
    main()
