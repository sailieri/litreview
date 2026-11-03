"""单篇超图提取：对指定 normalized md 跑 process_one_paper（实体+超边+A-C 精炼）。

批量入口是 pipelines/build/extract_hypergraph.py。
复用 modules.util.batch.batch_extract_all.process_one_paper，输出 entities/<stem>.json + summaries/<stem>.json。

用法：
  python tools/extract/extract_one.py data/normalized/My2024_Keyword.md
  python tools/extract/extract_one.py My2024_Keyword   # 自动拼 data/normalized/<stem>.md
"""
import sys, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))

from modules.database.schema import get_db
from modules.paths import entities_dir, normalized_dir
from modules.extraction_state import paper_id_for_stem
from modules.ingest_status import set_stage
from modules.util.batch.batch_extract_all import process_one_paper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="normalized md 路径，或 STEM 名（自动拼 data/normalized/<stem>.md）")
    args = ap.parse_args()

    t = args.target
    p = Path(t)
    if not p.exists() and not p.is_absolute():
        p = normalized_dir() / f"{t}.md"
    if not p.exists():
        print(f"[ERR] 找不到 {p}")
        return
    print(f"[extract] 处理 {p}")
    ok = process_one_paper(str(p))
    print(f"[extract] 完成，返回 {ok}")

    # 写 ingest_status：从产物 json 读实体/超边数回填
    try:
        import json
        stem = p.stem
        ent_json = entities_dir() / f"{stem}.json"
        ent_n = he_n = 0
        if ent_json.exists():
            d = json.loads(ent_json.read_text(encoding="utf-8"))
            ent_n = len(d.get("items") or [])
            he_n = len(d.get("hyperedges") or [])
        conn = get_db()
        pid = paper_id_for_stem(conn, stem)
        if pid:
            set_stage(pid, conn=conn, entities_done=True, entity_count=ent_n,
                      hyperedges_done=True, hyperedge_count=he_n)
            print(f"[status] ingest_status <- paper_id={pid}, 实体 {ent_n}, 超边 {he_n}")
        else:
            print(f"[status] 未找到 stem={stem} 对应的 paper_id，跳过")
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[status] ingest_status 写入失败（不阻断提取）: {e}")


if __name__ == "__main__":
    main()
