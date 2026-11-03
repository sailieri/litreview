"""kb_qa.py —— V2.3 综述管线脚本①：知识库问答。

主智能体唯一的信息获取通道。每次调用做四件事：
  1. 向 papers.db 提问（kb_agent，项目内的 RAG 问答模块）
  2. 把完整交付（问题 + 回答 + 引用原文块）落盘 research/_kb_rN_K_slug.md
  3. 把本次引用的题录增量登记进 research/_kb_refs.json
  4. stdout 返回「回答 + 本次引用增量（题录摘要，无原文）」——原文按需 --expand

用法（CLI）：
    python kb_qa.py --q "wind energy potential assessment remote sensing GIS" --round 1
    python kb_qa.py --q "GEDI L4A accuracy" --round 1 --expand S127 S128

程序化（main_agent.py 用）：
    from kb_qa import kb_query
    out = kb_query("...", round_no=1)   # {"text","new_sids","brief","file","seq","total_refs"}
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# 把仓库根加入 sys.path（脚本在子目录里运行）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from modules.kb_agent import KBAgent, render_citations
from modules.review.paths import RESEARCH
from modules.review import refs as refs_mod
from modules.review import rounds

MAX_PARALLEL = 4        # 一次调用最多并行派几个 kb agent


def _slug(q: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", q)[:40].strip("_").lower()


def _register_one(q: str, out: dict, r: int, s: int, quiet: bool = True) -> dict:
    """把一个问答的产出落盘 + 登记引用（**必须串行执行**：改的是全局引用表）。"""
    RESEARCH.mkdir(parents=True, exist_ok=True)
    slug = _slug(q)
    kbf = RESEARCH / f"_kb_r{r}_{s}{('_' + slug) if slug else ''}.md"

    # 引用登记：局部编号 → 全局编号；同时把回答文本里的 [S#] 重写成全局编号
    # （kb_agent 每次调用都从 S1 重新编号，不重写就会造成跨查询引用错位）
    refs = refs_mod.load()
    mapping = refs_mod.register_citations(refs, out.get("citations") or [], kbf.name)
    refs_mod.save(refs)

    text = out.get("text", "")
    for lid, gid in sorted(mapping.items(), key=lambda kv: -len(str(kv[0]))):
        if lid != gid:
            text = text.replace(f"[{lid}]", f"[{gid}]")
    out["text"] = text
    for c in out.get("citations") or []:
        if c.get("id") in mapping:
            c["id"] = mapping[c["id"]]
    new_sids = list(dict.fromkeys(mapping.values()))

    kbf.write_text(
        f"# KB 问答 Round {r} #{s}（{q}）\n\n"
        f"## 回答\n{out.get('text', '')}\n\n"
        f"## 引用来源（[S#] → 论文/超边元数据+原文全文）\n{render_citations(out)}",
        encoding="utf-8")

    brief = refs_mod.render_brief_list(refs, new_sids)
    if not quiet:
        print(f"=== 回答（Round {r} #{s}）===\n{out.get('text', '')}\n")
        print(f"=== 本次引用（{len(new_sids)} 条，已登记 {refs_mod.REFS_FILE.name} 共 {len(refs)} 条）===")
        print(brief)
        print(f"\n[完整交付已落盘 {kbf}]")
    return {"round": r, "seq": s, "question": q, "text": out.get("text", ""),
            "new_sids": new_sids, "brief": brief, "file": str(kbf),
            "total_refs": len(refs), "error": None}


def kb_query_multi(questions: list[str], round_no: int | None = None,
                   quiet: bool = True) -> dict:
    """并行查多个问题（最多 MAX_PARALLEL 个）。

    预算：主线程一次性预扣 N 次（每个问题各计 1 次）；某问失败就退还那 1 次。
    引用登记严格串行（全局引用表非线程安全），只有 LLM 问答并发。
    返回 {"items":[{question,text,new_sids,brief,file,total_refs,error}], "total_refs": n}
    """
    r = round_no or rounds.get_round()
    qs: list[str] = []
    for q in questions:
        q = (q or "").strip()
        if q and q not in qs:
            qs.append(q)
    if not qs:
        raise ValueError("questions 为空")
    if len(qs) > MAX_PARALLEL:
        raise ValueError(f"一次最多 {MAX_PARALLEL} 个问题（收到 {len(qs)}）")

    seqs = []
    for _ in qs:
        try:
            seqs.append(rounds.spend("kb_qa", r))
        except rounds.BudgetExceeded:
            for _ in seqs:          # 部分预扣的退回，保持「要么全发要么不发」
                rounds.refund("kb_qa", r)
            raise

    def _ask(q: str):
        kb = KBAgent(step="rag")       # 每个问题独立实例（自带 registry/client），可并发
        return kb.ask(q)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(qs)) as ex:
        futs = [ex.submit(_ask, q) for q in qs]
        outs = []
        for q, f in zip(qs, futs):
            try:
                outs.append(f.result())
            except Exception as e:
                outs.append({"__error__": f"{type(e).__name__}: {e}"})

    for q, s, out in zip(qs, seqs, outs):
        if "__error__" in out:
            rounds.refund("kb_qa", r)
            results.append({"round": r, "seq": s, "question": q, "text": "",
                            "new_sids": [], "brief": "", "file": None,
                            "total_refs": len(refs_mod.load()), "error": out["__error__"]})
            continue
        results.append(_register_one(q, out, r, s, quiet=quiet))

    return {"round": r, "items": results,
            "total_refs": results[-1]["total_refs"] if results else 0,
            "spent": rounds.spent("kb_qa", r),
            "limit": rounds.BUDGET_LIMITS["kb_qa"]}


def kb_query(q: str, round_no: int | None = None, seq: int | None = None,
             no_budget: bool = False, quiet: bool = False) -> dict:
    """查一次库（单问，CLI 与兼容用）：落盘 + 登记引用，返回回答与引用增量（不含原文）。"""
    r = round_no or rounds.get_round()
    if no_budget:
        s = seq or (rounds.spent("kb_qa", r) + 1)
    else:
        s = seq or rounds.spend("kb_qa", r)

    kb = KBAgent(step="rag")
    out = kb.ask(q)
    res = _register_one(q, out, r, s, quiet=quiet)
    return {"round": r, "seq": s, "text": res["text"],
            "new_sids": res["new_sids"], "brief": res["brief"], "file": res["file"],
            "total_refs": res["total_refs"]}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    ap = argparse.ArgumentParser(description="litreview V2.3 知识库问答")
    ap.add_argument("--q", required=True, help="检索问题（英文技术关键词效果最好）")
    ap.add_argument("--round", type=int, default=None, help="轮次（缺省读 _round_state.json）")
    ap.add_argument("--seq", type=int, default=None, help="本轮第几次问（缺省自动计数）")
    ap.add_argument("--expand", nargs="*", default=None, help="按需展开原文的 [S#] 编号")
    ap.add_argument("--no-budget", action="store_true", help="不计预算（调试用）")
    args = ap.parse_args()

    res = kb_query(args.q, round_no=args.round, seq=args.seq, no_budget=args.no_budget)
    print("[题录不含 doi；需看原文用 --expand S#，或在 evidence_pack 中核对]")
    if args.expand:
        refs = refs_mod.load()
        print("\n=== 按需展开原文 ===\n" + refs_mod.expand(refs, args.expand))


if __name__ == "__main__":
    main()
