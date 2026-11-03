"""litread 图书管理员智能体（Librarian）—— 独立问答库 + 工作区 + 工具循环。

设计：
  - 独立于单篇论文页的全局侧边栏智能体：帮挑论文、读原文、跨篇对比、库统计
  - 问答持久化：qa_threads / qa_messages / qa_fts —— 问答库随使用增长
  - 新问题自动检索历史相似问答注入参考（常见问题命中率随使用提升）
  - 工作区：会话级 paper_ids（用户圈定的论文），系统提示常驻清单
  - 传输 SSE：meta → tool（工具过程）→ delta（正文流）→ done/error
  - LLM 复用 config 的 paper_qa step（qwen-plus / DashScope）；流式 tool_calls
    分片与 paper_qa 同款（非流式长响应易被网关按空闲超时断开，必须流式）
  - 中文检索：qa_fts 建表未指定 tokenizer（默认 unicode61，中文整段一个 token
    查不动）→ 存取都过 _seg() 自分词（中文 2-gram + 英文整词），零外部依赖

入口：chat_stream(thread_id, message, paper_ids) -> iterator[dict]
      list_threads / get_thread / delete_thread（threads CRUD）
被 webapp/server.py 的 POST /api/agent/chat 等端点惰性导入。
"""
import json
import re
import sqlite3
import sys
import threading
from collections import Counter
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
LITROOT = WEB_ROOT.parent
if str(LITROOT) not in sys.path:
    sys.path.insert(0, str(LITROOT))

from modules.paths import data_root, db_path, metadata_dir, summaries_dir  # noqa: E402
from modules.database.embedder import compute_embedding, vector_to_blob  # noqa: E402
from modules.database.id_domain import to_merged, to_private  # noqa: E402
from modules.util.llm_client import call_llm, extract_json  # noqa: E402
from paper_qa import _db, _fulltext, _stem_of, _step_cfg  # noqa: E402
from paper_qa import FULLTEXT_MAX_CHARS  # noqa: E402

# ── 配置常量 ────────────────────────────────────────────────
MAX_TOOL_ROUNDS = 6          # 工具往返上限（含首次）
HISTORY_MAX_MSGS = 16        # 本会话历史回灌条数
HISTORY_MSG_CHARS = 6000     # 单条历史截断
WORKSPACE_MAX = 12           # 工作区论文上限
READ_MAX_CHARS = 30_000      # read_paper 单次返回上限
TOOL_BUDGET_CHARS = 120_000  # 一次回答里全部工具结果的总预算
PRIOR_MAX = 2                # 历史问答注入条数

# 本模块的日志前缀
LOG = "[librarian]"


# ── 数据库：表结构与连接 ─────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qa_threads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  paper_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS qa_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL,
  role TEXT NOT NULL,               -- 'user' | 'assistant'
  content TEXT NOT NULL DEFAULT '',
  citations TEXT NOT NULL DEFAULT '[]',
  tool_log TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_qa_messages_thread ON qa_messages(thread_id, id);
CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(content);
DROP TRIGGER IF EXISTS qa_msgs_ai;   -- 遗留触发器（存原文）：与应用层双写冲突（先占 rowid），必须删除，
DROP TRIGGER IF EXISTS qa_msgs_ad;   -- 否则新消息插入时它先占 rowid、应用层再插必撞
CREATE TABLE IF NOT EXISTS qa_edge_jobs(   -- 问答→超边提取台账（0 条入库也记，防重复烧 LLM）
  msg_id INTEGER PRIMARY KEY,
  n_edges INTEGER NOT NULL DEFAULT 0,
  done_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""
# 注：qa_fts 的写入全在应用层（_seg 分词后双写）——SQL 触发器里调不了 Python
# 分词，存原文的话 unicode61 会把中文整段切一个 token，中文查询全部漏掉。

_FTS_REBUILT: dict = {"done": False}


def ensure_schema() -> None:
    """幂等建表（server 启动时调一次）；含 QA 超边衍生列的 ALTER 兑底。"""
    conn = _dbw()
    try:
        conn.executescript(SCHEMA_SQL)
        # cross_doc_hyperedges 的两列（跨库 schema 在 modules/database/schema.py，
        # 老库由这里 ALTER 补齐）：qa_msg_id=问答来源溯源/participants_from=实体域
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(cross_doc_hyperedges)")]
        if cols:
            if "qa_msg_id" not in cols:
                conn.execute("ALTER TABLE cross_doc_hyperedges "
                             "ADD COLUMN qa_msg_id INTEGER")
            if "participants_from" not in cols:
                conn.execute("ALTER TABLE cross_doc_hyperedges "
                             "ADD COLUMN participants_from TEXT DEFAULT 'entities_merged'")
        # qa_threads.archived_at：会话归档时间（非空 = 已冻结，不可再追加消息）
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(qa_threads)")]
        if tcols and "archived_at" not in tcols:
            conn.execute("ALTER TABLE qa_threads ADD COLUMN archived_at TEXT")
        conn.commit()
        n_msg = conn.execute(
            "SELECT COUNT(*) FROM qa_messages WHERE role='user'").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM qa_fts").fetchone()[0]
        if n_msg and not n_fts and not _FTS_REBUILT["done"]:
            for r in conn.execute(
                    "SELECT id, content FROM qa_messages WHERE role='user'"):
                conn.execute("INSERT INTO qa_fts(rowid, content) VALUES(?, ?)",
                             (r["id"], _seg(r["content"])))
            conn.commit()
            _FTS_REBUILT["done"] = True
            print(f"{LOG} qa_fts rebuilt: {n_msg} questions")
    finally:
        conn.close()


def _dbw():
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    return conn


# ── 中文友好分词（存取同构） ─────────────────────────────────

def _seg(text: str) -> str:
    """中文 2-gram + 英文/数字整词，空格拼接——供 fts5 unicode61 索引与查询。"""
    text = (text or "").lower()
    out: list[str] = []
    for m in re.finditer(r"[a-z0-9_]+|[一-鿿]+", text):
        tok = m.group()
        if tok[0].isdigit() or tok[0].isascii():
            out.append(tok)
        elif len(tok) == 1:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return " ".join(out)


def _qa_fts_match(conn, q: str, exclude_thread: int | None) -> list[int]:
    """问答库检索：全查询 AND → 放宽前 8 个 2-gram OR → LIKE 兜底。返回 qa_messages.id。"""
    segs = _seg(q).split()
    if not segs:
        return []
    tries = [" ".join(segs),
             " OR ".join(segs[:8]) if len(segs) > 8 else None]
    for m in tries:
        if not m:
            continue
        try:
            rows = conn.execute(
                "SELECT rowid FROM qa_fts WHERE qa_fts MATCH ? ORDER BY rank LIMIT 6",
                (m,)).fetchall()
        except Exception:  # noqa: BLE001  fts 语法容错
            continue
        ids = [r["rowid"] for r in rows]
        if exclude_thread:
            ph = ",".join("?" * len(ids)) if ids else ""
            if ids:
                ids = [r["id"] for r in conn.execute(
                    f"SELECT id FROM qa_messages WHERE id IN ({ph}) "
                    f"AND thread_id != ? ORDER BY id",
                    tuple(ids) + (exclude_thread,))]
        if ids:
            return ids
    # LIKE 兜底（问答库量小，全表扫可接受）
    like = f"%{q.strip()[:60]}%"
    rows = conn.execute(
        "SELECT id FROM qa_messages WHERE role='user' AND content LIKE ? "
        "AND thread_id != ? ORDER BY id DESC LIMIT 6",
        (like, exclude_thread or -1)).fetchall()
    return [r["id"] for r in rows]


# ── threads CRUD ─────────────────────────────────────────────

def list_threads() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM qa_messages m WHERE m.thread_id=t.id) AS n_msgs "
            "FROM qa_threads t ORDER BY t.updated_at DESC LIMIT 100").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            pids = json.loads(r["paper_ids"] or "[]")
        except Exception:  # noqa: BLE001
            pids = []
        out.append({"id": r["id"], "title": r["title"], "paper_ids": pids,
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                    "n_msgs": r["n_msgs"],
                    "archived_at": (r["archived_at"]
                                    if "archived_at" in r.keys() else None)})
    return out


