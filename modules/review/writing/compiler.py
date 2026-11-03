"""成稿整合器：初稿（论证稿） → 成稿（表达稿）。

分工：
- **内容层**由主智能体负责，产物是 `thinking/round_NN/draft_rN.md`（论证稿）：
  论点句 + 证据[S#] + 数值 + 适用边界，技术报告体，不追求文笔，不写参考文献。
- **表达层**由本模块负责：把论证稿转写成叙述体学术综述——连贯、去罗列、统一术语、
  数值照抄并保留 [S#]、参考文献机械汇总。**不新增事实、不改数值**。

增量编译：第 N 轮只重写「初稿正文与第 N-1 轮不同」的章节，其余章节逐字复用上一轮成稿
（省一次 LLM 会话的开销）。

产物：`writing/round_NN/review_draft_rN.md`
"""
import re
from pathlib import Path

from modules.util.llm_client import get_step_config, call_llm, load_config
from modules.review.paths import (draft_path, review_draft_path, evidence_pack_path,
                                  key_findings_path, think_dir, round_dir)
from modules.review import refs as refs_mod

COMPILE_SYS = """你是学术综述的「表达层撰写人」。上游已经给出**论证稿**（每章：论点句 → 证据 → 数值 → 适用边界），你的任务是把论证稿**转写成叙述体学术综述**。

你是表达者，不是研究者：
- 事实、数值、结论、引用编号一律来自给定材料；**不得新增任何事实、不得修改任何数值**。
- 材料未覆盖的内容宁可留白说明，也不得编造。

硬性写作纪律：
1. 以论证意图组织段落，每段只发展一条论点，首句即论点句。
2. 用转折/因果/递进连词推进（然而 / 因此 / 关键在于 / 尽管 / 相比之下 / 由此），不要并列堆句。
3. 具体数字嵌入叙事句（如"精度从 R²=0.45 提升至 0.63"），绝不用 bullet 孤立数字；引用量化结论须保留其测试条件。
4. 引用自然融入句子（"正如 [S3] 所示"），不要以"[N] 提出了…"领衔；**保留全部 [S#]/[E#] 标记**。
5. 每章围绕其核心分析问题展开，明确方法间的差异与适用边界，不可孤立罗列单篇。
6. 严禁：分点列表（bullet/编号列表）、[强]/[中]/[弱] 标签、"呼应 Q"/"aggregation"/"chunks" 等内部流程表述。
7. 坦诚呈现证据缺口与矛盾，用自然语言说明根因，不回避、不粉饰。
8. 章末给出适用条件判断（何时有用、边界在哪；证据不足明确说"目前证据不足以区分"）。
9. 中文写作，学术但自然；章节标题与各级小标题一律纯中文，技术缩写（GEDI、ERA5、PV）嵌在中文句子里。
10. 篇幅：每章 700–1000 字；只输出该章正文，不重复输出标题以外的说明。"""

from modules.review.sections import CITE_RE, parse_sections, section_body  # noqa: E402


def _title_of(text: str) -> str:
    m = re.search(r"^#\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else "综述"


def _body_of_section(text: str, num: str) -> str:
    """从成稿里取回某章正文（去掉标题行）。"""
    return section_body(text, num)


def build_references(body: str) -> str:
    refs_map = refs_mod.load()
    cited, seen = [], set()
    for m in CITE_RE.finditer(body):
        sid = m.group(1)
        if sid not in seen:
            seen.add(sid)
            cited.append(sid)
    lines = refs_mod.biblio_lines(refs_map, cited)
    if not lines:
        return ("> ⚠️ 参考文献表未能生成：正文无 [S#] 引用，或引用表 "
                "`data/reviews/research/_kb_refs.json` 无对应题录（请先跑 kb_qa）。")
    return "## 参考文献\n\n" + "\n".join(lines)


def ensure_evidence_pack(r: int, draft_text: str) -> Path:
    """按初稿实际引用的编号生成/刷新引用证据包（论文 → [S#] → 原文片段）。"""
    sids = sorted(set(CITE_RE.findall(draft_text)), key=lambda s: int(s[1:]))
    pack = refs_mod.build_evidence_pack(refs_mod.load(), sids)
    p = evidence_pack_path(r)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pack, encoding="utf-8")
    print(f"[compile] evidence_pack: {len(sids)} 个编号 / {len(pack)} 字符 -> {p}")
    return p


