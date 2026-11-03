#!/usr/bin/env python3
"""litreview 综述主智能体（单文件，V2.3）。

主智能体由本文件独立实现，不依赖外部智能体会话。
系统提示词 = agents/main_agent_prompt.md（唯一真相源，跑之前先读它）。

架构：
    DeepSeek V4.1（reasoning_effort=low，开思考但最低强度）
      + function calling 工具循环（上限见 MAX_ITERS，每 5 轮提醒，到顶只能 finish）
      + 机械活全部下沉到已有脚本（kb_qa / probe_draft / check_draft / review_draft）

用法：
    python main_agent.py                 # 轮次读 _round_state.json
    python main_agent.py --round 1       # 指定轮次
    python main_agent.py --dry-run       # 不调 LLM，只校验提示词/工具/状态
    python main_agent.py --max-iters 8   # 覆盖循环上限（调试用）

设计要点（与 SOP 对应）：
    - 初稿 thinking/round_NN/draft_rN.md 是唯一内容源，由 write_draft / patch_draft 维护
    - 提交审核前必须自己把初稿改成定稿（write_final，含参考文献）；成稿另由 compile_draft.py 生成
    - run_review 前自动对定稿跑硬断言（引用存在性/数值可溯源），有 ERROR 直接拒绝
    - 每跑一次 run_review = 本轮结束，轮次自动 +1
"""
import argparse
import datetime
import json
import re
import sys
import traceback
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr = sys.stdout

# 仓库根在上两级（脚本在子目录里运行）
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 同目录的 kb_qa / probe_draft 等

from modules.util.llm_client import load_config, get_step_config          # noqa: E402
from modules.util.jsonllm import loads_llm                              # noqa: E402
from modules.review.paths import (think_dir, draft_path, key_findings_path,   # noqa: E402
                                  review_draft_path, META)
from modules.review import rounds                                        # noqa: E402
from modules.review import refs as refs_mod                              # noqa: E402
from modules.review import gap_audit as gap_mod                          # noqa: E402

import kb_qa as kb_qa_mod                                                # noqa: E402
import probe_draft as probe_mod                                          # noqa: E402
import check_draft as check_mod                                          # noqa: E402
import review_draft as review_mod                                        # noqa: E402

MAX_ITERS = 30          # 工具循环上限（"轮"= 一次 LLM 调用）；检索配额通常先于工具轮用完
REMIND_EVERY = 5        # 每 5 轮注入一次提醒
TOOL_RESULT_CHARS = 6000   # 工具结果进上下文的默认截断长度
READ_RESULT_CHARS = 30000  # read_file 的截断上限（初稿/审稿意见动辄数万字，截断过短会静默丢内容）
KB_ANSWER_CHARS = 4000     # 并行检索里单个回答的截断长度（kb 回答本身不设字数上限；
                           # 完整问答仍落盘 research/，超长时主智能体可读原文件）
KB_TOTAL_CHARS = 13000     # 并行检索单次返回的总截断长度（4 问）
PROMPT_FILE = PROJECT / "pipelines" / "review" / "main_agent_prompt.md"
PRD_FILE = META / "_prd.md"
REVIEWS_ROOT = PROJECT / "data" / "reviews"     # read_file 白名单根目录
READ_MAX_BYTES = 80_000                        # 单文件读取上限


