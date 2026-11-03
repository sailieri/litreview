#!/usr/bin/env python3
"""litreview 知识库问答智能体（单文件）。

连续对话 + RAG 检索 + 只读 SQL 直查 papers.db。设计核心：
- 高效：工具结果截断后进 LLM 上下文（省 token、稳 prefix cache），完整原文进注册表
- 引用口子：模型回答内联 [S#] 标记，render_answer() 解析为「正文 + 引用来源」，
  引用自动附完整内容与元数据（论文/年份/章节/chunk_id），下游撰写直接取用

用法:
  python3 kb_agent.py                     # REPL 连续对话
  python3 kb_agent.py --q "问题"          # 单问
程序化:
  from modules.kb_agent import KBAgent
  agent = KBAgent()
  out = agent.ask("...")   # {"text": "...[S1]...", "citations": [{id, type, content, metadata}]}
  print(render_answer(out))
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx

# 仓库根（用于定位 config.yaml）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.util.llm_client import load_config, get_step_config
from modules.util.jsonllm import loads_llm
from modules.database import retriever as R
from modules.paths import data_root as _data_root

PROJECT = Path(__file__).resolve().parents[1]
MAX_TOOL_ROUNDS = 4          # 单次提问最多检索轮数（多路检索通常 3–4 轮收敛；单轮成本低，成本主要在最终作答）
EXCERPT_CHARS = 700          # chunk 进上下文的截断（靠“多看几篇”而不是“单篇看全”）
SECTION_CHARS = 1600         # 章节进上下文的截断
SQL_ROW_LIMIT = 50           # sql_query 最大返回行数
SQL_CELL_CHARS = 400         # sql_query 单元格截断
HISTORY_KEEP = 40            # 对话历史上限（超出裁掉最旧，保留 system）
FINAL_MAX_TOKENS = 8192      # 最终作答上限。只作为失控保险，不用来限制回答长度：
                             # 回答长度取决于问题复杂度，限长会明显减少引用数量。
                             # 取最长合理回答（约 2100 token）的 4 倍；真被截断也能靠 json_repair 完整取回。


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"…(截断，共{len(s)}字符，完整内容经 [S#] 引用获取)"


# ═══════════════════════════════════════════════════════════════════════
# 来源注册表：全文 + 元数据在这里，LLM 上下文里只有截断版 + [S#]
# ═══════════════════════════════════════════════════════════════════════
class SourceRegistry:
    def __init__(self):
        self.sources = {}          # sid -> {"type","content","metadata"}
        self._dedup = {}           # dedup_key -> sid
        self._counter = 0          # 论文级证据编号（S#）
        self._counter_e = 0        # 跨文献超边编号（E#）

    def register(self, stype: str, content: str, metadata: dict) -> str:
        key = (stype, metadata.get("key") or hashlib.md5(
            (json.dumps(metadata, sort_keys=True, ensure_ascii=False) + content[:200])
            .encode()).hexdigest())
        if key in self._dedup:
            return self._dedup[key]
        if stype == "hyperedge":   # 超边独立命名空间（跨文献证据，引用语义不同）
            self._counter_e += 1
            sid = f"E{self._counter_e}"
        else:
            self._counter += 1
            sid = f"S{self._counter}"
        self.sources[sid] = {"type": stype, "content": content, "metadata": metadata}
        self._dedup[key] = sid
        return sid

    def lookup_cited(self, text: str) -> list[dict]:
        """解析文本中的 [S#] 标记，按出现顺序返回引用记录（含全文与元数据）。"""
        seen, out = set(), []
        for sid in re.findall(r"\[([SE]\d+)\]", text):
            if sid in self.sources and sid not in seen:
                seen.add(sid)
                rec = self.sources[sid]
                out.append({"id": sid, "type": rec["type"],
                            "content": rec["content"], "metadata": rec["metadata"]})
        return out

    def summary(self) -> str:
        lines = []
        for sid, rec in self.sources.items():
            m = rec["metadata"]
            label = m.get("paper_title") or m.get("label") or m.get("sql") or ""
            lines.append(f'{sid} [{rec["type"]}] {label}')
        return "\n".join(lines) if lines else "（空）"


# ═══════════════════════════════════════════════════════════════════════
# 工具定义（只读）
# ═══════════════════════════════════════════════════════════════════════
TOOLS = [
    {"type": "function", "function": {
        "name": "rag_search",
        "description": "粗检索（语义向量+BM25 混合）。**复合问题先拆成 2–4 条原子子查询用 queries 传（比单条长查询准得多）**；单一问题用 query。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "单条检索问题（中英文均可）"},
            "queries": {"type": "array", "items": {"type": "string"},
                        "description": "多条原子子查询（并行召回后按名次合并）"},
            "top_k": {"type": "integer", "description": "返回条数，默认12，同篇最多2条"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "gap_check",
        "description": "核实“库内到底有没有关于 X 的文献”：按论文级召回，返回相关论文清单（题录+最匹配片段+命中统计）。判断某主题是否为空白、或要写“库中未见/未检索到”之前，必须先跑一次。",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "要核实的主题；可用分号分隔多个同义表述以提升召回"},
            "top_papers": {"type": "integer", "description": "返回论文数，默认25"}},
            "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "search_hyperedges",
        "description": "检索实体与跨文档关系超边。适合'X与Y的联系''哪些论文对比过方法Z'等论文间关系问题。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "top_k": {"type": "integer", "description": "默认6"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_sections",
        "description": "FTS全文检索章节标题与内容，返回带高亮片段。适合定位术语/数值出现位置。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "description": "默认8"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_papers",
        "description": "列出知识库全部论文元数据（id/标题/作者/年份/期刊）。", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_paper_details",
        "description": "看某论文的摘要、关键词与章节树（从粗到细的中粒度一步）。拿到 section_id 后用 read_section 精读。",
        "parameters": {"type": "object", "properties": {
            "paper_id": {"type": "integer"}}, "required": ["paper_id"]}}},
    {"type": "function", "function": {
        "name": "read_section",
        "description": "读取某论文章节全文（可指定 section_id）。用于精读定位到的章节提取精确数值。",
        "parameters": {"type": "object", "properties": {
            "paper_id": {"type": "integer"}, "section_id": {"type": "integer"}},
            "required": ["paper_id"]}}},
    {"type": "function", "function": {
        "name": "sql_query",
        "description": "只读SQL直查数据库（仅SELECT/WITH）。适合精确统计与筛选。主要表：papers(id,title,authors,year,journal,doi), sections(id,paper_id,title,level,parent_id,content), chunks(id,paper_id,section_id,content), entities(name,type,description,paper_id), hyperedges(description,participants,source_papers)。另有各表对应FTS5虚拟表。",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "单条SELECT语句，建议带LIMIT"}}, "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "submit_answer",
        "description": "提交最终回答。检索完成后（或确认库中无相关信息后）必须调用本工具作答，不能用普通文本作答。",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string", "description": "最终回答全文。先结论后论据；每个来自检索的事实句末标注来源编号 [S#]（编号见工具返回的 sid 字段）；未检索到的部分明确说明，不要臆测。"}},
            "required": ["answer"]}}},
]
# 收口轮专用：强制模型只能调用 submit_answer
SUBMIT_TOOL = [TOOLS[-1]]


