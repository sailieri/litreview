"""litread MCP server —— 把文献库能力暴露给外部智能体（pi / Claude Desktop 等 MCP 客户端）。

设计：
  - 前端（Vue）给人用，MCP 给智能体用，两者共用同一套业务逻辑与同一个数据库。
  - 工具实现直接复用 librarian.tool_* 与 retriever.* —— 与前端内置智能体
    用同一份检索/读取代码，绝不另写一套（否则两边结果会漂移）。
  - 10 个只读工具：论文检索 / 原文阅读 / 库统计 + 跨文献超边 + 三路融合 RAG +
    全库列表 / 论文详情 / 章节阅读 + 网页上传地址 / 入库进度。不提供 SQL 直查（对外分享的注入/资源风险）。
  - 会写数据的 1 个：ask_librarian（与图书管理员连续对话）—— HTTP
    转发给 webapp 的 /api/agent/chat，让 webapp 当唯一写入口，避免两个进程同时写
    SQLite（DELETE 模式全库锁）互抢。
  - 【不做 MCP 上传】：智能体无法搬运文件，且不同客户端对本地文件的
    访问权限不一致。上传改由用户在浏览器里完成 —— get_litread_web_url() 把网页地址给
    用户，他打开后点「导入」。前提：webapp 绑 0.0.0.0 且防火墙放行 8790。
  - 鉴权：共享静态 Bearer token（LITREAD_MCP_TOKEN），走 FastMCP 的
    token_verifier。设计用途是【局域网分享】，不要暴露到公网。
  - 传输：streamable-http，默认路径 /mcp；绑 0.0.0.0:8791
    （用 MCP_HOST / MCP_PORT 环境变量可覆盖成具体局域网 IP）。

启动（Windows）：
    cd <仓库根>/webapp
    ../.venv/Scripts/python.exe mcp_server.py

别人的客户端配置（pi / Claude Desktop 等）：
    url:     http://<本机局域网IP>:8791/mcp
    headers: { "Authorization": "Bearer <LITREAD_MCP_TOKEN>" }

回归验证（换成真实 IP 与 token）：
    curl -sS -X POST http://127.0.0.1:8791/mcp \
      -H 'Content-Type: application/json' \
      -H 'Authorization: Bearer <token>' \
      -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
"""
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