def get_thread(tid: int) -> dict | None:
    conn = _db()
    try:
        t = conn.execute("SELECT * FROM qa_threads WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        msgs = conn.execute(
            "SELECT id, role, content, citations, tool_log, created_at "
            "FROM qa_messages WHERE thread_id=? ORDER BY id", (tid,)).fetchall()
    finally:
        conn.close()
    try:
        pids = json.loads(t["paper_ids"] or "[]")
    except Exception:  # noqa: BLE001
        pids = []
    return {
        "id": t["id"], "title": t["title"], "paper_ids": pids,
        "created_at": t["created_at"], "updated_at": t["updated_at"],
        "archived_at": (t["archived_at"] if "archived_at" in t.keys() else None),
        "messages": [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "citations": _jload(m["citations"]), "tool_log": _jload(m["tool_log"]),
             "created_at": m["created_at"]}
            for m in msgs],
    }


def delete_thread(tid: int) -> bool:
    conn = _dbw()
    try:
        t = conn.execute("SELECT 1 FROM qa_threads WHERE id=?", (tid,)).fetchone()
        if not t:
            return False
        # 同步删问答库索引（应用层双写，无触发器）。
        # qa_fts 是普通 FTS5 表（非 external-content），按 rowid 直接 DELETE 即可。
        # 用 INSERT ... VALUES('delete',...) 那种命令式写法只适用于 external-content 表，
        # 在普通 FTS5 表上会抛 SQL logic error。
        for r in conn.execute(
                "SELECT id FROM qa_messages WHERE thread_id=? AND role='user'",
                (tid,)).fetchall():
            conn.execute("DELETE FROM qa_fts WHERE rowid=?", (r["id"],))
        conn.execute("DELETE FROM qa_messages WHERE thread_id=?", (tid,))
        conn.execute("DELETE FROM qa_threads WHERE id=?", (tid,))
        conn.commit()
        return True
    finally:
        conn.close()


def _jload(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


# ── 工具实现 ────────────────────────────────────────────────

def _paper_meta_rows(conn, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, authors, year, journal, doi, file_path, "
        f"entity_count, hyperedge_count FROM papers WHERE id IN ({ph})", ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]   # 保持检索排序


def _narrative_of(stem: str) -> str:
    f = summaries_dir() / f"{stem}.json"
    if not f.exists():
        return ""
    try:
        t = json.loads(f.read_text(encoding="utf-8")).get("narrative") or ""
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(str(t).split())[:400]


def _match_score(hay: str, query: str) -> float:
    """小库暴力打分：完整串 > 完整词 > 中文 2-gram 命中比例（papers_fts 是
    unicode61、中文标题整段一个 token，MATCH 对中文基本不中，300 篇全扫即正义）。"""
    hay, query = (hay or "").lower(), (query or "").lower().strip()
    if not query:
        return 0.0
    if query in hay:
        return 5.0
    score = 0.0
    for term in query.split():
        if term in hay:
            score += 2.5
            continue
        grams = [term[i:i + 2] for i in range(len(term) - 1)] or [term]
        score += sum(1 for g in grams if g in hay) / len(grams) * 1.5
    return score


def tool_search_papers(query: str, top_k: int = 8) -> tuple[str, list[dict]]:
    """检索文献库帮用户挑论文：返回 id/年/期刊/标题/摘要片段。"""
    query = (query or "").strip()
    if not query:
        return "参数错误：缺少 query", []
    top_k = max(1, min(int(top_k or 8), 12))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, title, authors, year, journal, file_path "
            "FROM papers").fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        hay = f"{r['title'] or ''} {r['authors'] or ''} {r['journal'] or ''}"
        s = _match_score(hay, query)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    lines, cites = [], []
    for _, r in scored[:top_k]:
        stem = _stem_of(r["file_path"] or "")
        nar = _narrative_of(stem)
        cites.append({"id": r["id"], "title": r["title"],
                      "year": r["year"], "journal": r["journal"]})
        lines.append(
            f"#{r['id']} | {r['year'] or '—'} | {r['journal'] or '—'} | "
            f"{r['title'] or stem}\n  摘要：{nar or '（无摘要）'}")
    if not lines:
        return f"检索「{query}」零命中（可换关键词，或放宽到相近主题词）", []
    text = f"命中 {len(lines)} 篇（可继续换词检索，或直接 read_paper 读原文）：\n" + \
           "\n".join(lines)
    return text, cites