def _db_ro_path() -> str:
    conn = R.get_db()
    row = conn.execute("PRAGMA database_list").fetchone()
    conn.close()
    return row[2]


def _authors_short(a) -> list:
    """作者字段（逗号分隔字符串）→ 前 3 个作者姓名列表；无则空表。"""
    if not a:
        return []
    names = [x.strip() for x in re.split(r"\s*[,，]\s*", str(a)) if x.strip()]
    return names[:3]


def _paper_meta(conn, paper_id) -> dict:
    row = conn.execute("SELECT title, year, journal, authors FROM papers WHERE id=?", (paper_id,)).fetchone()
    return {"paper_id": paper_id, "paper_title": row["title"] if row else None,
            "year": row["year"] if row else None, "journal": row["journal"] if row else None,
            "authors": _authors_short(row["authors"] if row else None)}


def _paper_brief(paper_ids) -> dict:
    """批量取 paper_id -> (year, journal, authors_short)，供检索工具补全引用题录。"""
    ids = [int(p) for p in paper_ids if p is not None]
    if not ids:
        return {}
    conn = R.get_db()
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT id, year, journal, authors FROM papers WHERE id IN ({marks})", ids).fetchall()
        return {r["id"]: (r["year"], r["journal"], _authors_short(r["authors"])) for r in rows}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 智能体
