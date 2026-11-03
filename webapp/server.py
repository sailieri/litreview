"""litread 后端 —— litreview 文献阅读平台（FastAPI，只读 + 单进程托管前端）。

启动（使用项目自带的 .venv）：
    cd <仓库根>/webapp
    ../.venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8790
    # Windows: ../.venv/Scripts/python.exe -m uvicorn server:app --host 0.0.0.0 --port 8790
浏览器：http://127.0.0.1:8790（本机）/ http://<本机局域网IP>:8790（局域网共享）

局域网共享：host 必须用 0.0.0.0 才能被其他机器访问 —— 在浏览器打开
http://<本机局域网IP>:8790 即可浏览文献库、上传 PDF、与图书管理员对话。
⚠️ 本服务【没有任何鉴权】（不像 MCP 有 token），暴露到局域网 = 同网段人都能读/写。
    仅本机使用时把 host 改回 127.0.0.1。

设计要点：
  - 数据库连接一律 PRAGMA query_only=ON（硬只读）——与后台跑批零锁冲突
  - PDF 走手写 Range 协议（pdf.js 分段加载必需，Starlette FileResponse 不支持 Range）
  - 问答惰性导入 KBAgent（step=rag → kb_agent，每次请求新建实例 = 无跨请求状态）
  - papers.file_path 两种历史形态：.../<stem>.md 与 .../<stem>.pdf，取 stem 时都要剥
"""
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

