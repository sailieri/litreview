"""追问工具（思考环节 SOP ③）。

主智能体产出大纲+关键发现（追加轮再拼修改意见）后调用本工具；
本工具以苏格拉底视角挑刺（覆盖缺口 / 断言无据 / 结构隐患 / 可比性），
返回追问问题清单，由主智能体自行决定：直接修订 or 调 ask_kb 补调研后修订。

设计要点：追问者是「工具」而非对等角色——拼接什么由 think_agent 决定，
本模块只负责一次 LLM 调用 + 审计落盘（thinking/probe_rN.md）。
"""
import datetime
from pathlib import Path

from modules.util.llm_client import get_step_config, call_llm, load_config, extract_json
from modules.review.paths import THINKING

PROBE_SYS = """你是这篇综述的「内部评审读者」——身份是领域内的同行/答辩委员，任务是**读懂内容之后**从内容上判断它站不站得住。
你不做格式检查：长段落、模板句、自造术语、流程词、篇幅是否平均，这些有独立的审稿流程专门管，你重复只会浪费预算。

**第一步：先重建文稿的「对象—覆盖地图」（必做，写进 assessment）**
把文中讨论的对象按类别列出来（类别由主题决定：如能源类型、技术路线、数据源、区域、时间尺度、模型族、作物/材料种类……），
对每一类问三件事：
  ① 这一类的主要形态/分支，文中都出现了吗？哪些整篇没提？
  ② 只写了其中一支时，作者交代了为什么只用这一支吗？
  ③ 读者读完会不会把这一支当成整体？（最容易被忽略、也最伤可信度的毛病）
（示例，不是领域知识）某类对象的某一分支整篇未提且未说明取舍理由——这就是必须追问的问题。
这一步不看具体领域：任何主题都能列「对象 → 分支 → 本文覆盖情况」；分支清单从 PRD 的语料边界与正文实际内容推导，不要套外部清单。

**第二步：按五类追问内容层面的问题**（覆盖优先）：
  ① 范围与代表性：某类对象只覆盖了一支；某区域/时段/场景整篇缺席；取舍理由未交代；
  ② 断言无据：数值或强命题凭什么是这个说法，条件与口径在哪；
  ③ 机理落地：只报了现象/数字，没解释为什么，也没说这个机制对结论意味着什么；
  ④ 逻辑与可比性：章间递进是否成立、跨研究数值是否在同口径下比较、结论与证据是否对得上；
  ⑤ 读者会误解之处：范围、适用条件、外推边界没交代清楚。

追问的写法：
- 每条点明【章节/小节，或“整篇”】+【你怀疑缺的是什么】+【若成立对结论意味着什么】。
- 「缺席」类追问不需指向具体数值：例“第 2.1 节只覆盖了该类别的一个分支，其余分支整篇未提——体量主体缺席，读者会把那一支当成整体，需补上或交代取舍理由”。
- 数量 4–8 条，按重要性排序；不要凑数，也不要把一件事拆成几条。

（多轮追问）第二轮起你会看到自己上一轮的问题与主智能体的处理说明：先判「哪些真解决了、哪些只糊了一层」，
再往下深挖一层或转向文稿尚未触及的方面。同一件事要一层比一层深（如：一轮问“这个系数出自哪篇”→
二轮问“它的口径与其它章节的数字是否可比”）。不要重复问已交代清楚的东西，也不要老问题换说法。
（文中已给证据不要重复问，只追问没交代清楚的部分。）

输出严格 JSON（不要 markdown fence）：
{"assessment":"<先给对象—覆盖地图（列对象类别 + 每类覆盖情况），再两句话评价最强处与最薄弱处>",
 "resolved":[{"q":"<上一轮的问题>","verdict":"RESOLVED|PARTIAL|UNRESOLVED","note":"<一句话理由>"}],
 "questions":["<追问1>","<追问2>",...]}
（首次追问时 resolved 可为空数组）"""


