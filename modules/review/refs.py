"""增量式引用表。

思考环节每次 ask_kb 时，kb_agent 返回的 citations 是「引用的 chunk/section」。
本模块把这些引用的**题录**（标题 + 前 3 作者 + 年份 + 期刊）增量登记到一张会话级
引用表（data/reviews/research/_kb_refs.json），跨多次 kb 调用、跨轮累积。

其它环节（写 / 改）**不需要知道论文的 doi**——引用表的题录就是唯一真相源，
只含：标题 / 前 3 作者 / 年份 / 期刊（+ paper_id 供定位）。

思考智能体上下文宝贵：ask_kb 只把「新增引用题录摘要」返回，不全量塞原文；
需要看某条原文时按 sid 从引用表展开（expand()）。
"""

import hashlib
import json
import re
from modules.review.paths import RESEARCH

REFS_FILE = RESEARCH / "_kb_refs.json"


def load() -> dict:
    if REFS_FILE.exists():
        return json.loads(REFS_FILE.read_text(encoding="utf-8"))
    return {}


def save(refs: dict):
    REFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REFS_FILE.write_text(json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")


def _authors_list(a) -> list:
    """把作者字段统一成前 3 作者列表：兼容 list（chunk/section）与逗号分隔字符串（paper_detail）。"""
    if isinstance(a, list):
        return [str(x).strip() for x in a if x and str(x).strip()][:3]
    if isinstance(a, str) and a.strip():
        return [x.strip() for x in re.split(r"\s*[,，]\s*", a) if x.strip()][:3]
    return []


def _normalize_meta(c: dict) -> dict:
    """把一条 citation 折成引用表条目：题录（标题+前3作者+年份+期刊）+ 原文（供按需展开）。"""
    m = c.get("metadata") or {}
    ctype = c.get("type") or ""
    # paper_title（chunk/section）或 title（paper_detail）；paper_id（chunk/section）或 id（paper_detail）
    pid = m.get("paper_id") or m.get("id")
    # 只有指向单篇论文的引用才有完整题录（chunk / section_hit / section / paper_detail）
    has_biblio = ctype in ("chunk", "section_hit", "section", "paper_detail") and pid is not None
    rec = {
        "type": ctype,
        "paper_id": pid,
        "title": (m.get("paper_title") or m.get("title")) if has_biblio else None,
        "authors": _authors_list(m.get("authors")) if has_biblio else [],   # 只取前 3 作者
        "year": m.get("year"),
        "journal": m.get("journal"),
        # 非论文类引用（hyperedge/sql/papers_list）无单篇题录，用 label 兜底，不参与正式参考文献
        "label": m.get("label") or m.get("sql") or (m.get("paper_title") or m.get("title") or ""),
        "content": c.get("content") or "",   # 原文，供按需展开
    }
    if ctype == "hyperedge":   # 超边保留跨文献溯源字段（参与论文列表是追溯表的核心）
        rec["participants"] = m.get("participants") or []
        rec["source_papers"] = m.get("source_papers") or []
        rec["edge_id"] = m.get("edge_id")
        rec["scope"] = m.get("scope")
    return rec


def _content_key(rec: dict) -> str:
    """跨查询去重键：同一片段（论文/章节/超边）在任意一次 kb 调用中内容一致。"""
    base = json.dumps({k: rec.get(k) for k in
                       ("type", "paper_id", "label", "title", "year")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.md5((base + (rec.get("content") or "")[:300]).encode("utf-8")).hexdigest()


def _next_sid(refs: dict, prefix: str = "S") -> str:
    n = 0
    for k in refs:
        if k.startswith(prefix) and k[1:].isdigit():
            n = max(n, int(k[1:]))
    return f"{prefix}{n + 1}"


def register_citations(refs: dict, citations: list, file_name: str) -> dict:
    """把本次 citations 登记进全局引用表，返回 **局部编号 → 全局编号** 映射。

    关键：kb_agent 每次调用都从 S1 重新编号，若直接把局部
    编号当全局键，后一次查询的 [S14] 会因「已存在」被跳过，而正文里的 [S14] 实际
    指向另一篇论文 —— 造成引用错位。本函数按内容去重、必要时分配新的全局编号，
    调用方（kb_qa）据此把回答文本里的局部 [S#] 重写成全局 [S#]。
    """
    index = {}
    for sid, rec in refs.items():
        index.setdefault(_content_key(rec), sid)
    mapping = {}
    for c in citations or []:
        lid = c.get("id")
        if not lid:
            continue
        rec = _normalize_meta(c)
        key = _content_key(rec)
        if key in index:
            mapping[lid] = index[key]
            continue
        prefix = "E" if str(lid).startswith("E") else "S"
        gid = _next_sid(refs, prefix)
        rec["file"] = file_name
        refs[gid] = rec
        index[key] = gid
        mapping[lid] = gid
    return mapping


def brief_of(rec: dict) -> str:
    """题录摘要（不含原文、不含 doi）——思考智能体 / 撰写环节看到的最小形式。"""
    a = "；".join(rec.get("authors") or [])
    head = f"[{rec.get('type')}]"
    if rec.get("title"):
        head += f" {rec['title']}"
    if rec.get("year"):
        head += f" ({rec['year']})"
    if rec.get("journal"):
        head += f" {rec['journal']}"
    if a:
        head += f" — {a}"
    elif rec.get("label"):
        head += f" ({rec['label']})"
    return head


def render_brief_list(refs: dict, sids: list) -> str:
    """增量引用摘要（每条一行，无原文）。"""
    if not sids:
        return "（无新增引用）"
    lines = []
    for sid in sids:
        rec = refs.get(sid)
        if rec:
            lines.append(f"- {sid} {brief_of(rec)}")
    return "\n".join(lines)


def expand(refs: dict, sids: list, max_chars: int = 6000) -> str:
    """按 sid 展开原文（题录头 + 全文截断），供思考智能体按需查看。"""
    parts = []
    for sid in sids or []:
        rec = refs.get(sid)
        if not rec:
            parts.append(f"### {sid}（引用表无此条目）")
            continue
        parts.append(f"### {sid} {brief_of(rec)}\n\n> {rec.get('content', '')[:max_chars]}")
    return "\n\n---\n\n".join(parts)


def build_refs_pack(refs: dict, sids: list) -> str:
    """引用追溯表（撰写环节输入）——溯源映射，不含证据原文。

    结构（两个方向）：
    - 论文 → 其下证据片段 [S#]（编号+类型+label；原文按需到 research/_kb_r*.md 下钻）
    - 超边 [E#] → 跨文献关联描述（含数值）+ 参与论文列表
    """
    papers, edges, unknown = {}, [], []
    order = []
    for sid in sids:
        rec = refs.get(sid)
        if not rec:
            unknown.append(sid)
            continue
        if rec.get("type") == "hyperedge":   # 兼容：超边记录也可能挂在 S# 编号下
            edges.append((sid, rec))
            continue
        pid = rec.get("paper_id")
        key = pid if pid is not None else f"独立_{sid}"
        if key not in papers:
            papers[key] = {"rec": rec, "items": []}
            order.append(key)
        papers[key]["items"].append((sid, rec))

    # paper_id → 题录索引（供超边参与论文借题录）
    paper_meta = {}
    for rec in refs.values():
        pid = rec.get("paper_id")
        if pid is not None and rec.get("title") and pid not in paper_meta:
            paper_meta[pid] = rec

    lines = ["# 引用追溯表", "",
             "> [S#] = 论文下的证据片段（章节/分块），出处即其所属论文；"
             "[E#] = 跨文献超边，描述多篇论文的关联（含数值），引用时对应「参与论文」列表。"
             "证据原文不在本表，需要核对时按编号到 research/_kb_r*.md 下钻。"
             "参考文献按论文级归并：同一论文多个 [S#] 合并一条题录；[E#] 的参与论文全部进文献表。", ""]

    for key in order:
        meta = papers[key]["rec"]
        authors = "，".join((meta.get("authors") or [])[:3]) or "（作者不详）"
        lines.append(f"## 论文 P{key}：{meta.get('title') or '（无题）'}")
        lines.append(f"作者：{authors} ｜ 年份：{meta.get('year') or '?'} ｜ 期刊：{meta.get('journal') or '?'}")
        lines.append("- " + " ".join(f"[{sid}]" for sid, _ in papers[key]["items"]))
        lines.append("")

    if edges:
        lines += ["## 超边（跨文献证据）", ""]
        for sid, rec in edges:
            lines.append(f"### [{sid}]")
            lines.append(str(rec.get("content") or "（无描述）"))
            sp = rec.get("source_papers") or []
            if sp:
                lines.append("参与论文：" + "，".join(_paper_ref(p, paper_meta) for p in sp))
            lines.append("")
    if unknown:
        lines += ["## ⚠️ 引用表中不存在的编号", "，".join(unknown), ""]
    return "\n".join(lines)


def _paper_ref(pid, paper_meta: dict) -> str:
    rec = paper_meta.get(pid)
    if rec:
        a = (rec.get("authors") or ["?"])[0]
        return f"P{pid}（{a} et al. {rec.get('year') or '?'}）"
    return f"P{pid}"


def build_evidence_pack(refs: dict, sids: list, per_sid_chars: int = 600) -> str:
    """引用证据包（撰写环节/审核/硬断言共用）——论文 → 其下 [S#] → **原文片段**。

    与 build_refs_pack 的区别：本函数**含证据原文**（每条截断 per_sid_chars 字）：
    正文里的数值需要逐字核对，只给题录+S#索引的包无法支持这一动作。
    只收传入的 sids（初稿实际引用），体量可控。
    """
    papers, edges, unknown = {}, [], []
    order = []
    for sid in sids:
        rec = refs.get(sid)
        if not rec:
            unknown.append(sid)
            continue
        if rec.get("type") == "hyperedge":
            edges.append((sid, rec))
            continue
        pid = rec.get("paper_id")
        key = pid if pid is not None else f"独立_{sid}"
        if key not in papers:
            papers[key] = {"rec": rec, "items": []}
            order.append(key)
        papers[key]["items"].append((sid, rec))

    paper_meta = {}
    for rec in refs.values():
        pid = rec.get("paper_id")
        if pid is not None and rec.get("title") and pid not in paper_meta:
            paper_meta[pid] = rec

    lines = ["# 引用证据包", "",
             "> 每条 [S#] = 论文下的证据片段**原文**（截断）。"
             "正文里的数值/结论必须能在本文件中逐字找到；找不到就是无源，"
             "要么补证据，要么降级措辞（如「据 X 综述」）。", ""]

    for key in order:
        meta = papers[key]["rec"]
        authors = "，".join((meta.get("authors") or [])[:3]) or "（作者不详）"
        lines.append(f"## 论文 P{key}：{meta.get('title') or '（无题）'}")
        lines.append(f"作者：{authors} ｜ 年份：{meta.get('year') or '?'} ｜ 期刊：{meta.get('journal') or '?'}")
        lines.append("")
        for sid, rec in papers[key]["items"]:
            body = (rec.get("content") or "（无原文）").strip()
            if len(body) > per_sid_chars:
                body = body[:per_sid_chars] + " …（截断）"
            lines.append(f"### [{sid}]")
            lines.append(body)
            lines.append("")

    if edges:
        lines += ["## 超边（跨文献证据，含数值）", ""]
        for sid, rec in edges:
            body = (rec.get("content") or "（无描述）").strip()
            if len(body) > per_sid_chars:
                body = body[:per_sid_chars] + " …（截断）"
            lines.append(f"### [{sid}]")
            lines.append(body)
            sp = rec.get("source_papers") or []
            if sp:
                lines.append("参与论文：" + "，".join(_paper_ref(p, paper_meta) for p in sp))
            lines.append("")

    if unknown:
        lines += ["## ⚠️ 引用表中不存在的编号", "，".join(unknown), ""]
    return "\n".join(lines)


def biblio_lines(refs: dict, cited_ids: list) -> list:
    """最终参考文献条目（机械汇总，不含 doi）——按正文实际引用顺序，同一论文多个 [S#]/[E#] 合并为一条。

    超边 [E#] 展开其 source_papers 参与论文；参与论文无独立题录时借用 refs 内同 paper_id 的题录。
    """
    merged, order = {}, {}
    paper_meta = {}
    orphans = []
    for rec in refs.values():
        pid = rec.get("paper_id")
        if pid is not None and rec.get("title") and pid not in paper_meta:
            paper_meta[pid] = rec
    for sid in cited_ids:
        rec = refs.get(sid)
        if not rec:
            continue
        if sid.startswith("E") or rec.get("type") == "hyperedge":
            sp = rec.get("source_papers") or []
            if not sp:
                orphans.append(sid)
                continue
            for pid in sp:
                key = pid
                if key not in merged:
                    merged[key] = {"ids": [], "rec": paper_meta.get(pid) or {}}
                    order[key] = len(order)
                merged[key]["ids"].append(sid)
            continue
        if not rec.get("title"):
            continue
        pid = rec.get("paper_id")
        key = pid if pid is not None else sid
        if key not in merged:
            merged[key] = {"ids": [], "rec": rec}
            order[key] = len(order)
        merged[key]["ids"].append(sid)
    lines = []
    for key in sorted(order, key=order.get):
        entry = merged[key]
        rec = entry["rec"]
        ids = "".join(f"[{i}]" for i in entry["ids"])
        if not rec.get("title"):
            lines.append(f"{ids} （题录缺失：paper_id={key}，仅经超边引用）")
            continue
        a = "，".join(rec.get("authors") or [])
        year = f"（{rec['year']}）" if rec.get("year") else ""
        journal = f"《{rec['journal']}》" if rec.get("journal") else ""
        lines.append(f"{ids} {rec['title']} {year}{journal}" + (f" — {a}" if a else ""))
    if orphans:
        lines.append(f"（未归并的旧编号超边引用：{'，'.join(orphans)}）")
    return lines