WEB_ROOT = Path(__file__).resolve().parent
LITROOT = WEB_ROOT.parent
for _p in (str(LITROOT), str(WEB_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.paths import (  # noqa: E402
    data_root, db_path, extracted_dir, normalized_dir, pdfs_dir, summaries_dir,
)

app = FastAPI(title="litread", docs_url=None, redoc_url=None)

# 问答库表结构（qa_threads/qa_messages/qa_fts，幂等）——图书管理员智能体用
try:
    from librarian import ensure_schema as _ensure_qa_schema
    _ensure_qa_schema()
except Exception as _e:  # noqa: BLE001
    print(f"[server] qa schema init failed: {type(_e).__name__}: {_e}")


# ── 数据访问（只读） ────────────────────────────────────────────

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _stem_of(file_path: str) -> str:
    base = (file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def _pdf_file(file_path: str) -> Path | None:
    p = data_root() / "pdfs" / f"{_stem_of(file_path)}.pdf"
    return p if p.exists() else None


def _split(v: str) -> list[str]:
    """多值参数拆分：用 | 分隔（期刊名/关键词里可能含逗号）。"""
    return [x.strip() for x in (v or "").split("|") if x.strip()]


_kw_cache: dict = {"stamp": None, "map": None}

_narr_cache: dict = {"stamp": None, "map": None}


def _narratives_map() -> dict[str, str]:
    """stem -> narrative（截前 500 字，卡片列表用；summaries 目录 mtime+文件数缓存）。"""
    try:
        d = summaries_dir()
        stamp = (d.stat().st_mtime, len(list(d.glob("*.json"))))
    except OSError:
        return {}
    if _narr_cache["stamp"] == stamp:
        return _narr_cache["map"]
    out: dict[str, str] = {}
    for f in d.glob("*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8")).get("narrative") or ""
        except Exception:
            continue
        if t:
            out[f.stem] = " ".join(str(t).split())[:500]
    _narr_cache.update(stamp=stamp, map=out)
    return out


def _keywords_map() -> dict[str, list[str]]:
    """stem -> keywords（只在 metadata/*.json，papers 表无此列；目录 mtime+文件数缓存）。"""
    from modules.paths import metadata_dir
    d = metadata_dir()
    try:
        stamp = (d.stat().st_mtime, len(list(d.glob("*.json"))))
    except OSError:
        return {}
    if _kw_cache["stamp"] == stamp:
        return _kw_cache["map"]
    out: dict[str, list[str]] = {}
    for f in d.glob("*.json"):
        try:
            k = json.loads(f.read_text(encoding="utf-8")).get("keywords") or []
        except Exception:
            continue
        if isinstance(k, str):
            k = [x.strip() for x in k.split(",") if x.strip()]
        k = [str(x).strip() for x in k if str(x).strip()]
        if k:
            out[f.stem] = k
    _kw_cache.update(stamp=stamp, map=out)
    return out


def _years() -> list[int]:
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT year FROM papers WHERE year IS NOT NULL AND year != '' "
        "ORDER BY year DESC").fetchall()
    conn.close()
    return [r["year"] for r in rows]


def _fts_ids(q: str) -> list[int] | None:
    """FTS 检索 papers（失败/零命中回退给调用方走 LIKE）。"""
    safe = re.sub(r'[^\w\s\-:%.]', " ", q).strip()
    if not safe:
        return None
    conn = db()
    try:
        rows = conn.execute(
            "SELECT rowid FROM papers_fts WHERE papers_fts MATCH ? ORDER BY rank LIMIT 500",
            (safe,)).fetchall()
        return [r["rowid"] for r in rows]
    except Exception:
        return None
    finally:
        conn.close()


def _hydrate(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    conn = db()
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, authors, year, journal, doi, file_path, "
        f"entity_count, hyperedge_count FROM papers WHERE id IN ({ph})", ids).fetchall()
    conn.close()
    by_id = {r["id"]: dict(r) for r in rows}
    out = []
    for i in ids:                      # 保持 FTS 排序
        p = by_id.get(i)
        if p:
            out.append(p)
    return out


def _load_narrative(stem: str) -> str:
    f = summaries_dir() / f"{stem}.json"
    if not f.exists():
        return ""
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("narrative", "")
    except Exception:
        return ""


def _load_keywords(stem: str) -> list:
    """keywords 不在 papers 表（只在 metadata/<stem>.json），detail 时补带。"""
    from modules.paths import metadata_dir
    f = metadata_dir() / f"{stem}.json"
    if not f.exists():
        return []
    try:
        k = json.loads(f.read_text(encoding="utf-8")).get("keywords") or []
        if isinstance(k, str):
            k = [x.strip() for x in k.split(",") if x.strip()]
        return list(k)
    except Exception:
        return []


# ── API ────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    conn = db()
    out = {}
    for t in ("papers", "sections", "chunks", "entities", "hyperedges"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    return out


@app.get("/api/facets")
def facets():
    """三组分面（带篇数）：年份 / 期刊 / 关键词。关键词从 metadata json 聚合。"""
    conn = db()
    years = [{"value": str(r["year"]), "count": r["c"]} for r in conn.execute(
        "SELECT year, COUNT(*) AS c FROM papers WHERE year IS NOT NULL AND year != '' "
        "GROUP BY year ORDER BY year DESC").fetchall()]
    journals = [{"value": r["journal"], "count": r["c"]} for r in conn.execute(
        "SELECT TRIM(journal) AS journal, COUNT(*) AS c FROM papers "
        "WHERE journal IS NOT NULL AND TRIM(journal) != '' "
        "GROUP BY TRIM(journal) ORDER BY c DESC, journal LIMIT 80").fetchall()]
    conn.close()

    agg: dict[str, list] = {}          # lower -> [显示名, 计数]（大小写归一）
    for kws in _keywords_map().values():
        for k in kws:
            key = k.lower()
            if key in agg:
                agg[key][1] += 1
            else:
                agg[key] = [k, 1]
    keywords = [{"value": v, "count": c} for v, c in
                sorted(agg.values(), key=lambda x: (-x[1], x[0].lower()))[:150]]
    return {"years": years, "journals": journals, "keywords": keywords}


class PapersQuery(BaseModel):
    pass


def _parse_id_query(q: str):
    """纯数字或 #123 → 返回 int(id)，否则 None。"""
    s = (q or "").strip().lstrip("#").strip()
    return int(s) if s.isdigit() else None


def _title_key(m):
    """标题排序键：无标题退回文件名，统一小写。"""
    return (m["title"] or m["file_path"] or "").lower()


@app.get("/api/papers")
def papers(query: str = "", year: str = "", journal: str = "", keywords: str = "",
           sort: str = "", page: int = 1, page_size: int = 20, favorite: int = 0,
           collection_id: int = 0):
    """多维筛选：year/journal/keywords 均为 | 分隔多值；维度间 AND、维度内 OR。

    query: 文本走 FTS/LIKE；纯数字或 #123 → 按论文 ID 检索（精确优先，未命中转前缀）
    sort: ''(默认：有 query 按相关度，否则年新→旧) | year_desc | year_asc
          | title_asc | title_desc | id_asc | id_desc | relevance
    """
    query = (query or "").strip()
    years, journals, kws = _split(year), _split(journal), _split(keywords)
    conn = db()
    qid = _parse_id_query(query)                      # 纯数字 / #123 → 按 ID 检索
    if query:
        if qid is not None:
            # 精确匹配优先；精确未命中 → ID 前缀（输入 "5" 可命中 5/50-59）
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM papers WHERE id = ?", (qid,))]
            if not ids:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM papers WHERE CAST(id AS TEXT) LIKE ? "
                    "ORDER BY id LIMIT 200", (f"{qid}%",))]
        else:
            ids = _fts_ids(query)
            if not ids:                               # FTS 失败/零命中 → LIKE 兜底（中文友好）
                rows = conn.execute(
                    "SELECT id FROM papers WHERE title LIKE ? OR authors LIKE ? "
                    "OR journal LIKE ? LIMIT 500",
                    (f"%{query}%",) * 3).fetchall()
                ids = [r["id"] for r in rows]
    else:
        ids = [r["id"] for r in conn.execute("SELECT id FROM papers").fetchall()]
    if not ids:
        conn.close()
        return {"total": 0, "page": 1, "page_size": page_size, "items": [], "pages": 1}
    ph = ",".join("?" * len(ids))
    meta = {r["id"]: r for r in conn.execute(
        f"SELECT id, year, journal, file_path, title, favorite FROM papers WHERE id IN ({ph})", ids)}
    conn.close()

    keep = [i for i in ids if i in meta]              # 默认保持查询/FTS 相关度顺序
    if years:                                         # 维度间 AND、维度内 OR
        keep = [i for i in keep if str(meta[i]["year"] or "") in years]
    if journals:
        keep = [i for i in keep if (meta[i]["journal"] or "").strip() in journals]
    if kws:
        kmap = _keywords_map()
        sel = {k.lower() for k in kws}
        keep = [i for i in keep if sel & {k.lower() for k in
                kmap.get(_stem_of(meta[i]["file_path"]), [])}]

    if favorite or collection_id:                      # 收藏夹视图 / 指定夹筛选（以关系表为准）
        cconn = sqlite3.connect(str(db_path()))
        if collection_id:
            sel_ids = {r[0] for r in cconn.execute(
                "SELECT paper_id FROM collection_papers WHERE collection_id = ?", (collection_id,))}
        else:
            sel_ids = {r[0] for r in cconn.execute("SELECT DISTINCT paper_id FROM collection_papers")}
        cconn.close()
        keep = [i for i in keep if i in sel_ids]

    if sort == "relevance" or (not sort and query):
        pass                                          # 保持 FTS rank 顺序
    elif sort == "year_asc":
        keep.sort(key=lambda i: (meta[i]["year"] or 99999))
    elif sort == "title_asc":
        keep.sort(key=lambda i: _title_key(meta[i]))
    elif sort == "title_desc":
        keep.sort(key=lambda i: _title_key(meta[i]), reverse=True)
    elif sort == "id_asc":
        keep.sort(key=lambda i: i)
    elif sort == "id_desc":
        keep.sort(key=lambda i: i, reverse=True)
    else:                                             # ''（无 query）或 year_desc
        keep.sort(key=lambda i: (meta[i]["year"] or 0), reverse=True)

    total = len(keep)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    slice_ = keep[(page - 1) * page_size: page * page_size]
    items = _hydrate(slice_)
    if slice_:                                         # 列表页星标 + 所属夹回显
        fconn = sqlite3.connect(str(db_path()))
        fph = ",".join("?" * len(slice_))
        cmap: dict[int, list[int]] = {}
        for _pid, _cid in fconn.execute(
                f"SELECT paper_id, collection_id FROM collection_papers WHERE paper_id IN ({fph})",
                slice_):
            cmap.setdefault(_pid, []).append(_cid)
        fconn.close()
        for it in items:
            it["collections"] = sorted(cmap.get(it["id"], []))
            it["favorite"] = bool(it["collections"])
    kmap = _keywords_map()
    nmap = _narratives_map()
    for it in items:
        it["pdf"] = _pdf_file(it["file_path"]) is not None
        it["stem"] = _stem_of(it["file_path"])
        it["keywords"] = kmap.get(it["stem"], [])[:10]   # 列表页标签回显
        it["abstract"] = nmap.get(it["stem"], "")         # 卡片列表：叙事摘要
    return {"total": total, "page": page, "page_size": page_size, "items": items,
             "pages": max(1, (total + page_size - 1) // page_size)}



@app.get("/api/insights")
def insights():
    """库级分析仪表盘：概览 / 年度趋势(含年度关键词) / 期刊分布 / 关键词频次与共现。"""
    from collections import Counter
    from itertools import combinations

    conn = db()
    overview = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("papers", "sections", "chunks", "entities", "hyperedges",
                          "cross_doc_hyperedges")}
    yearly = [{"year": str(r["year"]), "count": r["c"]} for r in conn.execute(
        "SELECT year, COUNT(*) AS c FROM papers WHERE year IS NOT NULL AND year != '' "
        "GROUP BY year ORDER BY year").fetchall()]
    journals = [{"value": r["j"], "count": r["c"]} for r in conn.execute(
        "SELECT TRIM(journal) AS j, COUNT(*) AS c FROM papers "
        "WHERE journal IS NOT NULL AND TRIM(journal) != '' "
        "GROUP BY TRIM(journal) ORDER BY c DESC, j LIMIT 20").fetchall()]
    stem_year: dict[str, str] = {}
    for r in conn.execute(
            "SELECT file_path, year FROM papers WHERE year IS NOT NULL AND year != ''"):
        stem_year[_stem_of(r["file_path"])] = str(r["year"])
    conn.close()

    kmap = _keywords_map()

    # 关键词频次（大小写归一，保留首个显示名）
    disp: dict[str, str] = {}
    freq: Counter = Counter()
    for kws in kmap.values():
        for k in kws:
            key = k.lower()
            freq[key] += 1
            disp.setdefault(key, k)
    top_kw = [{"value": disp[k], "count": c, "key": k} for k, c in freq.most_common(150)]

    # 年度关键词演化：每年 top5（与 papers.year 按 stem 关联）
    per_year: dict[str, Counter] = {}
    for stem, y in stem_year.items():
        for k in kmap.get(stem, []):
            per_year.setdefault(y, Counter())[k.lower()] += 1
    yearly_kw = [{"year": y,
                  "keywords": [{"value": disp.get(k, k), "count": c}
                               for k, c in cnt.most_common(5)]}
                 for y, cnt in sorted(per_year.items())]

    # 关键词共现（同篇内两两共现；共现 >=2 才算边，取 top 120）
    pair: Counter = Counter()
    for kws in kmap.values():
        norm = sorted({k.lower() for k in kws})[:10]      # 单篇上限 10 防组合爆
        for a, b in combinations(norm, 2):
            pair[(a, b)] += 1
    cooc = [{"a": disp.get(a, a), "b": disp.get(b, b), "w": w}
            for (a, b), w in pair.most_common(400) if w >= 2][:120]

    year_span = [yearly[0]["year"], yearly[-1]["year"]] if yearly else ["", ""]
    return {"overview": overview, "year_span": year_span,
            "yearly": yearly, "yearly_keywords": yearly_kw,
            "journals": journals, "keywords": top_kw, "cooc": cooc}


# ===== 多收藏夹 =====
# 设计：collections / collection_papers 两张表，多对多，同一篇可属多夹。
# papers.favorite 保留为兼容字段（本服务不写），收藏归属一律以关系表为准。
class CollectionIn(BaseModel):
    name: str = ""


class PaperCollectionsIn(BaseModel):
    ids: list[int] = []


class MovePapersIn(BaseModel):
    paper_ids: list[int] = []
    to_collection_id: int


def _wdb():
    """写连接（server.db() 是 query_only 只读连接，写操作一律用本函数）。"""
    c = sqlite3.connect(str(db_path()))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _collections_of(c, pid: int) -> list[int]:
    return sorted(r[0] for r in c.execute(
        "SELECT collection_id FROM collection_papers WHERE paper_id = ?", (pid,)))


@app.get("/api/collections")
def list_collections():
    """列出全部收藏夹（带篇数），默认夹排最前。"""
    c = _wdb()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT c.id, c.name, c.is_default, "
            "  (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.id) AS count "
            "FROM collections c ORDER BY c.is_default DESC, c.name")]
        total = c.execute("SELECT COUNT(DISTINCT paper_id) FROM collection_papers").fetchone()[0]
        return {"items": rows, "total_papers": total}
    finally:
        c.close()


@app.post("/api/collections")
def create_collection(body: CollectionIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "收藏夹名称不能为空")
    c = _wdb()
    try:
        try:
            cur = c.execute("INSERT INTO collections(name) VALUES (?)", (name,))
            c.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400, "同名收藏夹已存在")
        return {"id": cur.lastrowid, "name": name, "is_default": 0, "count": 0}
    finally:
        c.close()


@app.patch("/api/collections/{cid}")
def rename_collection(cid: int, body: CollectionIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "收藏夹名称不能为空")
    c = _wdb()
    try:
        if not c.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone():
            raise HTTPException(404, "collection not found")
        try:
            c.execute("UPDATE collections SET name = ? WHERE id = ?", (name, cid))
            c.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(400, "同名收藏夹已存在")
        return {"ok": True, "id": cid, "name": name}
    finally:
        c.close()


