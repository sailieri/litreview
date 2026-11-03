"""litread 论文问答智能体（PaperQA）—— 单篇精读 + 跨库工具。

设计：
  - 上下文 = 论文原文全文 + 本篇超图（实体/超边）+ 回答注意点（system prompt）
  - 连续对话：前端每轮传历史，服务端无状态（不做会话存储）
  - kb_agent 降级为工具 cross_document_search（跨文献检索），原生 function calling
  - 传输走 SSE 流式 + delta.tool_calls 累积：非流式长回答容易被网关按空闲超时
    断开（HTTP 504/524），必须流式

入口：answer(pid, message, history) -> {"text", "citations", "tool_used"}
被 webapp/server.py 的 POST /api/papers/{pid}/chat 惰性导入。
"""
import json
import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
LITROOT = WEB_ROOT.parent
if str(LITROOT) not in sys.path:
    sys.path.insert(0, str(LITROOT))

import httpx  # noqa: E402
from modules.paths import data_root, db_path  # noqa: E402
from modules.util.llm_client import load_config, get_step_config  # noqa: E402

STEP = "paper_qa"
MAX_TOOL_ROUNDS = 3          # 工具往返上限（含首次）
HISTORY_MAX_MSGS = 20        # 历史只取最近 N 条
HISTORY_MSG_MAX_CHARS = 20000
FULLTEXT_MAX_CHARS = 500_000  # 安全阀（262k 上下文远够，正常永远不触发）

SYSTEM_TMPL = """你是「litread 论文问答助手」，正在与用户一起精读下面这一篇论文。你的职责是围绕本篇论文回答问题。

# 本篇论文
标题：{title}
作者：{authors}（{year}，{journal}）

# 回答注意点（必须遵守）
1. 单篇问题直接依据下方「论文原文」与「本篇超图」回答，不要调用任何工具
2. 涉及数值（R² / RMSE / 偏差 / 精度 / 指标等）必须与原文逐字一致；能标注章节出处就标注
3. 超图的超边描述已凝炼本篇关键数值，可直接引用
4. 原文与超图都没有的内容：明确说「本文未涉及」，严禁编造
5. 只有当问题明确需要**其它论文 / 全库**的知识时，才调用 cross_document_search 工具；本篇能答的不要调用
6. 调用工具后：回答开头标注 [跨库检索]，引用证据时保留工具返回的原始 [S#] 编号
7. 中文回答，先结论后依据，简洁准确；公式、变量名保留原文写法；不要复述用户的问题

# 论文原文（规范化 Markdown 全文）
<<<PAPER
{fulltext}
PAPER

# 本篇超图
## 实体（{n_entities} 个，概念锚点：名称 / 类型 / 定义）
{entity_lines}

## 超边（{n_hyperedges} 条，参与者 → 含关键数值的描述）
{hyperedge_lines}
"""

TOOLS = [{
    "type": "function",
    "function": {
        "name": "cross_document_search",
        "description": (
            "在文献库的**其它论文**中检索跨文献证据。仅当问题需要本篇论文之外的知识"
            "（其它论文的方法/数值/对比/综述性信息）时调用；本篇论文能回答的问题不要调用。"
            "question 要具体详细（可用中英文关键词）。"),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "针对全库的具体检索问题，具体详细"},
            },
            "required": ["question"],
        },
    },
}]


# ── 配置 ──────────────────────────────────────────────────────

def _step_cfg() -> dict:
    cfg = load_config()
    sc = get_step_config(STEP, cfg)
    step = cfg.get("steps", {}).get(STEP, {})
    params = dict(sc.get("params") or {})
    for k, v in step.items():
        if k not in ("provider", "model"):
            params[k] = v
    sc["params"] = params
    return sc


# ── 上下文构建 ────────────────────────────────────────────────

def _db():
    import sqlite3
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


def _fulltext(stem: str, conn) -> str:
    """normalized 全文优先；缺文件回退 sections 按 id 拼接。"""
    f = data_root() / "normalized" / f"{stem}.md"
    if f.exists():
        text = f.read_text(encoding="utf-8", errors="replace")
    else:
        rows = conn.execute(
            "SELECT s.title, s.content FROM sections s "
            "JOIN papers p ON p.id = s.paper_id "
            "WHERE p.file_path LIKE ? ORDER BY s.id", (f"%{stem}%",)).fetchall()
        text = "\n\n".join(
            f"## {r['title']}\n{r['content']}" for r in rows if r["content"])
    if len(text) > FULLTEXT_MAX_CHARS:
        text = (text[:400_000]
                + "\n\n…（原文过长，中段截断；可继续追问具体章节）…\n\n"
                + text[-90_000:])
    return text or "（本篇原文缺失）"


