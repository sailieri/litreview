# -*- coding: utf-8 -*-
"""缺口断言全库核实闸门（gap audit）。

问题：
    审核 p0=0、converged=true，也挡不住「尚无 / 缺失 / 空白」这类断言写错——
    主智能体凭先验写断言、只按"我要写什么"去查库，审稿团队又是纯文本 LLM
    没有库访问权，于是断言在无人核实的维度上"收敛"。

本模块把那件事变成管线里不可绕过的动作：
    ① 从稿子里抽出全部缺口断言
    ② LLM 转英文检索式 → `retriever.gap_check` 论文级全库召回
    ③ LLM 判定：库内是否已有**直接回答/推翻**该断言的文献
    ④ 命中的库内文献**登记成全局引用编号**（S#），使稿子"想引就能引"
    ⑤ 落盘报告；`main_agent.run_review` 见到"已有直接文献"未消化就拒绝送审

判定语义：
    - 已有直接文献 → 断言与库内证据冲突，**阻塞送审**（必须改写或引用）
    - 部分相关     → 不阻塞，但报告里给出收窄建议
    - 确实未见     → 断言成立（在"已核本库 N 篇"的口径下）
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from modules.database import retriever as R                          # noqa: E402
from modules.review import refs as refs_mod                          # noqa: E402
from modules.review.paths import REV, RESEARCH                       # noqa: E402
from modules.util.jsonllm import loads_llm                           # noqa: E402
from modules.util.llm_client import call_llm, get_step_config         # noqa: E402

CACHE = REV / "meta" / "_gap_audit_cache.json"
BATCH_Q_PER = 12        # 每次 LLM 调用翻译多少条断言（分批，防输出被截断）
BATCH_J_PER = 12

CLAIM_RE = re.compile(
    r"[^。；\n]{0,60}(?:尚无|尚未|未见报道|尚未见|缺失|空白|几乎空白)[^。；\n]{0,60}")

BATCH_Q = """下面是中文综述里的多条「研究缺口」断言，编号 {ids}。

请为**每一条**给出 2 条英文检索式（关键词串，不是问句），覆盖核心概念与同义词/上下位词。
只输出 JSON：{{"<编号>": ["...", "..."], ...}}

{claims}"""

BATCH_J = """下面是一个文献库的核实任务。对每条断言，已给出库内检索到的候选论文（id/年份/标题/片段）。

请严格判断每条断言：库里是否已有**直接回答或直接推翻**它的文献？
- "已有直接文献"：某候选明确做了断言所说"没有 / 缺失"的那件事（→ 断言站不住）
- "部分相关"：同主题但尺度/对象/口径不等价（→ 断言需要收窄口径）
- "确实未见"：候选都不相关（→ 断言在当前库范围内成立）

只输出 JSON 数组：
[{{"i": <编号>, "verdict": "已有直接文献|部分相关|确实未见",
   "direct_papers": [候选 id 整数...], "why": "一句话依据",
   "rewrite": "若已有直接文献/部分相关，给一句收窄后的断言写法；否则空串"}}]

