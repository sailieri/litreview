# -*- coding: utf-8 -*-
"""入库查重闸门（dedup gate）—— litreview「先 json 后 db」两条线的判据核心。

问题：入库路径若不做查重，同一篇论文经不同入口进来会各建一行。本模块把查重做成
两条线都能调用的闸门：

  ① 元数据线：normalizer.extract_metadata() 调 LLM 之前调用（免费，省一次提取）
  ② 数据库线：indexer.index_paper() INSERT papers 之前调用（第二道保险）

命中默认抛 DuplicateError 阻断导入并返回可读报告；调用方人工核对确认是查重误判
（标题改写 / 中译版 / 元数据错误）时用 force=True 放行，放行会写一行审计到
data/dedup_forced.jsonl，事后可统计误判率。

判据分级：
  doi         规范化 DOI 完全相同                    conf=1.00
  raw_md5     extracted/*_raw.md 字节级相同           conf=1.00（同一 PDF 换文件名，零误判）
  stem        文件名 stem 已在库 / 已有元数据          conf=1.00（幂等提示，非"同一篇"）
  title_exact 规范化标题完全相同                      conf=0.99
  title_fuzzy 标题相似度 ≥0.95（无需作者佐证）        conf=0.95
  title_auth  标题相似度 ≥0.90 且作者有交集           conf=0.93
  title_low   标题相似度 0.85–0.95（无作者佐证）      conf=0.85，报告标"低置信"

能力边界：中译版 vs 英文原版的标题跨语言相似度≈0，只能靠 DOI 或 raw_md5 抓。
因此 DOI 提取失败时，raw 文本指纹是最后兜底——
字节相同的 PDF 副本（换文件名）只能靠 md5 比对发现。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from modules.paths import data_root, db_path, extracted_dir, metadata_dir  # noqa: E402

from .deduplicate import PLACEHOLDER_TITLES  # noqa: E402  单一真相源（normalizer 也依赖它）

# ── 判据阈值 ──────────────────────────────────────────────
TH_FUZZY_HARD = 0.95   # ≥ 此值：无作者佐证也阻断
TH_FUZZY_AUTH = 0.90   # ≥ 此值 + 作者交集：阻断
TH_FUZZY_LOW = 0.85    # ≥ 此值无佐证：阻断但标低置信

CONF = {
    "doi": 1.00, "raw_md5": 1.00, "stem": 1.00,
    "title_exact": 0.99, "title_fuzzy": 0.95,
    "title_auth": 0.93, "title_low": 0.85,
}


@dataclass
class DupHit:
    """一条查重命中。level 恒为 block（从严全阻断；误判由 force 放行）。"""
    match_type: str        # doi / raw_md5 / stem / title_exact / title_fuzzy / title_auth / title_low
    confidence: float
    source: str            # papers（DB 表）/ metadata（data/metadata/*.json）
    matched_id: int | None  # papers.id，metadata 来源为 None
    matched_stem: str
    matched_title: str
    matched_doi: str
    similarity: float      # 标题相似度（非标题类判据为 0.0）
    evidence: str          # 人可读判据说明
    level: str = "block"


class DuplicateError(RuntimeError):
    """查重命中。调用方捕获后打印 str(e) 并退出；force 路径不会抛它。"""

    def __init__(self, hits: list[DupHit], context: str = "", current: dict | None = None):
        self.hits = hits
        self.context = context
        self.current = current or {}
        super().__init__(format_report(hits, context, current))


# ── 规范化 ────────────────────────────────────────────────

def norm_title(title) -> str | None:
    """规范化标题作比对键；占位/空返回 None（不可作键）。"""
    if not title:
        return None
    t = str(title).strip().lower()
    if t in PLACEHOLDER_TITLES:
        return None
    t = re.sub(r"[^\w一-鿿]+", "", t)  # 去标点，保留中英文数字
    t = re.sub(r"\s+", "", t)
    return t or None


def norm_doi(doi) -> str | None:
    """规范化 DOI：小写、剥 https://doi.org/ 前缀与尾部标点。"""
    if not doi:
        return None
    d = str(doi).strip().lower().rstrip(".;,")
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d or None


def norm_authors(authors) -> set[str]:
    """作者名集合（兼容 str / list），用于交集佐证。"""
    if not authors:
        return set()
    if isinstance(authors, (list, tuple)):
        parts = [str(a) for a in authors]
    else:
        parts = re.split(r"[,;，、]|\band\b", str(authors))
    return {p.strip().lower() for p in parts if len(p.strip()) > 1}