# ═══════════════════════════════════════════════════════════════════════
# 主题上下文：来自 PRD（唯一真相源），**不在代码里写死领域**。
# 领域主题只来自 PRD（meta/_prd.md）：写死在代码里，换库后问答会沿用与当前库不符的领域边界。
def _load_theme_block() -> str:
    """本库当前综述主题（读 PRD 前部：主题与定位 + 语料边界）。读不到则退化为中性声明。

    主题必须跟数据集走：写成固定路径时，换库（`LITREVIEW_DATA` / config.yaml `data_root`）
    后主题声明会与库内容不匹配，智能体会以「不在主题范围」拒答或按错误领域检索。
    候选顺序（与 indicators.yaml 同一模式）：
      ① {data_root}/prd.md                  ← 数据集自带主题（推荐，新库只放这一个文件）
      ② {data_root}/reviews/meta/_prd.md    ← 兼容既有库的目录位置
    两个候选都不存在时退化为中性声明（不预设任何领域）。
    """
    head = ""
    for p in (_data_root() / "prd.md", _data_root() / "reviews" / "meta" / "_prd.md"):
        try:
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                if raw.strip():
                    head = raw[:2600]   # 只取前部，避免整份 PRD 进 system
                    break
        except Exception:
            continue
    if not head.strip():
        head = "（未找到 PRD：{data_root}/prd.md）知识库主题以库内实际内容为准，不要预设任何领域。"
    return ("\n=== 本库当前综述主题（来自 PRD，唯一真相源）===\n" + head +
            "\n=== 主题声明结束 ===\n"
            "你的检索与回答必须落在上述主题范围内：主题之外的内容不得作为回答依据，"
            "被问到时应说明「该内容不在当前综述主题范围内」。")


THEME_BLOCK = _load_theme_block()


def _extract_answer(msg: dict) -> str:
    """从一次模型回复里取最终回答：优先 submit_answer 参数，其次正文。

    解析一律走 `loads_llm`（json.loads → json_repair），
    被 max_tokens 截断的半截 JSON 也能完整解析出 answer 字段，无需自行拼正则。
    即使拿不到答案，也返回空串由调用方重试，不在此处污染回答内容。
    """
    for c in msg.get("tool_calls") or []:
        if c.get("function", {}).get("name") != "submit_answer":
            continue
        args = loads_llm(c["function"].get("arguments"))
        if isinstance(args, dict) and (args.get("answer") or "").strip():
            return args["answer"].strip()
    return (msg.get("content") or "").strip()


def _load_catalog() -> str:
    """全库题录（id + [年] + 完整标题）：注入 system，作为所有 kb agent 的公共前缀。

    库内题录不必预设会被引用哪些论文：“库里有没有 X”这类判断需要全库视野，
    只靠检索容易漏掉库里本来就有、但没被召回的内容。题录随 system 一起进 prefix cache，
    并行调用只付一次 prefill，因此不必省。
    """
    try:
        conn = R.get_db()
        rows = conn.execute("SELECT id, year, title FROM papers ORDER BY id").fetchall()
        conn.close()
        lines = [f'{r["id"]} [{r["year"]}] {r["title"]}' for r in rows]
        return (f"\n=== 知识库全库题录（共 {len(lines)} 篇，按 id 排序）===\n"
                + "\n".join(lines) + "\n=== 题录结束 ===\n"
                "这是全库的完整清单。回答前先在这里自查：问题涉及的论文是否已出现。\n"
                "但标题未必包含你要找的术语（同一概念有多种表述），清单里没看到 ≠ 库里没有，\n"
                "仍须用 rag_search / gap_check / sql_query 兜底。凡要写“库中未见/未检索到”，\n"
                "必须先用 gap_check 核实一次。\n")
    except Exception as e:
        return f"\n（全库题录读取失败：{type(e).__name__}: {e}）\n"


CATALOG = _load_catalog()