def _hypergraph_lines(pid: int, conn) -> tuple[str, str, int, int]:
    ents = conn.execute(
        "SELECT name, type, description FROM entities WHERE paper_id=? ORDER BY id",
        (pid,)).fetchall()
    ent_lines = "\n".join(
        f"- {r['name']}（{r['type'] or '?'}）：{r['description'] or '—'}" for r in ents)

    he_rows = []
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
        names = []
        if pids:
            ph = ",".join("?" * len(pids))
            by_id = {x["id"]: x["name"] for x in conn.execute(
                f"SELECT id, name FROM entities WHERE id IN ({ph})", tuple(pids))}
            names = [by_id.get(x, f"#{x}") for x in pids]
        he_rows.append((names, r["description"] or "—"))
    he_lines = "\n".join(
        f"- [{' + '.join(names) or '?'}]：{desc}" for names, desc in he_rows)
    return ent_lines or "（无）", he_lines or "（无）", len(ents), len(he_rows)


def build_system(pid: int) -> str:
    conn = _db()
    try:
        p = conn.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
        if not p:
            raise ValueError(f"paper {pid} not found")
        stem = _stem_of(p["file_path"] or "")
        fulltext = _fulltext(stem, conn)
        ent_lines, he_lines, n_e, n_h = _hypergraph_lines(pid, conn)
    finally:
        conn.close()
    return SYSTEM_TMPL.format(
        title=p["title"] or stem,
        authors=p["authors"] or "未知",
        year=p["year"] or "—",
        journal=p["journal"] or "—",
        fulltext=fulltext,
        entity_lines=ent_lines,
        hyperedge_lines=he_lines,
        n_entities=n_e,
        n_hyperedges=n_h,
    )


# ── LLM 流式调用（SSE + tool_calls 累积）────────────────────────