def run(round_no: int, pack_only: bool = False) -> str:
    dp = draft_path(round_no)
    if not dp.exists():
        raise FileNotFoundError(f"找不到初稿 {dp}")
    draft = dp.read_text(encoding="utf-8")
    pack_path = ensure_evidence_pack(round_no, draft)
    if pack_only:
        return str(pack_path)

    kf = key_findings_path(round_no)
    kf_text = kf.read_text(encoding="utf-8") if kf.exists() else "（本轮未提供事实锚点清单）"
    pack = pack_path.read_text(encoding="utf-8")
    title = _title_of(draft)
    secs = parse_sections(draft)
    if not secs:
        raise ValueError("初稿解析不出章节（需 `## N. 标题` 或 `## 第N章 标题` 格式）")

    # 增量：与上一轮初稿完全相同的章节，复用上一轮成稿
    prev_draft = draft_path(round_no - 1)
    prev_final = review_draft_path(round_no - 1)
    prev_secs = {s["num"]: s["body"] for s in parse_sections(
        prev_draft.read_text(encoding="utf-8"))} if prev_draft.exists() else {}
    prev_final_text = prev_final.read_text(encoding="utf-8") if prev_final.exists() else ""

    cfg = get_step_config("compile", load_config())
    messages = [{"role": "system", "content": COMPILE_SYS}]
    deliver = (f"=== 论证稿（唯一内容来源）===\n{draft}\n\n"
               f"=== 事实锚点清单 ===\n{kf_text[:12000]}\n\n"
               f"=== 引用证据包（数值核对以此为准）===\n{pack[:400000]}")
    messages.append({"role": "user", "content":
        f"综述题目：{title}\n\n{deliver}\n\n"
        f"第一步：只输出写作大纲——沿用论证稿的章节划分，格式 `## N. 章节名`。不要写正文。"})

    outline_out = call_llm(messages, cfg, max_tokens=4000, temperature=0.2)
    messages.append({"role": "assistant", "content": outline_out})

    section_texts, reused = {}, []
    for sec in secs:
        n, t = sec["num"], sec["title"]
        if prev_secs.get(n) == sec["body"] and prev_final_text:
            body = _body_of_section(prev_final_text, n)
            if body:
                section_texts[n] = f"## {n}. {t}\n\n{body}"
                reused.append(n)
                print(f"[compile] section {n} 复用上一轮成稿（初稿未变）")
                continue
        messages.append({"role": "user", "content":
            f"现在写「第 {n} 章：{t}」正文（700–1000 字）。"
            f"以 `## {n}. {t}` 开头，只输出该章。"})
        out = call_llm(messages, cfg, max_tokens=8000, temperature=0.4)
        messages.append({"role": "assistant", "content": out})
        section_texts[n] = out.strip()
        print(f"[compile] section {n} done ({len(out)} chars)")

    closing_ask = ("全部章节完成。一次性输出两件（用以下小节标题）：\n"
                   "1. `## 摘要`——220–300 字中文摘要，概括全文分析主线与关键挑战；\n"
                   "2. `## 已知局限`——100–200 字，基于材料诚实说明语料边界与证据局限。")
    messages.append({"role": "user", "content": closing_ask})
    closing = call_llm(messages, cfg, max_tokens=4000, temperature=0.2)
    print(f"[compile] closing done ({len(closing)} chars)")

    body = "\n\n".join(section_texts[s["num"]] for s in secs if s["num"] in section_texts)
    refs_block = build_references(body)
    draft_out = f"# {title}\n\n{closing}\n\n{body}\n\n{refs_block}\n"

    out_path = review_draft_path(round_no)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(draft_out, encoding="utf-8")
    print(f"[compile] -> {out_path} ({len(draft_out)} chars, 复用章节 {reused or '无'})")
    return str(out_path)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    run(r)