@app.delete("/api/collections/{cid}")
def delete_collection(cid: int):
    """删除收藏夹：只删「夹-论文」关系，论文本身不动；默认夹不可删。"""
    c = _wdb()
    try:
        row = c.execute("SELECT is_default FROM collections WHERE id = ?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "collection not found")
        if row[0]:
            raise HTTPException(400, "默认收藏夹不可删除（可以改名）")
        c.execute("DELETE FROM collection_papers WHERE collection_id = ?", (cid,))
        c.execute("DELETE FROM collections WHERE id = ?", (cid,))
        c.commit()
        return {"ok": True}
    finally:
        c.close()


@app.post("/api/papers/{pid}/collections")
def set_paper_collections(pid: int, body: PaperCollectionsIn):
    """覆盖式设置某篇论文的收藏夹归属（ids = 目标夹全集）。"""
    c = _wdb()
    try:
        if not c.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone():
            raise HTTPException(404, "paper not found")
        valid = {r[0] for r in c.execute("SELECT id FROM collections")}
        want = [i for i in dict.fromkeys(int(x) for x in body.ids) if i in valid]
        c.execute("DELETE FROM collection_papers WHERE paper_id = ?", (pid,))
        c.executemany(
            "INSERT OR IGNORE INTO collection_papers(collection_id, paper_id) VALUES (?, ?)",
            [(i, pid) for i in want])
        c.commit()
        return {"id": pid, "collections": sorted(want), "favorite": bool(want)}
    finally:
        c.close()


@app.post("/api/collections/{cid}/move")
def move_papers(cid: int, body: MovePapersIn):
    """把论文从 cid 移动到 to_collection_id（先加目标再删源，单事务）。"""
    if cid == body.to_collection_id:
        raise HTTPException(400, "源夹与目标夹相同")
    c = _wdb()
    try:
        for t in (cid, body.to_collection_id):
            if not c.execute("SELECT 1 FROM collections WHERE id = ?", (t,)).fetchone():
                raise HTTPException(404, "collection %d not found" % t)
        pids = [int(p) for p in body.paper_ids]
        c.executemany(
            "INSERT OR IGNORE INTO collection_papers(collection_id, paper_id) VALUES (?, ?)",
            [(body.to_collection_id, p) for p in pids])
        c.executemany("DELETE FROM collection_papers WHERE collection_id = ? AND paper_id = ?",
                      [(cid, p) for p in pids])
        c.commit()
        return {"ok": True, "moved": len(pids), "to": body.to_collection_id}
    finally:
        c.close()


@app.post("/api/papers/{pid}/favorite")
def toggle_favorite(pid: int):
    """旧接口兼容：默认收藏夹的 toggle（卡片上 ☆ 一键收藏走这里）。

    注意：已收藏（在任意夹）时点这里只影响默认夹，不会动其他夹的归属。
    """
    c = _wdb()
    try:
        if not c.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone():
            raise HTTPException(404, "paper not found")
        d = c.execute("SELECT id FROM collections WHERE is_default = 1 LIMIT 1").fetchone()
        if d is None:
            raise HTTPException(500, "默认收藏夹缺失，请重启服务重建")
        did = d[0]
        inx = c.execute(
            "SELECT 1 FROM collection_papers WHERE collection_id = ? AND paper_id = ?",
            (did, pid)).fetchone()
        if inx:
            c.execute("DELETE FROM collection_papers WHERE collection_id = ? AND paper_id = ?",
                      (did, pid))
        else:
            c.execute("INSERT OR IGNORE INTO collection_papers(collection_id, paper_id) VALUES (?, ?)",
                      (did, pid))
        c.commit()
        ids = _collections_of(c, pid)
        return {"id": pid, "favorite": bool(ids), "in_default": bool(not inx),
                "collections": ids, "default_id": did}
    finally:
        c.close()


@app.get("/api/papers/{pid}")
def paper_detail(pid: int):
    conn = db()
    p = conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "paper not found")
    paper = dict(p)
    stem = _stem_of(paper.get("file_path") or "")

    sections = [dict(r) for r in conn.execute(
        "SELECT id, title, level, parent_id, length(content) AS char_count "
        "FROM sections WHERE paper_id=? ORDER BY id", (pid,))]

    entities = [dict(r) for r in conn.execute(
        "SELECT id, name, type, description FROM entities WHERE paper_id=? ORDER BY id",
        (pid,))]

    # 实体合并结果——把每个原始实体映射到公共标准名（entities_merged）。
    # source_ids 是 JSON 数组（原始 entities.id），万级行全扫，代价可接受。
    _eids = {e["id"] for e in entities}
    if _eids:
        for r in conn.execute(
                "SELECT id, name, aliases, source_ids, is_merged "
                "FROM entities_merged WHERE is_merged=1").fetchall():
            try:
                srcs = json.loads(r["source_ids"] or "[]")
            except Exception:  # noqa: BLE001
                srcs = []
            hit = _eids & set(srcs)
            if not hit:
                continue
            info = {
                "merged_id": r["id"],
                "merged_name": r["name"],
                "aliases": [a.strip() for a in (r["aliases"] or "").split(",") if a.strip()],
                "group_size": len(srcs),
            }
            for e in entities:
                if e["id"] in hit:
                    e["merged"] = info

    # 本篇超边（source_papers 是 JSON 数组 → 精确成员判断在 Python 侧做）
    # ⚠️ 必须 SELECT source_papers：下方按该列做成员判断，缺列会取不到值，超边全被过滤为空
    hes = []
    for r in conn.execute(
            "SELECT id, participants, description, source_papers FROM hyperedges "
            "WHERE source_papers LIKE ? ORDER BY id", (f"%{pid}%",)).fetchall():
        try:
            src = json.loads(r["source_papers"] or "[]")
        except Exception:
            src = []
        if pid not in src:
            continue
        try:
            pids = json.loads(r["participants"] or "[]")
        except Exception:
            pids = []
        hes.append({"id": r["id"], "description": r["description"], "participant_ids": pids})

    # 参与实体解析（跨篇实体也可能出现 → 统一按 id 查）
    pid_set = {x for h in hes for x in h["participant_ids"] if isinstance(x, int)}
    ent_by_id = {}
    if pid_set:
        ph = ",".join("?" * len(pid_set))
        for r in conn.execute(
                f"SELECT id, name, type, description FROM entities WHERE id IN ({ph})",
                tuple(pid_set)).fetchall():
            ent_by_id[r["id"]] = dict(r)
    conn.close()
    for h in hes:
        h["participants"] = [ent_by_id.get(x, {"id": x, "name": f"#{x}", "type": "", "description": ""})
                             for x in h["participant_ids"]]

    pdf = _pdf_file(paper.get("file_path") or "")
    _wc = _wdb()
    try:
        _cids = _collections_of(_wc, pid)
        paper["collections"] = _cids
        paper["favorite"] = bool(_cids)
    finally:
        _wc.close()
    return {
        "paper": paper,
        "stem": stem,
        "narrative": _load_narrative(stem),
        "keywords": _load_keywords(stem),
        "sections": sections,
        "entities": entities,
        "hyperedges": hes,
        "pdf": {"available": pdf is not None,
                "url": f"/api/papers/{pid}/pdf" if pdf else None},
    }


@app.get("/api/papers/{pid}/section/{sid}")
def section_content(pid: int, sid: int):
    conn = db()
    row = conn.execute(
        "SELECT id, title, level, parent_id, content FROM sections "
        "WHERE id=? AND paper_id=?", (sid, pid)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "section not found")
    return dict(row)


# ── PDF：手写 Range 协议（pdf.js 必需） ─────────────────────────

def _file_iter(path: Path, start: int, end: int, chunk: int = 1 << 20):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            buf = f.read(min(chunk, remaining))
            if not buf:
                break
            remaining -= len(buf)
            yield buf


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _range_response(path: Path, request: Request):
    """手写 Range 响应（原文 PDF 与对照 PDF 共用）。"""
    size = path.stat().st_size
    rng = (request.headers.get("range") or "").strip()
    m = _RANGE_RE.match(rng) if rng else None
    if m:
        if m.group(1) == "":                      # 后缀范围 bytes=-N
            n = int(m.group(2) or 0)
            start, end = max(0, size - n), size - 1
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        headers = {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": "application/pdf",
        }
        return StreamingResponse(_file_iter(path, start, end),
                                 status_code=206, headers=headers)

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size),
               "Content-Type": "application/pdf"}
    return StreamingResponse(_file_iter(path, 0, size - 1), headers=headers)


