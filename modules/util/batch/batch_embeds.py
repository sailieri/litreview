"""
批量计算 embedding：chunks + entities_merged + cross_doc_hyperedges
调用 config.embedding 配置的 OpenAI 兼容 /api/embed 端点。
"""
import sys, os, time, struct, httpx, sqlite3

# ── 配置（统一走 modules/config.py，不直接读 yaml；数据路径走 modules/paths）──
from modules.config import section as _cfg_section
from modules.paths import db_path

DB_PATH = db_path()
embed_cfg = _cfg_section("embedding")

EMBED_URL = str(embed_cfg.get("base_url") or "").rstrip("/") + "/api/embed"
EMBED_MODEL = embed_cfg.get("model", "")
EMBED_API_KEY = embed_cfg.get("api_key", "")
BATCH_SIZE = 32  # 单次批量上限

def compute_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """批量计算 embedding。返回与 texts 同长度的向量列表。"""
    if not texts:
        return []
    
    all_vecs = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        try:
            with httpx.Client(timeout=60, trust_env=False) as cl:   # 禁系统代理
                resp = cl.post(
                    EMBED_URL,
                    json={"model": EMBED_MODEL, "input": batch},
                    headers={"Authorization": f"Bearer {EMBED_API_KEY}"},
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings", [])
                all_vecs.extend(embeddings)
        except Exception as e:
            print(f"  [ERROR] batch {i//BATCH_SIZE}: {e}", file=sys.stderr)
            # 失败用 None 占位
            all_vecs.extend([None] * len(batch))
        
        # 小延迟，避免单端点过载
        time.sleep(0.1)
    
    return all_vecs

def vector_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)

def run():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    
    # ── 1. chunks ──
    rows = conn.execute("SELECT id, content FROM chunks WHERE vector IS NULL").fetchall()
    if rows:
        print(f"[1/3] Chunks: {len(rows)} 条待计算...")
        ids = [r["id"] for r in rows]
        texts = [r["content"] for r in rows]
        vecs = compute_embeddings_batch(texts)
        
        updated = 0
        for cid, vec in zip(ids, vecs):
            if vec:
                conn.execute("UPDATE chunks SET vector=? WHERE id=?", 
                           (vector_to_blob(vec), cid))
                updated += 1
        conn.commit()
        print(f"  → 完成 {updated}/{len(ids)}")
    else:
        print("[1/3] Chunks: 全部已计算")
    
    # ── 2. entities_merged ──（实体合并管线未跑过的库无此表，跳过不阻断）
    try:
        rows = conn.execute(
            "SELECT id, name, description FROM entities_merged WHERE vector IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
        print("[2/3] Entities_merged: 表不存在（实体合并管线未跑过），跳过")
    if rows:
        print(f"[2/3] Entities_merged: {len(rows)} 条待计算...")
        ids = [r["id"] for r in rows]
        texts = [(r["name"] + " " + r["description"]).strip() for r in rows]
        vecs = compute_embeddings_batch(texts)
        
        updated = 0
        for eid, vec in zip(ids, vecs):
            if vec:
                conn.execute("UPDATE entities_merged SET vector=? WHERE id=?", 
                           (vector_to_blob(vec), eid))
                updated += 1
        conn.commit()
        print(f"  → 完成 {updated}/{len(ids)}")
    else:
        print("[2/3] Entities_merged: 全部已计算")
    
    # ── 3. cross_doc_hyperedges ──
    rows = conn.execute(
        "SELECT id, title, description FROM cross_doc_hyperedges WHERE vector IS NULL"
    ).fetchall()
    if rows:
        print(f"[3/3] Cross-doc hyperedges: {len(rows)} 条待计算...")
        ids = [r["id"] for r in rows]
        texts = [(r["title"] + " " + r["description"]).strip() for r in rows]
        vecs = compute_embeddings_batch(texts)
        
        updated = 0
        for hid, vec in zip(ids, vecs):
            if vec:
                conn.execute("UPDATE cross_doc_hyperedges SET vector=? WHERE id=?", 
                           (vector_to_blob(vec), hid))
                updated += 1
        conn.commit()
        print(f"  → 完成 {updated}/{len(ids)}")
    else:
        print("[3/3] Cross-doc hyperedges: 全部已计算")
    
    conn.close()
    print("\n全部完成！")

# 命令行入口见 pipelines/build/embeds.py