WEB_ROOT = Path(__file__).resolve().parent
LITROOT = WEB_ROOT.parent
for _p in (str(LITROOT), str(WEB_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.auth.provider import TokenVerifier, AccessToken  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402

from modules.config import get as _cfg_get, section as _cfg_section  # noqa: E402

_webcfg = _cfg_section("webapp")  # 注意：不要叫 _web，会与下方 httpx 客户端工厂重名
HOST = os.environ.get("MCP_HOST") or str(_webcfg.get("mcp_host") or "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT") or _webcfg.get("mcp_port") or 8791)


def _load_token() -> str:
    """鉴权 token：config webapp.mcp_token（可写 ${LITREAD_MCP_TOKEN}）> 环境变量 > secrets_file。"""
    t = str(_webcfg.get("mcp_token") or "").strip()
    if t and not t.startswith("${"):
        return t
    t = (os.environ.get("LITREAD_MCP_TOKEN") or "").strip()
    if t:
        return t
    sf = str(_webcfg.get("secrets_file") or "").strip()
    if not sf:
        return ""
    p = Path(sf).expanduser()
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("LITREAD_MCP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return ""


class StaticTokenVerifier(TokenVerifier):
    """共享静态 token 校验（局域网分享用，非 OAuth）。"""

    def __init__(self, token: str):
        self._t = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token and secrets.compare_digest(token, self._t):
            return AccessToken(token=token, client_id="litread-shared",
                               scopes=["read"])
        return None


# 惰性导入：librarian/retriever 的导入链会拉起 embedder / llm_client 等重依赖，
# 放到工具首次调用时付成本，server 本身秒起、连接稳定。
_librarian = None
_retriever = None


def _lib():
    global _librarian
    if _librarian is None:
        import librarian  # noqa: PLC0415
        _librarian = librarian
    return _librarian


def _rt():
    global _retriever
    if _retriever is None:
        from modules.database import retriever  # noqa: PLC0415
        _retriever = retriever
    return _retriever


TOKEN = _load_token()
_auth_kw = {}
if TOKEN:
    _auth_kw = {
        "token_verifier": StaticTokenVerifier(TOKEN),
        "auth": AuthSettings(
            issuer_url="http://127.0.0.1:%d" % PORT,
            resource_server_url="http://127.0.0.1:%d/mcp" % PORT,
            required_scopes=["read"],
            validate_token_resource=False,
        ),
    }

# 版本号：客户端（Cherry Studio 等）在 serverInfo 里显示的就是它。
# 改了工具集就顺手 +0.1，方便肉眼确认客户端拿到的是新版。
SERVER_VERSION = "1.3.0"

INSTRUCTIONS = (
    "litread 文献库（本地文献库）工具集。"
    "用途：帮用户找/挑论文、读原文或指定章节、查跨文献知识关系、汇报库统计、"
    "以及与「图书管理员」连续对话（ask_librarian，用返回的 thread_id 续接同一会话）。"
    "【上传新论文 PDF 不在本工具集内】——智能体无法搬运文件（不同智能体对本地"
    "文件的访问权限也不一致）。用户要上传时，先调 get_litread_web_url() 拿到网页"
    "地址，把地址给他，让他在浏览器里打开并点「导入」自行上传。"
    "提到论文时务必带上 id（形如 #12），数值必须与工具返回原文一致，"
    "工具没给的信息说「库中未见」，不要编造。"
    "\n\n另提供【综述撰写流程】工具（review_*）：review_status 看本轮进度、"
    "read_draft/patch_draft/write_draft 改初稿、save_key_findings 写事实锚点、"
    "list_refs/expand_ref 管理 [S#] 引用、kb_query 查库调研、run_probe 跑独立追问、"
    "run_check 跑机械硬断言、run_gap_audit 核实缺口断言、compile_draft 出成稿、"
    "run_review 并行审核（跑完轮次 +1）。流程与文件契约见仓库 skill/SKILL.md。"
)

mcp = FastMCP(
    "litread",
    instructions=INSTRUCTIONS,
    host=HOST,
    port=PORT,
    **_auth_kw,
)

# FastMCP 不暴露 version 参数（内部 Server 默认取 mcp 库版本号），
# 构造后直接覆盖 —— 否则客户端显示的是 mcp 包的版本，不是 litread 的。
mcp._mcp_server.version = SERVER_VERSION

# ── 综述撰写流程工具（webapp/review_tools.py）────────────────────
# 自带智能体（Claude Code / Codex / 自建）挂上本服务即可按 skill/SKILL.md 的流程跑，
# 不必自己实现检索 / 追问 / 断言 / 审核这些机械动作。
import review_tools  # noqa: E402  （同目录）

for _fn in review_tools.TOOLS:
    _desc = (_fn.__doc__ or "").strip().splitlines()
    mcp.add_tool(_fn, name=_fn.__name__,
                 description="\n".join(_desc).strip() or _fn.__name__)


# ── 1-3：与前端图书管理员同源（librarian.tool_*）────────────────

@mcp.tool()
def search_papers(query: str, top_k: int = 8) -> str:
    """在文献库中检索论文，用于帮用户找/挑论文，或确认某篇论文的 id。

    返回命中论文的 id（形如 #12）、年份、期刊、标题与摘要片段。
    可换关键词多次调用来逐步细化（中英文主题、方法、作者名都可以）。

    Args:
        query: 检索词，中英文的主题 / 方法 / 作者均可
        top_k: 返回条数，默认 8，最大 12
    """
    text, _cites = _lib().tool_search_papers(query, top_k)
    return text


@mcp.tool()
def read_paper(paper_id: int, question: str = "") -> str:
    """读一篇论文的真实原文（不是摘要）。

    强烈建议带上 question：会围绕该问题定位论文里最相关的段落，
    比返回开头全文更准、也更省。论文很长时只返回相关片段。

    Args:
        paper_id: 论文 id（形如 #12 里的数字）
        question: 读这篇原文的目的 / 想找的内容；留空则返回开头部分全文
    """
    text, _cites = _lib().tool_read_paper(paper_id, question)
    return text


@mcp.tool()
def get_stats() -> str:
    """查询文献库的整体统计：论文总量、年份分布、期刊 Top、高频关键词、超图规模。"""
    text, _cites = _lib().tool_get_stats()
    return text


# ── 4：跨文献超边（论文间的知识关系）────────────────────────────

@mcp.tool()
def search_hyperedges(query: str, top_k: int = 6, scope: str = "cross_doc") -> str:
    """检索「跨文献知识关系」（超边）——回答「X 与 Y 有什么联系」
    「哪些论文对比过方法 Z」「谁和谁得出了相反结论」这类论文间关系问题。

    每条结果给出一组相关概念（已按合并后的标准实体名归一）、一句话结论
    （含关键数值与论文编号）、涉及论文与关系强度。

    Args:
        query: 关系主题，如「地形对 GEDI 精度的影响」
        top_k: 返回条数，默认 6，最大 12
        scope: 检索范围。默认 "cross_doc"（只跨文献，推荐）；
            传 "paper" 只看单篇内部超边；传 "all" 两者都查（单篇量极大，
            跨文献关系容易被淹没）
    """
    sc = scope if scope in ("cross_doc", "paper", "all") else "cross_doc"
    rs = _rt().search_hyperedges(query, top_k=max(1, min(int(top_k), 12)),
                                 scope=sc)
    if not rs:
        return "库中未见与该主题相关的跨文献关系。"
    out = []
    for r in rs:
        names = r.get("participant_names") or []
        papers = r.get("source_papers") or []
        out.append(
            "【%s】%s\n  相关概念：%s\n  涉及论文：%s\n  结论：%s\n  强度：%s"
            % ("跨文献" if r.get("scope") == "cross_doc" else "单篇",
               (r.get("title") or "").strip() or "(无标题)",
               " / ".join(names[:6]) or "—",
               " ".join("#%s" % p for p in papers[:10]) or "—",
               (r.get("description") or "").strip()[:500],
               r.get("strength") or "—")
        )
    return "\n\n".join(out)


# ── 5：三路融合 RAG（全文分块 + 向量 + 超边）──────────────────

@mcp.tool()
def hyper_rag(query: str, top_k_chunks: int = 8, top_k_edges: int = 5) -> str:
    """三路融合检索（全文分块 BM25 + 向量 + 跨文献超边）——需要综合回答时的首选。

    比单看论文标题（search_papers）深得多：直接返回原文片段与相关知识关系。

    Args:
        query: 研究问题，中英文均可
        top_k_chunks: 返回原文片段条数，默认 8
        top_k_edges: 返回超边条数，默认 5
    """
    d = _rt().search(query, top_k_chunks=max(1, min(int(top_k_chunks), 20)),
                     top_k_edges=max(0, min(int(top_k_edges), 12)))
    parts = []
    chunks = d.get("chunks") or []
    if chunks:
        lines = []
        for c in chunks:
            pid = c.get("paper_id")
            title = (c.get("paper_title") or "")[:60]
            snippet = (c.get("content") or "")[:400].replace("\n", " ")
            lines.append("· #%s %s\n  %s" % (pid, title, snippet))
        parts.append("【原文片段】\n" + "\n".join(lines))
    edges = d.get("hyperedges") or []
    if edges:
        lines = []
        for e in edges:
            lines.append("· %s\n  概念：%s\n  论文：%s"
                         % ((e.get("title") or e.get("description") or "")[:200],
                            " / ".join((e.get("participant_names") or [])[:6]),
                            " ".join("#%s" % p for p in (e.get("source_papers") or [])[:8])))
        parts.append("【跨文献关系】\n" + "\n".join(lines))
    return "\n\n".join(parts) or "库中未见相关内容。"


# ── 6：全库论文列表 ─────────────────────────────────────────────

@mcp.tool()
def list_papers(year: int = 0, journal: str = "", limit: int = 50) -> str:
    """列出文献库中的论文（可按年份 / 期刊过滤），用于浏览全库或找某年某刊的文章。

    Args:
        year: 只列该年份的论文；0 = 不限
        journal: 期刊名关键词过滤（不区分大小写）；空 = 不限
        limit: 最多返回条数，默认 50，最大 200
    """
    rows = _rt().list_papers()
    y = int(year or 0)
    jw = (journal or "").strip().lower()
    if y:
        rows = [r for r in rows if (r.get("year") or "") == str(y)
                or str(r.get("year") or "").startswith(str(y))]
    if jw:
        rows = [r for r in rows if jw in (r.get("journal") or "").lower()]
    lim = max(1, min(int(limit or 50), 200))
    head = "共 %d 篇（显示前 %d）" % (len(rows), min(len(rows), lim))
    lines = ["· #%s (%s) %s — %s" % (r.get("id"), r.get("year") or "—",
                                     (r.get("title") or "")[:70],
                                     r.get("journal") or "—")
             for r in rows[:lim]]
    return head + "\n" + "\n".join(lines)


# ── 7：论文详情（元数据 + 章节树）──────────────────────────────

@mcp.tool()
def get_paper_details(paper_id: int) -> str:
    """获取一篇论文的完整元数据与章节树（章节 id / 标题 / 层级 / 字数）。

    配合 read_section 使用：先看章节树选定 section_id，再精读那一节。
    比 read_paper 更细、更省 token。

    Args:
        paper_id: 论文 id
    """
    d = _rt().get_paper_details(int(paper_id))
    if not d:
        return "论文 #%d 不存在。" % paper_id
    lines = [
        "《%s》 #%s" % (d.get("title") or "?", d.get("id")),
        "作者：%s" % (d.get("authors") or "—"),
        "年份：%s   期刊：%s" % (d.get("year") or "—", d.get("journal") or "—"),
        "DOI：%s" % (d.get("doi") or "—"),
    ]
    ents = d.get("entities") or []
    if ents:
        lines.append("实体(%d)：%s" % (
            len(ents),
            ", ".join("%s[%s]" % (e.get("name"), e.get("type") or "?")
                      for e in ents[:12])))
    secs = d.get("sections") or []
    lines.append("\n章节树（%d 节）：" % len(secs))
    for s in secs[:60]:
        indent = "  " * (int(s.get("level") or 1) - 1)
        lines.append("%s· id=%s L%s %s (%s字)" % (
            indent, s.get("id"), s.get("level"),
            (s.get("title") or "")[:60], s.get("char_count")))
    return "\n".join(lines)


# ── 8：读指定章节 ───────────────────────────────────────────────

@mcp.tool()
def read_section(paper_id: int, section_id: int = 0) -> str:
    """读一篇论文的指定章节正文（section_id 来自 get_paper_details 的章节树）。

    不传 section_id 则返回该论文所有章节（可能很长，慎用）。

    Args:
        paper_id: 论文 id
        section_id: 章节 id；0 = 返回全部章节
    """
    rows = _rt().read_section(int(paper_id),
                              int(section_id) if section_id else None)
    if not rows:
        return "未找到（检查 paper_id / section_id，可用 get_paper_details 查看章节树）。"
    if section_id:
        s = rows[0]
        return "【%s】(L%s)\n\n%s" % (s.get("title") or "",
                                     s.get("level"), (s.get("content") or "")[:20000])
    out = []
    for s in rows[:40]:
        out.append("=== id=%s L%s %s ===\n%s" % (
            s.get("id"), s.get("level"), s.get("title") or "",
            (s.get("content") or "")[:2000]))
    return "\n\n".join(out)


# ── 写操作（HTTP 转发给运行中的 webapp，不直连 DB）──────────
# MCP 不写文件、不写库：上传与对话全部转给 webapp 串行处理，和浏览器前端
# 走同一套逻辑，避免双入口行为漂移。

WEB_BASE = (os.environ.get("LITREAD_WEB_BASE")
            or str(_webcfg.get("web_base") or "")
            or "http://127.0.0.1:%d" % int(_webcfg.get("port") or 8790)).rstrip("/")


def _web(timeout: float = 60.0) -> httpx.Client:
    """指向 webapp 的 HTTP 客户端。

    trust_env=False 是必须的：Windows 下 httpx 默认读系统代理，
    连 127.0.0.1 也可能被本地代理劫持 → 502 / 超时。
    """
    return httpx.Client(trust_env=False,
                        timeout=httpx.Timeout(timeout, connect=10.0))


def _job_of(client: httpx.Client, jid: str) -> dict | None:
    """在 webapp 的导入任务列表里找某条（内存态，重启即清）。"""
    r = client.get(f"{WEB_BASE}/api/ingest/jobs", params={"limit": 100})
    r.raise_for_status()
    for j in (r.json().get("jobs") or []):
        if j.get("job_id") == jid:
            return j
    return None


def _lan_ip() -> str:
    """探测本机对局域网的出口 IP（用于把网页地址给用户）。

    连一个外部地址只是让内核选出默认路由对应的本地 IP，不会真的发数据。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


WEB_UI_PORT = urlparse(WEB_BASE).port or int(_webcfg.get("port") or 8790)


@mcp.tool()
def get_litread_web_url() -> str:
    """获取 litread 网站地址 —— 用户要【上传新论文 PDF】时，把这个地址给他。

    智能体无法搬运文件（也拿不到用户本地的文件），所以上传必须由用户在浏览器里
    完成：打开这个网址 → 点页面上的「导入」→ 选 PDF。入库会自动跑完
    （解析 → 规范化 → 实体/超边提取 → 向量化），页面上能看到进度。

    同一个网址还能：浏览/检索文献库、读原文、与图书管理员对话。

    返回局域网地址（形如 http://192.168.x.x:8790）；用户需与本机在同一局域网才能打开。
    """
    url = f"http://{_lan_ip()}:{WEB_UI_PORT}"
    return (
        f"litread 网站：{url}\n\n"
        "打开后可以：\n"
        "· 上传新论文 PDF —— 点页面上的「导入」按钮选文件；入库自动完成，页面可看进度\n"
        "· 浏览 / 检索文献库、阅读原文\n"
        "· 与图书管理员对话（与 MCP 里的 ask_librarian 是同一套）\n\n"
        "要求：浏览器所在机器需与本服务在同一局域网、能访问该地址。"
    )


@mcp.tool()
def get_import_status(job_id: str = "", paper_id: int = 0) -> str:
    """查入库进度：传 job_id 看导入任务，或传 paper_id 看某篇论文的 8 阶段状态。

    Args:
        job_id: import_pdf 返回的任务 id
        paper_id: 论文 id（形如 #12 里的数字）
    """
    if not job_id and not paper_id:
        return "请至少提供 job_id 或 paper_id。"
    try:
        with _web(30.0) as c:
            if job_id:
                j = _job_of(c, job_id)
                if not j:
                    return f"没找到任务 {job_id}（webapp 重启后内存里的任务会清空）。"
                lines = [f"任务 {j.get('job_id')}（{j.get('stem')}）",
                         f"状态：{j.get('state')} — {j.get('msg')}"]
                tail = (j.get("log_tail") or "").strip()
                if tail:
                    lines.append("日志尾部：\n" + tail[-1500:])
                return "\n".join(lines)
            r = c.get(f"{WEB_BASE}/api/ingest/status/{int(paper_id)}")
            if r.status_code == 404:
                return f"论文 #{paper_id} 无入库状态记录。"
            r.raise_for_status()
            d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return f"查询失败（webapp 未启动？{WEB_BASE}）：{type(e).__name__}: {e}"

    stages = d.get("stages") or []
    marks = "".join("●" if s.get("done") else "○" for s in stages)
    out = [f"论文 #{d.get('paper_id')} 入库进度 {d.get('percent')}%"
           f"（{d.get('done_count')}/{d.get('total')}）  {marks}",
           "阶段：" + " / ".join(
               f"{s.get('label')}{'✓' if s.get('done') else ''}" for s in stages),
           f"实体 {d.get('entity_count')} · 超边 {d.get('hyperedge_count')}"
           f" · 更新于 {d.get('updated_at')}"]
    nxt = next((s.get("label") for s in stages if not s.get("done")), None)
    if nxt:
        out.append(f"下一步：{nxt}")
    return "\n".join(out)


@mcp.tool()
def ask_librarian(question: str, thread_id: int = 0, paper_ids: str = "") -> str:
    """跟「litread 图书管理员」对话——它会检索库、读原文后作答，支持连续追问。

    【连续对话】返回值首行是 thread_id；要接着追问，把那个数字填回本工具的
    thread_id 参数即可延续同一会话（不填 = 开新会话）。

    本工具会等回答完全生成才返回，可能耗 30–120 秒（多轮检索 + 生成），
    期间客户端会一直等待，属正常现象。

    Args:
        question: 你的问题（中英文均可）
        thread_id: 要延续的会话 id；0 = 新建会话
        paper_ids: 可选，圈定工作区的论文 id，逗号分隔（如 "12,48"）；
            会话内后续追问会沿用首次设定的工作区
    """
    q = (question or "").strip()
    if not q:
        return "问题不能为空。"
    pids = []
    for tok in (paper_ids or "").replace("，", ",").split(","):
        tok = tok.strip().lstrip("#").strip()
        if tok.isdigit():
            pids.append(int(tok))
    body = {"message": q, "thread_id": int(thread_id) or None,
            "paper_ids": pids or None}

    tid, content, cites, tools = (int(thread_id) or None), "", [], []
    try:
        with _web(900.0) as c:
            with c.stream("POST", f"{WEB_BASE}/api/agent/chat", json=body) as r:
                if r.status_code != 200:
                    return (f"图书管理员返回 {r.status_code}："
                            f"{r.read()[:300].decode('utf-8', 'replace')}")
                ev = ""
                for line in r.iter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            payload = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if ev == "meta":
                            tid = payload.get("thread_id") or tid
                        elif ev == "tool":
                            lb = payload.get("label") or payload.get("name") or ""
                            if lb:
                                tools.append(lb)
                        elif ev == "done":
                            tid = payload.get("thread_id") or tid
                            content = payload.get("content") or content
                            cites = payload.get("citations") or cites
                        elif ev == "error":
                            return f"❌ 图书管理员报错：{payload.get('message')}"
    except httpx.HTTPError as e:
        return f"对话失败（webapp 未启动？{WEB_BASE}）：{type(e).__name__}: {e}"

    out = [f"thread_id: {tid}", ""]
    if tools:
        out += ["（检索过程：" + "；".join(tools) + "）", ""]
    out.append(content or "（未收到回答）")
    if cites:
        out += ["", f"引用（{len(cites)} 条）："]
        for c in cites[:20]:
            if isinstance(c, dict):
                out.append("· #%s 《%s》 %s %s" % (
                    c.get("id", "?"), (c.get("title") or "?")[:60],
                    c.get("year") or "", c.get("journal") or ""))
            else:
                out.append(f"· {str(c)[:120]}")
    if tid:
        out += ["", f"（继续追问请带 thread_id={tid}）"]
    return "\n".join(out)


if __name__ == "__main__":
    if not TOKEN:
        print("[litread-mcp] ⚠ 未找到 LITREAD_MCP_TOKEN（env 或 secrets.env）"
              "——鉴权已禁用。局域网分享前请先设置！", flush=True)
    else:
        print("[litread-mcp] 静态 token 鉴权已启用", flush=True)
    print(f"[litread-mcp] streamable-http on http://{HOST}:{PORT}/mcp",
          flush=True)
    print(f"[litread-mcp] 别人这样连：http://<本机局域网IP>:{PORT}/mcp "
          f"+ header Authorization: Bearer <token>", flush=True)
    mcp.run(transport="streamable-http")