@app.get("/api/papers/{pid}/pdf")
def paper_pdf(pid: int, request: Request):
    conn = db()
    p = conn.execute("SELECT file_path FROM papers WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        raise HTTPException(404, "paper not found")
    path = _pdf_file(p["file_path"] or "")
    if path is None:
        raise HTTPException(404, "pdf missing for this paper")
    return _range_response(path, request)


# ── 中英对照 PDF（按需调本地 pdf2zh_next，产物缓存在 data/translated/） ──
# 翻译环境：独立的 .venv-pdf2zh（只装 pdf2zh_next 本体）。
# 独立于 webapp 的 .venv——两者 starlette(0.52 vs 1.7)/fastapi 硬冲突，不能合并。
# 无需常驻服务，一次翻译约 2 分钟；产物按 pid 缓存，二次访问零成本。

# 候选顺序：config paths.pdf2zh_python → 本项目 .venv-pdf2zh → config paths.pdf2zh_dirs。
# 一律从 config 读，代码里不写盘符。
from modules.config import get as _cfg_get  # noqa: E402

_PDF2ZH_SERVER_DIRS = [Path(str(d)) for d in (_cfg_get("paths.pdf2zh_dirs", []) or [])]


def _venv_python(venv_dir: Path) -> Path:
    """venv 内的解释器：Windows 用 Scripts/python.exe，POSIX 用 bin/python。"""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pdf2zh_paths():
    """PDFMathTranslate-next（pdf2zh_next）驱动入口：venv python + 薄驱动脚本。

    候选顺序：config paths.pdf2zh_python → 本项目 .venv-pdf2zh →
    config paths.pdf2zh_dirs 下的 venv。引擎/前缀/关思考
    全由 webapp/pdf2zh_vllm_driver.py 自己配。"""
    drv = WEB_ROOT / "pdf2zh_vllm_driver.py"
    candidates = []
    _explicit = _cfg_get("paths.pdf2zh_python", "")
    if _explicit:
        candidates.append(Path(str(_explicit)))
    candidates.append(_venv_python(LITROOT / ".venv-pdf2zh"))
    candidates += [_venv_python(d / "zotero-pdf2zh-next-venv")
                   for d in _PDF2ZH_SERVER_DIRS]
    for py in candidates:
        if py.exists() and drv.exists():
            return py, drv
    return None, None


# 翻译引擎开关：wsidx = Index-Translate-2B（默认，专用翻译模型，速度更快、
# 译文更学术）；ws = 通用大模型（回退）。
# 取值优先级：环境变量 LITREAD_TRANSLATE_ENGINE > config webapp.translate_engine
TRANSLATE_ENGINE = os.environ.get("LITREAD_TRANSLATE_ENGINE") or \
    _cfg_get("webapp.translate_engine", "wsidx")


def _vllm_ws():
    """翻译端点：全部取自 config.yaml（providers.workstation + endpoints.*）。

    配了局域网端点时优先直连（通常快于远程网关），探测不通则回落默认
    base_url。代码里不写具体地址。
    """
    import urllib.request
    import urllib.error

    from modules.config import require as _cfg_req

    ws = _cfg_req("providers.workstation",
                  "需要一个 OpenAI 兼容的 workstation provider（base_url/api_key）")
    key = ws.get("api_key", "")
    if TRANSLATE_ENGINE == "wsidx":
        model = _cfg_get("webapp.translate_model_idx", "index-translate-2b")
        lan = _cfg_get("endpoints.workstation_lan_idx", "")
        cf = _cfg_get("endpoints.workstation_cf_idx", "") or ws.get("base_url", "")
    else:
        model = (ws.get("models") or {}).get("default", "")
        lan = _cfg_get("endpoints.workstation_lan", "")
        cf = ws.get("base_url", "")
    if not lan:
        return cf, key, model           # 没配局域网地址就直接走 config 里的 base_url
    probe, base = lan.rstrip("/") + "/models", lan
    try:
        req = urllib.request.Request(probe, headers={"User-Agent": "curl/8.5.0"})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open(req, timeout=2).close()
    except urllib.error.HTTPError:
        pass                              # 通了，只是没鉴权
    except Exception:  # noqa: BLE001    网络错/超时 → 回落默认端点
        base = cf
    return base, key, model


def _winarg(p: Path) -> str:
    """喂给 Windows exe 的参数路径：POSIX 路径 → Windows 路径（Windows 下原样）。"""
    s = str(p)
    if os.name != "nt" and s.startswith("/mnt/") and len(s) > 6:
        return s[5].upper() + ":" + s[6:].replace("/", "\\")
    return s


def _bilingual_file(pid: int) -> Path:
    # 产物：持久化纯中文 mono（{pid}.zh.pdf），对照 dual 跑完即删
    return data_root() / "translated" / f"{pid}.zh.pdf"


_bil_lock = threading.Lock()
_bil_jobs: dict = {}          # pid -> {"state": running|error, "msg", "started"}


def _bil_state(pid: int) -> dict:
    ready = _bilingual_file(pid).exists()
    with _bil_lock:
        job = dict(_bil_jobs.get(pid) or {})
    running = job.get("state") == "running"
    return {
        "ready": ready,
        "running": (running and not ready),
        "error": job.get("msg") if job.get("state") == "error" else None,
    }


def _run_bilingual(pid: int, src: Path):
    """后台线程：拷 ASCII 工作副本 → pdf2zh_next 翻译 → 收编产物。"""
    out_dir = data_root() / "translated"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / f"{pid}.src.pdf"
    log = out_dir / f"{pid}.log"
    py, drv = _pdf2zh_paths()
    state, msg = "error", "pdf2zh driver not found"
    try:
        if py is None:
            raise RuntimeError("pdf2zh_next venv/driver not found")
        shutil.copyfile(src, work)
        base, key, model = _vllm_ws()
        cmd = [
            str(py), _winarg(drv), _winarg(work),
            "--openaicompatible",
            "--openai-compatible-base-url", base,
            "--openai-compatible-api-key", key,
            "--openai-compatible-model", model,
            "--openai-compatible-temperature", "0.3",
            "--openai-compatible-send-temperature",
            "--qps", "32",                       # 并发 32
            "--no-auto-extract-glossary",        # 粗翻：不自动提取术语表（术语表对粗翻无用）
            "--output", _winarg(out_dir),
            "--lang-in", "en", "--lang-out", "zh-CN",
            "--watermark-output-mode", "no_watermark",
            "--disable-config-auto-save",         # 防止把 vLLM key 回写到用户配置
        ]
        import time as _time
        _t_sub = _time.time()
        with open(log, "wb") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                  cwd=str(out_dir), timeout=1800)
        _t_done = _time.time()
        print(f"[bil {pid}] subprocess={_t_done - _t_sub:.1f}s rc={proc.returncode}",
              flush=True)
        # mono（纯中文）是持久缓存；dual（对照）是副产物，跑完即删
        mono = out_dir / f"{pid}.src.no_watermark.zh-CN.mono.pdf"
        dual = out_dir / f"{pid}.src.no_watermark.zh-CN.dual.pdf"
        if proc.returncode == 0 and mono.exists():
            mono.replace(_bilingual_file(pid))
            legacy = out_dir / f"{pid}.dual.pdf"      # 旧命名残留的对照缓存，一并清掉防混淆
            if legacy.exists():
                try:
                    legacy.unlink()
                except OSError:
                    pass
            print(f"[bil {pid}] collect={_time.time() - _t_done:.1f}s", flush=True)
            state, msg = "done", ""
        else:
            tail = ""
            if log.exists():
                tail = log.read_bytes()[-600:].decode("utf-8", "replace")[-300:]
            msg = f"exit={proc.returncode} {tail}".strip()
    except subprocess.TimeoutExpired:
        msg = "翻译超时(30min)"
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
    finally:
        # 清理中间件（mono 副本、工作副本），保留 log 供排障
        for junk in (work, out_dir / f"{pid}.src.no_watermark.zh-CN.dual.pdf",
                     out_dir / f"{pid}.src.zh-CN.dual.pdf",
                     out_dir / f"{pid}.src.no_watermark.zh-CN.glossary.csv"):
            try:
                if junk.exists():
                    junk.unlink()
            except OSError:
                pass
        with _bil_lock:
            _bil_jobs[pid] = {"state": state, "msg": msg,
                              "finished": time.time()}


@app.get("/api/papers/{pid}/bilingual")
def bilingual_status(pid: int):
    return _bil_state(pid)