def _stream_chat(sc: dict, messages: list, tools: list | None = None,
                 retries: int = 2) -> dict:
    base_url = sc["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {sc['api_key']}",
               "Content-Type": "application/json"}
    params = sc.get("params") or {}
    payload = {
        "model": sc["model"],
        "messages": messages,
        "temperature": params.get("temperature", 0.3),
        "max_tokens": params.get("max_tokens", 4096),
        "stream": True,
    }
    # 关闭 thinking：工具轮之后 thinking 可能吞光 max_tokens
    # 导致最终 content 为空（finish=length）；与 call_llm 对 Qwen 的默认处理一致
    if "qwen" in (sc["model"] or "").lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    timeout = params.get("timeout", 600)

    last_err = None
    for attempt in range(retries + 1):
        try:
            parts: list[str] = []
            tool_acc: dict[int, dict] = {}
            skipped: list[str] = []
            raw_lines: list[str] = []
            finish = None
            with httpx.Client(timeout=timeout, trust_env=False) as cl:   # 禁系统代理
                with cl.stream("POST", f"{base_url}/chat/completions",
                               json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        raw_lines.append(line)
                        if not line:
                            continue
                        if not line.startswith("data: "):
                            # 非 data: 前缀的行（裸 JSON 错误载荷就藏在这里）
                            if len(skipped) < 3:
                                skipped.append(f"non-data-line: {line[:300]}")
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            choice = obj["choices"][0]
                        except (KeyError, IndexError, json.JSONDecodeError) as e:
                            # 打捞异常行：可能是空回答前被静默吞掉的 error/无 choices 载荷
                            # ——每轮最多打 3 条供定位
                            if len(skipped) < 3:
                                skipped.append(f"{type(e).__name__}: {data[:300]}")
                            continue
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            parts.append(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            acc = tool_acc.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
            tool_calls = [v for _, v in sorted(tool_acc.items()) if v["name"]]
            for i, t in enumerate(tool_calls):
                if not t["id"]:
                    t["id"] = f"call_{i}"
            content = "".join(parts).strip()
            print(f"[paper_qa] chat finish={finish} content_len={len(content)} "
                  f"tools={len(tool_calls)}", file=sys.stderr)
            for s in skipped:
                print(f"[paper_qa] chat unusual line: {s}", file=sys.stderr)
            if not content and finish is None and not tool_calls:
                # 空流：打捞原始行定位（finish=None 且无内容）
                print(f"[paper_qa] EMPTY STREAM total_lines={len(raw_lines)}",
                      file=sys.stderr)
                for i, rl in enumerate(raw_lines[:8]):
                    print(f"[paper_qa] raw[{i}] {rl[:300]}", file=sys.stderr)
                if len(raw_lines) > 8:
                    for i, rl in enumerate(raw_lines[-3:]):
                        print(f"[paper_qa] raw[end{i}] {rl[:300]}", file=sys.stderr)
            return {"content": content,
                    "tool_calls": tool_calls or None,
                    "finish": finish}
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt >= retries:
                break
            import time
            wait = 5 * (attempt + 1)
            print(f"[paper_qa] chat attempt {attempt + 1} failed "
                  f"({type(e).__name__}: {str(e)[:80]}), retry in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"paper_qa LLM 不可达: {type(last_err).__name__}: {last_err}")


# ── 工具：kb_agent 跨文献检索 ─────────────────────────────────

def _kb_search(pid: int, title: str, question: str) -> tuple[str, list]:
    from modules.kb_agent import KBAgent    # 惰性导入（与 server.py /api/ask 同款）
    agent = KBAgent(step="rag")
    prefixed = (f"（用户正在阅读论文 #{pid}《{title}》，问题可能围绕该篇；"
                f"请从全库其它论文中检索证据回答。）\n{question}")
    out = agent.ask(prefixed)
    text = out.get("text") or "（跨库检索无结果）"
    cites = out.get("citations") or []
    return text, cites


def _cite_digest(cites: list) -> str:
    lines = []
    for c in cites[:12]:
        md = c.get("metadata") or {}
        label = (md.get("paper_title") or md.get("label") or md.get("sql")
                 or c.get("content", "")[:60])
        lines.append(f"[{c.get('id')}] {c.get('type', '?')} | {str(label)[:80]}")
    return "\n".join(lines)


# ── 主入口 ────────────────────────────────────────────────────

def answer(pid: int, message: str, history: list | None = None) -> dict:
    sc = _step_cfg()
    system = build_system(pid)

    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in (history or [])[-HISTORY_MAX_MSGS:]:
        role = h.get("role")
        role = "assistant" if role in ("ai", "assistant") else (
            "user" if role == "user" else None)
        content = h.get("content") or h.get("text") or ""
        if role and content:
            msgs.append({"role": role, "content": str(content)[:HISTORY_MSG_MAX_CHARS]})
    msgs.append({"role": "user", "content": message})

    title_row = None
    tool_used = False
    citations: list = []
    out = {"content": "", "tool_calls": None}

    for rnd in range(MAX_TOOL_ROUNDS + 1):
        out = _stream_chat(sc, msgs, tools=TOOLS)
        if not out.get("tool_calls"):
            break
        tool_used = True
        # 回灌必须是 OpenAI 标准嵌套格式：{id, type, function:{name, arguments}}
        # （扁平 {id,name,arguments} 会因缺 function/type 被服务端 411 校验拒）
        assistant_msg = {
            "role": "assistant",
            "content": out["content"] or None,
            "tool_calls": [
                {"id": t["id"], "type": "function",
                 "function": {"name": t["name"],
                              "arguments": t["arguments"] or "{}"}}
                for t in out["tool_calls"]
            ],
        }
        msgs.append(assistant_msg)
        for tc in out["tool_calls"]:
            if tc["name"] != "cross_document_search":
                result = json.dumps(
                    {"error": f"unknown tool: {tc['name']}"}, ensure_ascii=False)
            else:
                try:
                    q = (json.loads(tc["arguments"] or "{}").get("question") or "").strip()
                except json.JSONDecodeError:
                    q = ""
                if not q:
                    result = "工具参数错误：缺少 question"
                else:
                    if title_row is None:
                        conn = _db()
                        try:
                            r = conn.execute(
                                "SELECT title FROM papers WHERE id=?", (pid,)).fetchone()
                            title_row = r["title"] if r else ""
                        finally:
                            conn.close()
                    text, cites = _kb_search(pid, title_row or "", q)
                    citations.extend(cites)
                    digest = _cite_digest(cites)
                    result = text + (
                        "\n\n证据来源（回答时保留这些原编号）：\n" + digest if digest else "")
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": str(result)[:30000]})
        if rnd == MAX_TOOL_ROUNDS:
            # 用尽轮数：不再给工具，强制收敛
            msgs.append({"role": "user",
                         "content": "（工具预算已用完，基于已有信息直接给出最终回答，不要再调用工具。）"})
    else:
        # 循环正常走完仍未拿到无工具的最终回答 → 摘掉 tools 强制出答案
        out = _stream_chat(sc, msgs, tools=None)

    # 空回答兕底：重试一次（无工具），仍空则报 finish 原因
    if not (out.get("content") or "").strip():
        print(f"[paper_qa] empty final (finish={out.get('finish')}), retry once",
              file=sys.stderr)
        # 把失败请求完整 messages 落盘，供离线重放定位（不动，可重放）
        try:
            import time as _t
            dump = (WEB_ROOT / f"tmp_paperqa_fail_{int(_t.time())}.json")
            dump.write_text(json.dumps(msgs, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            print(f"[paper_qa] msgs dumped: {dump}", file=sys.stderr)
        except Exception as _e:  # noqa: BLE001
            print(f"[paper_qa] dump failed: {_e}", file=sys.stderr)
        out = _stream_chat(sc, msgs, tools=None)
        if not (out.get("content") or "").strip():
            out["content"] = (f"（模型输出为空，finish={out.get('finish')}，请重试）")

    # citations 去重（保留先出现者，id 对应正文 [S#]）
    seen, uniq = set(), []
    for c in citations:
        cid = c.get("id")
        if cid in seen:
            continue
        seen.add(cid)
        uniq.append(c)

    text = out.get("content") or "（模型无输出，请重试）"
    print(f"[paper_qa] pid={pid} tool_used={tool_used} "
          f"cites={len(uniq)} chars={len(text)}", file=sys.stderr)
    return {"text": text, "citations": uniq, "tool_used": tool_used}