def tool_read_paper(paper_id: int, question: str = "") -> tuple[str, list[dict]]:
    """读一篇论文的真实原文：question 非空时按 2-gram 打分取相关段落。"""
    conn = _db()
    try:
        p = conn.execute(
            "SELECT id, title, authors, year, journal, file_path "
            "FROM papers WHERE id=?", (int(paper_id),)).fetchone()
        if not p:
            return f"论文 #{paper_id} 不存在（检查工作区/检索结果里的 id）", []
        stem = _stem_of(p["file_path"] or "")
        full = _fulltext(stem, conn)
    finally:
        conn.close()
    header = (f"《{p['title'] or stem}》 #{p['id']}（{p['authors'] or '?'}， "
              f"{p['year'] or '—'}， {p['journal'] or '—'}）\n")
    cites = [{"id": p["id"], "title": p["title"],
              "year": p["year"], "journal": p["journal"]}]
    if len(full) <= READ_MAX_CHARS and not (question or "").strip():
        return header + "\n" + full, cites

    if (question or "").strip():
        grams = {question[i:i + 2] for i in range(len(question) - 1)} or {question}
        segs = [s for s in re.split(r"\n\s*\n", full) if s.strip()]
        scored = []
        for i, s in enumerate(segs):
            sc = sum(1 for g in grams if g in s)
            if sc:
                scored.append((sc, i, s))
        if scored:
            top = sorted(scored, key=lambda x: -x[0])[:8]
            top = sorted(top, key=lambda x: x[1])          # 还原原文顺序
            body = "\n\n".join(s for _, _, s in top)
            if len(body) > READ_MAX_CHARS:
                body = body[:READ_MAX_CHARS] + "\n…（相关段落过长已截断）…"
            return header + (f"\n（围绕问题「{question[:60]}」"
                             f"定位到 {len(top)} 个相关段落）\n\n" + body), cites
        # 打分零命中 → 走下面的截断全文
    body = full[:READ_MAX_CHARS - 200]
    return header + "\n" + body + "\n\n…（全文过长已截前段；可带 question 追问具体章节）", cites


def tool_get_stats() -> tuple[str, list[dict]]:
    """文献库统计：规模/年份/期刊/关键词/超图。"""
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
        years = [dict(r) for r in conn.execute(
            "SELECT year, COUNT(*) n FROM papers WHERE year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC LIMIT 30")]
        journals = [dict(r) for r in conn.execute(
            "SELECT journal, COUNT(*) n FROM papers WHERE journal IS NOT NULL "
            "AND journal != '' GROUP BY journal ORDER BY n DESC LIMIT 12")]
        n_ent = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        n_he = conn.execute("SELECT COUNT(*) c FROM hyperedges").fetchone()["c"]
        n_cd = conn.execute("SELECT COUNT(*) c FROM cross_doc_hyperedges").fetchone()["c"]
        n_sec = conn.execute("SELECT COUNT(*) c FROM sections").fetchone()["c"]
        stems = [r["file_path"] for r in conn.execute("SELECT file_path FROM papers")]
    finally:
        conn.close()
    pdf = sum(1 for fp in stems if _pdf_hint(fp))
    # 关键词聚合（metadata/*.json）
    kwc: Counter = Counter()
    try:
        for f in metadata_dir().glob("*.json"):
            try:
                k = json.loads(f.read_text(encoding="utf-8")).get("keywords") or []
            except Exception:  # noqa: BLE001
                continue
            if isinstance(k, str):
                k = [x.strip() for x in k.split(",") if x.strip()]
            kwc.update(str(x).strip() for x in k if str(x).strip())
    except OSError:
        pass
    lines = [
        f"论文总数 {total}（其中可打开 PDF 约 {pdf} 篇）",
        f"实体 {n_ent} 个，单篇超边 {n_he} 条，跨文献超边 {n_cd} 条，章节 {n_sec} 节",
        "年份分布：" + "，".join(f"{r['year']}年 {r['n']}篇" for r in years[:15]),
        "期刊 Top：" + "，".join(
            f"{(r['journal'] or '')[:30]}({r['n']})" for r in journals),
        "高频关键词：" + "，".join(f"{k}({v})" for k, v in kwc.most_common(15)),
    ]
    return "\n".join(lines), []


def _pdf_hint(file_path: str) -> bool:
    """与 server._pdf_file 同款判定：data_root()/pdfs/<stem>.pdf。"""
    stem = _stem_of(file_path or "")
    if not stem:
        return False
    return (data_root() / "pdfs" / f"{stem}.pdf").exists()


