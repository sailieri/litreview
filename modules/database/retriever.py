"""
Retriever: FTS5 + embedding 混合检索。
覆盖 chunks 和 hyperedges 两张表，unicode61 分词器，英文词汇完整匹配。
"""
import json, re, math, sys, sqlite3
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.database.schema import get_db
from modules.database.embedder import compute_embedding, blob_to_vector, cosine_similarity


# ═══════════════════════════════════════════════════════════════════════
# Helper: build FTS5 query with hyphen-safe quoted phrases
# ═══════════════════════════════════════════════════════════════════════

_FTS_STOP = set("""a an the of and or for with in on to from by as at into over under between
is are was were be been being this that these those their its it we our can could should would
may might must not no such than then there here which who whom whose what when where why how
all any both each few more most other some only own same so too very
do does did done doing have has had having
study studies paper papers result results show shows shown found find
using use used based however therefore also among within across toward towards via""".split())


def _build_fts_query(text: str) -> str:
    """Wrap hyphenated/special-char terms in double quotes for FTS5; drop stopwords/short terms.

    Hyphenated terms like 'NMC-0.5' must be quoted, otherwise FTS5 parses the
    hyphen as a NOT operator. Stopwords and terms shorter than 3 characters are
    dropped (3-character acronyms such as SSP/RCP/UAV/LCA are kept) and at most
    24 terms are used: a query that ORs every token matches most of the corpus,
    which leaves BM25 with little discriminative power.
    """
    terms = re.findall(r'[A-Za-z0-9][A-Za-z0-9.\-+/]*[A-Za-z0-9]|[A-Za-z0-9]', text)
    if not terms:
        return text
    picked, seen = [], set()
    for t in terms:
        tl = t.lower()
        if len(tl) < 3 or tl in _FTS_STOP or tl in seen:
            continue
        seen.add(tl)
        picked.append(t)
        if len(picked) >= 24:
            break
    if not picked:                       # 全被过滤时的退化：保留原样，避免空查询
        picked = terms[:12]
    # Quote terms containing special FTS5 chars: - . + /
    quoted = [f'"{t}"' if re.search(r'[-.+/]', t) else t for t in picked]
    return " OR ".join(quoted)


# ═══════════════════════════════════════════════════════════════════════
# Batch cosine similarity
# ═══════════════════════════════════════════════════════════════════════

def _batch_cosine(vectors: np.ndarray, q_vec: list[float]) -> np.ndarray:
    q = np.asarray(q_vec, dtype=np.float32)
    qn = np.linalg.norm(q) or 1.0
    norms = np.linalg.norm(vectors, axis=1)
    norms[norms == 0] = 1.0
    return np.dot(vectors, q) / (norms * qn)


def _zscore_fusion(vec_scores: np.ndarray, bm25_scores: np.ndarray,
                   weight: float = 0.5) -> np.ndarray:
    """Z-score normalization + weighted fusion."""
    v_mean, v_std = vec_scores.mean(), vec_scores.std()
    b_mean, b_std = bm25_scores.mean(), bm25_scores.std()
    v_norm = (vec_scores - v_mean) / (v_std or 1.0)
    b_norm = (bm25_scores - b_mean) / (b_std or 1.0)
    return weight * v_norm + (1 - weight) * b_norm


# ═══════════════════════════════════════════════════════════════════════
# Search chunks: FTS5 BM25 + embedding
# ═══════════════════════════════════════════════════════════════════════

def _ranks(scores: np.ndarray) -> np.ndarray:
    """分数 → 名次（1-based）。"""
    order = np.argsort(-scores)
    rk = np.empty(len(scores), dtype=np.int32)
    rk[order] = np.arange(1, len(scores) + 1)
    return rk


RRF_K = 60          # RRF 平滑常数（标准取值）