@app.post("/api/papers/{pid}/bilingual")
def bilingual_start(pid: int):
    st = _bil_state(pid)
    if st["ready"] or st["running"]:
        return st
    py, _ = _pdf2zh_paths()
    if py is None:
        raise HTTPException(503, "pdf2zh_next driver not found")
    conn = db()
    p = conn.execute("SELECT file_path FROM papers WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        raise HTTPException(404, "paper not found")
    src = _pdf_file(p["file_path"] or "")
    if src is None:
        raise HTTPException(404, "pdf missing for this paper")
    with _bil_lock:
        _bil_jobs[pid] = {"state": "running", "msg": "", "started": time.time()}
    threading.Thread(target=_run_bilingual, args=(pid, src), daemon=True).start()
    return {"ready": False, "running": True, "error": None}


@app.get("/api/papers/{pid}/bilingual/pdf")
def bilingual_pdf(pid: int, request: Request):
    path = _bilingual_file(pid)
    if not path.exists():
        raise HTTPException(404, "bilingual pdf not generated yet")
    return _range_response(path, request)


# ── 翻译进度：实时解析 [TS] 事件行 → 节点假进度 + 翻译段真实进度 ──
# 节点权重为经验估计，只用于给用户一个粗略的阶段预期
_BIL_NODES = [
    ("__start",                                     "启动预热",    5.6),
    ("Parse PDF and Create Intermediate Representation", "解析 PDF",  2.0),
    ("DetectScannedFile",                           "扫描检测",    0.4),
    ("Parse Page Layout",                           "版面分析",    7.8),
    ("Parse Paragraphs",                            "段落切分",    2.6),
    ("Parse Formulas and Styles",                   "公式样式",    2.8),
    ("Translate Paragraphs",                        "翻译",        6.9),
    ("Typesetting",                                 "排版",        1.9),
    ("Add Fonts",                                   "嵌入字体",    0.2),
    ("Generate drawing instructions",               "绘图指令",    0.2),
    ("Subset font",                                 "字体子集化",  2.6),
    ("Save PDF",                                    "生成 PDF",    4.1),
    ("__final",                                     "收尾",        7.4),
]

_TS_RE = {
    "type":  re.compile(r"'type': '(\w+)'"),
    "stage": re.compile(r"'stage': '([^']+)'"),
    "prog":  re.compile(r"'stage_progress': ([\d.]+)"),
    "cur":   re.compile(r"'stage_current': (\d+)"),
    "tot":   re.compile(r"'stage_total': (\d+)"),
}


def _read_ts(log: Path):
    """读日志末尾 256KB，按正则抠 [TS] 事件字段（dict repr 被截断也不怕）。"""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in data.replace("\r", "\n").split("\n"):
        if "[TS +" not in line:
            continue
        t = _TS_RE["type"].search(line)
        if not t:
            continue
        st = _TS_RE["stage"].search(line)
        pg = _TS_RE["prog"].search(line)
        cu = _TS_RE["cur"].search(line)
        ta = _TS_RE["tot"].search(line)
        out.append((t.group(1), st.group(1) if st else None,
                    float(pg.group(1)) if pg else None,
                    int(cu.group(1)) if cu else None,
                    int(ta.group(1)) if ta else None))
    return out


@app.get("/api/papers/{pid}/bilingual/progress")
def bilingual_progress(pid: int):
    """节点步骤条 + 翻译段真实进度（节点权重为预估，非真实进度）。"""
    st = _bil_state(pid)
    if st["ready"]:
        return {**st, "overall": 100.0,
                "nodes": [{"label": lb, "state": "done", "progress": 100.0}
                          for _, lb, _ in _BIL_NODES]}
    if not st["running"]:
        return {**st, "overall": 0.0, "nodes": []}

    events = _read_ts(data_root() / "translated" / f"{pid}.log")
    ends, cur, cur_prog = set(), None, 0.0
    tr_cur = tr_tot = None
    finished = saw = False
    for typ, stage, prog, cu, ta in events:
        saw = True
        if typ == "finish":
            finished = True
        elif typ == "progress_end" and stage:
            ends.add(stage)
        elif typ == "progress_start" and stage:
            cur, cur_prog = stage, 0.0
        elif typ == "progress_update" and stage:
            cur = stage
            if prog is not None:
                cur_prog = prog
            if stage == "Translate Paragraphs" and cu is not None:
                tr_cur, tr_tot = cu, ta

    total_w = sum(w for _, _, w in _BIL_NODES)
    acc, nodes = 0.0, []
    for key, label, w in _BIL_NODES:
        if key == "__start":
            pct, state = (100.0, "done") if saw else (20.0, "active")
        elif key == "__final":
            pct, state = (100.0, "done") if finished else (0.0, "pending")
        elif key in ends:
            pct, state = 100.0, "done"
        elif key == cur:
            pct, state = cur_prog, "active"
        else:
            pct, state = 0.0, "pending"
        acc += w * pct / 100.0
        node = {"label": label, "state": state, "progress": round(pct, 1)}
        if key == "Translate Paragraphs" and tr_cur is not None:
            node["current"], node["total"] = tr_cur, tr_tot
        nodes.append(node)
    return {**st, "overall": round(acc / total_w * 100.0, 1), "nodes": nodes}


# ── 问答（kb_agent 之上封装） ────────────────────────────

class AskIn(BaseModel):
    question: str
    paper_id: Optional[int] = None


class PaperChatIn(BaseModel):
    message: str
    history: Optional[list] = []   # [{role: user|assistant|ai, content|text}]


@app.post("/api/papers/{pid}/chat")
def paper_chat(pid: int, body: PaperChatIn):
    """单篇论文问答智能体（PaperQA）：原文+超图入上下文，kb_agent 为跨库工具。"""
    q = (body.message or "").strip()
    if not q:
        raise HTTPException(400, "empty message")
    conn = db()
    exists = conn.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(404, "paper not found")
    from paper_qa import answer                      # 惰性导入：与 kb_agent 同款
    try:
        return answer(pid, q, body.history or [])
    except Exception as e:                           # noqa: BLE001
        raise HTTPException(502, f"paper_qa failed: {type(e).__name__}: {e}")


@app.post("/api/ask")
def ask(body: AskIn):
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    prefix = ""
    if body.paper_id:
        conn = db()
        row = conn.execute("SELECT id, title FROM papers WHERE id=?",
                           (body.paper_id,)).fetchone()
        conn.close()
        if row:
            prefix = (f"（用户正在阅读论文 #{row['id']}《{row['title']}》。"
                      f"回答优先围绕该篇，证据可以来自全库。）\n")
    from modules.kb_agent import KBAgent   # 惰性导入：启动不必付 kb_agent 的导入成本
    agent = KBAgent(step="rag")
    out = agent.ask(prefix + q)
    return {"text": out.get("text") or "", "citations": out.get("citations") or []}


# ── 图书管理员智能体（librarian：独立问答库 + 工作区 + SSE）────

class AgentChatIn(BaseModel):
    message: str
    thread_id: Optional[int] = None      # 空 = 新建会话
    paper_ids: Optional[list] = None     # 工作区快照（None = 不更新）
    pending_ids: Optional[list] = None   # 本轮新加入的论文（智能体先读，用后清空）


@app.get("/api/agent/threads")
def agent_threads():
    from librarian import list_threads
    return {"threads": list_threads()}


@app.get("/api/agent/threads/{tid}")
def agent_thread(tid: int):
    from librarian import get_thread
    t = get_thread(tid)
    if not t:
        raise HTTPException(404, "thread not found")
    return t


@app.delete("/api/agent/threads/{tid}")
def agent_thread_delete(tid: int):
    from librarian import delete_thread
    if not delete_thread(tid):
        raise HTTPException(404, "thread not found")
    return {"ok": True}


@app.post("/api/agent/threads/{tid}/archive")
def agent_thread_archive(tid: int):
    """归档会话：冻结（不可再追加消息）+ 后台整理会话提取跨文献超边。"""
    from librarian import archive_thread
    r = archive_thread(tid)
    if not r.get("ok"):
        err = r.get("error") or "archive failed"
        raise HTTPException(404 if err == "thread not found" else 409, err)
    return r


@app.post("/api/agent/chat")
def agent_chat(body: AgentChatIn):
    """图书管理员智能体对话：SSE 事件流（meta/tool/delta/done/error）。"""
    from librarian import chat_stream

    def gen():
        try:
            for evt in chat_stream(body.thread_id, body.message,
                                   body.paper_ids, body.pending_ids):
                yield (f"event: {evt['event']}\n"
                       f"data: {json.dumps(evt['data'], ensure_ascii=False)}\n\n")
        except Exception as e:  # noqa: BLE001  生成器内兜底，保证前端拿到 error
            yield ("event: error\n"
                   + "data: " + json.dumps(
                       {"message": f"{type(e).__name__}: {e}"},
                       ensure_ascii=False) + "\n\n")

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",   # 反向代理不缓冲
                 "Connection": "keep-alive"})


# ── 导入 PDF + 入库进度 ─────────────────────────────────────
# 设计：上传 → 后台 subprocess 跑 tools/ingest/ingest_paper.py（→ extract_hypergraph.py）。
# 走子进程而非线程：崩了不影响 litread；且复用现成 CLI 不重写逻辑。
# server 的只读连接（db()）完全不动，写由子进程自己开。
# 查重：ingest_paper.py 命中时退出码 2 → job 置 need_confirm，前端弹窗后走 .../confirm。

_IMPORT_DIR = pdfs_dir()          # 跟随 data_root（config.yaml）
_import_jobs: dict = {}          # job_id -> {job_id, stem, state, msg, started, log_tail}
_import_lock = threading.Lock()


def _new_job(stem: str) -> str:
    jid = f"{int(time.time())}-{stem[:40]}"
    with _import_lock:
        _import_jobs[jid] = {"job_id": jid, "stem": stem, "state": "queued",
                             "msg": "排队中", "started": time.time(), "log_tail": ""}
    return jid