# ═══════════════════════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════════════════════
class Toolbox:
    def __init__(self, round_no: int, trace_path: Path):
        self.round = round_no
        self.trace = trace_path
        self.finished = None          # finish() 的 summary
        self.steps: list[dict] = []   # 每步轨迹
        self.probe_runs = 0           # 本轮追问者成功调用次数（收口硬门用）
        self.gap_result = None        # 最近一次缺口核实结果（run_review 闸门用）
        self.gap_draft_chars = None   # 核实时的定稿长度（判断定稿是否又改过）

    # ── 落盘小工具 ──
    def _trace(self, name: str, args: dict, result: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = (f"\n## step {len(self.steps)+1} · {name}（{ts}）\n"
                f"- 入参：{json.dumps(args, ensure_ascii=False)[:500]}\n"
                f"- 结果：{result[:1500]}\n")
        with self.trace.open("a", encoding="utf-8") as f:
            f.write(line)
        self.steps.append({"name": name, "args": args, "result": result[:2000]})

    @staticmethod
    def _truncate(s: str, n: int = TOOL_RESULT_CHARS) -> str:
        s = s or ""
        return s if len(s) <= n else s[:n] + f"\n…(截断，原长 {len(s)} 字符)"

    # ── 工具 0：读文件（限 data/reviews/，用于读上一轮产物）──
    def read_file(self, rel_path: str) -> str:
        root = REVIEWS_ROOT.resolve()
        p = (root / rel_path).resolve()
        if root not in p.parents and p != root:
            return "[ERROR] 只允许读 data/reviews/ 下的文件"
        if not p.exists():
            return f"[ERROR] 文件不存在：{rel_path}"
        if p.is_dir():
            return "目录内容：\n" + "\n".join(sorted(x.name for x in p.iterdir()))
        size = p.stat().st_size
        if size > READ_MAX_BYTES:
            return (f"[ERROR] 文件过大（{size} 字节 > {READ_MAX_BYTES}），"
                    f"请改读更具体的文件（如某个 round 下的 draft）")
        txt = p.read_text(encoding="utf-8")
        self._trace("read_file", {"rel_path": rel_path}, f"{size} 字节")
        return self._truncate(txt, READ_RESULT_CHARS)

    # ── 工具 1：查库（并行，一次最多 4 问）──
    def kb_query(self, questions) -> str:
        if isinstance(questions, str):
            questions = [questions]
        questions = [q for q in (questions or []) if isinstance(q, str) and q.strip()]
        if not questions:
            return "[ERROR] questions 为空"
        if len(questions) > kb_qa_mod.MAX_PARALLEL:
            return (f"[ERROR] 一次最多 {kb_qa_mod.MAX_PARALLEL} 个问题（收到 {len(questions)}）。"
                    f"请把同一方面的子问题合并，不同方面分开，分两次调用。")
        try:
            res = kb_qa_mod.kb_query_multi(questions, round_no=self.round, quiet=True)
        except rounds.BudgetExceeded as e:
            return f"[ERROR] {e}"
        except Exception as e:
            return f"[ERROR] kb_query 失败：{type(e).__name__}: {e}"

        blocks, fails = [], []
        for i, it in enumerate(res["items"], 1):
            if it.get("error"):
                fails.append(f"Q{i} 调用失败（已退还预算）：{it['error']}")
                continue
            body = self._truncate(it["text"], KB_ANSWER_CHARS)
            blocks.append(f"### Q{i}：{it['question']}\n{body}\n\n"
                          f"（本次新增引用 {len(it['new_sids'])} 条："
                          f"{', '.join(it['new_sids'][:24]) or '无'}；"
                          f"完整问答已落盘 research/{Path(it['file']).name}）")
        head = (f"## 并行检索结果（{len(blocks)} 问完成，引用表共 {res['total_refs']} 条；"
                f"本轮 kb_query 已用 {res['spent']}/{res['limit']} 问）\n"
                f"注意：各问回答里的 [S#] 已是全局唯一编号，可直接引用。\n")
        out = head + "\n\n".join(blocks)
        asked = _asked_questions()
        this_round = [q for r, s, q in asked if r == self.round]
        if this_round:
            out += ("\n\n## 本轮已问过的问题（不要重复问；可在此基础上追问更深一层）\n"
                    + "\n".join(f"  #{i}. {q[:110]}" for i, q in enumerate(this_round, 1)))
        if fails:
            out += "\n\n" + "\n".join(fails)
        self._trace("kb_query", {"questions": questions},
                    f"{len(blocks)} 问完成，新增引用 "
                    + str([it['new_sids'] for it in res['items'] if not it.get('error')]))
        return self._truncate(out, KB_TOTAL_CHARS)

    # ── 工具 2：写事实锚点 ──
    def save_key_findings(self, content: str, mode: str = "append") -> str:
        p = key_findings_path(self.round)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite" or not p.exists():
            p.write_text(content, encoding="utf-8")
        else:
            with p.open("a", encoding="utf-8") as f:
                f.write("\n" + content)
        n = len(p.read_text(encoding="utf-8"))
        self._trace("save_key_findings", {"mode": mode, "chars": len(content)},
                    f"{p.name} 现 {n} 字符")
        return f"[OK] key_findings.md 已{mode}（本次 {len(content)} 字符，现共 {n} 字符）"

    # ── 工具 3：写初稿 ──
    def write_draft(self, markdown: str) -> str:
        p = draft_path(self.round)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(markdown, encoding="utf-8")
        secs = check_mod.parse_sections(markdown)
        self._trace("write_draft", {"chars": len(markdown)}, f"{p.name} 章节 {len(secs)}")
        return (f"[OK] 初稿已写入 {p.name}（{len(markdown)} 字符，解析到 {len(secs)} 章）。"
                f"下一步：run_check() 看硬断言。")

    # ── 工具 4：改初稿（精确替换）──
    def patch_draft(self, old_text: str, new_text: str) -> str:
        p = draft_path(self.round)
        if not p.exists():
            return f"[ERROR] 初稿不存在（{p.name}），先 write_draft"
        cur = p.read_text(encoding="utf-8")
        cnt = cur.count(old_text)
        if cnt == 0:
            return "[ERROR] old_text 在初稿中找不到，请改用更长/更精确的原文片段"
        if cnt > 1:
            return f"[ERROR] old_text 在初稿中出现 {cnt} 次，不唯一，请带上更多上下文"
        p.write_text(cur.replace(old_text, new_text, 1), encoding="utf-8")
        self._trace("patch_draft", {"old": old_text[:80], "new": new_text[:80]},
                    f"{p.name} 现 {len(p.read_text(encoding='utf-8'))} 字符")
        return f"[OK] 已替换 1 处，初稿现 {len(p.read_text(encoding='utf-8'))} 字符"

    # ── 工具 5：追问者（可多轮深挖；本轮必须至少调用 1 次）──
    def run_probe(self, response: str = "") -> str:
        try:
            res = probe_mod.run_probe(self.round, response=response or "")
        except rounds.BudgetExceeded as e:
            return f"[ERROR] {e}"
        except FileNotFoundError as e:
            return f"[ERROR] {e}"
        qs = "\n".join(f"{i}. {q}" for i, q in enumerate(res["questions"], 1))
        out = f"## 追问者总体评价（第 {res.get('seq', 1)} 次追问）\n{res.get('assessment','')}\n\n"
        if res.get("resolved"):
            out += "## 上一轮问题判定\n" + "\n".join(
                f"- [{x.get('verdict','?')}] {x.get('q','')} — {x.get('note','')}"
                for x in res["resolved"]) + "\n\n"
        out += (f"## 追问清单\n{qs}\n\n"
                f"[已落盘 {Path(res['file']).name}；probe 已用 "
                f"{rounds.spent('probe_draft', self.round)}/{rounds.BUDGET_LIMITS['probe_draft']} 次]")
        self.probe_runs += 1
        self._trace("run_probe", {"response": (response or "")[:120]},
                    f"第 {res.get('seq',1)} 次：{len(res['questions'])} 条追问")
        return self._truncate(out)

    # ── 工具 6：硬断言 ──
    def run_check(self) -> str:
        res = check_mod.check(self.round)
        self._trace("run_check", {}, f"ERROR {len(res['errors'])} WARN {len(res['warnings'])}")
        return self._truncate(check_mod.render(res))

    # ── 工具 7：取参考文献条目（写定稿前用）──
    def list_refs(self, cited_only: bool = True) -> str:
        refs = refs_mod.load()
        if cited_only:
            dp = draft_path(self.round)
            text = dp.read_text(encoding="utf-8") if dp.exists() else ""
            seen, cited = set(), []
            for sid in re.findall(r"\[([SE]\d+)\]", text):
                if sid not in seen:
                    seen.add(sid)
                    cited.append(sid)
        else:
            cited = list(refs.keys())
        lines = refs_mod.biblio_lines(refs, cited)
        self._trace("list_refs", {"cited_only": cited_only},
                    f"{len(cited)} 个编号 → {len(lines)} 条题录")
        if not lines:
            return "[OK] 目前没有可用的参考文献条目（初稿里没有 [S#]/[E#]，或引用表为空）"
        return (f"## 参考文献条目（按正文引用顺序，同一论文已合并；共 {len(lines)} 条）\n"
                + "\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines))
                + "\n\n[把上面这些条目按同样顺序整理成文末参考文献表；"
                  "其中 `[S#]`/`[E#]` 标记要与正文一一对应，不得增删]")

    # ── 工具 7b：展开某条引用的原文片段（数值存疑时用） ──
    def expand_ref(self, sids) -> str:
        if isinstance(sids, str):
            sids = [sids]
        sids = [s.strip().strip("[]") for s in (sids or []) if s and s.strip()]
        if not sids:
            return "[ERROR] 请给定 1 个或多个 [S#]/[E#] 编号"
        if len(sids) > 6:
            return "[ERROR] 一次最多展开 6 条"
        refs = refs_mod.load()
        out = refs_mod.expand(refs, sids, max_chars=2500)
        missing = [s for s in sids if s not in refs]
        head = ("## 引用原文片段（库内登记时保存的证据文字，不是论文全文；"
                "正文数值必须能在这里或本轮 kb_query 返回里找到）\n")
        if missing:
            head += f"[提示] 引用表中无此编号：{', '.join(missing)}\n"
        self._trace("expand_ref", {"sids": sids}, f"展开 {len(sids)} 条")
        return self._truncate(head + out, 12000)

    # ── 工具 7c：缺口断言全库核实（gap audit）──
    def run_gap_audit(self) -> str:
        """把稿子里全部「尚无/缺失/空白」断言拿去全库核实（论文级召回 + 判定 + 登记引用）。

        只读稿子与 papers.db，不改进数据库；命中的库内文献会登记成 [S#] 供正文引用。
        """
        target = None
        if review_draft_path(self.round).exists():
            target = review_draft_path(self.round)
        elif draft_path(self.round).exists():
            target = draft_path(self.round)
        if target is None:
            return "[ERROR] 本轮还没有初稿/定稿，先 write_draft 或 write_final"
        try:
            res = gap_mod.audit(self.round, draft_file=target)
        except Exception as e:
            return f"[ERROR] 缺口核实失败：{type(e).__name__}: {e}"
        self.gap_result = res
        self.gap_draft_chars = len(target.read_text(encoding="utf-8"))
        txt = gap_mod.render_blocking(res)
        self._trace("run_gap_audit", {"draft": target.name},
                    f"断言 {res.get('claims')} 条，阻塞 {len(res.get('blocking') or [])} 条，"
                    f"报告 {Path(res.get('report', '')).name}")
        return self._truncate(txt, 12000)

    # ── 工具 8：写定稿（初稿 → 定稿，含参考文献）──
    def write_final(self, markdown: str) -> str:
        p = review_draft_path(self.round)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(markdown, encoding="utf-8")
        self._trace("write_final", {"chars": len(markdown)}, f"{p.name} {len(markdown)} 字符")
        return (f"[OK] 定稿已写入 writing/round_{self.round:02d}/{p.name}"
                f"（{len(markdown)} 字符）。下一步：run_review()。")

    # ── 工具 9：提交审核（自动先对定稿跑硬断言）──
    def run_review(self) -> str:
        final = review_draft_path(self.round)
        if not final.exists():
            return "[ERROR] 定稿不存在，先 write_final（初稿→定稿，补参考文献）"
        guard = check_mod.check(self.round, draft_file=final)
        if not guard["ok"]:
            self._trace("run_review", {}, f"被硬断言拦下：{guard['errors'][:3]}")
            return ("[ERROR] 定稿未通过硬断言，已拒绝送审。请先修：\n"
                    + check_mod.render(guard))

        # ── 缺口闸门：定稿里每条「尚无/缺失/空白」断言都要先过全库核实 ──
        # 定稿改过就重核（缓存按断言文本，没改的断言不重复烧 LLM）。
        try:
            g = gap_mod.audit(self.round, draft_file=final)
        except Exception as e:
            g = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.gap_result = g
        if not g.get("ok"):
            return f"[ERROR] 缺口核实未跑通（{g.get('error')}），暂停送审，请先处理。"
        self.gap_draft_chars = len(final.read_text(encoding="utf-8"))
        if g["blocking"]:
            self._trace("run_review", {},
                        f"被缺口闸门拦下：{len(g['blocking'])} 条断言与库内文献冲突")
            head = (f"[ERROR] 缺口闸门拒绝送审：定稿有 **{len(g['blocking'])} 条**断言与库内已有文献冲突"
                    f"（审核团队看不见这一层，必须你处理）。报告：{Path(g['report']).name}\n")
            return head + self._truncate(gap_mod.render_blocking(g), 10000)
        try:
            res = review_mod.run_review(self.round)
        except rounds.BudgetExceeded as e:
            return f"[ERROR] {e}"
        except Exception as e:
            return f"[ERROR] run_review 失败：{type(e).__name__}: {e}"
        self._trace("run_review", {}, f"p0={res['p0']} total={res['total']} converged={res['converged']}")
        return json.dumps(res, ensure_ascii=False, indent=2)

    # ── 工具 10：收口（硬门：初稿已写却没跑过追问者 → 拒绝收口）──
    def finish(self, summary: str) -> str:
        if draft_path(self.round).exists() and self.probe_runs == 0:
            return ("[ERROR] 本轮还没调用过 run_probe（追问者）。规则：初稿写完必须至少让追问者"
                    "审一次、把追问问题消化到稿子里，不能自己写完就收口。请现在调用 run_probe()。")
        self.finished = summary
        self._trace("finish", {"summary": summary[:200]}, "已收口")
        return "[OK] 已收口"


# ═══════════════════════════════════════════════════════════════════════
# 工具 schema（function calling）
# ═══════════════════════════════════════════════════════════════════════
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读 data/reviews/ 下的文件（相对路径，如 thinking/round_01/draft_r1.md、"
                       "research/_kb_r1_3_xxx.md）。追加轮所必需的上轮意见/初稿/事实锚点已直接注入对话，"
                       "无需再读；本工具用于查其它产物（某次问答全文、证据包、追问记录等）。",
        "parameters": {"type": "object", "properties": {
            "rel_path": {"type": "string", "description": "相对 data/reviews/ 的路径"}},
            "required": ["rel_path"]}}},
    {"type": "function", "function": {
        "name": "kb_query",
        "description": "并行查 papers.db 知识库（唯一信息源）。一次最多 4 个问题，**并行同时检索**。"
                       "拆分原则：同一方面的多个子问题合并为 1 个问题（越具体越好）；不同方面各占 1 个问题。"
                       "例：①风能 SAR 反演精度与模型函数 ②光伏辐照再分析偏差 ③生物质折减口径 ④验证基准差异，"
                       "一次调用 4 问比连发 4 次省 3 轮。每轮总预算 24 问（按问题数计，每问 1 次）；数值必须来自本工具返回。",
        "parameters": {"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "string"},
                          "description": "1–4 个检索问题（英文技术关键词效果最好）"}},
            "required": ["questions"]}}},
    {"type": "function", "function": {
        "name": "save_key_findings",
        "description": "写/追加本轮事实锚点清单（key_findings.md）。每条 KF = 一句结论 + 证据（数值/方法 + [S#]）+ 对论证的作用。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "markdown 片段"},
            "mode": {"type": "string", "enum": ["append", "overwrite"], "description": "默认 append"}},
            "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "write_draft",
        "description": "写入/覆盖本轮初稿（thinking/round_NN/draft_rN.md，唯一内容源）。"
                       "章节格式必须是 `## N. 标题` 或 `## 第N章 标题`（章内不要用 `数字.` 开头的小标题）；"
                       "章节以 PRD 为起点，可按语料合并/拆分/换序；需 `## 摘要` 与 `## 已知局限` 两个不编号块；"
                       "初稿不写参考文献章节。",
        "parameters": {"type": "object", "properties": {
            "markdown": {"type": "string", "description": "整篇初稿 markdown"}},
            "required": ["markdown"]}}},
    {"type": "function", "function": {
        "name": "patch_draft",
        "description": "对初稿做一处精确替换（old_text 必须在初稿中唯一出现）。用于消化追问/审稿意见时的局部修订。",
        "parameters": {"type": "object", "properties": {
            "old_text": {"type": "string"}, "new_text": {"type": "string"}},
            "required": ["old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "run_probe",
        "description": "让独立的追问者审当前初稿，返回总体评价 + 追问清单。**本轮必须至少调用 1 次**"
                       "（未完过追问者无法收口）。每一轮追问后都要把问题消化进稿子；"
                       "消化完可再调一次（传 response 说明改了什么），让追问者判定上轮问题的解决情况"
                       "并往下深挖一层——这比一次问很多条更有价值。每轮上限 3 次。",
        "parameters": {"type": "object", "properties": {
            "response": {"type": "string",
                         "description": "对上一轮追问的处理说明（首轮留空；深挖轮写上你补了什么、改了什么）"}}}}},
    {"type": "function", "function": {
        "name": "run_check",
        "description": "跑硬断言：章节完整性 / 每章有无 [S#] / 引用编号是否存在 / 数值能否在证据包原文命中。"
                       "ERROR 必须清零才能进下一步；WARN 逐条确认。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "expand_ref",
        "description": "展开某条引用的库内证据原文片段（如 ['S10','S17']）——核对某个数值到底有没有出处、原文怎么说时用它。"
                       "返回的是登记时保存的片段（非论文全文）；一次最多 6 条。",
        "parameters": {"type": "object", "properties": {
            "sids": {"type": "array", "items": {"type": "string"},
                     "description": "引用编号列表，如 ['S10','S121']"}},
            "required": ["sids"]}}},
    {"type": "function", "function": {
        "name": "list_refs",
        "description": "取参考文献条目（按正文引用顺序、同一论文已合并，含作者/年份/期刊）。"
                       "写定稿前调用，用返回值机械生成文末参考文献表，不要自己编造题录。",
        "parameters": {"type": "object", "properties": {
            "cited_only": {"type": "boolean", "description": "true（默认）= 只列初稿实际引用到的编号"}}}}},
    {"type": "function", "function": {
        "name": "write_final",
        "description": "把初稿改写成定稿并写入 writing/round_NN/review_draft_rN.md：叙述化、补全参考文献列表、"
                       "引用编号与文献表一一对应。定稿里不得出现初稿没有的事实。提交审核前必须先做这一步。",
        "parameters": {"type": "object", "properties": {
            "markdown": {"type": "string", "description": "整篇定稿 markdown"}},
            "required": ["markdown"]}}},
    {"type": "function", "function": {
        "name": "run_gap_audit",
        "description": "把稿子里全部「尚无 / 未见报道 / 缺失 / 空白」类断言拿去**全库核实**"
                       "（逐条转检索式 → 论文级召回 → 判定 → 把命中的库内文献登记成 [S#]）。"
                       "返回：哪几条断言被库内已有文献直接推翻（必须改写或引用）、哪几条需收窄口径、"
                       "哪几条确实未见。**规则**：①凡要写“尚无/未见报道/缺失”，先调它核实，不得凭印象断言；"
                       "②run_review 会自动再核一遍，有冲突断言直接拒送审；③“未检索到”不等于“不存在”。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_review",
        "description": "提交定稿给并行审稿人。脚本会先对定稿跑硬断言，有 ERROR 直接拒绝。"
                       "返回 p0/total/converged；每轮上限 2 次，跑一次 = 本轮结束（轮次自动 +1）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "收口本轮。summary 写清：本轮做了什么、初稿/定稿路径、硬断言结果、审核结果与下一步。"
                       "前置条件：初稿已写则必须至少跑过 1 次 run_probe，否则会被拒绝。",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}},
            "required": ["summary"]}}},
]
FINISH_ONLY = [TOOLS[-1]]


