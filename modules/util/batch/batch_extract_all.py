"""顺序批量提取所有论文的超图（带断点续跑 + DB 进度追踪）"""
import json, sys, time, os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent          # modules/util/batch
ROOT = PROJECT.parent.parent.parent                # litreview/ 根
sys.path.insert(0, str(ROOT))                      # 使 modules.* 可导入

from modules.hypergraph.summary import run_pipeline
from modules.hypergraph.refine import run_actor_critic_loop
from modules.util.llm_client import load_config, get_step_config
from modules.extraction_state import (
    get_db, migrate_extraction_tracking,
    paper_id_for_stem, record_extraction, bump_ac_rounds,
)
from concurrent.futures import ThreadPoolExecutor, as_completed

NORM_DIR = ROOT / "data" / "normalized"
ENTITIES_DIR = ROOT / "data" / "entities"
SUMMARIES_DIR = ROOT / "data" / "summaries"
META_DIR = ROOT / "data" / "metadata"

TARGET_AC = 2        # A-C 增强目标轮数（每成功一轮在 papers.ac_rounds +1）
BATCH_SIZE = 4       # 并行数


def _load_result(ent_path: Path) -> dict | None:
    """载入已存在的提取结果（供 A-C 续跑）。"""
    try:
        with open(ent_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def process_one_paper(md_path, conn=None, target_ac: int = TARGET_AC):
    """处理单篇论文：提取 + A-C。自带断点续跑。

    依据 DB 中 papers 表的 entity_count / hyperedge_count / ac_rounds 决定步骤，
    而非仅靠 ent_path 是否存在 —— 这样 A-C 中途崩溃不会被误判为"已完成"，
    下一轮只补 A-C、不必重新提取。
    """
    stem = Path(md_path).stem
    ent_path = ENTITIES_DIR / f"{stem}.json"
    sum_path = SUMMARIES_DIR / f"{stem}.json"

    own_conn = False
    if conn is None:
        conn = get_db()
        migrate_extraction_tracking(conn)
        own_conn = True

    pid = paper_id_for_stem(conn, stem)
    state = (conn.execute(
        "SELECT entity_count, hyperedge_count, ac_rounds FROM papers WHERE id=?",
        (pid,)).fetchone() if pid else None)
    ent_cnt = state["entity_count"] if state else 0
    he_cnt = state["hyperedge_count"] if state else 0
    ac_cnt = state["ac_rounds"] if state else 0

    extracted = (ent_cnt > 0) and (he_cnt > 0)
    ac_done = ac_cnt >= target_ac

    if extracted and ac_done:
        print(f"  ⏭️  {stem[:50]}: 已完成 (实体{ent_cnt} 超边{he_cnt} A-C{ac_cnt}/{target_ac})")
        if own_conn:
            conn.close()
        return True

    result = None

    # ── Phase 1: 初始提取（尚未成功才做）──
    if not extracted:
        print(f"\n{'='*60}\n[{stem[:60]}]\n{'='*60}")
        t0 = time.time()
        try:
            # 注入元数据（title/authors/year/keywords）提升提取质量
            meta = {}
            meta_path = META_DIR / f"{stem}.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            summary, result = run_pipeline(
                str(md_path), metadata=meta or None,
                max_validation_rounds=3,
                extract_hyperedges=True,
            )
            SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
            with open(sum_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            ENTITIES_DIR.mkdir(parents=True, exist_ok=True)
            with open(ent_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            n_ent = len(result.get("items", []))
            n_he = len(result.get("hyperedges", []))
            print(f"  [提取] {n_ent}实体 {n_he}超边 ({time.time()-t0:.0f}s)")
            if pid is not None:
                record_extraction(conn, pid, n_ent, n_he)
        except Exception as e:
            print(f"  ❌ 提取失败: {e}")
            if own_conn:
                conn.close()
            return False
    else:
        # 已提取，载入结果供 A-C 续跑
        result = _load_result(ent_path)
        if result is None:
            print(f"  ❌ 读取已有提取失败（json 缺失/损坏）: {ent_path}")
            if own_conn:
                conn.close()
            return False

    # ── Phase 2: A-C 增强（补到 target_ac 轮）──
    remaining = target_ac - ac_cnt
    if remaining > 0:
        try:
            with open(md_path, encoding="utf-8") as f:
                paper_text = f.read()
            cfg = load_config()
            ac_cfg = get_step_config("refine", cfg)

            entities, hyperedges, rounds_done = run_actor_critic_loop(
                paper_text=paper_text,
                entities=result.get("items", []),
                hyperedges=result.get("hyperedges", []),
                cfg=ac_cfg,
                max_rounds=remaining,
                paper_stem=stem,
            )
            result["items"] = entities
            result["hyperedges"] = hyperedges
            ENTITIES_DIR.mkdir(parents=True, exist_ok=True)
            with open(ent_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"  [A-C] {len(entities)}实体 {len(hyperedges)}超边 (完成{rounds_done}轮)")
            if pid is not None and rounds_done > 0:
                bump_ac_rounds(conn, pid, rounds_done)
        except Exception as e:
            print(f"  ❌ A-C 失败: {e}（提取结果已保留，ac_rounds 未变，可续跑）")
    else:
        print(f"  ⏭️  A-C 已达标 ({ac_cnt}/{target_ac})")

    if own_conn:
        conn.close()
    return True


def run():
    ENTITIES_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    papers = sorted(NORM_DIR.glob("*.md"))
    print(f"共 {len(papers)} 篇, 逐篇检查 DB 状态并续跑（提取 / A-C）所需步骤")

    if not papers:
        print("无 normalized 论文")
        return

    # 批量处理，每批 BATCH_SIZE 篇并行；续跑逻辑由 process_one_paper 内部按 DB 状态判定
    for batch_start in range(0, len(papers), BATCH_SIZE):
        batch = papers[batch_start:batch_start + BATCH_SIZE]
        print(f"\n--- 批次 {batch_start//BATCH_SIZE + 1}/{(len(papers)-1)//BATCH_SIZE + 1} ({len(batch)}篇) ---")

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futs = {ex.submit(process_one_paper, p): p for p in batch}
            for fut in as_completed(futs):
                fut.result()  # 异常已内部处理

    # 汇总
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    ents = list(ENTITIES_DIR.glob("*.json"))
    total_ent = 0
    total_he = 0
    for fp in ents:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        total_ent += len(d.get("items", []))
        total_he += len(d.get("hyperedges", []))
    print(f"  完成: {len(ents)}/{len(papers)} 篇")
    print(f"  总实体: {total_ent}  总超边: {total_he}")
    print(f"  平均: {total_ent/len(papers):.1f}实体/篇, {total_he/len(papers):.1f}超边/篇")


# 命令行入口见 pipelines/build/extract_hypergraph.py