SYSTEM_PROMPT = """你是 litreview 文献知识库的问答智能体，服务一位撰写学术综述的研究者。知识库为 papers.db（论文元数据、章节树、分块全文、实体与超边）。
""" + THEME_BLOCK + CATALOG + """

上游是思考环节：它只负责提出详细、具体的问题，不预设检索关键词；你负责把问题转化为检索词并执行检索。

**检索是从粗到细的过程，按这个顺序推进**（共 5 轮，每轮可并发多个工具调用）：
- 第 1 步（粗）`rag_search`：拿候选论文与片段。**复合问题先拆成 2–4 条原子子查询放进 `queries`**——
  长查询（把多个子主题揉成一句）会把正确文献排到 300 名以外，拆开后能拉到前几名。
  每次返回最多 12 条，同一篇论文最多 2 条。
- 第 2 步（中）`get_paper_details(paper_id)`：看这篇的**摘要、关键词与章节树**，判断哪几节真正相关。
- 第 3 步（细）`read_section(paper_id, section_id)`：按 section_id 读该节原文，取精确数值与口径。
- 第 4–5 步：补检索、下钻其它候选论文，或跑 `gap_check` 核实“库里是否真的没有”。
不要跳过第 2 步直接猜章节，也不要把 5 轮全花在 rag_search 上。

可用工具与适用场景：
- rag_search：粗检索（支持单条 query 或多条 queries 并行合并）
- gap_check：**“库内到底有没有关于 X 的文献”**——论文级召回，拿不准某主题是否是空白时用
- search_hyperedges：论文间关系（对比/传承/矛盾）
- search_sections：定位术语或数值的位置
- list_papers / get_paper_details / read_section：浏览库、看摘要与章节树、精读某节
- sql_query：精确统计筛选（如“2020 年后关于某类方法的论文数”）
- submit_answer：最终回答只能通过此工具提交，不能用普通文本作答

工作准则：
1. 检索预算共 5 轮（每轮可并发多个工具调用），用完系统会强制收口。规划好每一轮查什么。
2. 你是筛选者：综合、对比、提炼后作答，绝不把检索原文大段复制进正文——原文经 [S#] 引用由系统单独附上。
3. 引用纪律：回答中每个来自检索的事实，句末标注对应来源编号如 [S3]（编号见工具返回的 sid 字段）。
4. 所有数值必须来自工具返回，禁止凭记忆作答。
   **关于“库中没有”**：只有在①题录自查过、②用 rag_search 或 gap_check 实际检索过之后，
   才能说“库中未检索到”，并写明用了哪些关键词与工具；不得因单次检索无命中就断言不存在。
5. 中文回答，简洁、有结构，先结论后论据。**不设字数上限——以把问题说清为准**：
   - 问题复杂就写长，简单就不必凑长度；宁可长而完整，不可短而缺证据。
   - 每个子主题都要有落点：结论 + 关键数值（带条件与口径）+ **来源编号 [S#]**；
     引用是你判断过“对这个问题有帮助”的证据，**一条都不要省**——省掉一条，下游就少一份可用材料。
   - 唯一要避免的浪费是**复述原文**：引用块里已有完整原文，回答里不必重复。
6. 给数值时带上条件与口径（区域/林型/产品层级/验证基准）；若同一指标在不同研究里不可比，说明为何不可比。
7. 回答面向“写综述的人”而非“查资料的人”：除了事实，也给出这些事实之间的关系（一致 / 矛盾 / 互补）与适用边界。"""

CITE_NOTE = "工具返回中 sid=编号即引用编号：S#=论文证据片段，E#=跨文献超边；回答时在事实句末标注对应编号（[S#] 或 [E#]），不要输出 sources 原文。"