# ═══════════════════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════════════════
def call_agent(messages: list, cfg: dict, tools: list, force_finish: bool = False) -> dict:
    """调 DeepSeek（非流式，带工具）。返回 assistant message dict（保留 reasoning_content）。"""
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["params"].get("temperature", 0.3),
        "max_tokens": cfg["params"].get("max_tokens", 32768),
        "tools": tools,
        "stream": False,
    }
    eff = cfg["params"].get("reasoning_effort")
    if eff:
        payload["reasoning_effort"] = eff
    if force_finish:
        payload["tool_choice"] = {"type": "function", "function": {"name": "finish"}}
    else:
        payload["tool_choice"] = "auto"

    timeout = cfg["params"].get("timeout", 900)
    with httpx.Client(timeout=timeout, trust_env=False) as cl:   # 禁系统代理
        resp = cl.post(f"{cfg['base_url'].rstrip('/')}/chat/completions",
                       json=payload,
                       headers={"Authorization": f"Bearer {cfg['api_key']}",
                                "Content-Type": "application/json"})
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]


# ═══════════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════════
def _asked_questions() -> list[tuple[int, int, str]]:
    """历轮已问过的 kb_query 问题（从 research/_kb_rN_S_slug.md 头部解析）。

    目的是让每轮都看得见「以往问过什么」：不重复问，并在旧问题上往下深挖。
    返回 [(轮次, 该轮第几问, 问题原文)]，按轮次+序号排序。
    """
    out: list[tuple[int, int, str]] = []
    rdir = REVIEWS_ROOT / "research"
    if not rdir.is_dir():
        return out
    for f in rdir.glob("_kb_r*_*.md"):
        m = re.match(r"_kb_r(\d+)_(\d+)", f.name)
        if not m:
            continue
        r, s = int(m.group(1)), int(m.group(2))
        q = ""
        try:
            first = f.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
            mm = re.search(r"（(.+)）\s*$", first)
            if mm:
                q = mm.group(1).strip()
        except Exception:
            pass
        if not q:
            q = re.sub(r"^_kb_r\d+_\d+_", "", f.stem).replace("_", " ").strip()
        out.append((r, s, q))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _asked_block(round_no: int) -> str:
    """历轮已问问题的注入文本（含轮次与调用序号）。"""
    asked = [t for t in _asked_questions() if t[0] < round_no]
    if not asked:
        return ""
    lines = [f"=== 往轮已问过的问题（共 {len(asked)} 问，源自 research/_kb_r*.md）===",
             "不要重复问同样的东西；可以在旧问题基础上追问更深一层（口径/机制/边界）。"]
    cur, buf = None, []
    for r, s, q in asked:
        if r != cur:
            if buf:
                lines.append(" ".join(buf))
            cur, buf = r, [f"\n第 {r} 轮："]
        buf.append(f"  [{r}#{s}] {q[:120]}")
    if buf:
        lines.append(" ".join(buf))
    return "\n".join(lines) + "\n"