def assemble_deliverables(round_no: int, filenames: list[str] | None = None, ddir=None) -> str:
    """拼接当前已落盘的交付文件（供追问 / 收口校验）。缺省拼 大纲+关键发现+修改意见。

    ddir：轮次交付目录（交付文件按轮次隔离在 thinking/round_0N/）；None 回退顶层。"""
    if filenames is None:
        filenames = ["outline.md", "key_findings.md", "revision_notes.md"]
    base = ddir or THINKING
    parts = []
    for name in filenames:
        p = base / name
        if p.exists():
            parts.append(f"### 文件：{name}\n\n{p.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(parts) or "（交付文件尚未落盘）"


def _fmt_history(history: list[dict], response: str) -> str:
    """把上一轮追问 + 主智能体回应拼成第二轮起的前置说明。"""
    last = history[-1]
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(last.get("questions") or [], 1))
    out = (f"=== 上一轮（第 {last.get('seq', 1)} 次追问）你提出的问题 ===\n{qs}\n\n"
           f"=== 主智能体对上一轮的回应（它做了什么修改）===\n"
           f"{response.strip() or '（主智能体未提供说明——请直接对照下方更新后的草稿自行判断每条是否已解决）'}\n")
    return out


def run(deliver_text: str, round_no: int = 1, ddir=None,
        history: list[dict] | None = None, response: str = "", seq: int = 1) -> dict:
    cfg = get_step_config("socratic", load_config())
    head = f"=== 当前轮次：第 {round_no} 轮 ===\n\n"
    if history:
        head += _fmt_history(history, response) + "\n"
    user = (head +
            f"=== 思考主智能体当前交付拼接 ===\n{deliver_text}\n=== 结束 ===\n"
            + ("请先逐条判定上一轮问题是否真的解决，再继续追问（可深挖一层，也可提新方面）。"
               if history else "请追问。"))
    raw = call_llm([{"role": "system", "content": PROBE_SYS},
                    {"role": "user", "content": user}],
                   cfg, max_tokens=4000, temperature=0.4, stream=False)  # 大上下文流式会卡死，同 think_agent
    # 上限过小会把“对象—覆盖地图 + 4–8 条追问”截断在半路，追问者只能给出浅问题。
    data = extract_json(raw)
    if not isinstance(data, dict):
        data = {}
    questions = [q.strip() for q in (data.get("questions") or [])
                 if isinstance(q, str) and q.strip()]
    resolved = [r for r in (data.get("resolved") or []) if isinstance(r, dict)]
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    suffix = "" if seq <= 1 else f"_{seq}"
    out = (ddir or THINKING) / f"probe_r{round_no}{suffix}.md"
    body = (f"# 追问记录 Round {round_no} · 第 {seq} 次（{ts}）\n\n"
            f"## 总体评价\n{data.get('assessment', '（解析失败，原始输出见下）')}\n")
    if resolved:
        body += "\n## 上一轮问题的处理判定\n" + "\n".join(
            f"- [{r.get('verdict', '?')}] {r.get('q', '')} — {r.get('note', '')}" for r in resolved)
    body += ("\n## 追问\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)))
    if response:
        body += f"\n\n## 主智能体回应摘要\n{response.strip()[:1200]}"
    if not questions:
        body += f"\n\n## 原始输出\n```\n{raw[:2000]}\n```"
    out.write_text(body, encoding="utf-8")
    print(f"[probe] 第 {seq} 次追问：{len(questions)} 条 -> {out.name}")
    return {"assessment": data.get("assessment", ""), "questions": questions,
            "resolved": resolved, "seq": seq, "file": str(out)}


if __name__ == "__main__":
    import sys
    txt = sys.stdin.read() or assemble_deliverables(1)
    import json as _j
    print(_j.dumps(run(txt, 1), ensure_ascii=False, indent=1))