def search_chunks(query: str, top_k: int = 10, weight: float = 0.5,
                  max_per_paper: int | None = None, pool: int | None = None) -> list[dict]:
    """FTS5 BM25 + embedding hybrid search on chunks（论文级去重）。

    要点：
    1) 融合用 **RRF（名次融合）**：向量相似度与 BM25 的分数尺度不同，z-score 归一
       会把单路排名靠前的结果压到很后面，名次融合不受分数分布影响；
    2) BM25 命中行经 rowid→数组下标的映射定位，不依赖 SELECT 返回顺序；
    3) `max_per_paper`：同一篇论文最多保留几条 chunk；
    4) `pool`：先取大候选池再按论文去重，避免去重后条数不足 top_k。
    """
    conn = get_db()
    q_vec = compute_embedding(query)

    # Load all chunks with vectors
    rows = conn.execute(
        """SELECT c.id, c.content, c.vector, c.paper_id, c.section_id,
                  s.title as section_title, p.title as paper_title
           FROM chunks c
           JOIN sections s ON c.section_id = s.id
           JOIN papers p ON c.paper_id = p.id
           WHERE c.vector IS NOT NULL"""
    ).fetchall()

    N = len(rows)
    if N == 0:
        conn.close()
        return []

    ids = [r["id"] for r in rows]
    idx_of = {cid: i for i, cid in enumerate(ids)}   # rowid → 数组下标

    # Vector scores
    if q_vec:
        vecs_list = []
        for r in rows:
            v = blob_to_vector(r["vector"]) if r["vector"] else np.zeros(1024, dtype=np.float32)
            vecs_list.append(v)
        vecs = np.array(vecs_list, dtype=np.float32)
        vec_scores = _batch_cosine(vecs, q_vec)
    else:
        vec_scores = np.zeros(N, dtype=np.float32)

    # FTS5 BM25 scores
    bm25_arr = np.zeros(N, dtype=np.float32)
    fts_query = _build_fts_query(query)
    try:
        cur = conn.execute(
            "SELECT rowid, bm25(chunks_fts, 1.2, 0.75) AS score FROM chunks_fts WHERE chunks_fts MATCH ?",
            (fts_query,)
        )
        for row in cur:
            idx = idx_of.get(row["rowid"])
            if idx is not None:
                bm25_arr[idx] = -row["score"]  # bm25 returns negative, lower = better
    except Exception:
        pass

    # 融合：RRF（名次融合，对“稀疏 BM25 + 稠密向量”鲁棒，不受分数尺度影响；weight 仅为接口兼容保留）
    fused = 1.0 / (RRF_K + _ranks(vec_scores)) + 1.0 / (RRF_K + _ranks(bm25_arr))

    # 先取大池子，再按论文去重（max_per_paper），最后截到 top_k
    cand = min(int(pool or max(top_k * 8, 80)), N)
    cand_idx = np.argpartition(-fused, cand - 1)[:cand]
    cand_idx = cand_idx[np.argsort(-fused[cand_idx])]

    results, per_paper = [], {}
    for i in cand_idx:
        r = rows[i]
        if max_per_paper:
            pid = r["paper_id"]
            if per_paper.get(pid, 0) >= max_per_paper:
                continue
            per_paper[pid] = per_paper.get(pid, 0) + 1
        results.append({
            "chunk_id": r["id"],
            "paper_id": r["paper_id"],
            "paper_title": r["paper_title"],
            "section_title": r["section_title"],
            "content": r["content"],
            "score": round(float(fused[i]), 6),
            "vec_score": round(float(vec_scores[i]), 4),
            "bm25_score": round(float(bm25_arr[i]), 4),
        })
        if len(results) >= top_k:
            break

    conn.close()
    return results


# ═══════════════════════════════════════════════════════════════════════
# Search hyperedges: FTS5 BM25 + embedding
# ═══════════════════════════════════════════════════════════════════════