def _prev_artifacts(round_no: int) -> tuple[str, list[str]]:
    """上一轮产物全文（追加轮直接注入，省掉一轮 read_file，也避免 read 截断丢内容）。

    返回 (注入文本, 注入清单描述)。读不到的不注入、不报错（首次跑或文件缺失）。
    """
    if round_no <= 1:
        return "", []
    p = round_no - 1
    items: list[tuple[str, Path, str]] = [
        ("上一轮审核意见", REVIEWS_ROOT / "review" / f"review_issues_r{p}.json",
         "逐条消化：type=表述 直接改；type=事实（数值无源/证据缺口/引用错位）回 kb_query 补调研。"),
        ("上一轮初稿（本轮的修订基底）",
         REVIEWS_ROOT / "thinking" / f"round_{p:02d}" / f"draft_r{p}.md",
         "以上一轮初稿为基底修订：未被意见涉及的章节逐字保留，不是从零重写。"),
        ("上一轮事实锚点", REVIEWS_ROOT / "thinking" / f"round_{p:02d}" / "key_findings.md",
         "已核实过的事实直接用，不要重复查。"),
    ]
    parts, names = [], []
    for title, path, hint in items:
        if not path.exists():
            continue
        txt = path.read_text(encoding="utf-8")
        if path.suffix == ".json":      # 去掉解析失败时的 raw 垃圾，减轻上下文
            try:
                d = json.loads(txt)
                for rv in d.get("reviewers") or []:
                    rv.pop("raw", None)
                txt = json.dumps(d, ensure_ascii=False, indent=1)
            except Exception:
                pass
        rel = path.relative_to(REVIEWS_ROOT)
        parts.append(f"===== {title}（{rel}，{len(txt)} 字符）=====\n{txt}\n===== 结束 =====")
        names.append(f"{title}\uff08{rel}）")
        parts.append(f"[用法] {hint}\n")
    if not parts:
        return "", []
    return ("\n=== 上一轮产物（已全文注入，无需再读）===\n" + "\n".join(parts) + "\n", names)