class KBAgent:
    def __init__(self, step: str = "rag"):
        cfg = load_config(str(PROJECT / "config.yaml"))
        self.cfg = get_step_config(step, cfg)
        # 端点覆盖：KB_AGENT_BASE_URL 优先于 config（端点地址可能临时变更）
        import os
        if os.environ.get("KB_AGENT_BASE_URL"):
            self.cfg["base_url"] = os.environ["KB_AGENT_BASE_URL"].rstrip("/")
        self.registry = SourceRegistry()
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._seen_chunks: dict = {}      # chunk_id -> sid：同一片段不重复进上下文
        self.last_result = None
        # trust_env=False：不读系统代理，避免本地请求被代理劫持
        self._client = httpx.Client(timeout=self.cfg["params"].get("timeout", 300),
                                    trust_env=False)

    # ── LLM 调用（带 tools；默认流式）────────────────────────────
    def _chat(self, messages: list, tools: list | None, force_tool: bool = False):
        """一次模型调用，返回 message dict（含 content / tool_calls）。

        默认流式：非流式下远程网关常有响应时长上限（超时整次问答直接失败），
        而“回答不预设字数上限”会拉长生成时间，与这个上限直接冲突。
        流式下字节持续回流，不受该上限约束，也就不必靠截断回答来控制时长。
        （KB_NOSTREAM=1 可切非流式，仅用于排查。）
        """
        payload = {
            "model": self.cfg["model"], "messages": messages,
            "temperature": self.cfg["params"].get("temperature", 0.1),
            "max_tokens": FINAL_MAX_TOKENS, "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # force_tool: 收口轮强制模型调用 submit_answer（tool_choice 级约束，非提示词求恳）
            payload["tool_choice"] = {"type": "function",
                                      "function": {"name": tools[-1]["function"]["name"]}} \
                if force_tool else "auto"
        if "qwen" in self.cfg["model"].lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        url = f'{self.cfg["base_url"].rstrip("/")}/chat/completions'
        headers = {"Authorization": f'Bearer {self.cfg["api_key"]}'}
        _t0 = time.time()
        if os.environ.get("KB_NOSTREAM"):
            resp = self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            _body = resp.json()
        else:
            _body = self._stream_chat(url, headers, payload)
        if os.environ.get("KB_DEBUG_CTX"):
            u = _body.get("usage") or {}
            ch0 = (_body.get("choices") or [{}])[0]
            print(f"[kb-call] {time.time()-_t0:6.1f}s prompt={u.get('prompt_tokens')} "
                  f"completion={u.get('completion_tokens')} finish={ch0.get('finish_reason')} "
                  f"msgs={len(messages)}", file=sys.stderr)
        return _body["choices"][0]["message"]

    def _stream_chat(self, url: str, headers: dict, payload: dict) -> dict:
        """流式调用并重组成非流式的 response 形状（choices[0].message + usage）。

        tool_calls 在流式下按 index 分片下发（id/name 首片给全，arguments 逐片拼接），
        这里按 index 累加；拼接完成的 arguments 仍用 loads_llm 解析，容忍半截 JSON。
        """
        payload = dict(payload, stream=True, stream_options={"include_usage": True})
        content: list[str] = []
        slots: dict[int, dict] = {}
        finish, usage = None, {}
        with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except Exception:                       # 极少见：跳过坏帧，不让它毁掉整次调用
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        content.append(d["content"])
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        slot = slots.setdefault(i, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
        msg = {"role": "assistant", "content": "".join(content) or None}
        if slots:
            msg["tool_calls"] = [slots[i] for i in sorted(slots)]
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    # ── 工具执行 ────────────────────────────────────────────────
    def _reg(self, stype, content, metadata) -> str:
        return self.registry.register(stype, content, metadata)

    def _exec_tool(self, name: str, args: dict) -> str:
        try:
            fn = getattr(self, f"_tool_{name}", None)
            if fn is None:
                return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
            return fn(args)
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)

    def _tool_rag_search(self, a) -> str:
        qs = a.get("queries") or ([a["query"]] if a.get("query") else [])
        qs = [q for q in qs if isinstance(q, str) and q.strip()]
        if not qs:
            return json.dumps({"error": "需要 query 或 queries 参数"}, ensure_ascii=False)
        k = int(a.get("top_k", 12))
        if len(qs) == 1:
            rows = R.search_chunks(qs[0], top_k=k, max_per_paper=2)
        else:
            rows = R.search_chunks_multi(qs, top_k=k, max_per_paper=2)
        brief = _paper_brief({r["paper_id"] for r in rows})
        sources = []
        for r in rows:
            content = r["content"]
            yr, journal, authors = brief.get(r["paper_id"], ("", "", []))
            meta = {"key": f'chunk_{r["chunk_id"]}', "chunk_id": r["chunk_id"],
                    "paper_id": r["paper_id"], "paper_title": r["paper_title"],
                    "year": yr, "journal": journal, "authors": authors,
                    "section_title": r["section_title"]}
            sid = self._reg("chunk", content, meta)
            prev = self._seen_chunks.get(r["chunk_id"])
            item = {"sid": sid, "paper_id": r["paper_id"], "paper_title": r["paper_title"],
                    "section_title": r["section_title"]}
            if prev:
                # 同一片段重复返回：只给引用行，不再重复正文（5 轮预算容易被重复内容吃掉）
                item["note"] = f"（已在本会话前一轮返回过，编号 {prev}；正文省略）"
            else:
                self._seen_chunks[r["chunk_id"]] = sid
                item["excerpt"] = _truncate(content, EXCERPT_CHARS)
            sources.append(item)
        return json.dumps({"queries": qs, "sources": sources, "CITE_NOTE": CITE_NOTE}, ensure_ascii=False)

    def _tool_gap_check(self, a) -> str:
        topic = (a.get("topic") or "").strip()
        if not topic:
            return json.dumps({"error": "需要 topic 参数"}, ensure_ascii=False)
        res = R.gap_check(topic, top_papers=int(a.get("top_papers", 12)))
        # 只保留题录级信息 + 片段，避免把 25 篇的全文塞进上下文
        for i, p in enumerate(res.get("papers", []), 1):
            p["rank"] = i
            p["snippet"] = _truncate(p.get("snippet") or "", 180)
            p.pop("vector_rank", None)
            p.pop("bm25_rank", None)
        sid = self._reg("gap_check", json.dumps(res, ensure_ascii=False),
                        {"key": f'gap_{hashlib.md5(topic.encode()).hexdigest()[:8]}', "topic": topic})
        res["sid"] = sid
        res["usage"] = ("若清单里有相关论文 → 用 get_paper_details + read_section 下钻；"
                        "若清单里确实没有 → 方可写“库中未检索到”，并注明检索途径。")
        return json.dumps(res, ensure_ascii=False)

    def _tool_search_hyperedges(self, a) -> str:
        rows = R.search_hyperedges(a["query"], top_k=int(a.get("top_k", 6)))
        sources = [{"sid": self._reg("hyperedge", r["description"],
                                     {"key": f'edge_{r["scope"]}_{r["id"]}',
                                      "edge_id": r["id"], "scope": r["scope"],
                                      "participants": r["participant_names"],
                                      "source_papers": r["source_papers"],
                                      "label": " → ".join(r["participant_names"])}),
                    "participants": r["participant_names"],
                    "description": _truncate(r["description"], EXCERPT_CHARS),
                    "source_papers": r["source_papers"]} for r in rows]
        return json.dumps({"sources": sources, CITE_NOTE: ""}, ensure_ascii=False)

    def _tool_search_sections(self, a) -> str:
        rows = R.search_sections(a["query"], limit=int(a.get("limit", 8)))
        brief = _paper_brief({r["paper_id"] for r in rows})
        sources = []
        for r in rows:
            yr, journal, authors = brief.get(r["paper_id"], ("", "", []))
            # search_sections 只回 snippet（FTS 高亮片段），引用块展示完整章节内容
            content = R.read_section(int(r["paper_id"]), int(r["id"]))[0]["content"]
            sid = self._reg("section_hit", content,
                            {"key": f'sec_{r["id"]}', "section_id": r["id"],
                             "paper_id": r["paper_id"], "paper_title": r["paper_title"],
                             "year": yr, "journal": journal, "authors": authors,
                             "section_title": r["section_title"]})
            sources.append({"sid": sid, "paper_title": r["paper_title"],
                            "section_title": r["section_title"],
                            "snippet": r["snippet"]})
        return json.dumps({"sources": sources, CITE_NOTE: ""}, ensure_ascii=False)

    def _tool_list_papers(self, a) -> str:
        rows = R.list_papers()
        listing = "\n".join(f'{r["id"]}. [{r["year"]}] {r["title"]} — {r["journal"]}' for r in rows)
        sid = self._reg("papers_list", listing, {"label": f"全部论文列表({len(rows)}篇)"})
        return json.dumps({"total": len(rows), "sid": sid, "papers": rows}, ensure_ascii=False)

    def _tool_get_paper_details(self, a) -> str:
        d = R.get_paper_details(int(a["paper_id"]))
        if not d:
            return json.dumps({"error": f"paper_id={a['paper_id']} 不存在"}, ensure_ascii=False)
        meta = {k: d.get(k) for k in ("id", "title", "authors", "year", "journal", "doi")}
        sid = self._reg("paper_detail", json.dumps(d["sections"], ensure_ascii=False), meta)
        out = {"sid": sid, **meta,
               "summary": _truncate(d.get("summary") or "（库中此篇无摘要）", 1200),
               "entities": [e.get("name") for e in (d.get("entities") or [])],
               "sections": d["sections"],
               "next": "挑 1–3 个相关 section 的 id，用 read_section 精读"}
        return json.dumps(out, ensure_ascii=False)

    def _tool_read_section(self, a) -> str:
        rows = R.read_section(int(a["paper_id"]), a.get("section_id"))
        conn = R.get_db()
        sources = []
        for r in rows[:4]:
            meta = _paper_meta(conn, int(a["paper_id"]))
            meta.update({"key": f'sec_{r["id"]}', "section_id": r["id"],
                         "section_title": r["title"]})
            sid = self._reg("section", r["content"], meta)
            sources.append({"sid": sid, "section_title": r["title"], "level": r["level"],
                            "content": _truncate(r["content"], SECTION_CHARS)})
        conn.close()
        if a.get("section_id") is None:
            sources = sources[:1]
            sources[0]["note"] = "未指定section_id，仅返回首章；请先用 get_paper_details 看章节树再精读"
        return json.dumps({"sources": sources, CITE_NOTE: ""}, ensure_ascii=False)

    def _tool_sql_query(self, a) -> str:
        sql = a["sql"].strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", sql):
            return json.dumps({"error": "仅允许 SELECT/WITH 只读查询"}, ensure_ascii=False)
        if not Path(_db_ro_path()).exists():
            return json.dumps({"error": "知识库还没建：先入库至少一篇论文再提问"},
                              ensure_ascii=False)
        conn = sqlite3.connect(f"file:{_db_ro_path()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(SQL_ROW_LIMIT + 1)
        finally:
            conn.close()
        truncated = len(rows) > SQL_ROW_LIMIT
        rows = rows[:SQL_ROW_LIMIT]
        data = [[_truncate(str(v), SQL_CELL_CHARS) for v in r] for r in rows]
        table = {"columns": cols, "rows": data, "row_count": len(data),
                 "note": "结果已截断" if truncated else ""}
        sid = self._reg("sql", json.dumps(table, ensure_ascii=False), {"key": f'sql_{hashlib.md5(sql.encode()).hexdigest()[:8]}', "sql": sql})
        return json.dumps({"sid": sid, **table}, ensure_ascii=False)

    # ── 主循环 ──────────────────────────────────────────────────
    def ask(self, question: str) -> dict:
        self.history.append({"role": "user", "content": question})
        answer = None
        for rnd in range(MAX_TOOL_ROUNDS):
            msg = self._chat(self.history, TOOLS)
            calls = msg.get("tool_calls")
            # 模型主动提交：取 submit_answer 参数作为最终回答
            if calls and calls[0]["function"]["name"] == "submit_answer":
                answer = _extract_answer(msg)
                if answer:
                    self.history.append({"role": "assistant", "content": "", "tool_calls": calls})
                    self.history.append({"role": "tool", "tool_call_id": calls[0]["id"],
                                         "content": "已提交。"})
                    break
                # answer 为空则视为无效提交，落入正常工具处理报错
            if not calls:
                # 不允许普通文本作答：把这段文本转成交给 submit_answer 的要求
                self.history.append({"role": "assistant", "content": msg.get("content") or ""})
                self.history.append({"role": "user", "content":
                                     "（系统）最终回答必须通过 submit_answer 工具提交。"
                                     "若已检索完毕，请立即调用 submit_answer（answer=完整回答，"
                                     "事实句末标注 [S#]）；若还需检索，继续调用检索工具。"})
                continue
            self.history.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})
            # 并发执行本轮全部工具调用（submit_answer 之外的调用按普通工具处理）
            def run(tc):
                args = loads_llm(tc["function"].get("arguments"))   # 截断/畸形 JSON 均能取回参数
                if tc["function"]["name"] == "submit_answer":
                    return {"role": "tool", "tool_call_id": tc["id"],
                            "content": "submit_answer 仅在收口轮有效，此处忽略；请继续检索或等待系统收口。"}
                out = self._exec_tool(tc["function"]["name"], args)
                return {"role": "tool", "tool_call_id": tc["id"], "content": out}
            if len(calls) == 1:
                results = [run(calls[0])]
            else:
                with ThreadPoolExecutor(max_workers=len(calls)) as pool:
                    results = list(pool.map(run, calls))
            self.history.extend(results)
        if answer is None:
            # 轮数耗尽：收口轮。工具列表只留 submit_answer 并强制调用（tool_choice=required 级约束）
            self.history.append({
                "role": "user",
                "content": f"（系统提示）检索轮数已用完（{MAX_TOOL_ROUNDS}轮）。"
                           "请立即调用 submit_answer 提交最终回答：先结论后论据；"
                           "每个来自检索的事实句末标注 [S#]（编号见工具返回的 sid 字段）；"
                           "未检索到的部分明确说明，不要臆测。"})
            for _try in range(2):
                msg = self._chat(self.history, SUBMIT_TOOL, force_tool=True)
                answer = _extract_answer(msg)
                if answer:
                    break
                # 空回答兜底重试（回答长度不设上限）
                self.history.append({
                    "role": "user",
                    "content": "（系统）上一条没有拿到有效回答。请重新调用 submit_answer 提交**完整**回答："
                               "先结论后论据，每个子主题给出来源编号 [S#]，不要省略证据。"})
            if not answer:
                answer = "（本问题 kb agent 未产出有效回答：收口轮两次均未拿到可解析的 submit_answer）"
                print(f"[kb] WARNING 空回答（问题：{self.history[1].get('content', '')[:60]}…）", file=sys.stderr)
            self.history.append({"role": "assistant", "content": answer})
        self._trim_history()
        if os.environ.get("KB_DEBUG_CTX"):
            sizes = [len(json.dumps(m, ensure_ascii=False)) for m in self.history]
            total = sum(sizes)
            by_role: dict = {}
            for m, s in zip(self.history, sizes):
                by_role[m.get("role", "?")] = by_role.get(m.get("role", "?"), 0) + s
            print(f"[kb-ctx] 消息数={len(self.history)} 总字符={total} 约token={total//2} "
                  f"| 按角色: " + " ".join(f"{k}={v}" for k, v in by_role.items())
                  + f" | 最大单条={max(sizes)}", file=sys.stderr)
        self.last_result = {"text": answer, "citations": self.registry.lookup_cited(answer)}
        return self.last_result

    def _trim_history(self):
        if len(self.history) > HISTORY_KEEP:
            self.history = [self.history[0]] + self.history[-(HISTORY_KEEP - 1):]

    def reset(self):
        self.registry = SourceRegistry()
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._seen_chunks = {}
        self.last_result = None