def stem_of(file_path: str) -> str:
    """从 papers.file_path 取 stem（兼容 .md / .pdf 与 Windows 反斜杠）。"""
    base = (file_path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for suf in (".md", ".pdf"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def heuristic_title(raw_text: str) -> str | None:
    """从 raw 文本头部免费取标题（LLM 前预查用）。取不到/太短返回 None。"""
    for line in raw_text.splitlines()[:40]:
        s = line.strip().lstrip("#").strip()
        if not s or s.startswith("---"):
            continue
        low = s.lower()
        if low.startswith(("abstract", "keywords", "introduction", "doi:", "http")):
            continue
        if len(s) < 15 or len(s) > 200:
            continue
        if not re.search(r"[A-Za-z一-鿿]", s):
            continue
        nt = norm_title(s)
        if not nt or len(nt) < 12:  # 规范化后太短（如刊名/页码）不作比对键
            continue
        return s
    return None


def first_doi(raw_text: str) -> str | None:
    """取 raw 文本首页（前 4000 字符）第一个 DOI——出版商首页通常印本文 DOI。"""
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9<>]+", raw_text[:4000])
    return m.group(0).rstrip(".,;)") if m else None


# ── 候选来源（带缓存，目录 mtime 失效）────────────────────

_cache: dict[str, tuple, object] = {}


def _cached(key: str, path: Path, builder):
    mtime = path.stat().st_mtime if path.exists() else -1
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    val = builder()
    _cache[key] = (mtime, val)
    return val


def _load_papers() -> list[dict]:
    """已有论文清单（查重用）。首次建库前库文件还不存在 → 视为空库。"""
    import sqlite3
    if not db_path().exists():          # 干净环境第一次入库：还没有 papers 表
        return []
    conn = sqlite3.connect(f"file:{str(db_path()).replace(chr(92), '/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(source="papers", id=r["id"], title=r["title"] or "", doi=r["doi"] or "",
                 authors=r["authors"] or "", year=r["year"],
                 stem=stem_of(r["file_path"] or ""))
            for r in conn.execute(
                "SELECT id, title, doi, authors, year, file_path FROM papers")
        ]
    except sqlite3.OperationalError:     # 还没建表 = 空库
        rows = []
    finally:
        conn.close()
    return rows