def build_kickoff(round_no: int, prd: str, max_iters: int) -> str:
    parts = [f"=== 当前轮次：第 {round_no} 轮 ===", "",
             "=== 综述 PRD（主题唯一真相源）===", prd, "=== PRD 结束 ===", ""]
    if round_no > 1:
        injected, names = _prev_artifacts(round_no)
        if injected:
            parts += [injected]
            parts += ["=== 追加轮说明 ===",
                      "上面已把上一轮产物全文给你（" + "、".join(names) + "）。",
                      "不要再用 read_file 去读它们；直接基于它们开工。",
                      "其它产物（某次问答全文、证据包、追问记录）可用 read_file 按需读。", ""]
        else:
            parts += ["=== 追加轮说明 ===",
                      f"未找到上一轮产物，请用 read_file 确认 data/reviews/ 下的实际情况。", ""]
        ask = _asked_block(round_no)
        if ask:
            parts += [ask, ""]
    parts += ["=== 循环预算 ===",
              f"本会话最多 {max_iters} 轮 LLM 调用；每 {REMIND_EVERY} 轮会提醒一次；"
              f"到顶后只能调用 finish(summary)。",
              f"每轮脚本预算：kb_query {rounds.BUDGET_LIMITS['kb_qa']} 问（一次可并行 1–4 问）/ "
              f"run_probe {rounds.BUDGET_LIMITS['probe_draft']} 次 / "
              f"run_review {rounds.BUDGET_LIMITS['review_draft']} 次。",
              "工作节奏建议：先并行调研（每次 3–4 问）→ 写初稿 → 跑追问者 → 消化追问 → 写定稿 → 硬断言 → 送审。",
              "", "开始工作。"]
    return "\n".join(parts)