# ═══════════════════════════════════════════════════════════════════════
# 输出渲染：正文 + 引用来源（完整内容 + 元数据）——下游撰写直接取用
# 产物分两块：回答（LLM 总结）+ 引用（原文引用块，完整内容+元数据可溯源）
# ═══════════════════════════════════════════════════════════════════════
def render_citations(out: dict) -> str:
    """引用块：每条 [S#] 附元数据头 + 原文全文（回答块不复述原文，原文只在这里）。"""
    lines = []
    for c in out["citations"]:
        m = c["metadata"]
        head = f'[{c["id"]}]'
        if c["type"] == "chunk":
            head += f' {m.get("paper_title","")} ({m.get("year","")}) {m.get("journal") or ""} §{m.get("section_title","")} (paper_id={m.get("paper_id")}, chunk_id={m.get("chunk_id")})'
        elif c["type"] == "hyperedge":
            head += f' 超边: {" → ".join(str(p) for p in (m.get("participants") or []))} (edge_id={m.get("edge_id")}, scope={m.get("scope")}; 涉及论文: {", ".join(str(p) for p in (m.get("source_papers") or []))})'
        elif c["type"] in ("section", "section_hit"):
            head += f' {m.get("paper_title","")} ({m.get("year","")}) {m.get("journal") or ""} §{m.get("section_title","")} (paper_id={m.get("paper_id")}, section_id={m.get("section_id")})'
        elif c["type"] == "sql":
            head += f' SQL查询: `{m.get("sql","")}`'
        else:
            head += f' {m.get("label") or c["type"]}'
        lines.append(f"### {head}")
        if c["type"] in ("chunk", "section", "section_hit", "hyperedge"):
            lines.append(f'> {c["content"]}')
        elif c["type"] == "sql":
            lines.append("```json\n" + c["content"] + "\n```")
    return "\n\n".join(lines)