def search_chunks_multi(queries: list[str], top_k: int = 12,
                        max_per_paper: int | None = 2, pool: int | None = None) -> list[dict]:
    """多子查询合并检索：每条子查询各算向量/BM25 名次，跨查询 RRF 合并。

    复合问题（同时含多个技术侧面）整句送入 embedder 会被语义稀释，单条向量
    难覆盖各侧面；拆成原子子查询各算排名再合并，命中名次明显前移。
    行与向量只加载一次，逐条子查询复用。
    """
    queries = [q for q in (queries or []) if q and q.strip()]
    if not queries:
        return []
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.content, c.vector, c.paper_id, c.section_id,
                  s.title as section_title, p.title as paper_title
           FROM chunks c
           JOIN sections s ON c.section_id = s.id
           JOIN papers p ON p.id = c.paper_id
           WHERE c.vector IS NOT NULL"""
    ).fetchall()
    N = len(rows)
    if N == 0:
        conn.close()
        return []
    idx_of = {r["id"]: i for i, r in enumerate(rows)}
    vecs = np.array([blob_to_vector(r["vector"]) if r["vector"] else np.zeros(1024, dtype=np.float32)
                     for r in rows], dtype=np.float32)

    fused = np.zeros(N, dtype=np.float32)
    for q in queries:
        v = _batch_cosine(vecs, compute_embedding(q))
        b = np.zeros(N, dtype=np.float32)
        try:
            for row in conn.execute(
                    "SELECT rowid, bm25(chunks_fts,1.2,0.75) AS score FROM chunks_fts "
                    "WHERE chunks_fts MATCH ?", (_build_fts_query(q),)):
                i = idx_of.get(row["rowid"])
                if i is not None:
                    b[i] = -row["score"]
        except Exception:
            pass
        fused += 1.0 / (RRF_K + _ranks(v)) + 1.0 / (RRF_K + _ranks(b))

    cand = min(int(pool or max(top_k * 8, 120)), N)
    cand_idx = np.argpartition(-fused, cand - 1)[:cand]
    cand_idx = cand_idx[np.argsort(-fused[cand_idx])]

    results, per_paper = [], {}
    for i in cand_idx:
        r = rows[i]
        if max_per_paper:
            pid = r["paper_id"]
            if per_paper.get(pid, 0) >= max_per_paper:
                continue
            per_paper[pid] = per_paper.get(pid, 0) + 1
        results.append({
            "chunk_id": r["id"], "paper_id": r["paper_id"],
            "paper_title": r["paper_title"], "section_title": r["section_title"],
            "content": r["content"], "score": round(float(fused[i]), 6),
        })
        if len(results) >= top_k:
            break
    conn.close()
    return results


def gap_check(topic: str, top_papers: int = 25) -> dict:
    """“库内到底有没有关于 X 的文献”——论文级召回 + 命中统计，专治“没搜到当成不存在”。

    与 chunk 级检索的区别：这里按**论文**打分（取该论文全部 chunk 的最优值），因为
    “有没有相关研究”是论文级问题，chunk 级 top-k 会被同篇的 40 个 chunk 稀释。
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.content, c.vector, c.paper_id, s.title as section_title
           FROM chunks c JOIN sections s ON c.section_id = s.id
           WHERE c.vector IS NOT NULL"""
    ).fetchall()
    if not rows:
        conn.close()
        return {"topic": topic, "papers": [], "note": "库内无向量化 chunk"}
    pids = np.array([r["paper_id"] for r in rows])
    vecs = np.array([blob_to_vector(r["vector"]) if r["vector"] else np.zeros(1024, dtype=np.float32)
                     for r in rows], dtype=np.float32)
    v = _batch_cosine(vecs, compute_embedding(topic))

    fts_q = _build_fts_query(topic)
    b = np.zeros(len(rows), dtype=np.float32)
    fts_chunks = 0
    try:
        idx_of = {r["id"]: i for i, r in enumerate(rows)}
        for row in conn.execute("SELECT rowid, bm25(chunks_fts,1.2,0.75) AS s FROM chunks_fts "
                                "WHERE chunks_fts MATCH ?", (fts_q,)):
            i = idx_of.get(row["rowid"])
            if i is not None:
                b[i] = -row["s"]
                fts_chunks += 1
    except Exception:
        pass

    uniq = sorted(set(pids.tolist()))
    pv = np.array([float(v[pids == p].max()) for p in uniq])
    pb = np.array([float(b[pids == p].max()) for p in uniq])
    pfused = 1.0 / (RRF_K + _ranks(pv)) + 1.0 / (RRF_K + _ranks(pb))
    order = np.argsort(-pfused)[:top_papers]

    meta = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id,title,year,journal,doi FROM papers").fetchall()}
    out = []
    for k in order:
        p = uniq[int(k)]
        m = meta.get(p, {})
        mm = np.where(pids == p)[0]
        best = mm[int(np.argmax(v[mm]))]
        out.append({
            "paper_id": p, "title": m.get("title"), "year": m.get("year"),
            "journal": m.get("journal"), "doi": m.get("doi"),
            "vector_rank": int(_ranks(pv)[k]), "bm25_rank": int(_ranks(pb)[k]),
            "best_section": rows[best]["section_title"],
            "snippet": (rows[best]["content"] or "")[:400],
        })
    conn.close()
    return {"topic": topic, "papers_total": len(uniq), "fts_chunk_hits": fts_chunks,
            "fts_query": fts_q[:160], "papers": out,
            "note": "按论文级召回排序；snippet 是该论文最匹配片段（非全文）"}


def _resolve_names(id_list: list, table: str, conn) -> list[str]:
    """将实体 ID 列表解析为名称（table 为 'entities' 或 'entities_merged'）。"""
    ids = [i for i in id_list if isinstance(i, int)]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT id, name FROM {table} WHERE id IN ({ph})", ids
        ).fetchall()
    except Exception:
        return []
    m = {r["id"]: r["name"] for r in rows}
    return [m.get(i, f"#{i}") for i in ids]


def _to_merged(parts: list, conn) -> tuple[list[int], list[str]]:
    """原始 entities.id → 合并后 entities_merged.id（经 _o2m）+ 名称。

    超边参与者统一以【合并后实体】为基准。单篇超边
    (hyperedges) 与问答衍生边的 participants 存的是原始 entities.id
    （中间态），这里统一归一；多个原始 id 可映射到同一 merged id，去重保序。
    无映射的（极端情况）退回原 id 解析，不丢参与者。
    """
    ids = [i for i in parts if isinstance(i, int)]
    if not ids:
        return [], []
    ph = ",".join("?" * len(ids))
    try:
        mp = {r["source_id"]: r["merged_id"] for r in conn.execute(
            f"SELECT source_id, merged_id FROM _o2m WHERE source_id IN ({ph})",
            ids)}
    except Exception:  # _o2m 缺失 → 退回原 id
        mp = {}
    mids, seen = [], set()
    for i in ids:
        mid = mp.get(i, i)
        if mid not in seen:
            seen.add(mid)
            mids.append(mid)
    names = _resolve_names(mids, "entities_merged", conn)
    # 退回：merged 表里找不到的（无映射者）再从 entities 补一次
    if any(n.startswith("#") for n in names):
        fb = dict(zip(mids, _resolve_names(mids, "entities", conn)))
        names = [n if not n.startswith("#") else fb.get(i, n)
                 for i, n in zip(mids, names)]
    return mids, names


def _merged_or_fallback(cached_merged: list, parts: list, conn):
    """公有 id 优先；若为空或存在悬空（entities_merged 里查不到），退回私有列现算。"""
    cached = [x for x in (cached_merged or []) if isinstance(x, int)]
    if cached:
        ph = ",".join("?" * len(cached))
        alive = {r[0] for r in conn.execute(
            f"SELECT id FROM entities_merged WHERE id IN ({ph})", cached)}
        if alive == set(cached):                      # 全部有效
            names = _resolve_names(cached, "entities_merged", conn)
            return cached, names
    return _to_merged(parts, conn)


def search_hyperedges(query: str, top_k: int = 10, weight: float = 0.5,
                      scope: str | None = None) -> list[dict]:
    """FTS5 BM25 + embedding hybrid search on hyperedges.

    同时检索单篇超边表 `hyperedges` 与跨文献超边表 `cross_doc_hyperedges`。
    参与者名称统一归一到【合并后实体】(entities_merged)：两表的 participants
    都可能存原始 entities.id，解析名称前先经 _o2m 映射，避免两套 id 命名空间混用。

    scope: None=两表都查（默认）/ "cross_doc"=只要跨文献关系 /
        "paper"=只要单篇内部超边。单篇超边数量通常远多于跨文献超边，
        不传 scope 时跨文献关系容易被淹没——想找论文间关系请显式传 "cross_doc"。
    """
    conn = get_db()
    q_vec = compute_embedding(query)

    rows_paper = []
    if scope in (None, "paper", "all"):
        try:
            rows_paper = conn.execute(
                """SELECT id, participants, participants_merged, description, keywords,
                          strength, source_papers, vector
                   FROM hyperedges WHERE vector IS NOT NULL"""
            ).fetchall()
        except sqlite3.OperationalError:       # 老库尚未迁移：退化到只读私有列
            rows_paper = conn.execute(
                """SELECT id, participants, '[]' AS participants_merged, description,
                          keywords, strength, source_papers, vector
                   FROM hyperedges WHERE vector IS NOT NULL"""
            ).fetchall()
    # participants_from 是 QA 超边的 id 域溯源列：表可能缺这一列，
    # 存在才查询；缺失时按默认 entities_merged 解析，避免查询报错
    cross_cols = ("id, title, participants, participants_merged, description, "
                  "keywords, strength, source_papers, vector")
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(cross_doc_hyperedges)")}
        if "participants_from" in cols:
            cross_cols += ", participants_from"
        if "participants_merged" not in cols:
            cross_cols = cross_cols.replace("participants_merged, ", "")
    except Exception:  # noqa: BLE001
        pass
    rows_cross = []
    if scope in (None, "cross_doc", "all"):
        rows_cross = conn.execute(
            f"SELECT {cross_cols} FROM cross_doc_hyperedges "
            f"WHERE vector IS NOT NULL"
        ).fetchall()

    # 统一候选：(scope, row)
    candidates = [("paper", r) for r in rows_paper] + [("cross_doc", r) for r in rows_cross]
    id_pos = {(scope, r["id"]): i for i, (scope, r) in enumerate(candidates)}
    N = len(candidates)
    if N == 0:
        conn.close()
        return []

    # Vector scores
    if q_vec:
        vecs_list = []
        for _, r in candidates:
            v = blob_to_vector(r["vector"]) if r["vector"] else np.zeros(1024, dtype=np.float32)
            vecs_list.append(v)
        vecs = np.array(vecs_list, dtype=np.float32)
        vec_scores = _batch_cosine(vecs, q_vec)
    else:
        vec_scores = np.zeros(N, dtype=np.float32)

    # FTS5 BM25 scores (分别查两张 FTS，按 (scope,id) 定位候选下标)
    bm25_arr = np.zeros(N, dtype=np.float32)
    fts_query = _build_fts_query(query)
    try:
        for row in conn.execute(
            "SELECT rowid, bm25(hyperedges_fts, 1.2, 0.75) AS score FROM hyperedges_fts WHERE hyperedges_fts MATCH ?",
            (fts_query,)):
            idx = id_pos.get(("paper", row["rowid"]))
            if idx is not None:
                bm25_arr[idx] = -row["score"]
    except Exception:
        pass
    try:
        for row in conn.execute(
            "SELECT rowid, bm25(cross_doc_hyperedges_fts, 1.2, 0.75) AS score FROM cross_doc_hyperedges_fts WHERE cross_doc_hyperedges_fts MATCH ?",
            (fts_query,)):
            idx = id_pos.get(("cross_doc", row["rowid"]))
            if idx is not None:
                bm25_arr[idx] = -row["score"]
    except Exception:
        pass

    # Fuse across the combined candidate set
    fused = _zscore_fusion(vec_scores, bm25_arr, weight)
    top_idx = np.argpartition(-fused, min(top_k, N))[:top_k]
    top_idx = top_idx[np.argsort(-fused[top_idx])]

    results = []
    for i in top_idx:
        scope, r = candidates[i]
        parts = json.loads(r["participants"]) if r["participants"] else []
        # 公有列优先（合并时已重算）；为空或有悬空（公有实体被重建）则退回私有列现算
        try:
            cached = json.loads(r["participants_merged"] or "[]")
        except (KeyError, IndexError, ValueError):
            cached = []
        mids, pnames = _merged_or_fallback(cached, parts, conn)
        results.append({
            "id": r["id"],
            "scope": scope,
            "title": (r["title"] or "") if scope == "cross_doc" else "",
            "participants": mids,
            "participant_names": pnames,
            "participants_raw": parts,
            "description": r["description"],
            "keywords": json.loads(r["keywords"]) if r["keywords"] else [],
            "strength": r["strength"],
            "source_papers": json.loads(r["source_papers"]) if r["source_papers"] else [],
            "score": round(float(fused[i]), 4),
            "vec_score": round(float(vec_scores[i]), 4),
            "bm25_score": round(float(bm25_arr[i]), 4),
        })

    conn.close()
    return results[:top_k]


# ═══════════════════════════════════════════════════════════════════════
# Unified search
# ═══════════════════════════════════════════════════════════════════════

def search(query: str, top_k_chunks: int = 10, top_k_edges: int = 10) -> dict:
    """Unified hybrid search: chunks + hyperedges."""
    chunks = search_chunks(query, top_k=top_k_chunks)
    hyperedges = search_hyperedges(query, top_k=top_k_edges)
    return {"chunks": chunks, "hyperedges": hyperedges}


# ═══════════════════════════════════════════════════════════════════════
# 便捷封装（供既有调用方与 API 层使用）
# ═══════════════════════════════════════════════════════════════════════

def list_papers() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, authors, year, journal FROM papers ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_papers(query: str, limit: int = 10) -> list[dict]:
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT p.id, p.title, p.authors, p.year, p.journal,
                      rank as relevance
               FROM papers_fts f
               JOIN papers p ON f.rowid = p.id
               WHERE papers_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def get_paper_details(paper_id: int) -> dict | None:
    """Get full paper metadata including section list and entities.

    对应「从粗到细」检索的中粒度一步：先看章节树与实体概览，再用 section_id 精读；
    只回章节树时看不出这篇讲了什么。
    """
    conn = get_db()
    paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not paper:
        conn.close()
        return None
    result = dict(paper)
    result["sections"] = [
        dict(r) for r in conn.execute(
            "SELECT id, title, level, parent_id, length(content) as char_count "
            "FROM sections WHERE paper_id=? ORDER BY id",
            (paper_id,),
        ).fetchall()
    ]
    try:
        result["entities"] = [
            {"name": r["name"], "type": r["type"]}
            for r in conn.execute(
                "SELECT name, type FROM entities WHERE paper_id=? LIMIT 15", (paper_id,)
            ).fetchall()
        ]
    except Exception:
        result["entities"] = []
    conn.close()
    return result


def search_sections(query: str, limit: int = 10, paper_id: int | None = None) -> list[dict]:
    """Full-text search across paper sections with highlighted snippets."""
    conn = get_db()
    sql = """SELECT s.id, s.paper_id, p.title as paper_title, s.title as section_title,
                    s.level, s.parent_id,
                    snippet(sections_fts, 1, '<b>', '</b>', '...', 30) as snippet
             FROM sections_fts f
             JOIN sections s ON f.rowid = s.id
             JOIN papers p ON s.paper_id = p.id
             WHERE sections_fts MATCH ?"""
    params = [query]
    if paper_id:
        sql += " AND s.paper_id=?"
        params.append(paper_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()
    return [dict(r) for r in rows]


def read_section(paper_id: int, section_id: int | None = None) -> list[dict]:
    conn = get_db()
    if section_id:
        rows = conn.execute(
            "SELECT id, title, level, parent_id, content FROM sections WHERE paper_id=? AND id=? ORDER BY id",
            (paper_id, section_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, level, parent_id, content FROM sections WHERE paper_id=? ORDER BY id",
            (paper_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_section_children(paper_id: int, section_id: int | None = None) -> list[dict]:
    conn = get_db()
    if section_id:
        rows = conn.execute(
            "SELECT id, title, level, parent_id, length(content) as char_count FROM sections WHERE parent_id=? ORDER BY id",
            (section_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, level, parent_id, length(content) as char_count FROM sections WHERE paper_id=? AND parent_id IS NULL ORDER BY id",
            (paper_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_section_path(paper_id: int, section_id: int) -> list[dict]:
    conn = get_db()
    path = []
    current_id = section_id
    for _ in range(20):
        row = conn.execute(
            "SELECT id, title, level, parent_id FROM sections WHERE id=?",
            (current_id,),
        ).fetchone()
        if not row:
            break
        path.append(dict(row))
        if row["parent_id"] is None:
            break
        current_id = row["parent_id"]
    conn.close()
    return list(reversed(path))


def get_section_tree(paper_id: int) -> list[dict]:
    conn = get_db()
    all_secs = conn.execute(
        "SELECT id, title, level, parent_id FROM sections WHERE paper_id=? ORDER BY id",
        (paper_id,),
    ).fetchall()
    conn.close()
    id_map = {}
    for s in all_secs:
        node = dict(s)
        node["children"] = []
        id_map[node["id"]] = node
    roots = []
    for node in id_map.values():
        if node["parent_id"] and node["parent_id"] in id_map:
            id_map[node["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return roots