TOOLS = [
    {"type": "function", "function": {
        "name": "search_papers",
        "description": ("在文献库中检索论文，用于帮用户找/挑论文或确认论文 id。"
                        "返回 id、年份、期刊、标题与摘要片段。可换关键词多次调用。"),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词（中英文主题/方法/作者均可）"},
            "top_k": {"type": "integer", "description": "返回条数，默认 8，最大 12"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_paper",
        "description": ("读一篇论文的真实原文。工作区内有该 id 时直接读；"
                        "question 会用于定位相关段落（推荐带上，更准更省）。"),
        "parameters": {"type": "object", "properties": {
            "paper_id": {"type": "integer", "description": "论文 id（#号）"},
            "question": {"type": "string",
                         "description": "读原文的目的/问题（留空返回开头全文）"},
        }, "required": ["paper_id"]}}},
    {"type": "function", "function": {
        "name": "get_stats",
        "description": "查询文献库统计信息（总量、年份/期刊分布、关键词、超图规模）。",
        "parameters": {"type": "object", "properties": {}}}},
]


def _run_tool(name: str, args: dict) -> tuple[str, list[dict], str]:
    """执行工具 → (给模型的文本, citations, 给前端的过程标签)。"""
    if name == "search_papers":
        q = str(args.get("query") or "")
        k = int(args.get("top_k") or 8)
        text, cites = tool_search_papers(q, k)
        return text, cites, f"检索「{q[:40]}」→ 命中 {len(cites)} 篇"
    if name == "read_paper":
        pid = int(args.get("paper_id") or 0)
        qn = str(args.get("question") or "")
        text, cites = tool_read_paper(pid, qn)
        label = f"读 #{pid} 原文（{len(text)} 字）" + \
                (f"，定位「{qn[:30]}」" if qn else "")
        return text, cites, label
    if name == "get_stats":
        text, cites = tool_get_stats()
        return text, cites, "查询库统计"
    return f"unknown tool: {name}", [], f"未知工具 {name}"


# ── 系统提示：角色 + 工作区 + 历史问答参考 ─────────────────

SYSTEM_TMPL = """你是「litread 图书管理员」，服务于一个文献库（当前 {n_total} 篇）。你独立于任何单篇论文页面，职责：帮用户找/挑论文、读原文回答、对比多篇、汇报库统计、连续多轮讨论。

# 工作区（用户本次圈定的论文，回答优先围绕它们）
{workspace_block}
{fresh_block}
# 工具使用准则
1. 帮挑/找论文 → search_papers（可连续多次换词细化）
2. 需要真实内容（方法、数值、结论、对比细节）→ read_paper；工作区内有的直接读（带 question 定位更准），没有的先 search_papers 确认 id
3. 问库的情况（多少篇、分布、关键词、超图）→ get_stats
4. 简单寒暄或直接可答的问题不必调工具
5. 提到论文必须带 id（如 #12）；多篇对比逐篇列出
6. 数值必须与工具返回的原文一致；工具没给的信息说「库中未见」，严禁编造
7. 中文回答，先结论后依据，简洁；不要复述用户问题
{prior_block}"""


def _workspace_block(paper_ids: list[int]) -> str:
    if not paper_ids:
        return "（工作区为空。可 search_papers 找到 id 后再读；也可建议用户把重点论文加入工作区。）"
    conn = _db()
    try:
        rows = _paper_meta_rows(conn, paper_ids[:WORKSPACE_MAX])
    finally:
        conn.close()
    lines = []
    for r in rows:
        stem = _stem_of(r["file_path"] or "")
        nar = _narrative_of(stem)
        lines.append(f"- #{r['id']} | {r['year'] or '—'} | {r['journal'] or '—'} | "
                     f"{r['title'] or stem}\n  摘要：{nar[:220] or '（无）'}")
    return "\n".join(lines) or "（工作区论文不在库中，请重新添加）"


def _fresh_block(paper_ids: list[int]) -> str:
    """本轮新加入工作区的论文：要求智能体先读原文（一次性提示）。

    与 _workspace_block 的区别：那是「智能体已知」的常驻清单（不强制读），
    这是「本轮刚加、需要先看」的一次性强提示 —— 前端消费后即清空。
    """
    if not paper_ids:
        return ""
    ids = "、".join(f"#{p}" for p in paper_ids[:WORKSPACE_MAX])
    return ("\n# 本轮新建工作区（用户刚把以下论文加入，尚未阅读）\n"
            f"{ids}\n"
            "回答前请先调用 read_paper 阅读上述论文的相关内容，把它们纳入本轮讨论。"
            "这些论文后续轮次不必重复通读。\n")


def _prior_block(q: str, tid: int) -> str:
    """问答库历史相似问答（常见问题复用，随使用增多命中越来越高）。"""
    conn = _db()
    try:
        ids = _qa_fts_match(conn, q, exclude_thread=tid)
        blocks = []
        for mid in ids[:PRIOR_MAX]:
            m = conn.execute("SELECT thread_id, content FROM qa_messages WHERE id=?",
                             (mid,)).fetchone()
            if not m:
                continue
            a = conn.execute(
                "SELECT content FROM qa_messages WHERE thread_id=? AND id>? "
                "AND role='assistant' ORDER BY id LIMIT 1",
                (m["thread_id"], mid)).fetchone()
            if not a:
                continue
            blocks.append(f"问：{m['content'][:300]}\n答：{a['content'][:1000]}")
    finally:
        conn.close()
    if not blocks:
        return ""
    return "\n\n## 历史问答库参考（既往会话真实问答，与当前问题相关时可复用其结论）\n" + \
           "\n---\n".join(blocks)


# ── 流式 LLM 调用（delta 以 yield 逐块返回）──

def _stream_chat_gen(sc: dict, messages: list, tools: list | None, out: dict):
    """yield ("delta", 文本增量)；聚合结果写入 out = {content, tool_calls, finish}。

    重试的后续 attempt 不再 yield（防止前端重复拼接），由 chat_stream 按
    out.content 长度差补发。"""
    import httpx

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
    if "qwen" in (sc["model"] or "").lower():
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    timeout = params.get("timeout", 600)

    last_err = None
    for attempt in range(3):
        try:
            parts: list[str] = []
            tool_acc: dict[int, dict] = {}
            finish = None
            with httpx.Client(timeout=timeout, trust_env=False) as cl:   # 禁系统代理
                with cl.stream("POST", f"{base_url}/chat/completions",
                               json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            choice = obj["choices"][0]
                        except (KeyError, IndexError, json.JSONDecodeError):
                            continue
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            parts.append(delta["content"])
                            if attempt == 0:
                                yield ("delta", delta["content"])
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
            out.update(content="".join(parts).strip(),
                       tool_calls=tool_calls or None, finish=finish)
            print(f"{LOG} chat attempt={attempt} finish={finish} "
                  f"len={len(out['content'])} tools={len(tool_calls)}",
                  file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt >= 2:
                break
            import time
            wait = 5 * (attempt + 1)
            print(f"{LOG} attempt {attempt + 1} failed "
                  f"({type(e).__name__}: {str(e)[:80]}) retry in {wait}s",
                  file=sys.stderr)
            if attempt == 0:
                yield ("delta", "\n（连接中断，重试中…）\n")
            time.sleep(wait)
    raise RuntimeError(f"librarian LLM 不可达: {type(last_err).__name__}: {last_err}")


# ── 主入口：SSE 事件流 ─────────────────────────────────────

def chat_stream(thread_id: int | None, message: str,
                paper_ids: list[int] | None = None,
                pending_ids: list[int] | None = None):
    """生成器：yield {event, data}，由 server 转 SSE。

    事件：meta{thread_id,created} / tool{name,label} / delta{text} /
          done{thread_id,message_id,content,citations,tool_log} / error{message}

    paper_ids  工作区全量快照（常驻清单，不强制读）
    pending_ids 本轮新加入的论文（强提示：先 read_paper；前端消费后清空）
    """
    message = (message or "").strip()
    if not message:
        yield {"event": "error", "data": {"message": "空消息"}}
        return
    pids = []
    for p in (paper_ids or []):
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if p not in pids and len(pids) < WORKSPACE_MAX:
            pids.append(p)

    # 1) thread：建/更新 + 落 user 消息
    created = False
    conn = _dbw()
    try:
        t = None
        if thread_id:
            t = conn.execute("SELECT id, archived_at FROM qa_threads WHERE id=?",
                             (thread_id,)).fetchone()
        if t and t["archived_at"]:
            conn.close()
            yield {"event": "error",
                   "data": {"message": "该会话已归档（归档于 %s），不可继续对话。"
                                    % t["archived_at"]}}
            return
        if not t:
            cur = conn.execute(
                "INSERT INTO qa_threads(title, paper_ids) VALUES(?, ?)",
                (message[:80], json.dumps(pids, ensure_ascii=False)))
            tid = cur.lastrowid
            created = True
        else:
            tid = t["id"]
            if paper_ids is not None:   # 前端传了快照就更新，否则保持
                conn.execute("UPDATE qa_threads SET paper_ids=?, updated_at=datetime('now','localtime') WHERE id=?",
                             (json.dumps(pids, ensure_ascii=False), tid))
            conn.execute("UPDATE qa_threads SET updated_at=datetime('now','localtime') WHERE id=?",
                         (tid,))
        cur = conn.execute(
            "INSERT INTO qa_messages(thread_id, role, content) VALUES(?, 'user', ?)",
            (tid, message))
        user_mid = cur.lastrowid
        # 问答库索引：_seg 分词后写入（应用层双写，见 SCHEMA_SQL 注）
        conn.execute("INSERT INTO qa_fts(rowid, content) VALUES(?, ?)",
                     (user_mid, _seg(message)))
        conn.commit()
    finally:
        conn.close()
    yield {"event": "meta",
           "data": {"thread_id": tid, "created": created,
                    "user_message_id": user_mid}}

    # 2) 上下文：工作区 + 历史 + 问答库参考
    sc = _step_cfg()
    prior = _prior_block(message, tid)
    n_total = 0
    db0 = _db()
    try:
        n_total = db0.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
        ws_ids = pids
        if not ws_ids:      # 未显式传 → 用 thread 快照
            row = db0.execute("SELECT paper_ids FROM qa_threads WHERE id=?",
                              (tid,)).fetchone()
            try:
                ws_ids = json.loads(row["paper_ids"] or "[]")
            except Exception:  # noqa: BLE001
                ws_ids = []
        hist = [dict(r) for r in db0.execute(
            "SELECT role, content FROM qa_messages WHERE thread_id=? AND id<? "
            "ORDER BY id DESC LIMIT ?", (tid, user_mid, HISTORY_MAX_MSGS)).fetchall()]
    finally:
        db0.close()
    hist.reverse()
    fresh_ids = [int(p) for p in (pending_ids or []) if str(p).strip().isdigit()]
    system = SYSTEM_TMPL.format(
        n_total=n_total,
        workspace_block=_workspace_block(ws_ids),
        fresh_block=_fresh_block(fresh_ids),
        prior_block=prior)
    msgs: list[dict] = [{"role": "system", "content": system}]
    for h in hist:
        if h["content"]:
            msgs.append({"role": h["role"],
                         "content": h["content"][:HISTORY_MSG_CHARS]})
    msgs.append({"role": "user", "content": message})

    # 3) 工具循环
    tool_log: list[dict] = []
    citations: list[dict] = []
    budget = TOOL_BUDGET_CHARS
    final_text = ""
    try:
        for rnd in range(MAX_TOOL_ROUNDS + 1):
            out: dict = {}
            sent = 0
            for kind, payload in _stream_chat_gen(sc, msgs, TOOLS, out):
                if kind == "delta":
                    sent += len(payload)
                    yield {"event": "delta", "data": {"text": payload}}
            # 静默重试导致的缺量补发（done 会用完整 content 幂等收口）
            if out.get("content") and len(out["content"]) > sent:
                yield {"event": "delta",
                       "data": {"text": out["content"][sent:]}}
            if not out.get("tool_calls"):
                final_text = out.get("content") or ""
                break
            # 工具轮：执行 → 回灌
            assistant_msg = {
                "role": "assistant",
                "content": out.get("content") or None,
                "tool_calls": [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["name"],
                                  "arguments": t["arguments"] or "{}"}}
                    for t in out["tool_calls"]],
            }
            msgs.append(assistant_msg)
            for tc in out["tool_calls"]:
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, cites, label = _run_tool(tc["name"], args)
                tool_log.append({"name": tc["name"], "label": label})
                yield {"event": "tool", "data": {"name": tc["name"],
                                                  "label": label}}
                for c in cites:
                    if c.get("id") not in {x.get("id") for x in citations}:
                        citations.append(c)
                if len(text) > budget:
                    text = text[:budget] + "\n…（工具结果超出预算已截断）…"
                    budget = 0
                else:
                    budget -= len(text)
                msgs.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": str(text)[:30000]})
            if rnd == MAX_TOOL_ROUNDS:
                msgs.append({"role": "user",
                             "content": "（工具预算已用完，基于已有信息直接给出最终回答，不要再调用工具。）"})
        else:
            out = {}
            for kind, payload in _stream_chat_gen(sc, msgs, None, out):
                if kind == "delta":
                    yield {"event": "delta", "data": {"text": payload}}
            final_text = out.get("content") or ""

        if not final_text.strip():
            # 空回答兑底：无工具重试一次（done 会用完整 content 替换流式累积）
            out = {}
            for kind, payload in _stream_chat_gen(sc, msgs, None, out):
                if kind == "delta":
                    yield {"event": "delta", "data": {"text": payload}}
            final_text = out.get("content") or f"（模型输出为空，finish={out.get('finish')}，请重试）"
    except Exception as e:  # noqa: BLE001
        print(f"{LOG} chat_stream error: {type(e).__name__}: {e}", file=sys.stderr)
        yield {"event": "error", "data": {"message": f"{type(e).__name__}: {e}"}}
        return

    # 4) 落库 + done
    conn = _dbw()
    try:
        cur = conn.execute(
            "INSERT INTO qa_messages(thread_id, role, content, citations, tool_log) "
            "VALUES(?, 'assistant', ?, ?, ?)",
            (tid, final_text, json.dumps(citations, ensure_ascii=False),
             json.dumps(tool_log, ensure_ascii=False)))
        mid = cur.lastrowid
        conn.execute(
            "UPDATE qa_threads SET updated_at=datetime('now','localtime') WHERE id=?",
            (tid,))
        conn.commit()
    finally:
        conn.close()
    yield {"event": "done",
           "data": {"thread_id": tid, "message_id": mid,
                    "content": final_text, "citations": citations,
                    "tool_log": tool_log}}

    # 跨文献超边改在【会话归档时统一整理】（见 archive_thread）：
    # 单条回答各自为政、看不到跨轮次关系，且每答必烧一次 LLM。
    # _extract_edges 保留供归档复用。


# ── 问答 → 超边提取（cross_doc_hyperedges，participants 为 entities id 数组）──

EDGE_PROMPT = """你是文献库的「超边提取器」。从下面一条问答回答中抽取跨文献知识超边。
每条超边 = 一组相关概念（参与者）+ 一条带关键数值与论文编号的结论描述 + 依据的论文集合。

要求：
- 抽 2~5 条，覆盖回答的主要知识块；寒暄、格式性内容不抽
- title：≤30 字概括该边主题
- participants：3~6 个概念，**优先用候选实体列表里的写法**（一字不差）；列表里没有但回答里明确的重要概念也可自拟
- description：100~260 字，**必须保留关键数值（R²、RMSE、偏差等）与论文编号（如 #32）**，表述成独立可读的知识句，不出现「该回答」之类的元指代
- keywords：3~6 个（中英皆可）
- source_papers：该条结论依据的论文 id 整数数组（来自回答中出现的 #id 与 citations）
- 只输出 JSON 数组，不要任何其它文字

候选实体列表（participants 写法优先从这里选）：
{cands}

citations（可作为 source_papers）：
{cites}

问答回答：
{ans}
"""


def _match_entity(name: str, ent_by_key: dict) -> tuple[int, str] | None:
    """实体匹配：精确 → 双向包含（≥3 字）。返回 (id, name) 或 None。"""
    key = str(name).strip().lower()
    if key in ent_by_key:
        return ent_by_key[key]
    if len(key) >= 3:
        for k, v in ent_by_key.items():
            if len(k) >= 3 and (k in key or key in k):
                return v
    return None


def _extract_edges(msg_id: int) -> int:
    """一条 assistant 回答 → cross_doc_hyperedges（幂等：qa_msg_id 已存在则跳过）。

    参与者 id 域 = entities（append-only 稳定；retriever 按 participants_from 列
    选表解析名字）。source_papers < 2 的不算跨文献，丢弃。"""
    conn = _dbw()
    try:
        if conn.execute("SELECT 1 FROM cross_doc_hyperedges WHERE qa_msg_id=?",
                        (msg_id,)).fetchone():
            return 0                              # 已提取过（按 qa_msg_id 判重）
        if conn.execute("SELECT 1 FROM qa_edge_jobs WHERE msg_id=?",
                        (msg_id,)).fetchone():
            return 0                              # 台账：含「提取过但 0 条」
        m = conn.execute("SELECT * FROM qa_messages WHERE id=? AND role='assistant'",
                         (msg_id,)).fetchone()
        if not m:
            return 0
        ans = m["content"] or ""
        if len(ans) < 400:
            return 0                                  # 太短没料，不值得一次 LLM
        cites = _jload(m["citations"])
        # 候选实体：实体名出现在回答文本（去重，长名优先）
        text_l = ans.lower()
        seen: dict[str, tuple[int, str]] = {}
        for r in conn.execute("SELECT id, name FROM entities WHERE LENGTH(name) >= 2"):
            nm = (r["name"] or "").strip()
            if nm and nm.lower() in text_l:
                seen.setdefault(nm.lower(), (r["id"], nm))
        cand_names = [v[1] for v in sorted(seen.values(), key=lambda x: -len(x[1]))][:60]
    finally:
        conn.close()

    prompt = EDGE_PROMPT.format(
        cands=json.dumps(cand_names, ensure_ascii=False),
        cites=json.dumps([{"id": c.get("id"), "title": c.get("title")}
                          for c in cites], ensure_ascii=False),
        ans=ans)
    try:
        raw = call_llm([{"role": "user", "content": prompt}], _step_cfg(),
                       temperature=0.1)
    except Exception as e:  # noqa: BLE001
        print(f"{LOG} edge extract LLM failed msg={msg_id}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 0
    edges = extract_json(raw)
    if not isinstance(edges, list):
        print(f"{LOG} edge extract non-json msg={msg_id}", file=sys.stderr)
        return 0

    conn = _dbw()
    n = 0
    new_edges: list[tuple[int, str]] = []   # (id, 向量文本) 待即時向量化
    try:
        ent_by_key: dict[str, tuple[int, str]] = {}
        for r in conn.execute("SELECT id, name FROM entities"):
            nm = (r["name"] or "").strip()
            if nm:
                ent_by_key.setdefault(nm.lower(), (r["id"], nm))

        for e in edges:
            if not isinstance(e, dict) or not e.get("participants"):
                continue
            ids: list[int] = []
            for name in e["participants"]:
                hit = _match_entity(str(name), ent_by_key)
                if hit is None:
                    # 库里没有的新概念：建 paper_id=NULL 的衍生实体（幂等）
                    nm = str(name).strip()
                    if len(nm) < 2:
                        continue
                    row = conn.execute(
                        "SELECT id, name FROM entities WHERE LOWER(name)=? "
                        "AND paper_id IS NULL LIMIT 1", (nm.lower(),)).fetchone()
                    if row:
                        hit = (row["id"], row["name"])
                    else:
                        cur = conn.execute(
                            "INSERT INTO entities(name, type, description, paper_id) "
                            "VALUES(?, 'concept', ?, NULL)",
                            (nm, f"由问答 #{msg_id} 衍生的概念"))
                        hit = (cur.lastrowid, nm)
                        ent_by_key.setdefault(nm.lower(), hit)
                if hit[0] not in ids:
                    ids.append(hit[0])
            sp = []
            for x in (e.get("source_papers") or []):
                try:
                    v = int(x)
                except (TypeError, ValueError):
                    continue
                if v not in sp:
                    sp.append(v)
            if len(ids) < 3 or len(sp) < 2:
                continue          # 跨文档超边约束：≥3 参与者、≥2 篇来源
            cur = conn.execute(
                "INSERT INTO cross_doc_hyperedges "
                "(title, participants, participants_merged, description, keywords, "
                " strength, source_papers, qa_msg_id, participants_from) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(e.get("title") or "")[:500],
                 json.dumps(ids, ensure_ascii=False),
                 json.dumps(to_merged(conn, ids), ensure_ascii=False),
                 str(e.get("description") or "")[:2000],
                 json.dumps([str(k) for k in (e.get("keywords") or [])][:8],
                            ensure_ascii=False),
                 "moderate",
                 json.dumps(sp, ensure_ascii=False),
                 msg_id, "entities"))
            new_edges.append(
                (cur.lastrowid,
                 (str(e.get("title") or "") + " "
                  + str(e.get("description") or "")).strip()))
            n += 1
        # 台账（含 0 条正常结论；LLM 异常/非 JSON 不记，留给下次重试）
        conn.execute("INSERT OR REPLACE INTO qa_edge_jobs(msg_id, n_edges) VALUES(?, ?)",
                     (msg_id, n))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"{LOG} edge store failed msg={msg_id}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 0
    finally:
        conn.close()
    # 即时向量化（与 batch_embeds 同款文本 title+description；ollama 不可用则
    # 留 NULL，下次 batch_embeds 补算——检索端只取 vector IS NOT NULL）
    n_vec = 0
    for he_id, text in new_edges:
        try:
            vec = compute_embedding(text)
            if vec:
                conn = _dbw()
                try:
                    conn.execute(
                        "UPDATE cross_doc_hyperedges SET vector=? WHERE id=?",
                        (vector_to_blob(vec), he_id))
                    conn.commit()
                finally:
                    conn.close()
                n_vec += 1
        except Exception:  # noqa: BLE001
            break
    print(f"{LOG} qa→edge msg=#{msg_id}: {n} edges stored "
          f"(extracted {len(edges) if isinstance(edges, list) else 0}, "
          f"embedded {n_vec}/{n})",
          file=sys.stderr)
    return n


def _extract_safe(msg_id: int) -> None:
    try:
        _extract_edges(msg_id)
    except Exception as e:  # noqa: BLE001  后台线程不能让异常变噪音
        print(f"{LOG} edge extract crash msg={msg_id}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


# ── 会话归档：冻结 + 系统提示 + 整理会话提取超边 ──
# 归档是会话终结动作：冻结后不可再追加消息，同时后台整理整段对话。

ARCHIVE_PROMPT = """【系统归档指令】本会话已归档，不再接受新的提问。
请现在回顾上面整段对话，提取其中跨文献的知识关系（超边）。

要求：
- 抽 2~5 条，覆盖对话中讨论过的主要知识块
- title：≤30 字概括该边主题
- participants：3~6 个概念（用对话中出现的实体名）
- description：100~260 字，必须保留关键数值（R²、RMSE、偏差等）与论文编号（如 #32）
- keywords：3~6 个
- source_papers：该条结论依据的论文 id 整数数组
- 只输出 JSON 数组，不要任何其它文字

只提取真正跨文献（≥2 篇）且有研究价值的关系；寒暄、格式性内容不提取。"""


def _build_name_index(conn) -> dict:
    """实体名/别名 → merged_id（统一基准）。

    优先用 entities_merged 的标准名与别名；再补原始实体名（经 _o2m 归一）。
    """
    idx: dict = {}
    for r in conn.execute("SELECT id, name, aliases FROM entities_merged"):
        for nm in [(r["name"] or "")] + (r["aliases"] or "").split(","):
            nm = nm.strip()
            if len(nm) >= 2:
                idx.setdefault(nm.lower(), r["id"])
    try:
        for r in conn.execute(
                "SELECT e.name AS name, o.merged_id AS mid FROM entities e "
                "JOIN _o2m o ON o.source_id = e.id"):
            nm = (r["name"] or "").strip()
            if len(nm) >= 2:
                idx.setdefault(nm.lower(), r["mid"])
    except Exception:  # noqa: BLE001  _o2m 缺失时退化为只用 merged 表
        pass
    return idx


def _name_to_merged(name: str, idx: dict):
    """精确 → 双向包含（≥3 字）匹配（与 _match_entity 同策略）。"""
    key = str(name).strip().lower()
    if key in idx:
        return idx[key]
    if len(key) >= 3:
        for k, mid in idx.items():
            if len(k) >= 3 and (k in key or key in k):
                return mid
    return None


def archive_thread(tid: int) -> dict:
    """归档会话：冻结（archived_at）→ 注入系统归档提示 → 后台整理提取超边。

    归档后 chat_stream 拒绝新消息（前端应同步禁用输入框）。
    幂等：已归档返回 ok=False。
    """
    conn = _dbw()
    try:
        t = conn.execute("SELECT id, archived_at FROM qa_threads WHERE id=?",
                         (tid,)).fetchone()
        if not t:
            return {"ok": False, "error": "thread not found"}
        if t["archived_at"]:
            return {"ok": False, "error": "already archived",
                    "archived_at": t["archived_at"]}
        conn.execute(
            "UPDATE qa_threads SET archived_at=datetime('now','localtime') "
            "WHERE id=?", (tid,))
        cur = conn.execute(
            "INSERT INTO qa_messages(thread_id, role, content) "
            "VALUES(?, 'system', ?)", (tid, ARCHIVE_PROMPT))
        smid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_archive_extract_safe, args=(tid, smid),
                     daemon=True, name=f"archive-{tid}").start()
    return {"ok": True, "thread_id": tid, "archived_msg_id": smid}


def _archive_extract(tid: int, smid: int) -> int:
    """整理一个已归档会话 → cross_doc_hyperedges（参与者为 merged 实体）。"""
    conn = _dbw()
    try:
        rows = conn.execute(
            "SELECT role, content FROM qa_messages WHERE thread_id=? "
            "AND role IN ('user','assistant') ORDER BY id", (tid,)).fetchall()
        idx = _build_name_index(conn)
    finally:
        conn.close()
    if not rows:
        return 0
    convo = "\n\n".join(f"[{r['role']}] {r['content']}" for r in rows)
    if len(convo) < 400:
        return 0                      # 太短，不值得一次 LLM
    convo = convo[:24000]             # 防爆上下文
    prompt = ARCHIVE_PROMPT + "\n\n=== 会话内容 ===\n" + convo
    try:
        raw = call_llm([{"role": "user", "content": prompt}], _step_cfg(),
                       temperature=0.1)
    except Exception as e:  # noqa: BLE001
        print(f"{LOG} archive LLM failed tid={tid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 0
    edges = extract_json(raw)
    if not isinstance(edges, list):
        print(f"{LOG} archive non-json tid={tid}", file=sys.stderr)
        return 0

    conn = _dbw()
    n = 0
    new_edges = []
    try:
        for e in edges:
            if not isinstance(e, dict) or not e.get("participants"):
                continue
            ids: list = []
            for name in e["participants"]:
                mid = _name_to_merged(str(name), idx)
                if mid is not None and mid not in ids:
                    ids.append(mid)
            sp: list = []
            for x in (e.get("source_papers") or []):
                try:
                    v = int(x)
                except (TypeError, ValueError):
                    continue
                if v not in sp:
                    sp.append(v)
            if len(ids) < 3 or len(sp) < 2:
                continue          # 跨文档约束：≥3 参与者、≥2 篇来源
            cur = conn.execute(
                "INSERT INTO cross_doc_hyperedges "
                "(title, participants, participants_merged, description, keywords, "
                " strength, source_papers, qa_msg_id, participants_from) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(e.get("title") or "")[:500],
                 json.dumps(to_private(conn, ids), ensure_ascii=False),
                 json.dumps(ids, ensure_ascii=False),
                 str(e.get("description") or "")[:2000],
                 json.dumps([str(k) for k in (e.get("keywords") or [])][:8],
                            ensure_ascii=False),
                 "moderate",
                 json.dumps(sp, ensure_ascii=False),
                 smid, "entities"))
            new_edges.append(
                (cur.lastrowid,
                 (str(e.get("title") or "") + " "
                  + str(e.get("description") or "")).strip()))
            n += 1
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"{LOG} archive store failed tid={tid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 0
    finally:
        conn.close()

    # 即时向量化（ollama/服务不可用则留 NULL，下次批处理补算）
    n_vec = 0
    for he_id, text in new_edges:
        try:
            vec = compute_embedding(text)
            if not vec:
                continue
            conn = _dbw()
            try:
                conn.execute(
                    "UPDATE cross_doc_hyperedges SET vector=? WHERE id=?",
                    (vector_to_blob(vec), he_id))
                conn.commit()
            finally:
                conn.close()
            n_vec += 1
        except Exception:  # noqa: BLE001
            break
    print(f"{LOG} archive→edge tid={tid} msg=#{smid}: {n} edges "
          f"(embedded {n_vec}/{n})", file=sys.stderr)
    return n


def _archive_extract_safe(tid: int, smid: int) -> None:
    try:
        _archive_extract(tid, smid)
    except Exception as e:  # noqa: BLE001
        print(f"{LOG} archive crash tid={tid}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