def render_answer(out: dict) -> str:
    """分块渲染：回答块（LLM 总结）+ 引用块（原文全文，可溯源）。"""
    return "\n\n".join([
        "## 回答", out["text"], "",
        "---", "", "## 引用", render_citations(out),
    ])


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="litreview 知识库问答智能体")
    ap.add_argument("--q", help="单问模式；缺省进入 REPL")
    args = ap.parse_args()
    agent = KBAgent()

    if args.q:
        out = agent.ask(args.q)
        print(render_answer(out))
        return

    print("litreview 知识库问答智能体（/sources 来源列表 /save 存档 /reset 清空 /quit 退出）")
    while True:
        try:
            q = input("\n问> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q == "/quit":
            break
        if q == "/reset":
            agent.reset()
            print("已清空对话与来源注册表")
            continue
        if q == "/sources":
            print(agent.registry.summary())
            continue
        if q == "/save":
            if agent.last_result:
                p = _data_root() / "reviews" / "research" / f"kbqa_{__import__('datetime').datetime.now():%Y%m%d_%H%M%S}.md"
                p.parent.mkdir(parents=True, exist_ok=True)   # 跟 data_root 走（跨库）：写死路径时换库会把存档落到别的库目录
                p.write_text(render_answer(agent.last_result), encoding="utf-8")
                print(f"已存档: {p}")
            continue
        out = agent.ask(q)
        print("\n" + out["text"])
        n = len(out["citations"])
        cited = ", ".join(c["id"] for c in out["citations"])
        print(f"\n[引用 {n} 条: {cited} | /save 存档分「回答+引用」两块，含完整原文与元数据]")


if __name__ == "__main__":
    main()