def _run_ingest(jid: str, pdf: Path, stem: str, force: bool = False):
    """后台线程：先探模型能力，再按能力跑入库链，逐阶段回写 job 状态。

    入库前先探模型能力，能做什么就做到哪一步。
    能力不可达时不报错退出，而是跳过对应阶段并把状态记为 partial，
    之后可用面板的「补齐」补跑。
    """
    py, root = sys.executable, str(LITROOT)

    def setj(**kw):
        with _import_lock:
            _import_jobs[jid].update(**kw)

    try:
        from modules import model_health
        health = model_health.probe()
        cap = {k: bool(v.get("ok")) for k, v in health["capabilities"].items()}
        setj(capabilities=cap)
        llm_ok, emb_ok = cap.get("llm"), cap.get("embedding")
        skipped: list[str] = []

        # ── 1) 解析 / 规范化 / 索引 ──
        setj(state="running", msg="解析 / 规范化 / 索引（ingest_paper）")
        cmd = [py, str(LITROOT / "tools" / "ingest" / "ingest_paper.py"), str(pdf)]
        if force:
            cmd.append("--force")
        if not llm_ok:
            cmd.append("--no-llm")
            skipped.append("规范化（LLM 不可达，已回退清洗原文）")
        if not emb_ok:
            cmd.append("--no-embed")
            skipped.append("向量计算（embedding 不可达）")
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=3600)
        tail = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        with _import_lock:
            _import_jobs[jid]["log_tail"] = tail[-3000:]
        if r.returncode == 2:            # dedup_gate 拦截
            setj(state="need_confirm", msg="查重命中：疑似已存在，确认后强制入库")
            return
        if r.returncode != 0:
            setj(state="error", msg=f"ingest_paper 失败（退出码 {r.returncode}）")
            return

        # ── 2) 摘要（需 LLM）──
        if llm_ok:
            setj(msg="摘要生成（batch_summary）")
            rs = subprocess.run(
                [py, str(LITROOT / "tools" / "extract" / "batch_summary.py"),
                 "--stem", stem, "--workers", "1"],
                cwd=root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800)
            with _import_lock:
                _import_jobs[jid]["log_tail"] = (
                    (_import_jobs[jid].get("log_tail") or "") + "\n[summary]\n"
                    + (rs.stdout or "")[-1500:])[-4000:]
            if rs.returncode != 0:
                skipped.append(f"摘要（生成失败，退出码 {rs.returncode}）")
        else:
            skipped.append("摘要（LLM 不可达）")

        # ── 3) 实体 / 超边（需 LLM）──
        if llm_ok:
            setj(msg="实体 / 超边提取（extract_hypergraph）")
            r2 = subprocess.run([py, str(LITROOT / "tools" / "extract" / "extract_hypergraph.py"), stem],
                                cwd=root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=7200)
            with _import_lock:
                j = _import_jobs[jid]
                j["log_tail"] = ((j.get("log_tail") or "") + "\n" + (r2.stdout or ""))[-4000:]
            if r2.returncode != 0:
                skipped.append(f"实体/超边（失败，退出码 {r2.returncode}）")
        else:
            skipped.append("实体/超边（LLM 不可达）")

        # ── 3.5) 实体/超边入库：JSON → 数据库 ──
        # extract_hypergraph 只写 data/entities/<stem>.json，不写库；缺这步会出现
        # 阶段标记完成、而实体表中没有该论文记录的情况。
        if llm_ok:
            conn = db()
            try:
                row = conn.execute(
                    "SELECT id FROM papers WHERE file_path LIKE ? AND deleted_at IS NULL "
                    "ORDER BY id DESC LIMIT 1", (f"%{stem}%",)).fetchone()
                pid = row["id"] if row else 0
            finally:
                conn.close()
            if pid:
                setj(msg="实体 / 超边入库（import_hypergraph）")
                r3 = subprocess.run(
                    [py, str(LITROOT / "tools" / "graph" / "import_hypergraph.py"), stem,
                     "--paper-id", str(pid)],
                    cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=3600)
                with _import_lock:
                    j = _import_jobs[jid]
                    j["log_tail"] = ((j.get("log_tail") or "") + "\n[import]\n"
                                     + (r3.stdout or "")[-1500:])[-4000:]
                if r3.returncode != 0:
                    skipped.append(f"实体入库（失败，退出码 {r3.returncode}）")
            else:
                skipped.append("实体入库（找不到 paper_id）")

        # ── 4) 实体合并映射：全库增量，不在单篇内做 ──
        if not (cap.get("judge") and llm_ok):
            skipped.append("实体合并（决策模型不可达）")
        elif skipped:
            skipped.append("实体合并（建议用面板补齐一次性做全库增量）")

        if skipped:
            setj(state="partial",
                 msg="部分完成，跳过：" + "；".join(skipped), skipped=skipped)
        else:
            setj(state="done", msg="入库完成", skipped=[])
    except Exception as e:  # noqa: BLE001
        setj(state="error", msg=f"{type(e).__name__}: {e}")


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)):
    """上传 PDF → 存 data/pdfs/ → 起后台入库链。"""
    name = Path(file.filename or "upload.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 .pdf 文件")
    stem = Path(name).stem
    _IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    dst = _IMPORT_DIR / name
    body = await file.read()
    dst.write_bytes(body)
    jid = _new_job(stem)
    threading.Thread(target=_run_ingest, args=(jid, dst, stem), daemon=True).start()
    return {"job_id": jid, "stem": stem, "size": len(body),
            "saved": str(dst.relative_to(LITROOT))}


@app.post("/api/import/{jid}/confirm")
def api_import_confirm(jid: str):
    """查重命中后确认强制入库（重跑，带 --force）。"""
    with _import_lock:
        j = _import_jobs.get(jid)
    if not j:
        raise HTTPException(404, "job not found")
    pdf = _IMPORT_DIR / f"{j['stem']}.pdf"
    if not pdf.exists():
        raise HTTPException(404, f"PDF 已不在: {pdf.name}")
    jid2 = _new_job(j["stem"])
    threading.Thread(target=_run_ingest, args=(jid2, pdf, j["stem"], True), daemon=True).start()
    return {"job_id": jid2, "force": True}


@app.get("/api/ingest/jobs")
def api_ingest_jobs(limit: int = 20):
    """进行中 + 最近完成的导入任务（内存态，进程重启即清）。"""
    with _import_lock:
        jobs = sorted(_import_jobs.values(), key=lambda x: x.get("started", 0),
                      reverse=True)[:limit]
    return {"jobs": jobs}