{blocks}"""


# ────────────────────────────── 断言抽取 ──────────────────────────────
def extract_claims(text: str) -> list[str]:
    out = []
    for m in CLAIM_RE.finditer(text):
        c = re.sub(r"\s+", " ", m.group(0)).strip()
        c = c.strip("　 ")
        if 12 <= len(c) <= 130 and c not in out:
            out.append(c)
    return out


# ────────────────────────────── 缓存 ──────────────────────────────
def _load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(d: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


# ────────────────────────────── 主流程 ──────────────────────────────
def audit(round_no: int, draft_file: Path | None = None,
          progress=print) -> dict:
    """对某轮定稿跑一次缺口核实。结果按**断言文本**缓存，断言没变就不重复烧 LLM。"""
    t0 = time.time()
    if draft_file is None:
        draft_file = REV / "writing" / f"round_{round_no:02d}" / f"review_draft_r{round_no}.md"
    if not draft_file.exists():
        return {"ok": False, "error": f"定稿不存在：{draft_file}"}

    text = draft_file.read_text(encoding="utf-8")
    claims = extract_claims(text)
    cache = _load_cache()
    cfg = get_step_config("refine")

    todo = [c for c in claims if c not in cache]
    progress(f"[gap] 定稿 {len(text)} 字符 → {len(claims)} 条缺口断言"
             f"（缓存命中 {len(claims) - len(todo)}，待核 {len(todo)}）")

    # ── ① 翻译检索式（只对没缓存的断言）──
    for i in range(0, len(todo), BATCH_Q_PER):
        chunk = todo[i:i + BATCH_Q_PER]
        ids = [claims.index(c) + 1 for c in chunk]
        lst = "\n".join(f"{n}. {c}" for n, c in zip(ids, chunk))
        raw = call_llm([{"role": "user",
                         "content": BATCH_Q.format(ids="/".join(map(str, ids)), claims=lst)}],
                       cfg, max_tokens=3000)
        q = loads_llm(raw) or {}
        for n, c in zip(ids, chunk):
            v = q.get(str(n)) if isinstance(q, dict) else None
            cache.setdefault(c, {})["queries"] = [x for x in (v or []) if isinstance(x, str)][:2]
    _save_cache(cache)

    # ── ② 论文级全库召回（纯检索）──
    for c in todo:
        if cache[c].get("cands") is not None:
            continue
        qs = cache[c].get("queries") or [c[:100]]
        papers, seen = [], set()
        for q in qs:
            try:
                res = R.gap_check(q, top_papers=8)
            except Exception as e:
                progress(f"  [gap] gap_check 失败 {type(e).__name__}: {e}")
                continue
            for p in res.get("papers", []):
                if p["paper_id"] not in seen:
                    seen.add(p["paper_id"])
                    papers.append(p)
        cache[c]["cands"] = papers[:10]
        progress(f"  [gap] 召回 {len(cache[c]['cands'])} 篇 ← {c[:38]}")
    _save_cache(cache)

    # ── ③ 判定 ──
    for i in range(0, len(todo), BATCH_J_PER):
        chunk = todo[i:i + BATCH_J_PER]
        ids = [claims.index(c) + 1 for c in chunk]
        blocks = []
        for n, c in zip(ids, chunk):
            ps = cache[c].get("cands") or []
            lines = "\n".join(
                f"   - id={p['paper_id']} | {p.get('year')} | {(p.get('title') or '')[:95]}\n"
                f"     {(p.get('snippet') or '')[:150].replace(chr(10), ' ')}" for p in ps)
            blocks.append(f"[断言 {n}] {c}\n  候选：\n{lines or '   （无）'}")
        raw = call_llm([{"role": "user", "content": BATCH_J.format(blocks="\n\n".join(blocks))}],
                       cfg, max_tokens=4500)
        judged = loads_llm(raw)
        if isinstance(judged, dict):
            judged = judged.get("results") or list(judged.values())
        byi = {}
        for j in (judged or []):
            if isinstance(j, dict) and j.get("i") is not None:
                try:
                    byi[int(j["i"])] = j
                except Exception:
                    pass
        for n, c in zip(ids, chunk):
            j = byi.get(n, {})
            cache[c]["verdict"] = j.get("verdict") or "解析失败"
            cache[c]["direct_papers"] = j.get("direct_papers") or []
            cache[c]["why"] = j.get("why") or ""
            cache[c]["rewrite"] = j.get("rewrite") or ""
    _save_cache(cache)

    # ── ④ 命中文献登记为全局引用编号（让稿子能引）──
    refs = refs_mod.load()
    file_name = f"gap_audit_r{round_no}.md"
    rows = []
    for n, c in enumerate(claims, 1):
        e = cache.get(c, {})
        cands = {p["paper_id"]: p for p in (e.get("cands") or [])}
        direct = [p for p in (e.get("direct_papers") or []) if p in cands]
        if direct:
            cits = [{"id": f"G{p}", "type": "section_hit", "content": cands[p].get("snippet") or "",
                     "metadata": {"paper_id": p, "paper_title": cands[p].get("title"),
                                  "year": cands[p].get("year"), "journal": cands[p].get("journal")}}
                    for p in direct]
            m = refs_mod.register_citations(refs, cits, file_name)
            sids = [m.get(f"G{p}") for p in direct]
        else:
            sids = []
        rows.append({"i": n, "claim": c, "verdict": e.get("verdict") or "未核",
                     "why": e.get("why") or "", "rewrite": e.get("rewrite") or "",
                     "papers": [{"paper_id": p, "sid": sids[k],
                                 "title": cands[p].get("title"), "year": cands[p].get("year")}
                                for k, p in enumerate(direct)],
                     "queries": e.get("queries") or []})
    refs_mod.save(refs)

    blocking = [r for r in rows if r["verdict"] == "已有直接文献"]
    partial = [r for r in rows if r["verdict"] == "部分相关"]
    clear = [r for r in rows if r["verdict"] == "确实未见"]
    unparsed = [r for r in rows if r["verdict"] not in
                ("已有直接文献", "部分相关", "确实未见")]

    report = REV / f"gap_audit_r{round_no}.md"
    lines = [f"# 缺口断言全库核实 · round {round_no}", "",
             f"- 稿子：`{draft_file.name}`（{len(text)} 字符）",
             f"- 断言：**{len(claims)}** 条 → 已有直接文献 **{len(blocking)}** / "
             f"部分相关 {len(partial)} / 确实未见 {len(clear)}"
             + (f" / 未判定 {len(unparsed)}" if unparsed else ""),
             f"- 耗时：{time.time() - t0:.0f}s", "",
             "> 判定口径：'已有直接文献'= 库内某篇明确做了断言说'没有'的那件事 → 断言站不住；",
             "> '部分相关'= 同主题但尺度/对象/口径不等价 → 需收窄写法；",
             "> '确实未见'= 当前库范围内成立。**'未检索到' 不等于 '不存在'。**", ""]
    for grp, title in ((blocking, "⛔ 阻塞：库内已有直接文献"), (partial, "⚠️ 需收窄"),
                       (clear, "✅ 库内未见"), (unparsed, "❓ 未判定")):
        if not grp:
            continue
        lines += [f"## {title}（{len(grp)} 条）", ""]
        for r in grp:
            lines += [f"### {r['i']}. {r['claim']}", f"- 依据：{r['why']}"]
            for p in r["papers"]:
                lines.append(f"- 库内文献：**[{p['sid']}]** #{p['paper_id']} {p['year']} "
                             f"{(p['title'] or '')[:88]}")
            if r["rewrite"]:
                lines.append(f"- 建议改写：{r['rewrite']}")
            lines += [f"- 检索式：{(' / '.join('`' + q + '`' for q in r['queries']))}", ""]
    report.write_text("\n".join(lines), encoding="utf-8")

    progress(f"[gap] 完成：阻塞 {len(blocking)} / 收窄 {len(partial)} / 未见 {len(clear)}"
             f"（{time.time() - t0:.0f}s）→ {report.name}")
    return {"ok": True, "draft": str(draft_file), "claims": len(claims),
            "blocking": blocking, "partial": partial, "clear": len(clear),
            "unparsed": unparsed, "report": str(report), "elapsed": round(time.time() - t0, 1)}


def render_blocking(res: dict, limit: int = 6) -> str:
    """给主智能体的紧凑提示：哪几条断言被库内文献推翻、该引谁。"""
    if not res.get("ok"):
        return f"[ERROR] {res.get('error')}"
    b = res["blocking"]
    out = [f"## 缺口核实（全库 {len(res['blocking']) + len(res['partial']) + res['clear']}"
           f" 条断言已核）",
           f"- ⛔ 与库内文献冲突：**{len(b)}** 条（必须处理才能送审）",
           f"- ⚠️ 需收窄口径：{len(res['partial'])} 条（不阻塞，但建议改）",
           f"- ✅ 库内未见：{res['clear']} 条",
           f"- 全文报告：`{Path(res['report']).name}`"]
    for r in b[:limit]:
        out.append(f"\n### ⛔ {r['i']}. {r['claim'][:90]}")
        out.append(f"依据：{r['why'][:160]}")
        for p in r["papers"][:4]:
            out.append(f"  库内已有 → **[{p['sid']}]** #{p['paper_id']} {p['year']} "
                       f"{(p['title'] or '')[:76]}")
        if r["rewrite"]:
            out.append(f"  建议改写：{r['rewrite'][:200]}")
    if len(b) > limit:
        out.append(f"\n（其余 {len(b) - limit} 条见报告）")
    if b:
        out.append("\n**规则**：库里已有直接文献的断言不能保留原写法——要么引用并改写，"
                   "要么把口径收窄到真的成立的范围（如“在 XX 尺度/口径下”）"
                   "。“未检索到”不等于“不存在”。改完再调 run_gap_audit 复核。")
    return "\n".join(out)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="缺口断言全库核实")
    ap.add_argument("--round", type=int, default=None)
    a = ap.parse_args()
    from modules.review import rounds
    r = a.round or rounds.get_round()
    res = audit(r)
    print(json.dumps({k: v for k, v in res.items() if k not in ("partial",)},
                     ensure_ascii=False, indent=1)[:4000])


if __name__ == "__main__":
    main()