def preflight() -> tuple[bool, str]:
    """开跑前探活：知识库端点（config 的 steps.rag）必须通，否则直接终止，不产生调用费用。"""
    try:
        cfg = get_step_config("rag", load_config())
    except Exception as e:
        return False, f"读不到 rag 步骤配置：{e}"
    url = cfg["base_url"].rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=15, trust_env=False) as cl:   # 禁系统代理
            r = cl.get(url, headers={"Authorization": f"Bearer {cfg['api_key']}"})
            if r.status_code != 200:
                return False, f"知识库端点 {url} 返回 HTTP {r.status_code}"
            ids = [m.get("id") for m in (r.json().get("data") or [])]
            return True, f"知识库端点 OK：{url}（{ids}）"
    except Exception as e:
        return False, f"知识库端点 {url} 不可达：{type(e).__name__}: {e}"


def run(round_no: int, max_iters: int = MAX_ITERS, dry_run: bool = False,
        stop_before_review: bool = False) -> dict:
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"系统提示词缺失：{PROMPT_FILE}")
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
    prd = PRD_FILE.read_text(encoding="utf-8") if PRD_FILE.exists() else "（PRD 缺失）"

    ddir = think_dir(round_no)
    ddir.mkdir(parents=True, exist_ok=True)
    trace = ddir / f"_trace_r{round_no}.md"
    trace.write_text(f"# 主智能体执行轨迹 Round {round_no}"
                     f"（{datetime.datetime.now():%Y-%m-%d %H:%M}）\n", encoding="utf-8")

    if dry_run:
        return {"dry_run": True, "round": round_no, "prompt_chars": len(system_prompt),
                "tools": [t["function"]["name"] for t in TOOLS],
                "prd_chars": len(prd), "trace": str(trace)}

    cfg = get_step_config("agent", load_config())
    ok, msg = preflight()
    print(f"[preflight] {msg}")
    if not ok:
        return {"round": round_no, "preflight": False, "error": msg}

    # ── 成本透明：跑前报预估，不让人跑完才发现账单。失败不拦主流程。──
    try:
        from tools.cost_calculator import print_estimate_for_agent
        print_estimate_for_agent()
    except Exception as _e:
        print(f"[cost] 预估不可用（{_e}）")

    tb = Toolbox(round_no, trace)
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": build_kickoff(round_no, prd, max_iters)}]

    dispatch = {
        "read_file": lambda a: tb.read_file(a["rel_path"]),
        "kb_query": lambda a: tb.kb_query(a.get("questions") or a.get("question") or []),
        "save_key_findings": lambda a: tb.save_key_findings(a["content"], a.get("mode", "append")),
        "write_draft": lambda a: tb.write_draft(a["markdown"]),
        "patch_draft": lambda a: tb.patch_draft(a["old_text"], a["new_text"]),
        "run_probe": lambda a: tb.run_probe(a.get("response", "")),
        "expand_ref": lambda a: tb.expand_ref(a.get("sids") or a.get("sid") or []),
        "run_check": lambda a: tb.run_check(),
        "run_gap_audit": lambda a: tb.run_gap_audit(),
        "list_refs": lambda a: tb.list_refs(a.get("cited_only", True)),
        "write_final": lambda a: tb.write_final(a["markdown"]),
        "run_review": lambda a: tb.run_review(),
        "finish": lambda a: tb.finish(a["summary"]),
    }
    if stop_before_review:
        # 本轮停在审核之前：拦下 run_review，让 agent 直接收口。
        # 定稿已写就保留，但不跑审稿团队、不增加 review 预算、不推进轮次状态。
        def _blocked(_a):
            tb._trace("run_review", {}, "被 --stop-before-review 拦下（用户指定停在此处）")
            return ("[已拦截] 本轮按要求停在审核之前：不要调用 run_review，也不要再改稿。"
                    "请立即调用 finish(summary) 收口，summary 里写清：本轮做了什么、"
                    "初稿/定稿路径、硬断言结果、以及“审核未跑”。")
        dispatch["run_review"] = _blocked

    for it in range(1, max_iters + 1):
        force = (it == max_iters)
        if it % REMIND_EVERY == 0 and it < max_iters:
            gates = [f"[系统提醒] 已用 {it}/{max_iters} 轮，剩 {max_iters - it} 轮。"]
            if stop_before_review and draft_path(round_no).exists():
                gates.append("[用户指令] 本轮**不要**调用 run_review（停在审核之前）；"
                             "定稿写完后直接 finish(summary) 收口。")
            if tb.probe_runs == 0 and draft_path(round_no).exists():
                gates.append("本轮还没跑过 run_probe——它是收口的硬前置（至少 1 次），"
                             "请尽快调用，并至少消化一轮追问后再写定稿。")
            elif tb.probe_runs == 1 and rounds.spent("probe_draft", round_no) < rounds.BUDGET_LIMITS["probe_draft"]:
                if it >= 15:
                    gates.append("已跑过 1 次追问且轮次还有余量——建议再调一次 "
                                 "run_probe(response='…你改了什么…') 做深挖轮：追问者会先判定上轮问题的"
                                 "解决情况，再往下追一层（比一次问很多条更有价值）。若已充分则忽略。")
            gates.append("请检查进度：调研是否够（并行提效，缺证据就再并行查一批）、初稿是否成形、"
                         "追问问题是否已消化、硬断言是否全绿、定稿是否已写。"
                         "剩余轮次要留给 write_final 和 run_review。")
            messages.append({"role": "user", "content": "\n".join(gates)})
        if force:
            messages.append({"role": "user", "content":
                             f"[系统提醒] 已达 {max_iters} 轮上限，现在只能调用 finish(summary) 收口。"})

        print(f"\n───── agent iter {it}/{max_iters} ─────")
        try:
            msg = call_agent(messages, cfg, FINISH_ONLY if force else TOOLS, force_finish=force)
        except Exception as e:
            print(f"[agent] LLM 调用失败：{type(e).__name__}: {e}")
            trace.write_text(trace.read_text(encoding="utf-8")
                             + f"\n## iter {it} LLM ERROR\n{traceback.format_exc()}\n",
                             encoding="utf-8")
            return {"round": round_no, "error": f"{type(e).__name__}: {e}",
                    "iters": it, "finished": False}

        messages.append(msg)   # 保留 reasoning_content（DeepSeek 回放要求）
        calls = msg.get("tool_calls") or []
        if msg.get("content"):
            print(f"[agent] {self_trim(msg['content'])}")
        if not calls:
            print("[agent] 本轮没有工具调用，继续")
            continue

        for c in calls:
            name = c["function"]["name"]
            # LLM 输出参数一律走 loads_llm（json.loads → json_repair）：
            # max_tokens 截断/尾随逗号等畸形 JSON 不会静默退化成空参数
            args = loads_llm(c["function"].get("arguments"))
            print(f"[tool] {name}({json.dumps(args, ensure_ascii=False)[:120]})")
            try:
                result = dispatch[name](args) if name in dispatch else f"[ERROR] 未知工具 {name}"
            except Exception as e:
                result = f"[ERROR] {name} 执行异常：{type(e).__name__}: {e}"
                trace.write_text(trace.read_text(encoding="utf-8")
                                 + f"\n## iter {it} {name} EXCEPTION\n{traceback.format_exc()}\n",
                                 encoding="utf-8")
            print(f"       → {self_trim(result, 200)}")
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": result})

        if tb.finished is not None:
            print(f"\n[agent] finish：{tb.finished}")
            break

    return {"round": round_no, "iters": it, "finished": tb.finished is not None,
            "summary": tb.finished, "steps": [s["name"] for s in tb.steps],
            "trace": str(trace)}