def _load_metadata() -> list[dict]:
    def build():
        out = []
        for f in sorted(metadata_dir().glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append(dict(source="metadata", id=None,
                            title=d.get("title") or "", doi=d.get("doi") or "",
                            authors=d.get("authors") or "", year=d.get("year"),
                            stem=f.stem))
        return out
    return _cached("metadata", metadata_dir(), build)


def _load_raw_fingerprints() -> dict[str, list[str]]:
    """{md5hex: [stem, ...]} —— extracted/*_raw.md 字节指纹（同 PDF 必同 md5）。
    同一 md5 可对应多个文件名，必须存列表，否则候选会被自身顶掉。"""
    def build():
        fps: dict[str, list[str]] = {}
        d = extracted_dir()
        for f in d.glob("*_raw.md"):
            try:
                h = hashlib.md5(f.read_bytes()).hexdigest()
            except OSError:
                continue
            fps.setdefault(h, []).append(f.name[: -len("_raw.md")])
        return fps
    return _cached("raw_fp", extracted_dir(), build)


# ── 查重主函数 ────────────────────────────────────────────

def check_duplicate(
    title=None,
    doi=None,
    stem=None,
    authors="",
    raw_text=None,
    *,
    sources=("papers", "metadata"),
    exclude_stem: str | None = None,
    exclude_paper_id: int | None = None,
    use_fingerprint: bool = True,
) -> list[DupHit]:
    """查重。返回命中列表（空 = 无重复，可安全导入）；排序按置信度降序。

    Args:
        title/doi/authors: 待导入论文的元数据（可缺，缺则跳过相应判据）
        stem: 待导入文件的 stem（用于同名幂等判定与指纹定位）
        raw_text: 已提取的原始文本（无 stem 文件时用它算指纹）
        sources: 查哪些源，papers=库表，metadata=data/metadata/*.json（双保险）
        exclude_stem: 仅豁免 metadata 源里同 stem 的行（调用方刚写下的自己那份 json，
            防自命中）；papers 表同名行不豁免——那是“已入库”的幂等拦截，必须拦。
        exclude_paper_id: 更新既有行时传自己的 papers.id
        use_fingerprint: 是否做 extracted/*_raw.md 字节指纹比对（同 PDF 换名兜底）
    """
    hits: list[DupHit] = []
    n_title = norm_title(title)
    n_doi = norm_doi(doi)
    a_set = norm_authors(authors)

    cands: list[dict] = []
    if "papers" in sources:
        cands += _load_papers()
    if "metadata" in sources:
        cands += _load_metadata()

    def add(match_type, source, c, sim, evidence, mid=None, mstem="", mtitle="", mdoi=""):
        hits.append(DupHit(
            match_type=match_type, confidence=CONF[match_type], source=source,
            matched_id=mid, matched_stem=mstem, matched_title=mtitle,
            matched_doi=mdoi, similarity=round(sim, 4), evidence=evidence))

    for c in cands:
        if exclude_paper_id is not None and c.get("id") == exclude_paper_id:
            continue
        if exclude_stem and c["source"] == "metadata" and c["stem"] == exclude_stem:
            continue  # 只豁免自己刚写的 json；papers 同名行照拦（幂等）
        c_doi = norm_doi(c["doi"])
        if n_doi and c_doi and n_doi == c_doi:
            add("doi", c["source"], c, 1.0,
                f"DOI 完全相同：{n_doi}",
                mid=c["id"], mstem=c["stem"], mtitle=c["title"], mdoi=c["doi"])
            continue  # DOI 铁证，无需再比标题
        if stem and c["stem"] and c["stem"] == stem:
            add("stem", c["source"], c, 1.0,
                f"同名文件已存在（stem={stem}）—— 幂等提示，非内容判重",
                mid=c["id"], mstem=c["stem"], mtitle=c["title"], mdoi=c["doi"])
            continue
        c_title = norm_title(c["title"])
        if not n_title or not c_title:
            continue
        if n_title == c_title:
            add("title_exact", c["source"], c, 1.0,
                "规范化标题完全相同", mid=c["id"], mstem=c["stem"],
                mtitle=c["title"], mdoi=c["doi"])
            continue
        sim = difflib.SequenceMatcher(None, n_title, c_title).ratio()
        if sim >= TH_FUZZY_HARD:
            add("title_fuzzy", c["source"], c, sim,
                f"标题相似度 {sim:.3f} ≥ {TH_FUZZY_HARD}",
                mid=c["id"], mstem=c["stem"], mtitle=c["title"], mdoi=c["doi"])
        elif sim >= TH_FUZZY_AUTH and (a_set & norm_authors(c["authors"])):
            add("title_auth", c["source"], c, sim,
                f"标题相似度 {sim:.3f} ≥ {TH_FUZZY_AUTH} 且作者有交集",
                mid=c["id"], mstem=c["stem"], mtitle=c["title"], mdoi=c["doi"])
        elif sim >= TH_FUZZY_LOW:
            add("title_low", c["source"], c, sim,
                f"标题相似度 {sim:.3f}（低置信，无作者佐证，建议人工核对）",
                mid=c["id"], mstem=c["stem"], mtitle=c["title"], mdoi=c["doi"])

    # ── raw 文本指纹：同一 PDF 换文件名（DOI/标题都失真时的最后兜底）──
    if use_fingerprint:
        cur_md5, cur_file = None, None
        if stem:
            p = extracted_dir() / f"{stem}_raw.md"
            if p.exists():
                cur_md5, cur_file = hashlib.md5(p.read_bytes()).hexdigest(), p.name
        if cur_md5 is None and raw_text:
            cur_md5 = hashlib.md5(str(raw_text).encode("utf-8")).hexdigest()
            cur_file = f"{stem or 'raw_text'}_raw.md"
        if cur_md5:
            twins = _load_raw_fingerprints().get(cur_md5, [])
            other = next((s for s in twins if s != stem and s != exclude_stem), None)
            if other:
                twin = next((c for c in cands if c["stem"] == other), None)
                add("raw_md5", (twin or {}).get("source", "extracted"),
                    twin or {}, 1.0,
                    f"raw 提取文本字节级相同（md5={cur_md5[:12]}）→ {other}_raw.md；"
                    "同一 PDF 换了文件名",
                    mid=(twin or {}).get("id"), mstem=other,
                    mtitle=(twin or {}).get("title", ""),
                    mdoi=(twin or {}).get("doi", ""))

    # 同一目标只保留置信度最高的一条，按置信度/相似度降序
    best: dict[tuple, DupHit] = {}
    for h in hits:
        key = (h.source, h.matched_id, h.matched_stem)
        if key not in best or h.confidence > best[key].confidence:
            best[key] = h
    ordered = sorted(best.values(), key=lambda h: (-h.confidence, -h.similarity))
    if len(ordered) > 10:  # 防爆：同一篇泛标题可能命中多个候选，报告只留最相关的
        ordered = ordered[:10]
    return ordered


# ── 报告与审计 ────────────────────────────────────────────

def format_report(hits: list[DupHit], context: str = "", current: dict | None = None) -> str:
    """人可读拦截报告（也是 DuplicateError 的消息体）。"""
    cur = current or {}
    L = []
    L.append("=" * 68)
    L.append("!!  查重拦截：疑似重复论文，已停止导入  !!")
    L.append("=" * 68)
    if context:
        L.append(f"位置   : {context}")
    if cur.get("title"):
        L.append(f"当前标题: {cur['title']}")
    if cur.get("doi"):
        L.append(f"当前 DOI: {cur['doi']}")
    if cur.get("stem"):
        L.append(f"当前文件: {cur['stem']}")
    L.append(f"命中 {len(hits)} 条：")
    for i, h in enumerate(hits, 1):
        where = f"{h.source}"
        if h.matched_id is not None:
            where += f" #{h.matched_id}"
        if h.matched_stem:
            where += f" ({h.matched_stem})"
        L.append(f"  {i}. [{h.match_type}] 置信 {h.confidence:.2f} → {where}")
        L.append(f"     判据: {h.evidence}")
        if h.matched_title:
            L.append(f"     已有: {h.matched_title[:90]}")
        if h.matched_doi:
            L.append(f"     DOI : {h.matched_doi}")
    L.append("-" * 68)
    L.append("处置（二选一）：")
    L.append("  A) 确实是同一篇 → 不要导入；如需清理重复可跑:")
    L.append("       python modules/util/deduplicate.py --scan-db")
    L.append("  B) 确认是查重误判（标题改写 / 中译版 / 元数据错误 / 不同论文撞名）")
    L.append("     → 人工核对后加 --force 继续导入（force 会写审计 data/dedup_forced.jsonl）")
    return "\n".join(L)


def audit_forced(context: str, current: dict | None = None,
                 hits: list[DupHit] | None = None, reason: str = "") -> Path:
    """force 放行时落一行审计（追加 JSONL），事后可统计误判率。"""
    cur = current or {}
    rec = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "context": context,
        "title": cur.get("title"),
        "doi": cur.get("doi"),
        "stem": cur.get("stem"),
        "reason": reason,
        "hits": [asdict(h) for h in (hits or [])],
    }
    path = data_root() / "dedup_forced.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