@app.get("/api/ingest/status/{pid}")
def api_ingest_status(pid: int):
    """单篇入库状态（读 ingest_status 表 → 8 阶段进度）。"""
    from modules.ingest_status import progress_of
    conn = db()          # 复用 server 的只读连接
    try:
        row = conn.execute("SELECT * FROM ingest_status WHERE paper_id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, f"paper {pid} 无入库状态记录")
        return {"paper_id": pid, **progress_of(dict(row))}
    finally:
        conn.close()


# ===== 入库情况管理面板 =====
# 设计原则：**以数据库为准，产物目录只作为对账的另一侧**。
# 每个阶段同时看三样东西：
#   ① ingest_status 的阶段标记（流程声称做了什么）
#   ② papers 表里真正落地的数据列（数据库里到底有什么）
#   ③ 产物目录里的文件（文件系统上到底有什么）
# 三者对不上就是「状态绿、数据空」这类问题的入口。
# 只读端点 + 一个幂等修复端点（默认 dry-run，不调用 LLM、不花钱）。

_STAGE_DIR = {
    "extracted": extracted_dir,
    "normalized": normalized_dir,
    "summarized": summaries_dir,
}


def _dir_stems(fn, strip_raw: bool = False) -> set[str]:
    """目录下所有文件的 stem 集合；目录不存在返回空集。

    strip_raw：extracted 阶段的产物命名为 <stem>_raw.md，需剥掉 _raw 才能对着
    papers.file_path 的 stem。
    """
    try:
        d = Path(fn())
        if not d.exists():
            return set()
        out = set()
        for f in d.iterdir():
            if f.is_file() and not f.name.startswith("."):
                s = f.stem
                if strip_raw and s.endswith("_raw"):
                    s = s[:-4]
                out.add(s)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[ingest-overview] 读取目录失败 {fn.__name__}: {type(e).__name__}: {e}")
        return set()


def _ingest_snapshot():
    """一次取全量快照：论文行 + 状态行 + 四个目录 stem。"""
    from modules.ingest_status import STAGES
    conn = db()
    try:
        papers = [dict(r) for r in conn.execute(
            "SELECT id, title, year, journal, doi, file_path, summary, created_at "
            "FROM papers WHERE deleted_at IS NULL ORDER BY id")]
        status = {r["paper_id"]: dict(r) for r in conn.execute("SELECT * FROM ingest_status")}
    finally:
        conn.close()

    stems = {"pdfs": _dir_stems(pdfs_dir)}
    for k, fn in _STAGE_DIR.items():
        stems[k] = _dir_stems(fn, strip_raw=(k == "extracted"))
    # 阶段名叫 summarized，目录名叫 summaries —— 统一别名，避免各处取键不一致
    stems["summaries"] = stems["summarized"]

    by_stem = {}
    for p in papers:
        s = _stem_of(p.get("file_path"))
        if s:
            by_stem[s] = p
    return papers, status, stems, by_stem, STAGES


def _json_int_list(txt) -> list[int]:
    """解析 source_papers / source_ids 这类 JSON 整数数组文本；脏数据返回空列表。"""
    try:
        v = json.loads(txt or "[]")
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


@app.get("/api/ingest/overview")
def api_ingest_overview():
    """入库总览：每阶段的**数据库真实落地数**（标记只作对照）+ 三方对账 + 缺口计数。"""
    from modules.ingest_status import STAGE_LABELS
    papers, status, stems, by_stem, STAGES = _ingest_snapshot()
    total = len(papers)
    pid_set = {p["id"] for p in papers}

    conn = db()
    try:
        chunk_stat = {r[0]: (r[1] or 0, r[2] or 0) for r in conn.execute(
            "SELECT paper_id, COUNT(*), SUM(LENGTH(content)) FROM chunks GROUP BY paper_id")}
        ent_stat = {r[0]: r[1] for r in conn.execute(
            "SELECT paper_id, COUNT(*) FROM entities WHERE paper_id IS NOT NULL GROUP BY paper_id")}
        he_pids, cd_pids = set(), set()
        for r in conn.execute("SELECT source_papers FROM hyperedges"):
            he_pids.update(_json_int_list(r[0]))
        for r in conn.execute("SELECT source_papers FROM cross_doc_hyperedges"):
            cd_pids.update(_json_int_list(r[0]))
        # 实体映射：原始 entity.id 是否出现在 entities_merged.source_ids 里
        mapped_eids = set()
        for r in conn.execute("SELECT source_ids FROM entities_merged"):
            mapped_eids.update(_json_int_list(r[0]))
        paper_ent_ids = {}
        for r in conn.execute("SELECT id, paper_id FROM entities WHERE paper_id IS NOT NULL"):
            paper_ent_ids.setdefault(r[1], set()).add(r[0])
    finally:
        conn.close()

    def landed(s, p):
        """阶段 s 在数据库里是否真的落地（不用 ingest_status 标记）。"""
        pid = p["id"]
        stem = _stem_of(p.get("file_path"))
        if s == "extracted":
            return stem in stems["extracted"]
        if s == "normalized":
            return stem in stems["normalized"]
        if s == "metadata_done":
            # papers.year 是 INTEGER，统一 str() 后再 strip
            return (bool(str(p.get("title") or "").strip())
                    and bool(str(p.get("year") or "").strip())
                    and bool(str(p.get("journal") or "").strip()
                             or str(p.get("doi") or "").strip()))
        if s == "summarized":
            return bool((p.get("summary") or "").strip())
        if s == "indexed":
            n, chars = chunk_stat.get(pid, (0, 0))
            return n >= 1 and chars > 0
        if s == "entities_done":
            return ent_stat.get(pid, 0) >= 1
        if s == "hyperedges_done":
            return pid in he_pids or pid in cd_pids
        if s == "entities_mapped":
            return bool(paper_ent_ids.get(pid, set()) & mapped_eids)
        return False

    BASIS = {
        "extracted": "产物文件存在",
        "normalized": "产物文件存在",
        "metadata_done": "标题+年份+期刊或DOI",
        "summarized": "papers.summary 非空",
        "indexed": "chunks 有内容",
        "entities_done": "entities 表有该论文实体",
        "hyperedges_done": "超边 source_papers 含该论文",
        "entities_mapped": "实体已并入 entities_merged",
    }

    stages = []
    for s in STAGES:
        data_done = sum(1 for p in papers if landed(s, p))
        flag_done = sum(1 for p in papers if status.get(p["id"], {}).get(s))
        stages.append({
            "key": s, "label": STAGE_LABELS[s], "total": total,
            "data_done": data_done, "flag_done": flag_done,
            "consistent": data_done >= flag_done,
            "basis": BASIS.get(s, ""),
        })

    # ── 三方对账：以数据库为准 —— 「标记声称完成」必须被「数据真正落地」兑上。
    # 产物目录文件数只作旁证：它允许比论文多（历史遗留/已删论文的残留 = 孤儿文件），
    # 所以 ok 不看 files 等不等，而看 data >= flag 且 files 不少数据量。
    checks = []

    def add(name, flag, data, files, ok, detail):
        checks.append({"name": name, "flag": flag, "data": data, "files": files,
                       "ok": ok, "note": detail})

    sum_files = len(stems["summaries"])
    sum_data = sum(1 for p in papers if (p.get("summary") or "").strip())
    sum_flag = sum(1 for p in papers if status.get(p["id"], {}).get("summarized"))
    sum_match = sum(1 for p in papers if _stem_of(p.get("file_path")) in stems["summaries"])
    sum_ok = sum_data >= sum_flag
    add("摘要", sum_flag, sum_data, sum_files, sum_ok,
        "三方一致" if sum_ok else
        f"标记 {sum_flag} 条，摘要列有值 {sum_data} 条，差 {sum_flag - sum_data} 条未回填"
        + f"（摘要文件 {sum_files} 份，其中 {max(0, sum_files - sum_match)} 个孤儿）")

    ext_files = len(stems["extracted"])
    ext_data = sum(1 for p in papers if _stem_of(p.get("file_path")) in stems["extracted"])
    ext_flag = sum(1 for p in papers if status.get(p["id"], {}).get("extracted"))
    add("PDF 解析", ext_flag, ext_data, ext_files, ext_data >= ext_flag,
        f"{ext_data}/{ext_flag} 落地；产物文件 {ext_files} 份（含 {max(0, ext_files - ext_data)} 个孤儿）")

    nor_files = len(stems["normalized"])
    nor_data = sum(1 for p in papers if _stem_of(p.get("file_path")) in stems["normalized"])
    nor_flag = sum(1 for p in papers if status.get(p["id"], {}).get("normalized"))
    add("文本规范化", nor_flag, nor_data, nor_files, nor_data >= nor_flag,
        f"{nor_data}/{nor_flag} 落地；产物文件 {nor_files} 份（含 {max(0, nor_files - nor_data)} 个孤儿）")

    pdf_files = len(stems["pdfs"])
    add("PDF 原件", total, total, pdf_files, pdf_files >= total,
        f"论文 {total} 篇，PDF 原件 {pdf_files} 份"
        + ("" if pdf_files >= total else f"（差 {total - pdf_files} 份）"))

    # ── 缺口明细（用于 items 端点）──
    gaps = {}
    gaps["hypergraph_missing"] = [
        p["id"] for p in papers if p["id"] not in he_pids and p["id"] not in cd_pids]
    gaps["entity_missing"] = [
        p["id"] for p in papers if ent_stat.get(p["id"], 0) == 0]
    gaps["metadata_incomplete"] = [
        p["id"] for p in papers
        if not (str(p.get("title") or "").strip()
                and str(p.get("year") or "").strip()
                and (str(p.get("journal") or "").strip() or str(p.get("doi") or "").strip()))]
    gaps["summary_col_empty"] = [
        p["id"] for p in papers
        if _stem_of(p.get("file_path")) in stems["summaries"]
        and not (p.get("summary") or "").strip()]
    gaps["summary_file_missing"] = [
        p["id"] for p in papers
        if status.get(p["id"], {}).get("summarized")
        and _stem_of(p.get("file_path")) not in stems["summaries"]]
    gaps["extract_missing"] = [
        p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["extracted"]]
    gaps["normalize_missing"] = [
        p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["normalized"]]
    gaps["pdf_missing"] = [
        p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["pdfs"]]

    orphan = sorted(set(stems["summaries"]) - set(by_stem))

    return {
        "total_papers": total,
        "stages": stages,
        "checks": checks,
        "gaps": {k: len(v) for k, v in gaps.items()},
        "orphan_summary_files": len(orphan),
        "orphan_samples": orphan[:10],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


_ISSUE_LABEL = {
    "metadata_incomplete": "元数据不全（标题/年份/期刊或DOI）",
    "hypergraph_missing": "缺实体 / 超边",
    "entity_missing": "缺实体",
    "summary_col_empty": "有摘要文件但数据库列为空",
    "summary_file_missing": "标记已完成但摘要文件不存在",
    "extract_missing": "缺 PDF 解析产物",
    "normalize_missing": "缺规范化产物",
    "pdf_missing": "缺 PDF 原件",
}


@app.get("/api/ingest/items")
def api_ingest_items(issue: str = "summary_col_empty", page: int = 1,
                     size: int = 30, q: str = ""):
    """按缺口/阶段列出论文（管理用，分页）。"""
    papers, status, stems, by_stem, _ = _ingest_snapshot()
    pmap = {p["id"]: p for p in papers}

    if issue == "hypergraph_missing":
        _papers2, _st2, _stems2, _bs2, _ = _ingest_snapshot()
        c2 = db()
        try:
            he = set()
            for r in c2.execute("SELECT source_papers FROM hyperedges"):
                he.update(_json_int_list(r[0]))
            for r in c2.execute("SELECT source_papers FROM cross_doc_hyperedges"):
                he.update(_json_int_list(r[0]))
        finally:
            c2.close()
        ids = [p["id"] for p in papers if p["id"] not in he]
    elif issue == "entity_missing":
        c3 = db()
        try:
            ent = {r[0] for r in c3.execute(
                "SELECT DISTINCT paper_id FROM entities WHERE paper_id IS NOT NULL")}
        finally:
            c3.close()
        ids = [p["id"] for p in papers if p["id"] not in ent]
    elif issue == "metadata_incomplete":
        ids = [p["id"] for p in papers
               if not (str(p.get("title") or "").strip()
                       and str(p.get("year") or "").strip()
                       and (str(p.get("journal") or "").strip()
                            or str(p.get("doi") or "").strip()))]
    elif issue == "summary_col_empty":
        ids = [p["id"] for p in papers
               if _stem_of(p.get("file_path")) in stems["summaries"]
               and not (p.get("summary") or "").strip()]
    elif issue == "summary_file_missing":
        ids = [p["id"] for p in papers
               if status.get(p["id"], {}).get("summarized")
               and _stem_of(p.get("file_path")) not in stems["summaries"]]
    elif issue == "extract_missing":
        ids = [p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["extracted"]]
    elif issue == "normalize_missing":
        ids = [p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["normalized"]]
    elif issue == "pdf_missing":
        ids = [p["id"] for p in papers if _stem_of(p.get("file_path")) not in stems["pdfs"]]
    else:
        raise HTTPException(400, f"未知 issue: {issue}")

    if q:
        ql = q.lower()
        ids = [i for i in ids if ql in (pmap[i].get("title") or "").lower()]

    total = len(ids)
    page = max(1, page)
    size = max(1, min(200, size))
    chunk = ids[(page - 1) * size: page * size]
    items = [{
        "id": pmap[i]["id"], "title": pmap[i].get("title") or "",
        "year": pmap[i].get("year"), "journal": pmap[i].get("journal"),
        "stem": _stem_of(pmap[i].get("file_path")),
        "has_pdf": _stem_of(pmap[i].get("file_path")) in stems["pdfs"],
        "has_summary_file": _stem_of(pmap[i].get("file_path")) in stems["summaries"],
    } for i in chunk]

    return {"issue": issue, "issue_label": _ISSUE_LABEL.get(issue, issue),
            "total": total, "page": page, "size": size, "items": items}


@app.get("/api/models/health")
def api_models_health(refresh: bool = False):
    """三类模型能力探活（llm / embedding / judge）+ 各阶段可行性。

    refresh=true 跳过 30s 缓存强制重探（面板上的「重新检查」用）。
    """
    from modules import model_health
    return model_health.probe(refresh=refresh)


FILL_ACTIONS = {
    "summary":    {"stage": "summary",    "label": "摘要生成"},
    "hypergraph": {"stage": "hypergraph", "label": "实体 / 超边"},
    "embedding":  {"stage": "embedding",  "label": "向量计算"},
}


class FillIn(BaseModel):
    action: str
    dry_run: bool = False


def _run_fill(jid: str, action: str):
    """后台补齐：按能力跑对应脚本，结果写回 job。"""
    py, root = sys.executable, str(LITROOT)

    def setj(**kw):
        with _import_lock:
            _import_jobs[jid].update(**kw)

    try:
        if action == "hypergraph":
            conn = db()
            try:
                he = set()
                for r in conn.execute("SELECT source_papers FROM hyperedges"):
                    he.update(_json_int_list(r[0]))
                for r in conn.execute("SELECT source_papers FROM cross_doc_hyperedges"):
                    he.update(_json_int_list(r[0]))
                todo = [(_stem_of(r[0]), r[1]) for r in conn.execute(
                    "SELECT file_path, id FROM papers WHERE deleted_at IS NULL")
                    if r[1] not in he and _stem_of(r[0])]
            finally:
                conn.close()
            setj(state="running", msg=f"补齐超边：{len(todo)} 篇")
            ok = 0
            for stem, _pid in todo:
                rr = subprocess.run([py, str(LITROOT / "tools" / "extract" / "extract_hypergraph.py"), stem],
                                    cwd=root, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=3600)
                ok += 1 if rr.returncode == 0 else 0
            setj(state="done", msg=f"超边补齐完成：成功 {ok}/{len(todo)}")
            return

        if action == "summary":
            cmd = [py, str(LITROOT / "tools" / "extract" / "batch_summary.py"), "--workers", "2"]
        elif action == "embedding":
            cmd = [py, str(LITROOT / "tools" / "embed" / "embed_missing.py")]
        else:
            setj(state="error", msg=f"未知补齐动作 {action}")
            return

        setj(state="running", msg=f"补齐 {action}（{Path(cmd[1]).name}）")
        rr = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=7200)
        tail = ((rr.stdout or "") + (("\n[stderr]\n" + rr.stderr) if rr.stderr else ""))[-4000:]
        setj(state="done" if rr.returncode == 0 else "error",
             msg=("补齐完成" if rr.returncode == 0 else f"补齐失败（退出码 {rr.returncode}）"),
             log_tail=tail)
    except Exception as e:  # noqa: BLE001
        setj(state="error", msg=f"{type(e).__name__}: {e}")


@app.post("/api/ingest/fill")
def api_ingest_fill(body: FillIn):
    """按能力补齐：先探活，能力不够只报告不启动。"""
    from modules import model_health
    spec = FILL_ACTIONS.get(body.action)
    if not spec:
        raise HTTPException(400, f"未知补齐动作: {body.action}")
    ok, why = model_health.can(spec["stage"])
    if body.dry_run or not ok:
        return {"action": body.action, "label": spec["label"], "ok": ok,
                "reason": why, "dry_run": body.dry_run, "started": False}
    jid = _new_job(f"fill-{body.action}")
    threading.Thread(target=_run_fill, args=(jid, body.action), daemon=True).start()
    return {"action": body.action, "label": spec["label"], "ok": True, "reason": "",
            "dry_run": False, "started": True, "job_id": jid}


class RepairIn(BaseModel):
    action: str = "backfill_summary"
    dry_run: bool = True
    limit: int = 0          # 0 = 全部


@app.post("/api/ingest/repair")
def api_ingest_repair(body: RepairIn):
    """幂等修复：把 data/summaries/*.json 的 narrative 回填到 papers.summary。

    - 只填空值，覆盖已有值一律跳过（可重复执行）
    - dry_run=True 时只报告将改动多少条，不写库
    - 不调用任何 LLM，不产生费用
    """
    if body.action != "backfill_summary":
        raise HTTPException(400, f"未知修复动作: {body.action}")

    papers, status, stems, by_stem, _ = _ingest_snapshot()
    todo = []
    for p in papers:
        if (p.get("summary") or "").strip():
            continue
        stem = _stem_of(p.get("file_path"))
        if stem not in stems["summaries"]:
            continue
        f = Path(summaries_dir()) / f"{stem}.json"
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        text = (obj.get("narrative") or obj.get("summary") or "").strip()
        if text:
            todo.append((p["id"], text))
    if body.limit and body.limit > 0:
        todo = todo[: body.limit]

    if body.dry_run:
        return {"action": body.action, "dry_run": True, "would_update": len(todo),
                "samples": [{"id": i, "chars": len(t)} for i, t in todo[:10]]}

    conn = _wdb()
    try:
        n = 0
        for pid, text in todo:
            conn.execute(
                "UPDATE papers SET summary=? WHERE id=? AND (summary IS NULL OR TRIM(summary)='')",
                (text, pid))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return {"action": body.action, "dry_run": False, "updated": n}


# ── SPA 托管（必须放在最后：/api/* 之外的路径全部回落 index.html） ──

DIST = WEB_ROOT / "frontend" / "dist"

# 静态资源 MIME 表：Python mimetypes 不认 .mjs → 会回退 text/plain，
# 而浏览器对 <script type=module> 强制 MIME 检查 → pdf.worker.min-*.mjs 加载失败。
# 显式表优先，mimetypes 兜底。
_ASSET_MIME = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".map": "application/json",
    ".css": "text/css", ".json": "application/json", ".html": "text/html",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".wasm": "application/wasm", ".pdf": "application/pdf",
}


def _media_type(p: Path) -> str | None:
    return _ASSET_MIME.get(p.suffix.lower()) or mimetypes.guess_type(str(p))[0]


@app.get("/{full_path:path}")
def spa(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404, "unknown api")
    if not DIST.exists():
        return Response("litread 前端未构建：cd webapp/frontend && npm run build",
                        status_code=503, media_type="text/plain; charset=utf-8")
    target = (DIST / full_path).resolve()
    if full_path and target.is_file() and str(target).startswith(str(DIST.resolve())):
        return FileResponse(target, media_type=_media_type(target))
    return FileResponse(DIST / "index.html")