def self_trim(s: str, n: int = 400) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def main():
    ap = argparse.ArgumentParser(description="litreview 综述主智能体 V2.3")
    ap.add_argument("--round", type=int, default=None, help="轮次（缺省读 _round_state.json）")
    ap.add_argument("--max-iters", type=int, default=MAX_ITERS)
    ap.add_argument("--dry-run", action="store_true", help="不调 LLM，只校验提示词/工具/状态")
    ap.add_argument("--stop-before-review", action="store_true",
                    help="跑完定稿后停在审核之前（拦下 run_review，直接收口）")
    ap.add_argument("--print-kickoff", action="store_true",
                    help="【自带智能体模式】只打印开场上下文（PRD + 上轮产物 + 已问过的问题），不调 LLM")
    args = ap.parse_args()

    if args.print_kickoff:
        # 模式 B（BYO）：让用户自己的 agent 拿到与模式 A 完全一致的开场上下文。
        r = args.round or rounds.get_round()
        prd = PRD_FILE.read_text(encoding="utf-8") if PRD_FILE.exists() else "（PRD 缺失）"
        print(f"# 【模式 B / 自带智能体】开场上下文 · Round {r}\n"
              f"> 先把 `{PROMPT_FILE.relative_to(PROJECT).as_posix()}` 全文当作业纪律读入（照做），\n"
              f"> 再读下面这份开场消息；随后按 SKILL.md「模式 B 步骤手册」逐步执行。\n")
        print(build_kickoff(r, prd, args.max_iters))
        return 0

    r = args.round or rounds.get_round()
    res = run(r, max_iters=args.max_iters, dry_run=args.dry_run,
              stop_before_review=args.stop_before_review)
    print("\n=== 运行结果 ===")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