# ── 全库体检（CLI --scan-db 用）──────────────────────────

def find_db_duplicates(fuzzy_threshold: float = 0.90) -> list[list[dict]]:
    """papers 表内两两比对：DOI 相同 / 规范化标题相同 / 相似度≥阈值 → 返回重复组。"""
    rows = _load_papers()
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_doi: dict[str, list] = {}
    by_title: dict[str, list] = {}
    for r in rows:
        d = norm_doi(r["doi"])
        if d:
            by_doi.setdefault(d, []).append(r)
        t = norm_title(r["title"])
        if t:
            by_title.setdefault(t, []).append(r)
    for g in by_doi.values():
        for other in g[1:]:
            union(g[0]["id"], other["id"])
    for g in by_title.values():
        for other in g[1:]:
            union(g[0]["id"], other["id"])

    # 模糊：规范化标题长度 ≥12 才参与两两比对（避免短泛标题误报）
    cand = [r for r in rows if len(norm_title(r["title"]) or "") >= 12]
    for i, a in enumerate(cand):
        ta = norm_title(a["title"])
        for b in cand[i + 1:]:
            if find(a["id"]) == find(b["id"]):
                continue
            tb = norm_title(b["title"])
            if abs(len(ta) - len(tb)) > max(len(ta), len(tb)) * 0.3:
                continue  # 长度差过大先跳过，省算力
            if difflib.SequenceMatcher(None, ta, tb).ratio() >= fuzzy_threshold:
                union(a["id"], b["id"])

    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)
    return [g for g in groups.values() if len(g) > 1]
